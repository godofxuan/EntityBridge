"""Optional mature Ditto/RoBERTa pair matcher, with a frozen local model boundary.

Torch/Transformers are imported only when neural inference is requested. Model
weights are safetensors; no pickle, remote custom code or implicit download.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath

from .normalization import MATCHER_COLUMNS
from .store import digest

FORMAT = "entitybridge-ditto-roberta-v1"
UPSTREAM = "52985564a93fb11308439516d3e17a033d43ec8f"
FIELDS = {"product": ("title", "brand", "description", "price", "priceCurrency"),
          "company": ("name", "address", "city", "postcode", "country")}


def serialize_record(record, domain):
    if domain not in FIELDS:
        raise ValueError("Ditto requires an explicit supported domain")
    allowed = set(FIELDS[domain]) | ({"record_id"} if domain == "product" else set(MATCHER_COLUMNS))
    if set(record) - allowed:
        raise ValueError("Evaluator identifiers, labels and provenance are not Ditto features")
    parts = []
    for field in FIELDS[domain]:
        value = record.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError("Ditto descriptive fields must be strings or null")
        text = " ".join((value or "").split())
        # The upstream augmenter uses a literal separator. Values cannot create
        # extra segments or masquerade as its structural markers.
        for marker in ("[SEP]", "[CLS]", "COL", "VAL"):
            text = text.replace(marker, marker.lower())
        parts.extend(("COL", field, "VAL", text))
    return " ".join(parts)


def pair_texts(records, pairs, domain):
    lookup = {}
    for record in records:
        key = record.get("record_id")
        if not isinstance(key, str) or not key or key in lookup:
            raise ValueError("Ditto records require distinct nonempty record IDs")
        lookup[key] = serialize_record(record, domain)
    result, seen = [], set()
    for endpoints in pairs:
        if (not isinstance(endpoints, (tuple, list)) or len(endpoints) != 2
                or any(not isinstance(key, str) or not key for key in endpoints)):
            raise ValueError("Ditto pairs require two nonempty string IDs")
        left, right = endpoints
        pair = tuple(sorted((left, right)))
        if left == right or pair in seen or left not in lookup or right not in lookup:
            raise ValueError("Invalid, duplicate or missing Ditto pair endpoints")
        seen.add(pair)
        result.append((lookup[pair[0]], lookup[pair[1]]))
    return result


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bundle_manifest(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("domain") not in FIELDS:
        raise ValueError("Not a supported frozen Ditto model")
    expected = dict(manifest)
    fingerprint = expected.pop("fingerprint", None)
    if digest(expected) != fingerprint:
        raise ValueError("Ditto model manifest fingerprint mismatch")
    names = manifest.get("files", {})
    if (not isinstance(names, dict)
            or not {"model.safetensors", "config.json", "tokenizer/tokenizer.json"} <= set(names)):
        raise ValueError("Incomplete Ditto model bundle")
    if (manifest.get("fields") != list(FIELDS[manifest["domain"]])
            or manifest.get("upstream_revision") != UPSTREAM):
        raise ValueError("Ditto feature schema or upstream revision mismatch")
    alpha = manifest.get("alpha_aug")
    if type(alpha) not in (float, int) or not math.isfinite(alpha) or alpha <= 0:
        raise ValueError("Invalid Ditto augmentation alpha")
    for name, checksum in names.items():
        if not isinstance(name, str) or not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError("Invalid Ditto bundle filename or SHA256")
        path = PurePosixPath(name)
        if path.as_posix() != name or path.is_absolute() or ".." in path.parts or ":" in name or "\\" in name:
            raise ValueError("Unsafe Ditto bundle path")
        target = directory / name
        if target.is_symlink() or not target.resolve().is_relative_to(directory.resolve()) or file_hash(target) != checksum:
            raise ValueError("Ditto model file mismatch")
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
    if actual != set(names) | {"manifest.json"}:
        raise ValueError("Unregistered files in frozen Ditto bundle")
    if type(manifest.get("max_length")) is not int or not 16 <= manifest["max_length"] <= 512:
        raise ValueError("Invalid frozen tokenizer length")
    return manifest


class DittoMatcher:
    def __init__(self, model, tokenizer, manifest, *, device="cpu", batch_size=16):
        if type(batch_size) is not int or not 1 <= batch_size <= 256:
            raise ValueError("Ditto batch_size must be between 1 and 256")
        self.model, self.tokenizer, self.manifest = model, tokenizer, manifest
        self.device, self.batch_size = device, batch_size
        self.model.to(device).eval()

    @property
    def fingerprint(self):
        return self.manifest["fingerprint"]

    @classmethod
    def load(cls, directory, *, device="cpu", batch_size=16):
        manifest = bundle_manifest(directory)
        from safetensors.torch import load_file
        from transformers import RobertaConfig, RobertaModel, RobertaTokenizerFast

        from .vendor.ditto_model import DittoModel
        directory = Path(directory)
        config = RobertaConfig.from_pretrained(directory, local_files_only=True)
        model = DittoModel(RobertaModel(config, add_pooling_layer=False), alpha_aug=manifest["alpha_aug"])
        model.load_state_dict(load_file(str(directory / "model.safetensors")), strict=True)
        tokenizer = RobertaTokenizerFast.from_pretrained(directory / "tokenizer", local_files_only=True)
        if bundle_manifest(directory)["fingerprint"] != manifest["fingerprint"]:
            raise ValueError("Ditto bundle changed while loading")
        return cls(model, tokenizer, manifest, device=device, batch_size=batch_size)

    def score_pairs(self, records, pairs, *, domain=None):
        import torch
        domain = domain or self.manifest["domain"]
        if domain != self.manifest["domain"]:
            raise ValueError("Ditto model domain does not match the requested feature view")
        pairs = list(pairs)
        examples = pair_texts(records, pairs, domain)
        pairs = [tuple(sorted(pair)) for pair in pairs]
        values = []
        self.model.eval()
        with torch.inference_mode():
            for start in range(0, len(examples), self.batch_size):
                batch = examples[start:start + self.batch_size]
                encoded = self.tokenizer([x[0] for x in batch], [x[1] for x in batch], padding=True,
                    truncation=True, max_length=self.manifest["max_length"], return_tensors="pt")
                encoded = {k: v.to(self.device) for k, v in encoded.items() if k in {"input_ids", "attention_mask"}}
                values.extend(self.model(**encoded).float().softmax(dim=1)[:, 1].cpu().tolist())
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
            raise ValueError("Ditto produced invalid probabilities")
        return dict(zip(pairs, values, strict=True))

    def score(self, records, candidates):
        if self.manifest["domain"] != "company":
            raise ValueError("Company matching cannot deploy a product-domain Ditto model")
        pairs = [(row["left"], row["right"]) for row in candidates]
        scores = self.score_pairs(records, pairs)
        versions = {row["record_id"]: row["record_version_id"] for row in records}
        return [{"left": left, "right": right, "score": scores[tuple(sorted((left, right)))],
                 "left_version": versions[left], "right_version": versions[right],
                 "candidate_rules": candidate.get("rules", []),
                 "evidence": {"method": "ditto_roberta", "model_fingerprint": self.fingerprint}}
                for candidate, (left, right) in zip(candidates, pairs, strict=True)]
