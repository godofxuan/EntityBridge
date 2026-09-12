"""Connect published human review exports to a candidate company Ditto model."""
import json
from pathlib import Path

from .benchmark_metrics import evaluate_labeled_pairs, group_bootstrap_intervals
from .ditto import DittoMatcher, file_hash
from .ditto_training import fit_ditto, write_json
from .learning import _read_split, verify_learning_dataset


def train_review_ditto(directory, output, *, base_model, device="cpu", config=None):
    directory, output = Path(directory), Path(output)
    # Verification inspects all split identifiers/labels for consistency, but
    # test descriptive features and predictions are deferred until model freeze.
    verified = verify_learning_dataset(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for split in ("train", "validation", "test"):
        stats = manifest["statistics"][split]
        if stats["pairs"] < 10 or min(stats["positive"], stats["negative"]) < 2 or stats["groups"] < 2:
            raise ValueError(f"Insufficient independent {split} review labels: need 10 pairs, 2 per class and 2 groups")
    train_records, train_labels = _read_split(directory, "train")
    validation_records, validation_labels = _read_split(directory, "validation")
    training = fit_ditto(train_records, train_labels, validation_records, validation_labels,
        domain="company", base_model=base_model, output=output, config=config, device=device,
        provenance={"review_dataset_manifest_sha256": verified["manifest_sha256"],
                    "source_revision": manifest["source_revision"], "source_event_cutoff": manifest["source_event_cutoff"]})
    frozen = {"training": training, "review_dataset_manifest_sha256": verified["manifest_sha256"],
              "automatic_deployment": False, "test_features_read": False}
    write_json(output / "frozen_config.json", frozen)
    model = DittoMatcher.load(output / "model", device=device)
    records, labels = _read_split(directory, "test")
    scores = model.score_pairs(records, [(r["left_id"], r["right_id"]) for r in labels])
    report = {"kind": "review-trained-company-ditto", "automatic_deployment": False,
        "frozen_config_sha256": file_hash(output / "frozen_config.json"), "statistics": manifest["statistics"],
        "policies": {name: {"metrics": evaluate_labeled_pairs(labels, scores.keys(), scores,
            threshold=selection["threshold"], score_kind="probability"),
            "intervals": group_bootstrap_intervals(labels, scores.keys(), scores, threshold=selection["threshold"])}
            for name, selection in training["validation_policies"].items()},
        "limitations": ["Review selection bias remains; probabilities are uncalibrated",
            "Historical published labels can later expire or be revoked; this artifact never updates itself",
            "Test metadata checked before training, but test features scored only after freeze",
            "Deployment requires an explicitly configured company model and manual review of merge decisions"]}
    write_json(output / "report.json", report)
    return report
