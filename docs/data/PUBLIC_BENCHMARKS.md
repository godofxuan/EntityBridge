# Public pair-labelled benchmarks

EntityBridge now has adapters and reproducible local downloads for two small public entity-matching datasets. They test matching behaviour outside the clean company-register overlap. They **do not establish company-domain production accuracy**, and their labels do not constitute a complete cluster truth map.

## Author-hosted sources and permission status

The DeepMatcher authors list Fodors–Zagats and DBLP–ACM as structured pair-labelled benchmarks. Their dataset notes explain that candidate pairs were blocked, labelled using a gold-match list, and randomly divided into train/validation/test **pairs**. They also caution that the supplied tables are the experimental tuple collections and may differ from the original source tables. See the [official dataset catalogue](https://github.com/anhaidgroup/deepmatcher/blob/master/Datasets.md).

| Dataset | Domain | Author-hosted download directory | Downloaded records | Supplied explicit pair labels | Positive labels |
| --- | --- | --- | ---: | ---: | ---: |
| Fodors–Zagats | Restaurants | [Wisconsin Fodors–Zagats](https://pages.cs.wisc.edu/~anhai/data1/deepmatcher_data/Structured/Fodors-Zagats/exp_data/) | 864 = 533 + 331 | 946 | 110 |
| DBLP–ACM | Bibliographic citations | [Wisconsin DBLP–ACM](https://pages.cs.wisc.edu/~anhai/data1/deepmatcher_data/Structured/DBLP-ACM/exp_data/) | 4,910 = 2,616 + 2,294 | 12,363 | 2,220 |

The official software repository has a [BSD 3-Clause software licence](https://github.com/anhaidgroup/deepmatcher/blob/master/LICENSE). The dataset catalogue does not specify an independent licence for these third-party data contents. EntityBridge therefore records `dataset_license = not specified on official dataset page`; it does **not** pretend that the code licence grants a dataset redistribution licence. Downloads and derived record files stay under ignored `artifacts/`; the public repository contains the adapter, synthetic tests, URLs, hashes, methodology, and aggregate results. No original or converted records are uploaded with the code.

All ten CSV files were successfully downloaded from the authors' HTTPS server on 2026-09-12. The downloader accepts only the pinned source-file hashes listed below; a changed response requires an explicit new snapshot review instead of silently replacing the benchmark.

| Dataset / file | SHA-256 |
| --- | --- |
| Fodors–Zagats / tableA.csv | `dae8867efd8da4cc0ce729d08b506f4c37a9d106b977fa75929618c2a311a356` |
| Fodors–Zagats / tableB.csv | `d31e7dfa7fe363c594bf8ebe4eaaf5018e11362125edd49042271d2aae7a1d37` |
| Fodors–Zagats / train.csv | `fda195b062d7becc9abb941daef30c97aa22c4914330c3dac15e675353c52d2c` |
| Fodors–Zagats / valid.csv | `7d0b35d5e5e90defb7c88a6bd5d359057fd8919a7d36321f6dc8087ab66939e3` |
| Fodors–Zagats / test.csv | `20b066790d1a5982a360c99c88af6e58ebf13f8d3150b7ff8172720c72f07a24` |
| DBLP–ACM / tableA.csv | `a83dfac196a4e263f3adac7aaf095c7198254a98fcaed0ec68d59130c74c43a7` |
| DBLP–ACM / tableB.csv | `bd103ffdccdff4d8b9d04c18d90d04110b70b6fc87dec83c4e52e9616c58431a` |
| DBLP–ACM / train.csv | `ad94b36b178bbf76023d3cee689565fbda1fe01b19d9a3926a51db382f45f0a5` |
| DBLP–ACM / valid.csv | `862f848ed3f3f005ae6c8997ecf571984bd575f6bbd319fc1a2170830a91132b` |
| DBLP–ACM / test.csv | `e49adc4590d24c18b1a9bbd96011d9c745e10432e10e93e050d856a206fac394` |

The downloader has a 5 MiB cap per file, verifies SHA-256 before replacing a completed local download, records a verification receipt, and leaves an existing valid snapshot intact. It uses no archive extraction and follows no arbitrary URL supplied by a dataset row.

## Feature mapping and exclusions

Every matcher split has exactly eight nullable string columns, in this order:

```text
record_id, record_version_id, source, name, address, city, postcode, country
```

Record IDs are opaque, deterministic UUIDs assigned from source-local IDs independently of labels, grouping, or split. Source-local IDs never become name/address features. Versions are derived from the projected feature contents. They are identifiers, not additional learned features.

Fodors–Zagats maps `name → name`, `addr → address`, and `city → city`. It excludes `id`, `phone`, `type`, and `class`. In particular, the provided `class` field cannot enter model features. Postcode and country remain null; the adapter does not invent geographical facts from the dataset's reputation.

DBLP–ACM maps only `title → name`; the other five text fields are null. Authors, venue, year, and original ID are excluded. Authors are not misrepresented as postal addresses. This is explicitly a **title-only, reduced-feature diagnostic**, so its result must not be compared as though EntityBridge used the same features or protocol as a published multi-attribute DeepMatcher result.

The DeepMatcher catalogue also lists a larger Company corpus based on Wikipedia and company-homepage text. Those are long document-matching inputs with a specific preprocessing protocol. It is a future option for a dedicated text matcher; forcing its document bodies into the current company name/address schema would be misleading. See the [official Company dataset description](https://github.com/anhaidgroup/deepmatcher/blob/master/Datasets.md#company).

## Known-entity-disjoint split protocol

The protocol is fixed as `public-pair-positive-component-split-v1`, seed `20260912`, nominal fractions 60%/20%/20%:

1. Parse both tables with unique source-local IDs and resolve every supplied labelled endpoint.
2. Reject conflicting labels for the same pair. Duplicate agreeing pair labels can be collapsed with duplicate counts and original split provenance retained.
3. Form connected components of **positive** labelled pairs. Reject any explicitly negative pair whose endpoints fall inside that positive transitive closure. The downloaded snapshots contain zero such contradictions and zero duplicate pair rows.
4. Assign each known-positive component, including isolated records, to one split using a deterministic seed/component hash. Labels inform only the known-entity grouping; no matching score or test effect is used to choose the split or seed.
5. Keep a supplied labelled pair only when both endpoints are in that same new split. Count and discard cross-split pairs; do not move an endpoint, invent a label, or convert a discarded negative to an unknown-as-negative training example. Every supplied positive pair is retained because its endpoints share a component.
6. For bootstrap dependence groups, form connected components of **all retained labelled edges**, both positive and negative. Pairs sharing an endpoint cannot masquerade as independent bootstrap groups. Large components remain large and must be reported honestly.

This ensures source-record-disjoint and **known-positive-component-disjoint** splits. Because the supplied annotations are partial, it cannot prove that every unannotated real-world duplicate has been separated perfectly. `record_groups.parquet` is a grouping aid for that protocol, not exhaustive cluster ground truth.

Original split leakage measured before replacement:

| Dataset | Records appearing in multiple original pair splits | Known-positive components appearing in multiple original pair splits | Cross-split negative pairs discarded | Positive pairs discarded |
| --- | ---: | ---: | ---: | ---: |
| Fodors–Zagats | 279 | 232 | 429 | 0 |
| DBLP–ACM | 2,861 | 1,692 | 5,367 | 0 |

The new split sizes are observations from that predeclared hash, not targets achieved by resampling the test set:

| Dataset / split | Records | Labelled pairs | Positive | Negative | Label-dependence components |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fodors–Zagats / train | 526 | 413 | 64 | 349 | 53 |
| Fodors–Zagats / validation | 184 | 49 | 26 | 23 | 25 |
| Fodors–Zagats / test | 154 | 55 | 20 | 35 | 16 |
| DBLP–ACM / train | 2,981 | 5,411 | 1,344 | 4,067 | 493 |
| DBLP–ACM / validation | 958 | 758 | 432 | 326 | 315 |
| DBLP–ACM / test | 971 | 827 | 444 | 383 | 306 |

Removing cross-split negatives changes the class balance and candidate distribution. These scores therefore measure this declared disjoint protocol, not the original paper's random-pair protocol. The small Fodors–Zagats test has only 20 known positive pairs; interval and raw-count reporting are necessary.

## Partial labels and output contract

Each converted directory contains:

```text
manifest.json
matcher/train.parquet
matcher/validation.parquet
matcher/test.parquet
evaluator/labelled_pairs.parquet
evaluator/record_groups.parquet
```

`labelled_pairs.parquet` has `left_id`, `right_id`, `label` (int8, 0 or 1), `split`, `original_split`, `left_group_id`, `right_group_id`, and `group_id`. The two side groups are known-positive components. The final group is the retained-label dependence component used for resampling. Multiple original splits for an agreeing duplicate are joined with `|` in provenance.

`record_groups.parquet` has `record_id`, `entity_group_id`, and `split`. All group/label columns are evaluator-only and absent from the eight matcher columns.

The manifest declares `partial_labels_only: true`, contains source URLs, source bytes/hashes/row counts, licence status, dropped columns, split protocol, leakage and retention counts, and a SHA-256 for each output file. `verify_benchmark(directory)` checks all output hashes, the exact eight-column schema, evaluator schema types, record/group coverage and split disjointness, explicit-label consistency, reversed duplicate pairs, and shared-endpoint bootstrap grouping before an experiment starts. Schema inspection is separate from reads: matcher data is projected to `record_id`, `record_version_id`, and `source` only. No test name/address features are materialised during this integrity preflight. Test labels/groups may be inspected for consistency, but no test scores or effects are computed.

**Unlisted pairs are unknown.** Candidate recall means coverage of known positive labels. Precision/recall/confusion matrices can use only explicitly labelled pairs under a clearly stated evaluation population. Generated candidates with no label must be counted separately, not charged as false positives or treated as true negatives. No B-cubed or full-entity clustering accuracy is available from this adapter.

## Reproduce

From a project environment installed using the existing lock file:

```powershell
.venv\Scripts\python.exe scripts/fetch_benchmarks.py

# Reuse pinned raw downloads but put another conversion in a fresh directory.
.venv\Scripts\python.exe scripts/fetch_benchmarks.py --output-root artifacts/benchmarks/reproduction

.venv\Scripts\python.exe -m pytest tests/test_benchmarks.py -q
```

The original local conversion is under `artifacts/benchmarks/v1/{fodors_zagats,dblp_acm}`. Output directories are never overwritten. Eleven synthetic tests cover feature leakage, ordering, positive closure contradictions, duplicate IDs, missing label endpoints, title-only projection, cross-split negative retention, dependence groups, file hashes, reused output rejection, schema tampering, reversed-pair duplication, and the restricted preflight feature reads. Combined with the experiment runner's freeze-before-test integration test, 12 tests pass. The implementation and tests pass Ruff.
