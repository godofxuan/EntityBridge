from fastapi.testclient import TestClient

from entitybridge.api import create_app
from entitybridge.store import Store


def test_api_import_publish_provenance_and_role_boundary(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'api.db'}", tmp_path / "artifacts")
    store.initialize()
    client = TestClient(create_app(store, tokens={"review-token": ("reviewer", "reviewer"), "admin-token": ("admin", "admin")}))
    payload = {"source": "demo", "rows": [{"source_key": "a", "name": "Example Limited", "country": "GB",
        "category": "PRI/LBG/NSC (Private, Limited by guarantee, no share capital, use of 'Limited' exemption)"}]}
    assert client.post("/imports", json=payload).status_code == 401
    assert client.post("/imports", json=payload, headers={"Authorization": "Bearer review-token"}).status_code == 403
    admin = {"Authorization": "Bearer admin-token"}
    client.headers.update(admin)
    assert client.post("/imports", json=payload, headers=admin).status_code == 200
    candidate = client.post("/match-runs", json={"method": "exact"}, headers=admin)
    assert candidate.status_code == 200, candidate.text
    rid = candidate.json()["revision_id"]
    assert client.get("/entities").json() == []
    assert client.post(f"/revisions/{rid}/publish", json={"expected_parent": None}, headers=admin).status_code == 200
    entity = client.get("/entities").json()[0]
    assert client.get(f"/entities/{entity['entity_id']}").json()["canonical"]["name"]["source"] == "demo"
    record_id = entity["members"][0]
    changed = {"source": "demo", "rows": [{"source_key": "a", "name": "Changed Limited", "country": "GB"}]}
    assert client.post("/imports", json=changed, headers=admin).status_code == 200
    next_revision = client.post("/match-runs", json={"method": "exact"}, headers=admin).json()["revision_id"]
    assert client.post(f"/revisions/{next_revision}/publish", json={"expected_parent": rid}, headers=admin).status_code == 200
    assert client.get(f"/records/{record_id}/links", params={"revision": rid}).json()["record"]["name"] == "Example Limited"
    assert client.get(f"/records/{record_id}/links").json()["record"]["name"] == "Changed Limited"
    historical_page = client.get(f"/entity/{entity['entity_id']}", params={"revision": rid}).text
    assert f"/links?revision={rid}" in historical_page
    for path in ("/", "/reviews", "/history"):
        response = client.get(path)
        assert response.status_code == 200
        assert "EntityBridge" in response.text


def test_local_demo_refuses_untrusted_host_before_granting_loopback_admin(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'host.db'}", tmp_path / "artifacts")
    store.initialize()
    client = TestClient(create_app(store, local_demo=True))
    response = client.post("/imports", json={"source": "demo", "rows": [{"source_key": "a", "name": "Alpha"}]},
        headers={"Host": "attacker.example", "Origin": "http://attacker.example"})
    assert response.status_code == 400


def test_identifier_rich_route_is_explicit_and_does_not_merge_branch_relationships(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'registry.db'}", tmp_path / "artifacts")
    store.initialize()
    client = TestClient(create_app(store, local_demo=True))
    common = {"name": "Example", "registration_authority": "RA000585", "registration_number": "00000001"}
    gleif = common | {"source_key": "lei", "status": "ACTIVE", "category": "GENERAL", "registration_status": "ISSUED"}
    ch = common | {"source_key": "00000001", "status": "Active"}
    assert client.post("/imports", json={"source": "gleif", "rows": [gleif]}).status_code == 200
    assert client.post("/imports", json={"source": "companies_house", "rows": [ch]}).status_code == 200
    candidate = client.post("/registry-runs").json()
    assert candidate["candidate_pairs"] == 1
    gleif["relationships"] = [{"kind": "branch_of", "target_source": "companies_house", "target_key": "00000001",
                               "evidence": "Synthetic branch relationship; not same-as"}]
    assert client.post("/imports", json={"source": "gleif", "rows": [gleif]}).status_code == 200
    separate = client.post("/registry-runs").json()
    assert separate["candidate_pairs"] == 0
    assert len(separate["excluded_record_ids"]) == 1
