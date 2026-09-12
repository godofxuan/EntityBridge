"""Local company identity API and three server-rendered governance pages."""

from __future__ import annotations

import csv
import io
import secrets
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .jobs import JobConflict
from .matching_service import run_matching
from .store import Store, VersionConflict, digest


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RelatedEntity(StrictModel):
    kind: Literal["branch_of", "successor_of"]
    target_source: str = Field(min_length=1, max_length=80)
    target_key: str = Field(min_length=1, max_length=200)
    evidence: str = Field(min_length=1, max_length=2000)


class SourceRow(StrictModel):
    source_key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=500)
    address: str | None = Field(None, max_length=1000)
    city: str | None = Field(None, max_length=200)
    postcode: str | None = Field(None, max_length=40)
    country: str | None = Field(None, max_length=10)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    registration_authority: str | None = Field(None, max_length=30)
    registration_number: str | None = Field(None, max_length=50)
    lei: str | None = Field(None, max_length=20)
    category: str | None = Field(None, max_length=300)
    status: str | None = Field(None, max_length=50)
    registration_status: str | None = Field(None, max_length=30)
    relationships: list[RelatedEntity] = Field(default_factory=list, max_length=30)
    tombstone: bool = False


class ImportRequest(StrictModel):
    source: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    rows: list[SourceRow] = Field(min_length=1, max_length=10000)


class RunRequest(StrictModel):
    method: Literal["exact", "fuzzy", "splink"] = "splink"
    threshold: float = Field(default=0.9, ge=0, le=1)
    review_threshold: float = Field(default=0.5, ge=0, le=1)
    incremental: bool = True
    verify_full: bool = False
    candidate_mode: Literal["fixed", "hybrid"] = "fixed"

    @model_validator(mode="after")
    def validate_thresholds(self):
        if self.review_threshold > self.threshold:
            raise ValueError("Review threshold must not exceed the automatic threshold")
        if self.method == "exact" and self.threshold == 0:
            raise ValueError("Exact matching requires a positive threshold; zero would accept non-matches")
        return self


class JobRequest(StrictModel):
    settings: RunRequest
    idempotency_key: str = Field(min_length=1, max_length=200)
    max_attempts: int = Field(default=3, ge=1, le=5)


class PublishRequest(StrictModel):
    expected_parent: str | None


class DecisionRequest(StrictModel):
    left: str
    right: str
    action: Literal["accept", "reject", "abstain"]
    reason: str = Field(min_length=1, max_length=2000)
    base_revision: str
    left_version: str
    right_version: str
    policy_version: str


class RevokeRequest(StrictModel):
    base_revision: str
    reason: str = Field(default="Revocation preview", min_length=1, max_length=2000)
    preview_cutoff: int | None = Field(default=None, ge=0)


