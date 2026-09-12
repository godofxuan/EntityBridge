# Matching and candidate audit — 2026-09-12

The project is useful as an evidence-preserving entity resolution workbench. The evidence does **not** support presenting it as a generally accurate automatic company matcher. Its clean registry benchmark is much easier than matching a historical name with no address. The existing defaults remain in place; the new name retrieval option is an experimental way to find more review candidates.

## Reproduced defects and repairs

1. **Pure-name records could crash Splink.** An entirely null address column was represented as a pandas `object` column. DuckDB inferred `INTEGER`, so compiling `jaro_winkler_similarity(address_l, address_r)` failed even though the null comparison branch would handle those rows. Text columns now use `string[pyarrow]`; the regression test fits and scores actual Splink on data with every address/postcode absent.
2. **The scorer was coupled to the old blocker.** `SplinkMatcher.score(records, candidates)` accepted an explicit candidate list but repeated four hard-coded blocking rules internally. A legitimate externally retrieved pair outside those rules could not be scored. Scoring now gives each requested edge a unique internal token present only at its two endpoints. Splink's documented `arrays_to_explode` blocking joins equal tokens; exactly `2 × requested_edges` token memberships are supplied. The test requests one pair outside every old rule and verifies exactly one actual prediction. No unrestricted Cartesian join or Python UDF over all record pairs is used.
3. **A completed human decision remained in the pending review queue.** The manual must/cannot constraints correctly changed entity membership, but a later review-only scored edge reported `review` again. The governance resolver now gives active human judgments precedence in scored-edge status. HTTP regressions verify that accepted/rejected pairs leave `/reviews?status=review` and appear under accepted/conflict respectively while preserving their real score.

The old fixed candidate policy is unchanged. A real 10,000-record replay of rename, delete, and insert scenarios still yields identical incremental/full scores at absolute tolerance `1e-12` and identical partitions. Actual rescored prediction counts are `1`, `0`, and `1` respectively. This result applies to **fixed blocking**, not the new top-k policy.

## Optional candidate retrieval

`FrozenNameRetriever.fit(train_records)` learns a character 3–5 gram, word-boundary TF-IDF vocabulary and IDF from allowed **training features only**. At inference it transforms with the frozen vocabulary/IDF and retrieves cosine-similar names across sources. The union includes the old fixed candidates plus symmetric top-k retrieval candidates. Known conflicting countries are removed before top-k selection; a missing country does not exclude a pair. Deterministic record-ID tie-breaking makes results independent of input order.

The persisted model contains the vocabulary, IDF, complete training-feature hash, parameters, runtime versions, and a fingerprint. Loading rejects a changed hash or incompatible runtime. It never reads the evaluator truth map. Its similarity is labelled as retrieval evidence, not as a calibrated match probability.

Resource limits are explicit: default 20,000 records, at most 100 million cross-source pair interactions, 100,000 vocabulary features, 1,024 characters per name, 2 million name characters in total, and sparse products of 64 query rows per block. Exceeding a limit raises `CandidateOverflow` rather than silently dropping a large bucket. This is bounded local exact sparse retrieval; it is not an approximate nearest-neighbour or distributed million-record system.

**Every hybrid run requires full candidate regeneration.** Adding one record can change another record's top-k neighbours even if its own name is unchanged. Reusing the existing local bucket-based incremental refresh for this policy would therefore be incorrect. `requires_full_recompute=True` exposes this contract to the workflow.

The HTTP service additionally treats `candidate_mode="hybrid"` as review-only: it preserves the actual scorer value but sets `auto_merge=False` on every resulting edge, including fixed-rule edges in the union. A score of 1.0 therefore cannot automatically merge entities in a hybrid run. An active, explicit human accept can merge them; revoking that judgment allows them to separate again. Every subsequent hybrid match run is full, even with unchanged input. Requesting hybrid mode without a configured frozen candidate model returns HTTP 422. The offline experiment below deliberately measures what automatic thresholds would do so that this guardrail has empirical justification.

### Top-k changes and previous review decisions

Top-k, minimum similarity, training features/IDF, and frozen runtime versions are included in the candidate model fingerprint, which is part of the service policy hash. Changing top-k therefore creates a new policy even if a particular pair still receives the same match score.

An active manual **accept/reject** is an assertion about the same record versions and survives that policy change. A **revocation/abstention** suppresses only the policy under which it was recorded; it does not silently veto a new candidate policy. The old immutable revision still shows the old suppression and results. In a new hybrid policy the pair returns to review and still cannot auto-merge. If the operator explicitly selects a different automatic policy such as fixed blocking, its new evidence can qualify for automatic merging again; use an active reject when the intent is to assert that the two record versions are different entities across model changes. Updated source record versions expire their old decisions as described in the governance documentation.

This distinction is covered by HTTP tests: accept → merge → preview/revoke → split; an unchanged hybrid run remains full; a top-k change releases the old scoped suppression without auto-merging; an active accept survives the same top-k change.

## Measurements and selection discipline

The v3 official-previous-name-derived dataset has 18,596 records / 9,298 entities. It is a derived stress case, not a population sample of independent business records. The training and validation splits were used for development. The test split had already been inspected during the original project; all new test results below are explicitly **exploratory reuse**, never a newly untouched holdout. No original frozen report was overwritten.

Validation candidate grid (3,596 records / 1,798 true pairs):

| Top-k | Minimum cosine | Candidate pairs | Candidate recall |
| --- | --- | ---: | ---: |
| 3 | 0.20 | 7,142 | 43.44% |
| 10 | 0.20 | 17,404 | 47.16% |
| 20 | 0.20 | 27,221 | 48.39% |
| 10 | 0.35 | 9,277 | 41.32% |

