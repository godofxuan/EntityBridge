"""Local demo and API startup; no external deployment or account required."""
import argparse
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import URL

from .api import create_app
from .store import Store


def database_url(root, *, portable=False):
    if portable:
        config = json.loads((root / ".tools/database.json").read_text())
        return URL.create("postgresql+psycopg", username=config["user"], password=config["password"],
            host=config["host"], port=config["port"], database="entitybridge")
    return os.environ.get("DATABASE_URL", f"sqlite:///{(root / 'artifacts/demo.sqlite').as_posix()}")


def seed_demo(store):
    """Deliberately synthetic company names and addresses, isolated from benchmarks."""
    if store.current_revision():
        return store.current_revision()
    a = store.import_records("demo_erp", [
        {"source_key": "a", "name": "NORTHSTAR ENERGY LIMITED", "address": "10 EXAMPLE ROAD", "postcode": "ZZ1 1ZZ", "country": "GB"},
        {"source_key": "c", "name": "NORTHSTAR ENERGY SERVICES LIMITED", "address": "10 EXAMPLE ROAD", "postcode": "ZZ1 1ZZ", "country": "GB"},
        {"source_key": "d", "name": "HARBOUR DIGITAL LIMITED", "address": "22 DEMO STREET", "postcode": "ZZ2 2ZZ", "country": "GB"},
    ])["records"]
    b = store.import_records("demo_contracts", [
        {"source_key": "b", "name": "NORTHSTAR ENERGY LTD", "address": "10 EXAMPLE ROAD", "postcode": "ZZ1 1ZZ", "country": "GB"},
        {"source_key": "e", "name": "HARBOUR DIGITAL LTD", "address": "22 DEMO STREET", "postcode": "ZZ2 2ZZ", "country": "GB"},
    ])["records"]
    raw = store.active_records()
    edges = []
    for left, right, score in [(a[0], b[0], .99), (a[1], b[0], .96), (a[2], b[1], .78)]:
        l, r = left["record_id"], right["record_id"]
        edges.append({"left": l, "right": r, "left_version": left["record_version_id"],
            "right_version": right["record_version_id"], "score": score, "candidate_rules": ["synthetic_governance_challenge"],
            "evidence": {"kind": "synthetic_demo_score_not_a_model_measurement",
                "name": {"left": raw[l]["name"], "right": raw[r]["name"]},
                "address": {"left": raw[l]["address"], "right": raw[r]["address"]}}})
    first = store.prepare_revision(edges, policy_version="synthetic-governance-v1")["revision_id"]
    store.publish(first, expected_parent=None)
    return first


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["demo", "serve", "init-db", "worker", "submit-job", "job", "cancel-job", "retry-job",
        "backup", "verify-backup", "restore-backup", "export-labels", "train-review-model", "train-review-ditto",
        "verify-projection", "rebuild-projection"])
    parser.add_argument("--portable-postgres", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--base-model", type=Path, help="Local RoBERTa safetensors directory for Ditto training")
    parser.add_argument("--device", default="cpu", help="Ditto training device, e.g. cpu or cuda; service inference uses CPU")
    parser.add_argument("--candidate-model", type=Path, help="Frozen training-only retriever for optional hybrid review candidates")
    parser.add_argument("--database-url", help="Database URL; DATABASE_URL environment is preferred for secrets")
    parser.add_argument("--artifact-root", type=Path, help="Identity artifact directory")
    parser.add_argument("--input", type=Path, help="Input backup, review dataset, or JSON matching settings")
    parser.add_argument("--output", type=Path, help="New output directory")
    parser.add_argument("--restore-database-url", help="Empty restore target; ENTITYBRIDGE_RESTORE_DATABASE_URL also supported")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--job-id")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--once", action="store_true", help="Claim at most one durable job and exit")
    parser.add_argument("--poll-seconds", type=float, default=2)
    parser.add_argument("--lease-seconds", type=float, default=60)
    parser.add_argument("--calibrate", action="store_true", help="Require an independent calibration split")
    parser.add_argument("--revision")
    parser.add_argument("--workspace-config", type=Path, help="Startup-only isolated workspace configuration")
    parser.add_argument("--workspace", help="Workspace name; cannot be changed by an HTTP request")
    parser.add_argument("--disable-review-recovery", action="store_true",
                        help="Disable explicit review recovery while retaining receipts and historical reads")
    args = parser.parse_args()
    if bool(args.workspace_config) != bool(args.workspace):
        parser.error("Supply --workspace-config and --workspace together")
    if args.workspace_config and (args.database_url or args.artifact_root or args.portable_postgres or args.command == "demo"):
        parser.error("Workspace configuration cannot be combined with database/artifact overrides or demo mode")
    root = Path.cwd()
    def show(value):
        print(json.dumps(value, ensure_ascii=False, default=str))
    if args.command in {"verify-backup", "restore-backup", "train-review-model", "train-review-ditto"}:
        if args.input is None:
            parser.error("This command requires --input")
        if args.command == "verify-backup":
            from .operations import verify_backup
            show(verify_backup(args.input))
        elif args.command == "restore-backup":
            from .operations import restore_backup
            target_url = args.restore_database_url or os.environ.get("ENTITYBRIDGE_RESTORE_DATABASE_URL")
            if not target_url or args.output is None:
                parser.error("Restore requires an explicit empty target database and --output artifact directory")
            show(restore_backup(args.input, target_url, args.output))
        elif args.command == "train-review-ditto":
            from .ditto_learning import train_review_ditto
            if args.output is None or args.base_model is None:
                parser.error("Ditto training requires --base-model and --output")
            if args.calibrate:
                parser.error("Ditto probabilities are uncalibrated; --calibrate is not supported")
            show(train_review_ditto(args.input, args.output, base_model=args.base_model, device=args.device))
        else:
            from .learning import train_candidate_model
            if args.output is None:
                parser.error("Training requires --output")
            show(train_candidate_model(args.input, args.output, calibrate=args.calibrate))
        return
    (root / "artifacts").mkdir(exist_ok=True)
    selected = None
    if args.workspace_config:
        from .workspaces import load_workspace
        selected = load_workspace(args.workspace_config, args.workspace)
    store = Store(selected.database_url if selected else args.database_url or database_url(root, portable=args.portable_postgres),
                  selected.artifact_root if selected else args.artifact_root or root / "artifacts/revisions")
    if args.command == "backup":
        if args.output is None:
            parser.error("Backup requires --output")
        from .operations import create_backup
        show(create_backup(store, args.output, expected_workspace=selected.name if selected else None))
        return
    store.initialize()
    if selected:
        from .workspace_binding import bind_workspace
        bind_workspace(store, selected.name)
    if args.command == "init-db":
        print("Database initialized")
        return
    if args.command == "export-labels":
        if args.output is None:
            parser.error("This command requires --output")
        from .learning import export_review_dataset
        show(export_review_dataset(store, args.output))
        return
    if args.command in {"verify-projection", "rebuild-projection"}:
        if args.command == "rebuild-projection":
            show(store.rebuild_query_projection(args.revision))
        else:
            show(store.projection_status(args.revision, verify=True))
        return
    if args.command in {"worker", "submit-job", "job", "cancel-job", "retry-job"}:
        from .jobs import JobQueue
        from .worker import execute_matching_job, model_fingerprints
        queue = JobQueue(store)
        if args.command == "worker":
            if args.poll_seconds < .1 or args.lease_seconds < 1:
                parser.error("Polling must be >=0.1s and the lease must be >=1s")
            owner = "worker-" + uuid4().hex
            try:
                while True:
                    result = queue.run_once(owner, lambda lease: execute_matching_job(
                        store, queue, lease, model_path=args.model, candidate_model_path=args.candidate_model),
                        lease_seconds=args.lease_seconds)
                    if result is not None:
                        show(result)
                    if args.once:
                        break
                    if result is None:
                        time.sleep(args.poll_seconds)
            except KeyboardInterrupt:
                pass
        elif args.command == "submit-job":
            from .api import RunRequest
            if args.input is None or not args.idempotency_key:
                parser.error("Submission requires --input settings JSON and --idempotency-key")
            settings = RunRequest.model_validate_json(args.input.read_text(encoding="utf-8"))
            show(queue.enqueue({"settings": settings.model_dump(), "models": model_fingerprints(
                settings, args.model, args.candidate_model)}, idempotency_key=args.idempotency_key,
                max_attempts=args.max_attempts))
        else:
            if not args.job_id:
                parser.error("This command requires --job-id")
            action = {"job": queue.get, "cancel-job": queue.cancel, "retry-job": queue.retry}[args.command]
            show(action(args.job_id))
        return
    if args.command == "demo":
        seed_demo(store)
    token_config = selected.tokens if selected else json.loads(os.environ.get("ENTITYBRIDGE_TOKENS", "{}"))
    oidc_config = selected.oidc if selected else json.loads(os.environ.get("ENTITYBRIDGE_OIDC", "null"))
    verifier = None
    if oidc_config:
        from .auth import OidcConfig, OidcVerifier
        verifier = OidcVerifier(OidcConfig(**oidc_config))
    if args.command == "serve" and not token_config and verifier is None:
        raise SystemExit("Configure ENTITYBRIDGE_TOKENS or ENTITYBRIDGE_OIDC before serving")
    import uvicorn
    print(f"EntityBridge local URL: http://127.0.0.1:{args.port}")
    uvicorn.run(create_app(store, tokens=token_config, model_path=args.model, candidate_model_path=args.candidate_model,
                         local_demo=args.command == "demo", oidc_verifier=verifier,
                         review_recovery_enabled=not args.disable_review_recovery),
                host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
