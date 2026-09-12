# ADR 010 — Explicit review labels, independent learning splits and active sampling

Status: implemented; models remain candidates and are never deployed automatically.

## Decision

Human review is now a reproducible source of labelled training evidence. It does not turn the resolver's scores, accepted automatic edges, cluster membership, abstentions or missing pairs into labels. `snapshot_review_labels(store)` reads the current published artifact, record versions, decisions and their latest events. Only explicit `accept` / `reject` judgments whose latest event is `CREATE` and whose endpoint versions are still current are exported. `REVOKE`, `EXPIRE`, missing endpoints and abstentions are excluded with counts. Same-source judgments are preserved in the operational audit but excluded from this cross-source review classifier's training export with a stated reason.

A source import or review event newer than the published revision makes the learning basis stale; export refuses until a current revision is published. Artifact SHA-256, input hash, revision and event cutoff must agree. Before/after read checks reject concurrent basis changes. Agreeing judgments for one unordered pair share one label and retain every judgment's decision ID, reviewer, reason, base revision, record versions and decision/event policy provenance. Conflicting explicit judgments are rejected. These read-only operations do not publish identities or alter Store events.

An export is an immutable **as-of snapshot**, not a guarantee that its judgments will remain valid forever. Later withdrawals do not rewrite an already frozen dataset or model. A future deployment process must compare the recorded source revision/event cutoff with the current basis and handle withdrawn evidence. This implementation has no deployment operation.

## Dataset and model boundary

`export_review_dataset(store, output, seed=20260912)` requires a fresh output directory. It writes `source.json`, four `matcher/*.parquet` files, four `labels/*.json` files, and `manifest.json`. The manifest binds file hashes, published artifact/revision/cutoff, split seed/protocol and achieved counts. `verify_learning_dataset()` checks hashes, source binding, unordered pair uniqueness, positive-closure consistency, record versions, group membership and exact split coverage. Its preflight reads feature schemas and only the `record_id`, `record_version_id`, `source` columns, including for test.

The matcher files contain exactly the existing eight string columns: `record_id`, `record_version_id`, `source`, `name`, `address`, `city`, `postcode`, `country`. The first three identify endpoints; the pair classifier's numerical features use only the permitted descriptive fields. Reviewers, reasons, labels, group IDs, company registration identifiers and evaluator truth never enter its feature vectors.

Splitting first verifies positive-label equivalence closure, including rejecting a negative edge inside a positive component. All **positive and negative labelled edges** then define shared-endpoint connected components. Entire components are assigned by a deterministic hash to train / validation / calibration / test with target fractions 50 / 20 / 15 / 15. There is no edge dropping, forced component splitting or seed search to balance results. Large components can produce very uneven achieved sizes or make training impossible. The export remains useful for audit; training rejects insufficient independent data.

`train_candidate_model(directory, output, calibrate=False)` requires at least ten pairs, two labels per class and two dependency groups in every required split. `FrozenPairClassifier` fits the fixed C=1 logistic model using **train labels only**. Validation independently chooses a threshold minimizing the declared FP:FN cost (default 10:1), including the possibility of rejecting every pair. Optional regularized sigmoid calibration uses the fourth independent calibration split; it never reuses training or validation labels. Insufficient calibration support raises an error rather than claiming a calibrated model.

Model, optional calibration parameters and the complete validation decision are frozen in `frozen_config.json` before reading test descriptive features. The selected threshold is `validation_selection.selected_threshold`; `null` means reject all. The report records the model/dataset fingerprints, test confusion counts and sample-conditional ranking/calibration diagnostics. This is evaluation on supplied explicit pairs; it does not measure candidate retrieval recall or deployment calibration. Reviewed labels are selected samples and can remain biased even with independent splits.

## Review queue contract

`rank_review_candidates(records, candidates, strategy=..., seed=..., limit=..., excluded_pairs=...)` accepts only real finite scores in [0,1] and rejects evaluator labels/truth groups. It returns a list preserving each candidate's fields, plus `selection_rank`, `sampling_strategy`, `sampling_reason` and `uncertainty = abs(score - 0.5)`. Unordered excluded pairs are removed. Ties use a stable seeded pair hash.

