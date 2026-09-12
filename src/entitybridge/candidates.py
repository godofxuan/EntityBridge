"""Fixed blocking plus an explicit, frozen, bounded name-retrieval option."""
import copy
import hashlib
import importlib.metadata
import json
from collections import defaultdict
from functools import cached_property
from itertools import combinations, product
from pathlib import Path

FEATURE_COLUMNS = frozenset({"record_id", "record_version_id", "source", "name",
                             "address", "city", "postcode", "country"})
BLOCKING_VERSION = "fixed-union-v2-missing-country"
RETRIEVAL_VERSION = "char-wb-tfidf-3to5-symmetric-topk-v1"


class CandidateOverflow(ValueError):
    """The declared complete candidate set exceeds the configured bucket budget."""


def validate_records(records):
    records = list(records)
    seen = set()
    for record in records:
        if set(record) - FEATURE_COLUMNS:
            raise ValueError("Matcher input contains columns outside the identifier-missing allowlist")
        if not record.get("record_id") or record["record_id"] in seen:
            raise ValueError("record_id must be non-empty and unique")
        if not record.get("source"):
            raise ValueError("source is required")
        seen.add(record["record_id"])
    return records


def bucket_keys(record):
    """Return stable keys determined by this record alone (usable by incremental)."""
    name = record.get("name") or ""
    postcode = record.get("postcode") or ""
    address = record.get("address") or ""
    keys = set()
    if name:
        keys.add(("name_exact", name))
        if len(name) >= 8:
            keys.add(("name_prefix8", name[:8]))
        if postcode:
            keys.add(("postcode_name_prefix3", postcode, name[:3]))
    if address and postcode:
        keys.add(("address_exact", postcode, address))
    return keys


