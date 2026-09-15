from fastapi.testclient import TestClient

from entitybridge.api import create_app
from entitybridge.candidates import generate_candidates
from entitybridge.country import country_candidate_view, country_key
from entitybridge.store import Store


def test_import_accepts_full_country_name_and_keeps_raw_value(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'country.db'}", tmp_path / "artifacts")
    store.initialize()
    client = TestClient(create_app(store, local_demo=True))
    country = "United States of America"
    response = client.post("/imports", json={"source": "a", "rows": [
        {"source_key": "1", "name": "Example Incorporated", "country": country}]})
    assert response.status_code == 200, response.text
    assert next(iter(store.active_records().values()))["country"] == country


def test_iso_aliases_are_exact_and_keep_unknowns_and_territories_distinct():
    assert country_key("USA") == country_key("United States of America") == country_key("840")
    assert country_key("GBR") == country_key("United Kingdom")
    assert country_key(None) is None and country_key("  ") is None
    assert country_key("Congo") != country_key("Congo, The Democratic Republic of the")
    assert len({country_key(s) for s in ("CN", "TW", "HK")}) == 3
    assert country_key("USA typo") != country_key("US")
    assert country_key("unmapped country") is not None


def test_candidate_country_copy_recovers_alias_pair_but_preserves_country_conflict():
    records = [{"record_id": key, "source": key, "name": "SYNTHETIC BANK", "country": country}
               for key, country in [("a", "USA"), ("b", "United States of America"), ("c", "Canada")]]
    assert generate_candidates(records) == []
    new = country_candidate_view(records)
    candidates = generate_candidates(new)
    assert [(c["left"], c["right"]) for c in candidates] == [("a", "b")]
    assert records[0]["country"] == "USA"


def test_iso_matching_is_review_only_and_preserves_original_evidence(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'iso.db'}", tmp_path / "artifacts")
    store.initialize()
    for source, country in [("a", "USA"), ("b", "United States of America")]:
        store.import_records(source, [{"source_key": "1", "name": "SYNTHETIC BANK", "country": country}])
    client = TestClient(create_app(store, local_demo=True))
    old = client.post("/match-runs", json={"method": "exact"}).json()
    assert old["candidate_pairs"] == 0
    new = client.post("/match-runs", json={"method": "exact", "candidate_mode": "fixed_iso"}).json()
    assert new["candidate_pairs"] == 1 and new["automatic_merge"] is False
    payload = store._payload(new["revision_id"], published_only=False)
    assert payload["edges"][0]["auto_merge"] is False
    assert {r["country"] for r in payload["records"].values()} == {"USA", "United States of America"}
    assert store.current_revision() is None
