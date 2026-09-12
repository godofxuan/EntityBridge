"""Real offline neural training/inference; requires the optional neural extra."""
import json

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("transformers")

from fastapi.testclient import TestClient
from tokenizers import ByteLevelBPETokenizer
from transformers import RobertaConfig, RobertaModel, RobertaTokenizerFast

from entitybridge.api import create_app
from entitybridge.ditto import DittoMatcher, bundle_manifest
from entitybridge.ditto_training import TrainingConfig, fit_ditto
from entitybridge.jobs import JobQueue
from entitybridge.store import Store
from entitybridge.worker import execute_matching_job


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    root = tmp_path_factory.mktemp("offline-ditto")
    base = root / "base"
    base.mkdir()
    tokens = ByteLevelBPETokenizer()
    tokens.train_from_iterator(["COL name VAL SYNTHETIC ALPHA BETA GAMMA COMPANY ROAD CITY GB 123"],
        vocab_size=300, special_tokens=["<s>", "<pad>", "</s>", "<unk>", "<mask>"])
    tokens.save_model(str(base))
    tokenizer = RobertaTokenizerFast(vocab_file=str(base / "vocab.json"), merges_file=str(base / "merges.txt"))
    tokenizer.save_pretrained(base)
    config = RobertaConfig(vocab_size=len(tokenizer), hidden_size=16, num_hidden_layers=1,
        num_attention_heads=2, intermediate_size=32, max_position_embeddings=130,
        hidden_dropout_prob=0, attention_probs_dropout_prob=0)
    torch.manual_seed(11)
    RobertaModel(config, add_pooling_layer=False).save_pretrained(base, safe_serialization=True)
    def partition(split):
        records = [{"record_id": f"{split}-{i}", "name": name}
                   for i, name in enumerate(("SYNTHETIC ALPHA", "SYNTHETIC ALPHA", "BETA COMPANY", "GAMMA " * 20))]
        labels = [{"left_id": records[a]["record_id"], "right_id": records[b]["record_id"],
                   "label": label, "split": split} for a, b, label in ((0, 1, 1), (0, 2, 0), (2, 3, 0))]
        return records, labels
    training, validation = partition("train"), partition("validation")
    result = fit_ditto(*training, *validation, domain="company", base_model=base, output=root / "fit",
        config=TrainingConfig(epochs=2, patience=2, batch_size=2, accumulation=2, max_length=64,
                              learning_rate=.001, bf16=False), device="cpu")
    return root / "fit/model", validation, result


def test_real_training_reload_padding_and_domain_contract(trained):
    directory, (records, labels), result = trained
    manifest = bundle_manifest(directory)
    assert result["model_fingerprint"] == manifest["fingerprint"]
    assert result["epochs_completed"] == 2 and result["best_epoch"] in (1, 2)
    pairs = [(r["left_id"], r["right_id"]) for r in labels]
    model = DittoMatcher.load(directory, batch_size=1)
    singleton = model.score_pairs(records, pairs)
    model.batch_size = 3
    batched = model.score_pairs(records, pairs)
    assert batched == pytest.approx(singleton, abs=1e-6)
    with pytest.raises(ValueError, match="domain"):
        model.score_pairs(records, pairs, domain="product")
    assert all(0 <= score <= 1 for score in batched.values())


def test_real_company_ditto_durable_job_requires_explicit_review_and_publish(trained, tmp_path):
    directory, _, _ = trained
    store = Store(f"sqlite:///{tmp_path / 'test.db'}", tmp_path / "revisions")
    store.initialize()
    for source in ("left", "right"):
        store.import_records(source, [{"source_key": "1", "name": "SYNTHETIC ALPHA", "country": "GB"}])
    client = TestClient(create_app(store, local_demo=True, model_path=directory))
    assert 'value="ditto"' in client.get("/tasks").text
    assert 'value="splink"' not in client.get("/tasks").text
    submitted = client.post("/jobs", json={"settings": {"method": "ditto", "threshold": 0,
        "review_threshold": 0}, "idempotency_key": "real-neural"})
    assert submitted.status_code == 202, submitted.text
    queue = JobQueue(store)
    result = queue.run_once("real-cpu-worker", lambda lease: execute_matching_job(
        store, queue, lease, model_path=directory))
    assert result["status"] == "succeeded", result
    assert store.current_revision() is None
    revision = result["result_revision"]
    assert client.post(f"/revisions/{revision}/publish", json={"expected_parent": None}).status_code == 200
    payload = store._payload(revision)
    assert len(payload["edges"]) == 1 and payload["edges"][0]["auto_merge"] is False
    assert len(client.get("/entities").json()) == 2
    # Product artifacts are rejected before a company task enters the queue.
    bad = tmp_path / "product"
    import shutil
    shutil.copytree(directory, bad)
    from entitybridge.ditto import FIELDS
    from entitybridge.ditto_training import write_json
    from entitybridge.store import digest
    manifest = json.loads((bad / "manifest.json").read_text())
    manifest.update(domain="product", fields=list(FIELDS["product"]))
    manifest.pop("fingerprint")
    manifest["fingerprint"] = digest(manifest)
    write_json(bad / "manifest.json", manifest)
    other = TestClient(create_app(store, local_demo=True, model_path=bad))
    rejected = other.post("/jobs", json={"settings": {"method": "ditto"}, "idempotency_key": "wrong-domain"})
    assert rejected.status_code == 422, rejected.text


def test_real_review_export_to_company_training_defers_test_features(trained, tmp_path, monkeypatch):
    from test_learning import fixture_snapshot

    from entitybridge import ditto_learning, learning
    base = trained[0].parents[1] / "base"
    directory, output = tmp_path / "dataset", tmp_path / "trained"
    monkeypatch.setattr(learning, "snapshot_review_labels", lambda _: fixture_snapshot(240))
    learning.export_review_dataset(None, directory)
    original = learning.pq.read_table
    reads = []
    def read(path, *args, **kwargs):
        if str(path).endswith("test.parquet") and not kwargs.get("columns"):
            assert (output / "frozen_config.json").exists()
            assert bundle_manifest(output / "model")["domain"] == "company"
            reads.append("test_features")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(learning.pq, "read_table", read)
    report = ditto_learning.train_review_ditto(directory, output, base_model=base, device="cpu",
        config=TrainingConfig(epochs=1, batch_size=8, accumulation=2, max_length=64, bf16=False))
    assert reads == ["test_features"] and report["automatic_deployment"] is False
    assert set(report["policies"]) == {"f1", "cost_10_1"}