def generate_candidates(records, *, max_bucket_pairs=250_000, retriever=None):
    records = validate_records(records)
    retrieval_candidates = retriever.generate(records) if retriever is not None else []
    buckets = defaultdict(list)
    for record in records:
        for key in bucket_keys(record):
            buckets[key].append(record)
    pairs = {}
    for key in sorted(buckets):
        bucket = buckets[key]
        source_counts = defaultdict(int)
        for record in bucket:
            source_counts[record["source"]] += 1
        size = sum(a * b for a, b in combinations(source_counts.values(), 2))
        if size > max_bucket_pairs:
            raise CandidateOverflow(f"{key[0]} bucket has {size} cross-source pairs; limit={max_bucket_pairs}")
        by_source = defaultdict(list)
        for record in bucket:
            by_source[record["source"]].append(record)
        for source_a, source_b in combinations(by_source.values(), 2):
            for a, b in product(source_a, source_b):
                if a.get("country") and b.get("country") and a["country"] != b["country"]:
                    continue
                left, right = sorted((a["record_id"], b["record_id"]))
                pair = pairs.setdefault((left, right), {"left": left, "right": right, "rules": set()})
                pair["rules"].add(key[0])
    if retriever is not None:
        for candidate in retrieval_candidates:
            key = (candidate["left"], candidate["right"])
            pair = pairs.setdefault(key, {"left": key[0], "right": key[1], "rules": set()})
            pair["rules"].update(candidate["rules"])
            pair["retrieval_similarity"] = candidate["retrieval_similarity"]
    return [{**pairs[key], "rules": sorted(pairs[key]["rules"])} for key in sorted(pairs)]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class FrozenNameRetriever:
    """Train-only TF-IDF, symmetric top-k sparse cosine retrieval.

    This intentionally changes candidate semantics: top-k depends on the complete
    record set. Every run therefore requires full candidate generation. A fixed
    row limit and cross-source interaction limit fail explicitly before matrix
    work; 64-row sparse products bound intermediate matrix size. No N-by-N dense
    similarity matrix, answer IDs, or test-fitted vocabulary/IDF is used.
    """

    requires_full_recompute = True

    def __init__(self, state):
        self._state = copy.deepcopy(state)
        self.last_stats = {}

    @property
    def metadata(self):
        return copy.deepcopy({key: value for key, value in self._state.items() if key not in ("vocabulary", "idf")})

    @cached_property
    def fingerprint(self):
        return hashlib.sha256(_json(self._state).encode()).hexdigest()

    @classmethod
    def fit(cls, train_records, *, top_k=10, min_similarity=0.25, max_records=20_000,
            max_cross_source_pairs=100_000_000, max_features=100_000):
        import numpy as np
        from sklearn.feature_extraction.text import TfidfVectorizer
        records = validate_records(train_records)
        if not 1 <= top_k <= 50 or not 0 <= min_similarity <= 1:
            raise ValueError("top_k must be in [1, 50] and min_similarity in [0, 1]")
        if not 1 <= max_records <= 100_000 or not 1 <= max_cross_source_pairs <= 1_000_000_000:
            raise ValueError("Retrieval resource budgets exceed the supported bounds")
        if not 1 <= max_features <= 100_000:
            raise ValueError("max_features must be in [1, 100000]")
        if len(records) > max_records:
            raise CandidateOverflow("TF-IDF training exceeds record budget")
        names = [r.get("name") or "" for r in sorted(records, key=lambda r: r["record_id"])]
        if any(len(name) > 1024 for name in names):
            raise CandidateOverflow("TF-IDF name exceeds 1024 character budget")
        if sum(map(len, names)) > 2_000_000:
            raise CandidateOverflow("TF-IDF training exceeds total name character budget")
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), lowercase=False,
                                     max_features=max_features, dtype=np.float64)
        vectorizer.fit(names)
        return cls({"version": RETRIEVAL_VERSION, "top_k": top_k, "min_similarity": min_similarity,
                    "max_records": max_records, "max_cross_source_pairs": max_cross_source_pairs,
                    "max_features": max_features, "block_rows": 64,
                    "max_total_name_characters": 2_000_000,
                    "runtime_versions": {package: importlib.metadata.version(package)
                                         for package in ("scikit-learn", "numpy", "scipy")},
                    "training_records": len(records), "training_feature_sha256": hashlib.sha256(
                        _json(sorted(records, key=lambda row: row["record_id"])).encode()).hexdigest(),
                    "vocabulary": {key: int(value) for key, value in vectorizer.vocabulary_.items()},
                    "idf": vectorizer.idf_.tolist()})

    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "retriever.json").write_text(_json(self._state) + "\n", encoding="utf-8")
        (directory / "manifest.json").write_text(_json({**self.metadata, "fingerprint": self.fingerprint}) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        model = cls(json.loads((directory / "retriever.json").read_text(encoding="utf-8")))
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if model._state["version"] != RETRIEVAL_VERSION or manifest != {**model.metadata, "fingerprint": model.fingerprint}:
            raise ValueError("Frozen candidate model hash or version mismatch")
        if model._state["runtime_versions"] != {package: importlib.metadata.version(package)
                                               for package in ("scikit-learn", "numpy", "scipy")}:
            raise ValueError("Candidate model runtime versions differ; rebuild explicitly")
        return model

    def generate(self, records):
        import numpy as np
        from sklearn.feature_extraction.text import TfidfVectorizer
        records = sorted(validate_records(records), key=lambda r: r["record_id"])
        config = self._state
        if len(records) > config["max_records"]:
            raise CandidateOverflow("TF-IDF retrieval exceeds record budget")
        if any(len(r.get("name") or "") > 1024 for r in records):
            raise CandidateOverflow("TF-IDF name exceeds 1024 character budget")
        if sum(len(r.get("name") or "") for r in records) > config["max_total_name_characters"]:
            raise CandidateOverflow("TF-IDF retrieval exceeds total name character budget")
        by_source = defaultdict(list)
        for i, record in enumerate(records):
            by_source[record["source"]].append(i)
        interactions = sum(len(a) * len(b) for a, b in combinations(by_source.values(), 2))
        if interactions > config["max_cross_source_pairs"]:
            raise CandidateOverflow("TF-IDF retrieval exceeds cross-source interaction budget")
        self.last_stats = {"records": len(records), "cross_source_interactions_upper_bound": interactions,
                           "block_rows": config["block_rows"], "full_recompute_required": True}
        if len(by_source) < 2:
            return []
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), lowercase=False,
                                     vocabulary=config["vocabulary"], dtype=np.float64)
        vectorizer.idf_ = np.asarray(config["idf"], dtype=np.float64)
        matrix = vectorizer.transform([r.get("name") or "" for r in records])
        pairs = {}
        for indices_a, indices_b in combinations(by_source.values(), 2):
            for queries, targets in ((indices_a, indices_b), (indices_b, indices_a)):
                target_matrix = matrix[targets].T.tocsc()
                for offset in range(0, len(queries), config["block_rows"]):
                    block = queries[offset:offset + config["block_rows"]]
                    similarities = (matrix[block] @ target_matrix).tocsr()
                    for position, query_index in enumerate(block):
                        start, end = similarities.indptr[position:position + 2]
                        a = records[query_index]
                        eligible = []
                        for target_position, similarity in zip(similarities.indices[start:end], similarities.data[start:end]):
                            b = records[targets[target_position]]
                            if similarity <= 0 or similarity < config["min_similarity"]:
                                continue
                            if a.get("country") and b.get("country") and a["country"] != b["country"]:
                                continue
                            eligible.append((float(similarity), b["record_id"]))
                        eligible.sort(key=lambda item: (-item[0], item[1]))
                        for similarity, other in eligible[:config["top_k"]]:
                            left, right = sorted((a["record_id"], other))
                            pair = pairs.setdefault((left, right), {"left": left, "right": right,
                                "rules": [RETRIEVAL_VERSION], "retrieval_similarity": similarity})
                            pair["retrieval_similarity"] = max(pair["retrieval_similarity"], similarity)
        self.last_stats["retrieval_pairs"] = len(pairs)
        return [pairs[key] for key in sorted(pairs)]
