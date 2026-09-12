"""Copy a strict allowlist of measured evidence, excluding raw data, labels and secrets."""
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs/evaluation/evidence"


def main():
    TARGET.mkdir(parents=True, exist_ok=True)
    copied = {}
    for experiment in ("baseline_10k_v2", "baseline_100k_v2", "release_final_v2", "aliases_name_only_v1",
                       "aliases_partial_address_v1", "aliases_current_address_v1"):
        for name in ("report.json", "frozen_config.json", "input_verification.json"):
            source = ROOT / "artifacts/reports" / experiment / name
            if source.exists():
                target = TARGET / f"{experiment}_{name}"
                shutil.copyfile(source, target)
                copied[target.name] = hashlib.sha256(source.read_bytes()).hexdigest()
    for name in ("incremental_10k_v2.json", "failure_recovery_sqlite.json", "failure_recovery_postgres.json",
                 "matching_compatibility.json", "v4_release_final_integrity.json", "real_workflow_10k.json",
                 "clean_environment.json", "pytest_release.log", "pytest_postgres_store.log", "pytest_repro.log"):
        source = ROOT / "artifacts/reports" / name
        if source.exists():
            shutil.copyfile(source, TARGET / name)
            copied[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    for name in ("real_10000", "real_100000"):
        source = ROOT / "artifacts/datasets/v3" / name / "manifest.json"
        target = TARGET / f"v3_{name}_manifest.json"
        shutil.copyfile(source, target)
        copied[target.name] = hashlib.sha256(source.read_bytes()).hexdigest()
    source = ROOT / "artifacts/datasets/v4_release_final/manifest.json"
    shutil.copyfile(source, TARGET / "release_dataset_manifest.json")
    copied["release_dataset_manifest.json"] = hashlib.sha256(source.read_bytes()).hexdigest()
    for name in ("final_eval_source.zip", "final_eval_source.json"):
        if (TARGET / name).exists():
            copied[name] = hashlib.sha256((TARGET / name).read_bytes()).hexdigest()
    (TARGET / "SHA256.json").write_text(json.dumps(copied, indent=2), encoding="utf-8")
    print(f"Copied {len(copied)} allowlisted evidence files")


if __name__ == "__main__":
    main()
