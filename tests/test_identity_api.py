"""Signed JWTs cross the complete API authorization and audit boundary."""

import time
from datetime import UTC, datetime

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_oidc import issuer as issuer  # noqa: PLC0414 - reuse local JWKS HTTP fixture
from test_oidc import keys as keys  # noqa: PLC0414 - reuse actual RSA key fixture
from test_oidc import token

from entitybridge import schema as s
from entitybridge.api import create_app
from entitybridge.auth import OidcVerifier
from entitybridge.store import Store


@pytest.fixture
def app_state(tmp_path, issuer):
    _, config = issuer
    store = Store(f"sqlite:///{tmp_path / 'identity.db'}", tmp_path / "artifacts")
    store.initialize()
    store.import_records("registry", [{"source_key": "a", "name": "ALPHA"},
                                      {"source_key": "b", "name": "BETA"}])
    parent = store.prepare_revision([], policy_version="identity-api")["revision_id"]
    store.publish(parent, expected_parent=None)
    app = create_app(store, tokens={"legacy-token": ("legacy-reviewer", "reviewer")},
                     oidc_verifier=OidcVerifier(config))
    with TestClient(app) as client:
        client.headers["Idempotency-Key"] = "identity-audit"
        yield store, client, config
    store.engine.dispose()


def decision_body(store):
    left, right = list(store.active_records().values())
    return {"left": left["record_id"], "right": right["record_id"], "action": "reject",
            "reason": "Signed API identity integration evidence", "base_revision": store.current_revision(),
            "left_version": left["record_version_id"], "right_version": right["record_version_id"],
            "policy_version": "identity-api"}


@pytest.mark.parametrize("role", ["viewer", "reviewer", "admin"])
def test_signed_role_controls_reads_reviews_imports_and_publication(keys, app_state, role):
    store, client, config = app_state
    value = token(keys, config, {"sub": f"oidc-{role}", "entitybridge_roles": {"workspace-a": role}})
    client.headers["Authorization"] = "Bearer " + value
    assert client.get("/entities").status_code == 200
    assert client.get("/history").status_code == (403 if role == "viewer" else 200)
    parent = store.current_revision()
    prepared = store.prepare_revision([], policy_version="identity-api")["revision_id"]
    published = client.post(f"/revisions/{prepared}/publish", json={"expected_parent": parent})
    assert published.status_code == (200 if role == "admin" else 403)
    decision = client.post("/reviews/decision", json=decision_body(store))
    assert decision.status_code == (403 if role == "viewer" else 200)
    if role != "viewer":
        with store.engine.connect() as con:
            event = con.execute(select(s.events).where(
                s.events.c.decision_id == decision.json()["decision_id"])).mappings().one()
            row = con.execute(select(s.decisions).where(
                s.decisions.c.decision_id == decision.json()["decision_id"])).mappings().one()
        assert row["reviewer"] == event["reviewer"] == f"oidc-{role}"
    imported = client.post("/imports", json={"source": "other", "rows": [{"source_key": "c", "name": "GAMMA"}]})
    assert imported.status_code == (200 if role == "admin" else 403)


def test_cookie_revalidates_signed_expiration_on_every_request(keys, app_state, monkeypatch):
    _, client, config = app_state
    expiration = int(time.time()) + 300
    value = token(keys, config, {"exp": expiration, "entitybridge_roles": {"workspace-a": "viewer"}})
    login = client.post("/ui/login", data={"token": value}, follow_redirects=False)
    assert login.status_code == 303
    assert "HttpOnly" in login.headers["set-cookie"] and "SameSite=strict" in login.headers["set-cookie"]
    assert client.get("/entities").status_code == 200

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(expiration + 1, tz=UTC)

    monkeypatch.setattr(jwt.api_jwt, "datetime", ExpiredClock)
    assert client.cookies.get("eb_session") == value
    assert client.get("/entities").status_code == 401
    assert client.get("/", headers={"Accept": "text/html"}).status_code == 401
    assert client.post("/ui/login", data={"token": value}, follow_redirects=False).status_code == 401


def test_workspace_b_token_cannot_enter_a_via_header_cookie_or_workspace_selectors(keys, app_state):
    store, client, config = app_state
    value = token(keys, config, {"sub": "workspace-b-admin", "workspace": "workspace-b",
                                "entitybridge_roles": {"workspace-b": "admin"}})
    headers = {"Authorization": "Bearer " + value, "X-Workspace": "workspace-b", "X-Tenant": "workspace-b"}
    assert client.get("/entities?workspace=workspace-b", headers=headers).status_code == 401
    assert client.post("/reviews/decision", json=decision_body(store), headers=headers).status_code == 401
    assert client.post("/ui/login", data={"token": value}, follow_redirects=False).status_code == 401
    client.cookies.set("eb_session", value)
    assert client.get("/entities").status_code == 401
    with store.engine.connect() as con:
        assert con.execute(select(s.decisions)).all() == []


def test_static_token_still_works_without_contacting_oidc_and_keeps_static_audit_subject(app_state, issuer):
    store, client, _ = app_state
    state, _ = issuer
    state["status"] = 503
    client.headers["Authorization"] = "Bearer legacy-token"
    assert client.get("/entities").status_code == 200
    result = client.post("/reviews/decision", json=decision_body(store))
    assert result.status_code == 200
    with store.engine.connect() as con:
        assert con.execute(select(s.decisions.c.reviewer)).scalar_one() == "legacy-reviewer"
    assert state["requests"] == []
    client.headers.pop("Authorization")
    assert client.post("/ui/login", data={"token": "legacy-token"}, follow_redirects=False).status_code == 303
    assert client.get("/entities").status_code == 200
    assert state["requests"] == []


def test_invalid_explicit_bearer_does_not_fall_back_to_valid_admin_cookie(keys, app_state):
    _, client, config = app_state
    admin = token(keys, config, {"entitybridge_roles": {"workspace-a": "admin"}})
    assert client.post("/ui/login", data={"token": admin}, follow_redirects=False).status_code == 303
    response = client.get("/entities", headers={"Authorization": "Bearer invalid-explicit-token"})
    assert response.status_code == 401
    assert "invalid-explicit-token" not in response.text and admin not in response.text
