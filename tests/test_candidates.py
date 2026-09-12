import pytest

from entitybridge.candidates import CandidateOverflow, generate_candidates


def record(key, source, name, postcode="SW1A1AA", address="1 TEST ROAD"):
    return {"record_id": key, "record_version_id": key + "v1", "source": source,
            "name": name, "postcode": postcode, "address": address,
            "country": "GB", "city": "LONDON"}


def test_fixed_rules_union_deduplicates_pairs_and_preserves_evidence():
    records = [record("a", "gleif", "ALPHA LIMITED"),
               record("b", "companies_house", "ALPHA LIMITED"),
               record("c", "gleif", "ALPHA LIMITED")]
    candidates = generate_candidates(records)
    assert [(c["left"], c["right"]) for c in candidates] == [("a", "b"), ("b", "c")]
    assert "name_exact" in candidates[0]["rules"]
    assert "address_exact" in candidates[0]["rules"]


def test_large_bucket_fails_explicitly_instead_of_returning_top_k():
    records = [record("a", "gleif", "ALPHA LIMITED"),
               record("b", "companies_house", "ALPHA LIMITED"),
               record("c", "companies_house", "ALPHA LIMITED")]
    with pytest.raises(CandidateOverflow, match="2 cross-source pairs"):
        generate_candidates(records, max_bucket_pairs=1)


def test_answer_columns_are_rejected_at_matcher_boundary():
    contaminated = record("a", "gleif", "ALPHA LIMITED") | {"company_number": "00012345"}
    with pytest.raises(ValueError, match="allowlist"):
        generate_candidates([contaminated])


def test_missing_country_does_not_block_a_name_only_query():
    a = record("a", "gleif", "ALPHA LIMITED", postcode=None, address=None) | {"country": None}
    b = record("b", "companies_house", "ALPHA LIMITED")
    assert [(edge["left"], edge["right"]) for edge in generate_candidates([a, b])] == [("a", "b")]
