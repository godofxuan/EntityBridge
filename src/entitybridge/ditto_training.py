"""Validation-only Ditto fine tuning; test data is deliberately not an input."""
from __future__ import annotations

import json
import math
import random
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path

from .benchmark_metrics import select_cost_threshold
from .ditto import FIELDS, FORMAT, UPSTREAM, DittoMatcher, file_hash, pair_texts
from .store import digest


@dataclass(frozen=True)
class TrainingConfig:
    seed: int = 20260913
    epochs: int = 20
    patience: int = 4
    max_length: int = 256
    batch_size: int = 8
    accumulation: int = 8
    learning_rate: float = 5e-5
    weight_decay: float = .01
    warmup_fraction: float = .06
    alpha_aug: float = .8
    gradient_checkpointing: bool = True
    bf16: bool = True

    def validate(self):
        for name, low, high in (("seed", 0, 2**32 - 1), ("epochs", 1, 200), ("patience", 1, 200),
                               ("max_length", 16, 512), ("batch_size", 1, 256), ("accumulation", 1, 256)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid training {name}")
        for name in ("learning_rate", "weight_decay", "warmup_fraction", "alpha_aug"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid training {name}")
        if self.learning_rate <= 0 or self.alpha_aug <= 0 or self.warmup_fraction >= 1:
            raise ValueError("Learning rate/alpha must be positive and warmup below one")
        if type(self.gradient_checkpointing) is not bool or type(self.bf16) is not bool:
            raise ValueError("Training precision/checkpointing flags must be boolean")


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                                   allow_nan=False) + "\n", encoding="utf-8")


def select_policies(labels, scores, *, exact=False):
    """Both objectives use validation only, with conservative threshold ties."""
    selection = select_cost_threshold(labels, scores.keys(), scores, split="validation",
        false_positive_cost=10, false_negative_cost=1, thresholds=[1] if exact else None, return_curve=True)
    if selection["status"] != "selected":
        raise ValueError("Both validation classes are required")
    best = max(selection["curve"], key=lambda row: row["f1"] or 0)
    return {"f1": {"objective": "maximum_validation_f1", **best},
            "cost_10_1": {"objective": "minimum_validation_10FP_plus_FN", **selection["selected"]}}


def development_examples(records, labels, domain, split):
    labels = list(labels)
    if split not in {"train", "validation"} or any(row.get("split") != split for row in labels):
        raise ValueError("Training accepts only explicit train and validation partitions")
    if any(type(row.get("label")) is not int or row["label"] not in (0, 1) for row in labels):
        raise ValueError("Training requires explicit binary labels")
    if {row["label"] for row in labels} != {0, 1}:
        raise ValueError("Both classes are required in each development partition")
    pairs = [(row["left_id"], row["right_id"]) for row in labels]
    return pair_texts(records, pairs, domain), [row["label"] for row in labels]


