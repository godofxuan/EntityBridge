"""Candidate recall and exact requested-pair scoring regression tests."""
import pytest

from entitybridge.candidates import CandidateOverflow, generate_candidates


def row(key, source, name, country="GB"):
    return {"record_id": key, "record_version_id": key + "-v1", "source": source,
            "name": name, "address": None, "city": None, "postcode": None, "country": country}


def test_splink_scores_explicit_pair_outside_legacy_blocking():
    from entitybridge.matching import SplinkMatcher
    training = [row(f"{s}{i}", s, f"ALPHA BUSINESS {i} LIMITED")
                for s in ("left", "right") for i in range(6)]
    model = SplinkMatcher.fit(training, max_u_pairs=100)
    records = [row("a", "left", "OLD ALPHA BUSINESS LIMITED"),
               row("b", "right", "NEW ALPHA BUSINESS LIMITED"),
               row("c", "right", "UNRELATED BUSINESS LIMITED")]
    assert generate_candidates(records) == []
    requested = [{"left": "a", "right": "b", "rules": ["external_retrieval"]}]
    scores = model.score(records, requested)
    assert [(e["left"], e["right"]) for e in scores] == [("a", "b")]
    assert model.last_prediction_count == 1


def test_frozen_name_retriever_recovers_changed_prefix_and_is_order_invariant(tmp_path):
    from entitybridge.candidates import FrozenNameRetriever
    training = [row("t1", "left", "ALPHA BUSINESS LIMITED"),
                row("t2", "right", "BETA SERVICES LIMITED")]
    retrieval = FrozenNameRetriever.fit(training, top_k=1, min_similarity=0.1)
    records = [row("a", "left", "OLD ALPHA BUSINESS LIMITED"),
               row("b", "right", "NEW ALPHA BUSINESS LIMITED"),
               row("c", "right", "BETA SERVICES LIMITED")]
    assert generate_candidates(records) == []
    candidates = generate_candidates(records, retriever=retrieval)
    assert ("a", "b") in {(c["left"], c["right"]) for c in candidates}
    assert generate_candidates(list(reversed(records)), retriever=retrieval) == candidates
    assert retrieval.requires_full_recompute
    retrieval.save(tmp_path / "retriever")
    loaded = FrozenNameRetriever.load(tmp_path / "retriever")
    assert loaded.fingerprint == retrieval.fingerprint
    assert generate_candidates(records, retriever=loaded) == candidates
    before = retrieval.fingerprint
    exposed = retrieval.metadata
    exposed["top_k"] = 50
    assert retrieval.metadata["top_k"] == 1
    loaded.generate(records + [row("d", "right", "ALPHA BUSINESS LIMITED")])
    assert retrieval.fingerprint == before


def test_retrieval_respects_country_and_budget_and_rejects_answer_columns():
    from entitybridge.candidates import FrozenNameRetriever
    train = [row("t1", "left", "ACME LIMITED"), row("t2", "right", "BETA LIMITED")]
    retrieval = FrozenNameRetriever.fit(train, top_k=1, min_similarity=0, max_records=4)
    records = [row("a", "left", "ACME LIMITED"), row("b", "right", "ACME LIMITED", "US"),
               row("c", "right", "ACME LIMTED", None)]
    assert [(c["left"], c["right"]) for c in retrieval.generate(records)] == [("a", "c")]
    with pytest.raises(CandidateOverflow, match="record budget"):
        retrieval.generate(records + [row("d", "right", "D"), row("e", "right", "E")])
    with pytest.raises(ValueError, match="allowlist"):
        FrozenNameRetriever.fit([train[0] | {"company_number": "12345678"}])


def test_retrieval_explicit_interaction_budget_and_model_corruption(tmp_path):
    import json

    from entitybridge.candidates import FrozenNameRetriever
    train = [row("t1", "left", "ACME LIMITED"), row("t2", "right", "BETA LIMITED")]
    retrieval = FrozenNameRetriever.fit(train, max_cross_source_pairs=1)
    with pytest.raises(CandidateOverflow, match="interaction budget"):
        retrieval.generate([row("a", "left", "ACME"), row("b", "right", "ACME"), row("c", "right", "BETA")])
    retrieval.save(tmp_path / "model")
    path = tmp_path / "model/retriever.json"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["idf"][0] += 0.1
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        FrozenNameRetriever.load(tmp_path / "model")
