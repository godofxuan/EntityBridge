# WDC Products: corrected-validation external diagnostic

This experiment is a first-scored external diagnostic of supplied product pairs. It is not the paper's original protocol, a candidate-retrieval test, or evidence of generalization to company records. The official public schema/sample pair was inspected before this run; “first-scored” does not mean that no test example had ever been visible.

## Predeclared configuration

The [official WDC Products release](https://webdatacommons.org/largescaleproductcorpus/wdc-products/) offers pairwise tasks varying product difficulty, training size and unseen products. It describes labels derived from product identifiers, with sampled manual checks, rather than exhaustive human annotation. Its additional validation sets are intended to support unseen-product validation. These facts motivate this external diagnostic, but the downloaded files are checked independently.

The fixed configuration is 80% corner cases, small training and the 100% unseen-product test. Only the following archive members are read:

| Role | Official archive/member |
|---|---|
| Train | `80pair.zip / wdcproducts80cc20rnd000un_train_small.json.gz` |
| Original validation | `val_pair.zip / 80pair_addvalid.zip / wdcproducts80cc20rnd100un_valid_small.json.gz` |
| Test | `80pair.zip / wdcproducts80cc20rnd100un_gs.json.gz` |

Source archive SHA-256 values are pinned in the adapter:

- `80pair.zip`: `b2044939cee5ea6f12148a2f3551508de3cb77660dfc91767c44daaf9d8a9c4a`.
- `val_pair.zip`: `15bb323e9aff4771be1baaeb048c9b6ef7459446ccbadca61cc9cb1272cb4143`.

The unmodified train/original-validation comparison found **110 shared offer IDs and 250 shared product clusters**. Before any full test access, the protocol was explicitly corrected: delete validation pairs if either endpoint belongs to a training product cluster. Keep the original audit, ID-set hashes, removed-label counts and prevalence change. The test is never trimmed; any record or product overlap between train, retained validation and test stops scoring. This correction is disclosed and makes the run unsuitable for direct ranking against the paper's numbers.

| Validation audit | Original | Retained |
|---|---:|---:|
| Pairs | 2,500 | 897 |
| Positive | 500 | 250 |
| Negative | 2,000 | 647 |
| Offers | 1,000 | 500 |
| Product clusters | 500 | 250 |
| Positive fraction | 20.00% | 27.87% |

The correction removes 250 positive and 1,353 negative pairs. Its remaining negative pairs comprise 387 hard negatives and 260 random negatives. The retained validation has four dependency groups; its largest contains 894/897 pairs. No seed, input member or correction rule is selected from test performance.

## Models and boundary

Four fixed methods are declared before test scoring:

| Method | Features / training |
|---|---|
| Exact | Exact normalized nonempty title; accept only score=1 or reject every pair |
| Fuzzy | RapidFuzz ratio on normalized title |
| Title logistic | Existing fixed C=1 logistic pair classifier; title only through the existing eight-column feature view |
| Product logistic | Fixed C=1 logistic on title, brand, description and comparable price features listed below |

Product features are title ratio, title token-set ratio, exact title and both-present flag; brand ratio, exact brand and both-present flag; description token-set ratio and both-present flag; and price `min/max` ratio with a comparable-price flag. Text uses the existing NFKC / uppercase / punctuation normalization. Prices must be finite, strictly positive decimal/scientific-notation strings with the same nonempty normalized currency. Symbol-bearing, comma-formatted, missing and invalid prices are not guessed; different currencies are not converted. Identifiers, cluster IDs, pair IDs, labels and `is_hard_negative` are evaluator-only and never features.

The product model receives only `record_id` plus the five real product attributes. The title baseline uses one honest `wdc-products-offer-pool` source; it does not invent left/right shops. `FrozenPairClassifier.fit(..., allow_within_source=True)` explicitly enables this supplied-pair use case. The default remains cross-source-only, and existing model files retain their format.

Train labels fit the two logistic models. The retained validation independently minimizes `10 × FP + FN` to select each method's threshold, including reject-all; exact is restricted to threshold 1 or reject-all. There is no hyperparameter search or separate probability calibration. The protocol, archive hashes, source-code copies/hashes, fitted models and validation policies are saved before full test access. Test models are scored once; outcomes do not select a winner or trigger tuning.

## First-scored result, 2026-09-12

The single main run completed. All three train / retained-validation / test comparisons have **zero shared offers and zero shared product clusters**. The official test remains intact at 4,500 pairs: 500 positives, 3,000 hard negatives and 1,000 random negatives, involving 1,000 offers and 500 products.

| Method | Frozen threshold | TP | FP | FN | Precision | Recall | F1 | Test cost `10 FP + FN` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Exact | Reject all | 0 | 0 | 500 | Undefined | 0 | 0 | 500 |
| Fuzzy | 0.986667 | 1 | 1 | 499 | 0.5000 | 0.0020 | 0.003984 | 509 |
| Title logistic | 0.709865 | 8 | 0 | 492 | 1.0000 | 0.0160 | 0.031496 | 492 |
| Product logistic | 0.714841 | 15 | 4 | 485 | 0.7895 | 0.0300 | 0.057803 | 525 |

The four models' hard-negative false-positive counts are respectively **0 / 1 / 0 / 4 out of 3,000**. Every method has zero false positives among the 1,000 random negatives. These small false-positive counts accompany very low recall and are not evidence that the task is solved.

| Method | Average precision | ROC AUC | Brier, where scores are probabilities |
|---|---:|---:|---:|
| Exact | 0.11111 | 0.50000 | Not applicable |
| Fuzzy | 0.20449 | 0.73346 | Not applicable |
| Title logistic | 0.35452 | 0.80511 | 0.09059 |
| Product logistic | 0.38027 | 0.82457 | 0.08776 |

Adding legitimate product fields improves these descriptive ranking metrics and recovers seven additional positives at the frozen decision thresholds, but it also introduces four false positives. Under the declared 10:1 cost, the product model's test cost is **525 versus 492** for title logistic. It therefore does not establish a better operating policy. Both learned models miss most positives, and validation-selected thresholds transfer poorly enough that this prototype cannot justify automatic matching in this external domain. No alternative threshold or feature set was chosen from these results.

All 4,500 test pairs belong to one dependency component once shared offers, product clusters and negative links are kept together. Every bootstrap interval is consequently `unavailable: insufficient_independent_groups`; offers or pairs are not split into fictitious independent groups. The validation component concentration, changed positive prevalence and identifier-derived label noise also limit conclusions. This is a useful failure diagnostic, not a paper-comparable performance claim.

## Reproduction and evidence

```powershell
python scripts/run_wdc_benchmark.py --raw-directory artifacts/raw/wdc_products --output artifacts/reports/wdc_unseen_reproduction --test-status exploratory-replayed
```

The first scoring run uses `--test-status first-scored-external-diagnostic` and is retained at `artifacts/reports/wdc_unseen_v1`. Reproductions must use a fresh directory and disclose replay status. Outputs include `plan.json`, `development_audit.json`, archived source, two model directories, `frozen_config.json`, scores and `report.json`. A failed overlap check writes `blocked_audit.json` and refuses scoring.

The runner reports positive/negative counts, hard-negative and random-negative false positives, and dependency-group diagnostics. Bootstrap groups connect all shared labelled endpoints and known product clusters; a dominant connected component produces unavailable intervals rather than fictitious independent resamples. Supplied-pair classification cannot estimate blocking recall or the quality of unlabelled pairs.

Raw data remains under ignored `artifacts/`. An explicit redistribution license has not been established for these downloaded data; this repository does not redistribute it. Source URLs and content hashes remain in the experiment plan. The domain scorer is diagnostic-only and is not integrated into company merging or automatically deployed.

`tests/test_wdc_benchmark.py` verifies evaluator isolation, within-source opt-in and legacy-model loading, conservative price comparison, validation-only correction, code/model/threshold freezing before first test read, unavailable intervals for a single dependency group and refusal to score overlapping test records/products.