def fit_ditto(train_records, train_labels, validation_records, validation_labels, *, domain,
              base_model, output, config=None, device="cpu", provenance=None):
    """Save the best validation-F1 checkpoint. No test-driven epoch selection."""
    config = config or TrainingConfig()
    config.validate()
    train_records, train_labels = list(train_records), list(train_labels)
    validation_records, validation_labels = list(validation_records), list(validation_labels)
    texts, targets = development_examples(train_records, train_labels, domain, "train")
    development_examples(validation_records, validation_labels, domain, "validation")
    if {r["record_id"] for r in train_records} & {r["record_id"] for r in validation_records}:
        raise ValueError("Train and validation records must be disjoint")
    if domain not in FIELDS:
        raise ValueError("Unknown Ditto domain")
    base_model, output = Path(base_model), Path(output)
    if output.exists():
        raise ValueError("Ditto training requires a fresh output directory")
    required = ("config.json", "model.safetensors", "tokenizer.json", "vocab.json", "merges.txt", "tokenizer_config.json")
    base_hashes = {name: file_hash(base_model / name) for name in required}
    output.mkdir(parents=True, exist_ok=False)
    plan = {"config": asdict(config), "domain": domain, "device": device,
        "base_files": base_hashes, "upstream_revision": UPSTREAM, "provenance": provenance or {},
        "selection": "best validation F1; earliest epoch and highest threshold on ties",
        "test_input": False, "augmentation": "upstream del + pair flip + MixDA",
        "training_pairs": len(train_labels), "validation_pairs": len(validation_labels),
        "development_sha256": digest([train_records, train_labels, validation_records, validation_labels])}
    write_json(output / "training_plan.json", plan)

    import numpy as np
    import torch
    from safetensors.torch import load_file, save_file
    from transformers import RobertaModel, RobertaTokenizerFast, get_linear_schedule_with_warmup

    from .vendor.ditto_augment import Augmenter
    from .vendor.ditto_model import DittoModel

    random.seed(config.seed)
    np.random.seed(config.seed)
    torch.manual_seed(config.seed)
    torch.set_num_threads(min(8, torch.get_num_threads()))
    if device.startswith("cuda"):
        if not torch.cuda.is_available():
            raise ValueError("CUDA requested but unavailable")
        torch.cuda.manual_seed_all(config.seed)
        torch.cuda.reset_peak_memory_stats()
    mixed = config.bf16 and device.startswith("cuda")
    if mixed and not torch.cuda.is_bf16_supported():
        raise ValueError("Requested bf16 is unsupported; declare another training configuration")
    tokenizer = RobertaTokenizerFast.from_pretrained(base_model, local_files_only=True)
    encoder = RobertaModel.from_pretrained(base_model, local_files_only=True, use_safetensors=True,
                                           add_pooling_layer=False)
    if config.gradient_checkpointing:
        encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model = DittoModel(encoder, config.alpha_aug).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay)
    effective_batch = config.batch_size * config.accumulation
    updates_per_epoch = math.ceil(len(texts) / effective_batch)
    total_updates = updates_per_epoch * config.epochs
    scheduler = get_linear_schedule_with_warmup(optimizer, int(total_updates * config.warmup_fraction), total_updates)
    augment = Augmenter()
    live = {"domain": domain, "max_length": config.max_length, "fingerprint": "unfrozen-development-only"}
    matcher = DittoMatcher(model, tokenizer, live, device=device, batch_size=config.batch_size)
    validation_pairs = [(r["left_id"], r["right_id"]) for r in validation_labels]
    best_f1, best_epoch, history = -1.0, 0, []
    started = time.perf_counter()
    last_log = started

    def encode(pairs):
        encoded = tokenizer([p[0] for p in pairs], [p[1] for p in pairs], padding=True,
            truncation=True, max_length=config.max_length, return_tensors="pt")
        return {k: v.to(device) for k, v in encoded.items() if k in {"input_ids", "attention_mask"}}

    for epoch in range(1, config.epochs + 1):
        model.train()
        order = list(range(len(texts)))
        random.shuffle(order)
        loss_sum = 0.0
        for group_start in range(0, len(order), effective_batch):
            group = order[group_start:group_start + effective_batch]
            optimizer.zero_grad(set_to_none=True)
            for offset in range(0, len(group), config.batch_size):
                indices = group[offset:offset + config.batch_size]
                original = [texts[i] for i in indices]
                augmented = [augment.augment_sent(a + " [SEP] " + b, op="del").split(" [SEP] ")
                             for a, b in original]
                amp = torch.autocast("cuda", dtype=torch.bfloat16) if mixed else nullcontext()
                with amp:
                    logits = model(**encode(original), augmented=encode(augmented))
                    loss = torch.nn.functional.cross_entropy(logits, torch.tensor(
                        [targets[i] for i in indices], dtype=torch.long, device=device))
                if not torch.isfinite(loss):
                    raise ValueError("Nonfinite training loss")
                # Correctly weight the final partial accumulation group.
                (loss * len(indices) / len(group)).backward()
                loss_sum += float(loss.detach()) * len(indices)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True)
            optimizer.step()
            scheduler.step()
            if time.perf_counter() - last_log >= 30:
                print(json.dumps({"stage": "train", "epoch": epoch, "pairs": min(group_start + len(group), len(order)),
                                  "seconds": round(time.perf_counter() - started, 2)}), flush=True)
                last_log = time.perf_counter()
        scores = matcher.score_pairs(validation_records, validation_pairs)
        policies = select_policies(validation_labels, scores)
        f1 = policies["f1"]["f1"]
        improved = f1 > best_f1
        if improved:
            best_f1, best_epoch = f1, epoch
            save_file({k: v.detach().cpu().contiguous() for k, v in model.state_dict().items()},
                      str(output / "best.safetensors"))
        row = {"epoch": epoch, "loss": loss_sum / len(texts), "validation": policies,
               "selected": improved, "elapsed_seconds": time.perf_counter() - started}
        history.append(row)
        write_json(output / "history.json", history)
        print(json.dumps({"stage": "validation", **row}), flush=True)
        if epoch - best_epoch >= config.patience:
            break

    model.load_state_dict(load_file(str(output / "best.safetensors")), strict=True)
    scores = matcher.score_pairs(validation_records, validation_pairs)
    policies = select_policies(validation_labels, scores)
    bundle = output / "model"
    bundle.mkdir()
    # Move only our freshly created checkpoint, after verifying its final model.
    (output / "best.safetensors").replace(bundle / "model.safetensors")
    model.bert.config._name_or_path = ""
    model.bert.config.save_pretrained(bundle)
    tokenizer.save_pretrained(bundle / "tokenizer")
    manifest = {"format": FORMAT, "domain": domain, "fields": list(FIELDS[domain]),
        "max_length": config.max_length, "alpha_aug": config.alpha_aug,
        "upstream_revision": UPSTREAM, "training_plan_sha256": file_hash(output / "training_plan.json"),
        "best_epoch": best_epoch, "validation_policies": policies,
        "calibrated": False, "automatic_deployment": False,
        "files": {p.relative_to(bundle).as_posix(): file_hash(p) for p in sorted(bundle.rglob("*")) if p.is_file()}}
    manifest["fingerprint"] = digest(manifest)
    write_json(bundle / "manifest.json", manifest)
    result = {"model_fingerprint": manifest["fingerprint"], "best_epoch": best_epoch,
        "epochs_completed": len(history), "validation_policies": policies,
        "elapsed_seconds": time.perf_counter() - started,
        "peak_gpu_allocated_bytes": torch.cuda.max_memory_allocated() if device.startswith("cuda") else None,
        "training_precision": "bf16 autocast" if mixed else "float32", "inference_precision": "float32"}
    write_json(output / "training_result.json", result)
    del matcher, model, encoder, optimizer, scheduler
    if device.startswith("cuda"):
        torch.cuda.empty_cache()
    return result
