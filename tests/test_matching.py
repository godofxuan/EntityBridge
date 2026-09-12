import pytest

from entitybridge.candidates import generate_candidates
from entitybridge.matching import score_baselines


def rows():
    return [{"record_id": "a", "record_version_id": "av1", "source": "gleif", "name": "ALPHA LIMITED",
             "address": "1 TEST ROAD", "city": "LONDON", "postcode": "AA11AA", "country": "GB"},
            {"record_id": "b", "record_version_id": "bv1", "source": "companies_house", "name": "ALPHA LTD",
             "address": "1 TEST ROAD", "city": "LONDON", "postcode": "AA11AA", "country": "GB"}]


def test_baselines_score_same_candidates_and_keep_field_and_version_evidence():
    result = score_baselines(rows(), generate_candidates(rows()))
    assert result["exact"][0]["score"] == 0.0
    assert 0 < result["fuzzy"][0]["score"] < 1
    assert result["fuzzy"][0]["left_version"] == "av1"
    assert result["fuzzy"][0]["evidence"]["name"]["left"] == "ALPHA LIMITED"
    assert result["fuzzy"][0]["right"] == "b"


def test_real_splink_fit_save_reload_freezes_scores_and_term_frequencies(tmp_path):
    from entitybridge.matching import SplinkMatcher
    training = []
    for i in range(12):
        for row in rows():
            training.append(row | {"record_id": f"{row['record_id']}{i}",
                                    "name": f"{row['name']} {i}",
                                    "postcode": f"AB{i}1AA", "address": f"{i} TEST ROAD"})
    model = SplinkMatcher.fit(training, seed=41, max_u_pairs=500)
    candidates = generate_candidates(training)
    before = model.score(training, candidates)
    model.save(tmp_path / "model")
    loaded = SplinkMatcher.load(tmp_path / "model")
    unrelated = training[0] | {"record_id": "extra", "postcode": "ZZ1ZZ", "address": "OTHER ROAD"}
    after = loaded.score(training + [unrelated], candidates)
    assert {(e["left"], e["right"]) for e in before} == {(e["left"], e["right"]) for e in after}
    assert [e["score"] for e in before] == pytest.approx([e["score"] for e in after], abs=1e-12)
    assert all("comparison_level" in edge["evidence"]["name"] for edge in before)
    assert model.fingerprint == loaded.fingerprint
    exposed = loaded.settings
    exposed["probability_two_random_records_match"] = 0.5
    assert loaded.settings["probability_two_random_records_match"] != 0.5
    subset = loaded.score(training, candidates[:1])
    assert len(subset) == loaded.last_prediction_count == 1