Selected k=10 / cosine=0.20: it uses the fewest candidates among settings retaining at least 95% of the best grid recall. It retains 97.47% of the best candidate recall with 36.1% fewer pairs than k=20. Automatic matching thresholds are still selected only from validation, separately for each method and candidate policy.

Exploratory comparison on the same previously seen 3,804-record / 1,902-pair test split:

| Metric | Original fixed blocking | Hybrid name retrieval |
| --- | ---: | ---: |
| Candidate pairs | 539 | 18,481 |
| True pairs in candidates | 371 | 876 |
| Candidate recall | 19.51% | 46.06% |
| Fuzzy validation-selected threshold | 0.50 | 0.80 |
| Fuzzy test precision | 71.07% | 21.01% |
| Fuzzy test recall | 19.51% | 18.40% |
| Fuzzy test F1 | 30.61% | 19.62% |
| Splink accepted pairs | 0 | 0 |
| Full 18,596-record candidate pairs | 6,234 | 125,671 |
| Full candidate-generation time | 0.272 s | 22.130 s |
| Whole experiment time | 3.115 s | 35.219 s |
| Process peak working set | 336,207,872 B | 1,296,150,528 B |

These are single-machine observations, not controlled repeated performance estimates. The original run fitted Splink; the hybrid run loaded exactly that frozen train model. Total times therefore include different fitting work. Candidate-generation times isolate the recall-expansion cost more directly.

**Interpretation:** retrieval finds 505 additional true candidate pairs, but substantially increases false candidates and does not improve automatic matching. The old Jaro-Winkler Splink model has too little identifiable training signal in this name-only case; training metadata explicitly lists unobserved levels using defaults. Broad candidate generation cannot repair that scoring limitation. Hybrid retrieval is therefore useful for an explicit review workflow and further labelled evaluation, and must not replace the production/default matcher on the strength of candidate recall alone.

## Reproduce and inspect

The immutable original reports remain under `artifacts/reports/aliases_name_only_v1`. The final revised run is `artifacts/reports/aliases_name_only_hybrid_v2`; its `candidate_model`, `frozen_config.json`, `validation_curves.json`, `report.json`, and `errors.json` provide provenance. A first intermediate hybrid run is preserved as `aliases_name_only_hybrid_v1`; v2 adds frozen dependency versions and total-character limits without changing its scores. Raw and bulky experiment artifacts are intentionally excluded from the public repository.

```powershell
# Train/validation-only grid; preflight checks test IDs/hashes for integrity,
# but this command does not score or evaluate test effects.
.venv\Scripts\python.exe scripts/run_experiment.py --dataset artifacts/datasets/v3/aliases_name_only --output artifacts/reports/my_validation_grid --candidate-validation-grid

# Explicit exploratory reuse of an already inspected test split.
.venv\Scripts\python.exe scripts/run_experiment.py --dataset artifacts/datasets/v3/aliases_name_only --output artifacts/reports/my_hybrid_run --frozen-model artifacts/reports/aliases_name_only_v1/model --candidate-mode hybrid-name --candidate-top-k 10 --candidate-min-similarity 0.2 --test-status exploratory-reused

.venv\Scripts\python.exe -m pytest tests/test_recall_upgrade.py tests/test_matching.py tests/test_candidates.py tests/test_evaluation.py tests/test_incremental.py tests/test_workflow.py -q

.venv\Scripts\python.exe -m pytest tests/test_hybrid_api.py -q
```

The focused matching suite passed 17 tests, including pure-null fields, explicit pair scoring, frozen reload, order invariance, conflicting/missing country behaviour, answer-column rejection, resource limits, tamper detection, and fixed-policy incremental equivalence. Four additional HTTP tests pass the review-only lifecycle, review queue state, and policy-scope checks described above. `ruff` passed for the changed matching/experiment files. The real incremental replay is recorded in `artifacts/reports/incremental_explicit_candidates_v3.json`.

## Comparison with established projects and remaining gap

Splink already provides multiple blocking policies, scalable SQL execution, probabilistic estimation, diagnostics, and evaluation. EntityBridge uses Splink; it does not replace or outperform that library. Its additional work is source-version evidence, review/revocation, constrained clustering, identity lineage, and controlled publication. The repaired scorer now uses Splink's documented external-blocking mechanism rather than duplicating candidate rules. See [Splink blocking API](https://moj-analytical-services.github.io/splink/api_docs/blocking.html), [blocking settings and Cartesian-join warning](https://moj-analytical-services.github.io/splink/api_docs/settings_dict_guide.html), and [training documentation](https://moj-analytical-services.github.io/splink/api_docs/training.html).

Dedupe incorporates human-labelled examples into learned matching weights and blocking through active learning. EntityBridge records review decisions as governance constraints but does not yet convert representative review data into a validated retraining and calibration pipeline. That is a real remaining difference, and likely more valuable for the name-only task than another unmeasured similarity heuristic. See [Dedupe matching and active learning](https://docs.dedupe.io/en/latest/how-it-works/Matching-records.html) and [threshold selection](https://docs.dedupe.io/en/latest/how-it-works/Choosing-a-good-threshold.html).

The TF-IDF component follows standard fit/transform semantics rather than introducing a new retrieval algorithm. See [scikit-learn TF-IDF documentation](https://scikit-learn.org/stable/modules/generated/sklearn.feature_extraction.text.TfidfVectorizer.html).

Next evidence needed: independently labelled historical-name pairs and nonmatches, representative unmatchable entities, prospective holdout data, precision-oriented review/merge thresholds, calibrated scoring or an explicitly trained name-only model, and repeated scale/latency measurements. There is no measured reduction in human review effort, no production-scale guarantee for hybrid retrieval, and no evidence that the project exceeds established entity-resolution products in overall accuracy.
