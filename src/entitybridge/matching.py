"""Identifier-missing baselines and the actual Splink probabilistic model."""
import copy
import hashlib
import importlib.metadata
import json
from functools import cached_property
from pathlib import Path

import duckdb
import pandas as pd
from rapidfuzz.fuzz import ratio
from splink import DuckDBAPI, Linker, SettingsCreator, block_on
from splink import comparison_library as cl

from entitybridge.candidates import validate_records


def _evidence(left, right):
    return {field: {"left": left.get(field), "right": right.get(field),
                    "exact": bool(left.get(field)) and left.get(field) == right.get(field)}
            for field in ("name", "address", "postcode", "city", "country")}


def _edge(candidate, records, score, model):
    left, right = records[candidate["left"]], records[candidate["right"]]
    edge = {"left": left["record_id"], "right": right["record_id"],
            "left_version": left.get("record_version_id"), "right_version": right.get("record_version_id"),
            "score": float(score), "model": model, "candidate_rules": candidate["rules"],
            "evidence": _evidence(left, right)}
    if "retrieval_similarity" in candidate:
        edge["evidence"]["candidate_retrieval"] = {
            "name_tfidf_cosine": candidate["retrieval_similarity"], "used_as_match_probability": False}
    return edge


def score_baselines(records, candidates):
    records = {r["record_id"]: r for r in validate_records(records)}
    result = {"exact": [], "fuzzy": []}
    for candidate in candidates:
        left, right = records[candidate["left"]], records[candidate["right"]]
        a, b = left.get("name") or "", right.get("name") or ""
        result["exact"].append(_edge(candidate, records, bool(a) and a == b, "exact-name-v1"))
        result["fuzzy"].append(_edge(candidate, records, ratio(a, b) / 100 if a and b else 0, "rapidfuzz-ratio-v1"))
    return result