def create_app(store: Store, *, tokens=None, model_path=None, candidate_model_path=None, local_demo=False,
               oidc_verifier=None):
    app = FastAPI(title="EntityBridge", version="0.4.0")
    from .telemetry import RequestMetrics
    metrics = RequestMetrics()
    tokens = dict(tokens or {})
    for token, identity in tokens.items():
        if (not isinstance(token, str) or not token or not token.isascii() or any(c.isspace() for c in token)
                or not isinstance(identity, (list, tuple)) or len(identity) != 2
                or not isinstance(identity[0], str) or not identity[0].strip()
                or not isinstance(identity[1], str) or identity[1] not in {"admin", "reviewer", "viewer"}):
            raise ValueError("Each token must be ASCII without whitespace and map to [name, admin/reviewer/viewer]")
    tokens = {token: tuple(identity) for token, identity in tokens.items()}
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "web/templates"))
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web/static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        hosts = {"127.0.0.1", "localhost", "::1"}
        if request.client and request.client.host == "testclient":
            hosts.add("testserver")
        if urlsplit("http://" + request.headers.get("host", "")).hostname not in hosts:
            return JSONResponse({"detail": "Untrusted Host"}, status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Cross-origin writes are not allowed"}, status_code=403)
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 5 * 1024 * 1024:
                    return JSONResponse({"detail": "Request exceeds 5 MiB"}, status_code=413)
            request._body = bytes(data)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        return response

    @app.middleware("http")
    async def measure(request: Request, call_next):
        started, status_code = time.monotonic(), 500
        request_id = str(uuid4())
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            metrics.record(request.method, route, status_code, time.monotonic() - started)

    def authenticate(token):
        if not isinstance(token, str) or not token or not token.isascii():
            raise HTTPException(401, "Sign in or supply a valid bearer token")
        found = next((identity for key, identity in tokens.items() if secrets.compare_digest(token, key)), None)
        if found is None and oidc_verifier is not None:
            from .auth import AuthenticationError
            try:
                found = oidc_verifier.verify(token)
            except AuthenticationError:
                pass
        if found is None:
            raise HTTPException(401, "Sign in or supply a valid bearer token")
        return tuple(found)

    def actor(request: Request):
        if local_demo and request.client and request.client.host in {"127.0.0.1", "::1", "testclient"}:
            request.state.identity = ("local-demo", "admin")
            return ("local-demo", "admin")
        token = request.headers.get("authorization", "").removeprefix("Bearer ") or request.cookies.get("eb_session", "")
        found = authenticate(token)
        request.state.identity = found
        return found

    def reviewer(identity=Depends(actor)):
        if identity[1] not in {"reviewer", "admin"}:
            raise HTTPException(403, "Reviewer permission required")
        return identity

    def admin(identity=Depends(actor)):
        if identity[1] != "admin":
            raise HTTPException(403, "Administrator permission required")
        return identity

    @app.exception_handler(HTTPException)
    async def access_error(request, exc):
        if exc.status_code == 401 and "text/html" in request.headers.get("accept", ""):
            return templates.TemplateResponse(request=request, name="app.html", status_code=401,
                context={"page": "login", "revision": None, "demo": local_demo, "role": None})
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(VersionConflict)
    async def conflict(request, exc):
        if request.url.path.startswith("/ui/"):
            return templates.TemplateResponse(request=request, name="app.html", status_code=409,
                context={"page": "error", "revision": store.current_revision(), "demo": local_demo,
                         "message": "依据版本已变化，请回到版本页刷新后重新操作。", "detail": str(exc)})
        return JSONResponse({"detail": str(exc), "current_revision": store.current_revision()}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(JobConflict)
    async def job_conflict(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(_request, _exc):
        return JSONResponse({"detail": "Resource not found"}, status_code=404)

    @app.post("/imports")
    def import_records(body: ImportRequest, _identity=Depends(admin)):
        return store.import_records(body.source, [row.model_dump(exclude_none=True) for row in body.rows])

    @app.post("/match-runs")
    def match_run(body: RunRequest, _identity=Depends(admin)):
        return run_matching(store, body, model_path, candidate_model_path)

    @app.post("/jobs", status_code=202)
    def submit_job(body: JobRequest, _identity=Depends(admin)):
        from .jobs import JobQueue
        from .worker import model_fingerprints
        payload = {"settings": body.settings.model_dump(),
                   "models": model_fingerprints(body.settings, model_path, candidate_model_path)}
        result = JobQueue(store).enqueue(payload, idempotency_key=body.idempotency_key,
                                         max_attempts=body.max_attempts)
        return {**result, "status_url": f"/jobs/{result['job_id']}",
                "completion_means": "prepared identity revision; administrator publication still required"}

    @app.get("/jobs")
    def list_jobs(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), _identity=Depends(admin)):
        from .jobs import JobQueue
        return JobQueue(store).list_jobs(limit=limit, offset=offset)

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, _identity=Depends(admin)):
        from .jobs import JobQueue
        return JobQueue(store).get(job_id, include_events=True)

    @app.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, _identity=Depends(admin)):
        from .jobs import JobQueue
        return JobQueue(store).cancel(job_id)

    @app.post("/jobs/{job_id}/retry")
    def retry_job(job_id: str, _identity=Depends(admin)):
        from .jobs import JobQueue
        return JobQueue(store).retry(job_id)

    @app.get("/learning/summary")
    def learning_summary(_identity=Depends(reviewer)):
        from .learning import snapshot_review_labels
        snapshot = snapshot_review_labels(store)
        return {"summary": snapshot["summary"], "scope": "effective explicit review labels; no automatic labels"}

    @app.get("/reviews/queue")
    def learning_queue(strategy: Literal["uncertainty_diversity", "random"] = "uncertainty_diversity",
                       limit: int = Query(50, ge=1, le=200), seed: int = 20260912, _identity=Depends(reviewer)):
        from .learning import rank_review_candidates, snapshot_review_labels
        snapshot = snapshot_review_labels(store)
        revision = snapshot["revision_id"]
        payload = store._payload(revision)
        excluded = [(label["left_id"], label["right_id"]) for label in snapshot["labels"]]
        excluded.extend((item["left"], item["right"]) for item in payload["edge_decisions"]
                        if item["status"] == "suppressed")
        candidates = [edge for edge in payload["edges"] if edge.get("score") is not None]
        return {"revision_id": revision, "strategy": strategy,
                "score_interpretation": "heuristic review ordering; raw scores may be uncalibrated",
                "items": rank_review_candidates(snapshot["records"], candidates, strategy=strategy,
                                                 seed=seed, limit=limit, excluded_pairs=excluded)}

    @app.get("/ops/metrics")
    def process_metrics(_identity=Depends(admin)):
        return metrics.snapshot()

    @app.get("/ops/status")
    def operational_status(_identity=Depends(admin)):
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError
        try:
            with store.engine.connect() as con:
                con.execute(text("SELECT 1"))
            projection = store.projection_status()
        except SQLAlchemyError:
            raise HTTPException(503, "Database readiness check failed") from None
        return {"database": "ready", "projection": projection,
                "verification": "lightweight readiness; use explicit full verification for integrity audit"}

    @app.post("/registry-runs")
    def registry_run(_identity=Depends(admin)):
        from .identifier_rich import trusted_registry_edges
        raw = store.active_records()
        result = trusted_registry_edges(raw.values())
        candidate = store.prepare_revision(result["edges"], policy_version="identifier-rich-RA000585-v2-manual-status",
            threshold=1.0, review_threshold=1.0, expected_input_hash=digest(raw))
        return {**candidate, "mode": "identifier-rich", "candidate_pairs": len(result["edges"]),
                "conflicts": result["conflicts"], "excluded_record_ids": result["excluded_record_ids"]}

    @app.get("/entities")
    def entities(response: Response, query: str = "", revision: str | None = None,
                 offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000), _identity=Depends(actor)):
        page = store.entities_page(query, revision=revision, offset=offset, limit=limit)
        response.headers["X-Total-Count"] = str(page["total"])
        if page["revision_id"]:
            response.headers["X-Revision-Id"] = page["revision_id"]
        return page["items"]

    @app.get("/entities/{entity_id}")
    def entity(entity_id: str, revision: str | None = None, _identity=Depends(actor)):
        return store.entity(entity_id, revision=revision)

    @app.get("/records/{record_id}/links")
    def links(record_id: str, revision: str | None = None, _identity=Depends(actor)):
        current = revision or store.current_revision()
        if not current:
            raise KeyError(record_id)
        payload = store._payload(current)
        if record_id not in payload["records"]:
            raise KeyError(record_id)
        return {"revision_id": current, "record": payload["records"][record_id],
            "links": [edge for edge in payload["edges"] if record_id in (edge["left"], edge["right"])]}

    @app.post("/reviews/decision")
    def decide(body: DecisionRequest, identity=Depends(reviewer)):
        return store.decide(**body.model_dump(), reviewer=identity[0])

    @app.post("/decisions/{decision_id}/revoke-preview")
    def revoke_preview(decision_id: str, body: RevokeRequest, _identity=Depends(reviewer)):
        return store.revoke_preview(decision_id, base_revision=body.base_revision)

    @app.post("/decisions/{decision_id}/revoke")
    def revoke(decision_id: str, body: RevokeRequest, identity=Depends(reviewer)):
        if body.preview_cutoff is None:
            raise ValueError("Confirm the exact preview_cutoff shown by revoke-preview")
        return store.revoke(decision_id, **body.model_dump(), reviewer=identity[0])

    @app.post("/revisions/{revision_id}/publish")
    def publish(revision_id: str, body: PublishRequest, _identity=Depends(admin)):
        return store.publish(revision_id, **body.model_dump())

    @app.get("/exports")
    def export(revision: str, _identity=Depends(actor)):
        payload = store._payload(revision)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["revision_id", "entity_id", "record_id", "record_version_id", "source", "name", "canonical_name_source_version"])
        for entity in payload["entities"].values():
            for member in entity["members"]:
                row = payload["records"][member]
                # Prevent formula interpretation when a consumer opens CSV in a spreadsheet.
                name = row.get("name", "")
                if name.startswith(("=", "+", "-", "@", "\t", "\r")):
                    name = "'" + name
                writer.writerow([revision, entity["entity_id"], member, row["record_version_id"], row["source"], name,
                    entity["canonical"].get("name", {}).get("record_version_id", "")])
        return Response(output.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="entities-{revision}.csv"'})

    def render(request, page, **context):
        labels = {"active": "当前身份", "current": "当前身份", "historical": "历史身份", "retired": "身份已变更",
            "accepted": "已接纳", "review": "待复核", "conflict": "约束冲突", "suppressed": "已抑制",
            "below_threshold": "依据不足", "prepared": "待发布", "published": "已发布",
            "accept": "确认同一企业", "reject": "判定不同", "abstain": "暂缓", "CREATE": "建立判断",
            "REVOKE": "撤销判断", "EXPIRE": "依据已过期", "name": "企业名称", "address": "地址",
            "city": "城市", "postcode": "邮编", "country": "国家",
            "score_at_or_above_threshold": "评分达到自动接纳阈值。", "explicit_must_link": "人工确认同一企业。",
            "explicit_cannot_link": "人工明确判定为不同企业。",
            "transitive_must_link": "其他有效人工确认已将两条记录连接为同一企业。",
            "score_in_review_band": "评分处于待复核区间。", "cluster_cannot_link": "归并会违反企业簇内的不同企业判断。",
            "candidate_requires_review": "扩展检索候选，需要人工确认后才能归并。",
            "revoked_basis_requires_review": "此依据已被撤销或暂缓，需要重新复核。",
            "insufficient_score_not_a_cannot_link": "当前依据不足以归并；不代表已判定为不同企业。"}
        return templates.TemplateResponse(request=request, name="app.html", context={"page": page,
            "revision": store.current_revision(), "demo": local_demo, "labels": labels,
            "role": getattr(request.state, "identity", (None, None))[1], **context})

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(request=request, name="app.html",
            context={"page": "login", "revision": None, "demo": local_demo, "role": None})

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, query: str = "", revision: str | None = None,
             offset: int = Query(0, ge=0), _identity=Depends(actor)):
        page = store.entities_page(query, revision=revision, offset=offset, limit=100)
        return render(request, "entities", entities=page["items"], total=page["total"],
                      query=query, offset=offset, revision=page["revision_id"])

    @app.get("/entity/{entity_id}", response_class=HTMLResponse)
    def detail(request: Request, entity_id: str, revision: str | None = None, _identity=Depends(actor)):
        selected = store.entity(entity_id, revision=revision)
        return render(request, "detail", entity=selected, revision=selected["revision_id"])

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews(request: Request, revision: str | None = None, offset: int = Query(0, ge=0),
                status: Literal["all", "review", "conflict", "accepted", "suppressed", "below_threshold"] = "all",
                _identity=Depends(actor)):
        current = revision or store.current_revision()
        payload = store._payload(current) if current else None
        queue = []
        if payload:
            evidence = {tuple(sorted((edge["left"], edge["right"]))): edge for edge in payload["edges"]}
            for decision in payload["edge_decisions"]:
                if decision["score"] is None or (status != "all" and decision["status"] != status):
                    continue
                pair = tuple(sorted((decision["left"], decision["right"])))
                queue.append({**decision, "left_record": payload["records"][decision["left"]],
                    "right_record": payload["records"][decision["right"]], "evidence": evidence[pair].get("evidence", {})})
            queue.sort(key=lambda row: (row["status"] == "accepted", abs(row["score"] - 0.5)))
        return render(request, "reviews", queue=queue[offset:offset + 100], total=len(queue), payload=payload,
                      offset=offset, status=status, revision=current)

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request, _identity=Depends(reviewer)):
        return render(request, "history", history=store.history(), decisions=store.decision_history())

    @app.get("/tasks", response_class=HTMLResponse)
    def task_page(request: Request, _identity=Depends(admin)):
        from .jobs import JobQueue
        return render(request, "tasks", jobs=JobQueue(store).list_jobs(limit=100),
                      request_key=str(uuid4()), model_configured=model_path is not None)

    @app.post("/ui/jobs")
    def ui_submit_job(idempotency_key: str = Form(), method: str = Form("exact"),
                      threshold: float = Form(.9), review_threshold: float = Form(.5), identity=Depends(admin)):
        body = JobRequest(settings=RunRequest(method=method, threshold=threshold,
                                              review_threshold=review_threshold), idempotency_key=idempotency_key)
        submit_job(body, identity)
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/ui/jobs/{job_id}/cancel")
    def ui_cancel_job(job_id: str, identity=Depends(admin)):
        cancel_job(job_id, identity)
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/ui/jobs/{job_id}/retry")
    def ui_retry_job(job_id: str, identity=Depends(admin)):
        retry_job(job_id, identity)
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/ui/login")
    def login(token: str = Form()):
        authenticate(token)
        response = RedirectResponse("/reviews", status_code=303)
        response.set_cookie("eb_session", token, httponly=True, samesite="strict", max_age=3600)
        return response

    @app.post("/ui/decision")
    def ui_decision(left: str = Form(), right: str = Form(), action: str = Form(), reason: str = Form(),
                    base_revision: str = Form(), left_version: str = Form(), right_version: str = Form(),
                    policy_version: str = Form(), identity=Depends(reviewer)):
        store.decide(left, right, action=action, reason=reason, reviewer=identity[0], base_revision=base_revision,
            left_version=left_version, right_version=right_version, policy_version=policy_version)
        return RedirectResponse("/history", status_code=303)

    @app.post("/ui/publish")
    def ui_publish(revision_id: str = Form(), expected_parent: str = Form(""), _identity=Depends(admin)):
        store.publish(revision_id, expected_parent=expected_parent or None)
        return RedirectResponse("/history", status_code=303)

    @app.post("/ui/revoke-preview", response_class=HTMLResponse)
    def ui_preview(request: Request, decision_id: str = Form(), base_revision: str = Form(), _identity=Depends(reviewer)):
        preview = store.revoke_preview(decision_id, base_revision=base_revision)
        records = store._payload(base_revision)["records"]
        groups = [[records[record] for record in group] for group in preview["partitions"]]
        return render(request, "preview", preview=preview, groups=groups)

    @app.post("/ui/revoke")
    def ui_revoke(decision_id: str = Form(), base_revision: str = Form(), reason: str = Form(),
                  preview_cutoff: int = Form(), identity=Depends(reviewer)):
        store.revoke(decision_id, base_revision=base_revision, reason=reason, reviewer=identity[0], preview_cutoff=preview_cutoff)
        return RedirectResponse("/history", status_code=303)

    @app.get("/health")
    def health():
        return {"status": "ok", **({"revision": store.current_revision()} if local_demo else {})}

    return app