`random` orders by that hash. `uncertainty_diversity` first orders by distance to 0.5, then selects pairs with distinct endpoints within each round, deferring shared endpoints until the next round. This is endpoint diversity, not semantic diversity, and uncertainty of an uncalibrated score is a heuristic rather than guaranteed information gain. Neither strategy reads labels to rank the queue. Operational review eligibility, access control and active suppression remain the caller's responsibility.

## Reproducible oracle simulation

```powershell
python scripts/check_learning_loop.py --dataset artifacts/benchmarks/v1/dblp_acm --output artifacts/reports/learning_simulation_reproduction --budgets 20,50,100,200 --seeds 11,29,47 --split-seed 20260912 --batch-size 10
```

The frozen run is `artifacts/reports/learning_simulation_v1`. It reads only the **original public DBLP-ACM train** feature file and a `split=train` predicate on the label file. Original public validation and test are not materialized by this experiment. The known labels serve as a simulated oracle; unknown pairs are not negative examples. The dataset is repartitioned within that original training area using the full shared-endpoint protocol above.

| Inner partition | Pairs | Positive | Negative | Independent components |
|---|---:|---:|---:|---:|
| Acquisition pool / train | 4,987 | 1,068 | 3,919 | 253 |
| Validation | 112 | 95 | 17 | 87 |
| Calibration, reserved and unused | 127 | 87 | 40 | 71 |
| Inner test | 185 | 94 | 91 | 82 |

These sizes are the actual result of retaining giant components; they are not the target fractions. No cross-split edge was dropped. In particular, validation has a much higher positive fraction than the pool or inner test. This distribution shift limits threshold transfer.

The protocol, three seeds and all budgets are written to `plan.json` before learned scoring. Both methods use the same initial 20 randomly selected pairs for each seed. Active sampling retrains in fixed batches of ten, scores the remaining pool, selects without labels, and then reveals the oracle judgments. Every selected label counts against the budget. A one-class initial sample produces an explicit untrainable checkpoint and random fallback acquisition, not hidden extra labels. Threshold selection uses another **112 fixed validation labels in addition to the acquisition budget** for both methods. Every checkpoint saves its acquisition trace, model and validation policy before inner-test scoring. No method is selected as the winner.

| Acquisition budget | Mean positive labels, random | Mean positive labels, active | Mean inner-test F1, random | Mean inner-test F1, active |
|---|---:|---:|---:|---:|
| 20 | 4.33 | 4.33 | 0.78448 | 0.78448 |
| 50 | 11.00 | 28.00 | 0.78448 | 0.78448 |
| 100 | 23.67 | 53.67 | 0.78448 | 0.78448 |
| 200 | 46.00 | 101.67 | 0.78448 | 0.78448 |

All 24 checkpoints evaluated; each has TP=91, FP=47, FN=3, TN=44 on the same 185-pair inner test (precision 0.65942, recall 0.96809). Models and selected numerical thresholds do change. A descriptive post-run audit found 127 inner-test pairs with identical full matcher feature vectors because their normalized titles match exactly and all other descriptive fields are absent: 85 are positive and 42 are negative. The title-only model cannot distinguish those pairs. This observation did not trigger tuning or rerunning the frozen experiment.

There is **no measured F1 improvement** from active sampling in this experiment. The additional positive labels describe sampling composition, not saved annotation time. No human study measured review speed, error, abstention or disagreement. Three acquisition seeds on one inner holdout are not dataset-level confidence intervals, and no claim about company matching or product matching follows from this bibliographic simulation.

## Verification

`tests/test_learning.py` exercises real Store publication, revocation, version expiry, abstention exclusion, artifact corruption, endpoint/positive-closure leakage checks, score-only deterministic sampling and exclusions, insufficient splits, independent calibration and freeze-before-test behavior. Its miniature oracle run forbids original public validation/test reads, checks a fresh frozen policy before each inner-test evaluation, enforces equal budgets/shared initial samples, and covers a single-class acquisition checkpoint.