def _frame(records):
    frame = pd.DataFrame(records)
    for field in ("name", "address", "postcode", "country", "city"):
        if field not in frame:
            frame[field] = None
        # All-null object columns are inferred as INTEGER by DuckDB; pin the
        # semantic string type so name-only inputs still compile similarities.
        frame[field] = frame[field].replace("", None).astype("string[pyarrow]")
    return frame


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class SplinkMatcher:
    """Frozen Splink settings and name TF lookup; only fit() learns from inputs.

    Scores are uncalibrated model probabilities. They are evidence for a separate
    resolver, not a business assertion that two firms are identical.
    """

    def __init__(self, settings, tf_name, training_metadata):
        self._settings = copy.deepcopy(settings)
        self._tf_name = copy.deepcopy(tf_name)
        self._training_metadata = copy.deepcopy(training_metadata)
        self.last_prediction_count = 0

    @property
    def settings(self):
        return copy.deepcopy(self._settings)

    @property
    def tf_name(self):
        return copy.deepcopy(self._tf_name)

    @property
    def training_metadata(self):
        return copy.deepcopy(self._training_metadata)

    @cached_property
    def fingerprint(self):
        state = {"settings": self.settings, "tf_name": self.tf_name,
                 "training_metadata": self.training_metadata}
        return hashlib.sha256(_canonical_json(state).encode()).hexdigest()

    @classmethod
    def fit(cls, train_records, *, seed=20260912, max_u_pairs=1_000_000):
        train_records = validate_records(train_records)
        if len({r["source"] for r in train_records}) != 2 or len(train_records) < 4:
            raise ValueError("Splink fitting requires at least four records from exactly two sources")
        counts = pd.Series([r["source"] for r in train_records]).value_counts()
        settings = SettingsCreator(
            link_type="link_only", unique_id_column_name="record_id", source_dataset_column_name="source",
            retain_intermediate_calculation_columns=True,
            # Prior is a declared design assumption, not derived from held-out IDs.
            probability_two_random_records_match=1 / max(counts),
            blocking_rules_to_generate_predictions=[block_on("name")],
            comparisons=[cl.JaroWinklerAtThresholds("name", [0.95, 0.85]).configure(term_frequency_adjustments=True),
                         cl.ExactMatch("postcode"),
                         cl.JaroWinklerAtThresholds("address", [0.9, 0.7])])
        linker = Linker(_frame(train_records), settings, db_api=DuckDBAPI())
        linker.training.estimate_u_using_random_sampling(max_pairs=max_u_pairs, seed=seed)
        trained_blocks = []
        for field in ("postcode", "name"):
            values = {}
            for record in train_records:
                if record.get(field):
                    values.setdefault(record[field], set()).add(record["source"])
            if any(len(sources) > 1 for sources in values.values()):
                linker.training.estimate_parameters_using_expectation_maximisation(block_on(field))
                trained_blocks.append(field)
        if not trained_blocks:
            raise ValueError("No cross-source equality block available for unsupervised EM fitting")
        fitted = linker.misc.save_model_to_json()
        tf_name = linker.table_management.compute_tf_table("name").as_record_dict()
        tf_name.sort(key=lambda row: row["name"])
        untrained = [{"comparison": comparison["output_column_name"], "condition": level["sql_condition"],
                      "missing": [p for p in ("m_probability", "u_probability") if p not in level]}
                     for comparison in fitted["comparisons"] for level in comparison["comparison_levels"]
                     if not level.get("is_null_level") and any(p not in level for p in ("m_probability", "u_probability"))]
        return cls(fitted, tf_name, {"records": len(train_records), "seed": seed,
                                    "training_feature_sha256": hashlib.sha256(_canonical_json(sorted(train_records, key=lambda row: row["record_id"])).encode()).hexdigest(),
                                    "max_u_pairs": max_u_pairs, "em_blocks": trained_blocks,
                                    "splink_version": importlib.metadata.version("splink"),
                                    "unobserved_levels_using_splink_defaults": untrained,
                                    "tf_policy": "name-lookup-frozen-from-train", "calibrated": False})

    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "model.json").write_text(_canonical_json(self.settings) + "\n", encoding="utf-8")
        pd.DataFrame(self.tf_name).to_parquet(directory / "tf_name.parquet", index=False)
        metadata = {**self.training_metadata, "fingerprint": self.fingerprint}
        (directory / "manifest.json").write_text(_canonical_json(metadata) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        fingerprint = metadata.pop("fingerprint")
        if metadata["splink_version"] != importlib.metadata.version("splink"):
            raise ValueError("Splink version differs from the frozen model; rebuild explicitly")
        model = cls(json.loads((directory / "model.json").read_text(encoding="utf-8")),
                    pd.read_parquet(directory / "tf_name.parquet").to_dict("records"), metadata)
        if model.fingerprint != fingerprint:
            raise ValueError("Frozen model or TF lookup hash mismatch")
        return model

    def score(self, records, candidates):
        records = validate_records(records)
        candidates = list(candidates)
        if not candidates:
            self.last_prediction_count = 0
            return []
        indexed = {r["record_id"]: r for r in records}
        requested = {tuple(sorted((c["left"], c["right"]))): c for c in candidates}
        for left, right in requested:
            if indexed[left]["source"] == indexed[right]["source"]:
                raise ValueError("Only cross-source candidates are supported")
        with duckdb.connect() as connection:
            settings = copy.deepcopy(self.settings)
            # Public Splink exploded-array blocking: every requested edge has a
            # unique token present at its two endpoints only. This is an equality
            # join over exactly 2*E tokens and also scores pairs outside the old
            # four fixed rules. Internal tokens never enter fitted features/TF.
            memberships = {key: [] for key in indexed}
            for number, (left, right) in enumerate(sorted(requested)):
                memberships[left].append(number)
                memberships[right].append(number)
            frame = _frame(records)
            frame["eb_candidate_tokens"] = [memberships[r["record_id"]] for r in records]
            settings["blocking_rules_to_generate_predictions"] = [{
                "blocking_rule": "l.eb_candidate_tokens = r.eb_candidate_tokens",
                "arrays_to_explode": ["eb_candidate_tokens"]}]
            linker = Linker(frame, settings, db_api=DuckDBAPI(connection))
            linker.table_management.register_term_frequency_lookup(pd.DataFrame(self.tf_name), "name")
            predictions = linker.inference.predict().as_record_dict()
            self.last_prediction_count = len(predictions)
        results = {}
        for prediction in predictions:
            pair = tuple(sorted((prediction["record_id_l"], prediction["record_id_r"])))
            if pair not in requested:
                continue
            edge = _edge(requested[pair], indexed, prediction["match_probability"], self.fingerprint)
            edge["match_weight"] = prediction["match_weight"]
            edge["score_semantics"] = "uncalibrated_splink_probability"
            for field in ("name", "postcode", "address"):
                edge["evidence"][field].update(comparison_level=prediction.get(f"gamma_{field}"),
                                               bayes_factor=prediction.get(f"bf_{field}"))
            edge["evidence"]["name"]["tf_adjustment_factor"] = prediction.get("bf_tf_adj_name")
            results[pair] = edge
        if set(results) != set(requested):
            raise RuntimeError("Splink failed to score the complete requested candidate set")
        return [results[pair] for pair in sorted(results)]
