import pytest
from fastapi.testclient import TestClient

from entitybridge.api import create_app
from entitybridge.cli import seed_demo
from entitybridge.store import Store


def test_normal_service_requires_authentication_for_company_data(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite'}", tmp_path / "revisions")
    store.initialize()
    seed_demo(store)
    client = TestClient(create_app(store, tokens={"viewer-token": ("auditor", "viewer")}))
    revision = store.current_revision()
    entity = store.entities()[0]
    paths = ["/entities", f"/entities/{entity['entity_id']}",
             f"/records/{entity['members'][0]}/links", f"/exports?revision={revision}"]
    for path in paths:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer viewer-token"}).status_code == 200
    assert client.get("/health").status_code == 200


def test_viewer_has_no_review_or_publication_authority_and_can_log_in(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'roles.sqlite'}", tmp_path / "revisions")
    store.initialize()
    seed_demo(store)
    client = TestClient(create_app(store, tokens={"viewer-token": ("auditor", "viewer"),
                                                 "review-token": ("reviewer", "reviewer")}))
    assert client.get("/login").status_code == 200
    assert 'name="token"' in client.get("/login").text
    assert 'name="token"' in client.get("/", headers={"Accept": "text/html"}).text
    assert client.post("/ui/login", data={"token": "viewer-token"}).status_code == 200
    assert client.get("/entities").status_code == 200
    assert client.get("/history").status_code == 403
    assert client.post("/ui/decision", data={"left": "a", "right": "b", "action": "accept",
        "reason": "test", "base_revision": "r", "left_version": "a", "right_version": "b",
        "policy_version": "p"}).status_code == 403
    assert client.post("/registry-runs").status_code == 403
    review = {"Authorization": "Bearer review-token"}
    assert client.get("/history", headers=review).status_code == 200
    assert client.post("/ui/publish", data={"revision_id": "r"}, headers=review).status_code == 403


def test_pagination_remains_on_a_published_snapshot(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'pages.sqlite'}", tmp_path / "revisions")
    store.initialize()
    store.import_records("test", [{"source_key": str(i), "name": f"Company {i}"} for i in range(105)])
    original = store.prepare_revision([], policy_version="pages")["revision_id"]
    store.publish(original, expected_parent=None)
    client = TestClient(create_app(store, local_demo=True))
    first = client.get("/entities")
    assert len(first.json()) == 100
    assert first.headers["X-Total-Count"] == "105"
    assert first.headers["X-Revision-Id"] == original
    store.import_records("test", [{"source_key": "new", "name": "New Company"}])
    new = store.prepare_revision([], policy_version="pages")["revision_id"]
    store.publish(new, expected_parent=original)
    second = client.get("/entities", params={"offset": 100, "revision": original})
    assert len(second.json()) == 5
    assert len({e["entity_id"] for e in first.json() + second.json()}) == 105
    page = client.get("/", params={"revision": original})
    assert "offset=100" in page.text and original in page.text
    assert "下一页" in page.text
    assert client.get("/entities?offset=-1").status_code == 422
    assert client.get("/entities?limit=1001").status_code == 422


@pytest.mark.parametrize("tokens", [{"bad token": ["name", "admin"]}, {"good": ["name", []]}])
def test_malformed_permission_configuration_is_rejected(tmp_path, tokens):
    store = Store(f"sqlite:///{tmp_path / 'tokens.sqlite'}", tmp_path / "revisions")
    with pytest.raises(ValueError, match="Each token"):
        create_app(store, tokens=tokens)
