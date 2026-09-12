"""Feature, artifact and split boundaries run without the neural extra."""
import json
import random

import pytest

from entitybridge.ditto import (
    FIELDS,
    FORMAT,
    UPSTREAM,
    bundle_manifest,
    file_hash,
    pair_texts,
    serialize_record,
)
from entitybridge.ditto_training import TrainingConfig, development_examples, select_policies, write_json
from entitybridge.store import digest
from entitybridge.vendor.ditto_augment import Augmenter


def fake_bundle(path, domain="company"):
    path.mkdir()
    (path / "tokenizer").mkdir()
    for name in ("model.safetensors", "config.json", "tokenizer/tokenizer.json"):
        (path / name).write_text("synthetic manifest test", encoding="utf-8")
    manifest = {"format": FORMAT, "domain": domain, "fields": list(FIELDS[domain]),
        "upstream_revision": UPSTREAM, "max_length": 64, "alpha_aug": .8,
        "files": {p.relative_to(path).as_posix(): file_hash(p) for p in path.rglob("*") if p.is_file()}}
    rewrite_manifest(path, manifest)
    return path


def rewrite_manifest(path, manifest):
    manifest.pop("fingerprint", None)
    manifest["fingerprint"] = digest(manifest)
    write_json(path / "manifest.json", manifest)


def test_features_exclude_identifiers_and_protect_structure():
    record = {"record_id": "id-a", "title": " Camera [SEP] COL VAL 42 "}
    text = serialize_record(record, "product")
    assert "id-a" not in text and "[SEP]" not in text
    assert text.count("COL ") == 5 and text.count("VAL ") == 5
    for forbidden in ("cluster_id", "label", "split", "source_key"):
        with pytest.raises(ValueError, match="features"):
            serialize_record(record | {forbidden: "secret"}, "product")
    with pytest.raises(ValueError, match="strings"):
        serialize_record({"price": 42}, "product")


@pytest.mark.parametrize("pairs", [[("a", "a")], [("a", "missing")], [("a", "b"), ("b", "a")],
                                  [("a", 2)], [("a",)], ["ab"]])
def test_invalid_pairs_fail_before_tokenization(pairs):
    with pytest.raises(ValueError):
        pair_texts([{"record_id": "a"}, {"record_id": "b"}], pairs, "company")


def test_manifest_tampering_and_unregistered_files_are_rejected(tmp_path):
    directory = fake_bundle(tmp_path / "bundle")
    assert bundle_manifest(directory)["domain"] == "company"
    (directory / "config.json").write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch"):
        bundle_manifest(directory)
    directory = fake_bundle(tmp_path / "bundle2")
    (directory / "extra.py").write_text("not allowed", encoding="utf-8")
    with pytest.raises(ValueError, match="Unregistered"):
        bundle_manifest(directory)


@pytest.mark.parametrize("change", [{"max_length": 513}, {"alpha_aug": -1}, {"fields": ["cluster_id"]},
    {"files": []}, {"files": {"../outside": "0" * 64}}, {"upstream_revision": "unknown"}])
def test_malformed_manifest_is_rejected_even_with_valid_fingerprint(tmp_path, change):
    directory = fake_bundle(tmp_path / "bundle")
    manifest = json.loads((directory / "manifest.json").read_text())
    rewrite_manifest(directory, manifest | change)
    with pytest.raises(ValueError):
        bundle_manifest(directory)


def test_same_validation_policies_can_favor_different_tradeoffs():
    labels = [{"left_id": "a", "right_id": key, "label": label, "split": "validation"}
              for key, label in (("b", 1), ("c", 1), ("d", 0))]
    policies = select_policies(labels, {("a", "b"): .9, ("a", "c"): .7, ("a", "d"): .8})
    assert policies["f1"]["threshold"] == .7
    assert policies["cost_10_1"]["threshold"] == .9
    with pytest.raises(ValueError, match="validation"):
        select_policies([{**r, "split": "test"} for r in labels], {("a", "b"): .9, ("a", "c"): .7, ("a", "d"): .8})


def test_training_rejects_test_and_unknown_labels():
    with pytest.raises(ValueError, match="partitions"):
        development_examples([], [{"split": "test"}], "product", "train")
    with pytest.raises(ValueError, match="binary"):
        development_examples([], [{"split": "train", "label": True}], "product", "train")
    with pytest.raises(ValueError):
        TrainingConfig(batch_size=0).validate()


def test_upstream_deletion_preserves_marker_and_label_alignment():
    augmenter = Augmenter()
    tokens = ["COL", "title", "VAL", "synthetic", "camera", "[SEP]", "COL", "title", "VAL", "camera"]
    labels = ["HD", "O", "HD", "O", "O", "<SEP>", "HD", "O", "HD", "O"]
    rng = random.getstate()
    try:
        for seed in range(20):
            random.seed(seed)
            changed, aligned = augmenter.augment(tokens, labels, op="del")
            assert len(changed) == len(aligned)
            assert [(t, l) for t, l in zip(changed, aligned) if l != "O"] == [
                (t, l) for t, l in zip(tokens, labels) if l != "O"]
    finally:
        random.setstate(rng)


def test_review_ditto_refuses_too_few_independent_labels_before_model_load(tmp_path, monkeypatch):
    from test_learning import fixture_snapshot

    from entitybridge import ditto_learning, learning
    monkeypatch.setattr(learning, "snapshot_review_labels", lambda _: fixture_snapshot(2))
    directory = tmp_path / "dataset"
    learning.export_review_dataset(None, directory)
    with pytest.raises(ValueError, match="Insufficient independent"):
        ditto_learning.train_review_ditto(directory, tmp_path / "candidate", base_model=tmp_path / "missing")
    assert not (tmp_path / "candidate").exists()
