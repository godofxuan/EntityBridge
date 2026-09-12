"""Public benchmark parsing never turns unlabelled pairs into negatives."""
import csv
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from entitybridge.benchmarks import CATALOG, adapt_tables, convert_benchmark, file_hash, verify_benchmark
from entitybridge.normalization import MATCHER_COLUMNS


def tables():
    return {
        "tableA.csv": [{"id": "A001", "name": "Alpha Cafe", "addr": "1 North Road", "city": "Town",
                        "phone": "123", "class": "ENTITY-ANSWER"},
                       {"id": "A002", "name": "Beta Cafe", "addr": "2 North Road", "city": "Town"}],
        "tableB.csv": [{"id": "B001", "name": "Alpha CAFE", "addr": "1 North Rd", "city": "Town"},
                       {"id": "B002", "name": "Gamma Cafe", "addr": "3 North Road", "city": "Town"}],
    }


def test_adaptation_has_exact_feature_boundary_and_known_components_never_cross_splits():
    labels = [{"ltable_id": "A001", "rtable_id": "B001", "label": "1", "original_split": "train"},
              {"ltable_id": "A001", "rtable_id": "B002", "label": "0", "original_split": "test"}]
    result = adapt_tables("fodors_zagats", tables(), labels)
    records = [row for rows in result["matcher"].values() for row in rows]
    assert len(records) == 4
    assert all(set(row) == set(MATCHER_COLUMNS) for row in records)
    assert all("A001" != row["record_id"] for row in records)
    assert all("ENTITY-ANSWER" not in str(row) and "phone" not in row for row in records)
    positive = next(row for row in result["labels"] if row["label"] == 1)
    assert positive["left_group_id"] == positive["right_group_id"]
    groups = {row["record_id"]: row for row in result["record_groups"]}
    assert groups[positive["left_id"]]["split"] == groups[positive["right_id"]]["split"]
    assert len(result["labels"]) <= 2  # Two other Cartesian pairs remain unknown.
    assert result["statistics"]["original_records_in_multiple_pair_splits"] == 1
    assert result["statistics"]["complete_cluster_truth"] is False
    assert adapt_tables("fodors_zagats", {k: list(reversed(v)) for k, v in tables().items()},
                        list(reversed(labels))) == result


def test_conflicting_duplicate_labels_and_negative_inside_positive_closure_fail():
    positive = {"ltable_id": "A001", "rtable_id": "B001", "label": 1, "original_split": "train"}
    with pytest.raises(ValueError, match="Contradictory labels"):
        adapt_tables("fodors_zagats", tables(), [positive, positive | {"label": 0}])
    chain = [positive,
             positive | {"rtable_id": "B002"},
             positive | {"ltable_id": "A002", "rtable_id": "B002"},
             positive | {"ltable_id": "A002", "label": 0}]
    with pytest.raises(ValueError, match="positive-label closure"):
        adapt_tables("fodors_zagats", tables(), chain)


def test_unknown_record_label_and_duplicate_source_id_are_rejected():
    label = {"ltable_id": "missing", "rtable_id": "B001", "label": 1, "original_split": "train"}
    with pytest.raises(ValueError, match="Unknown labelled endpoint"):
        adapt_tables("fodors_zagats", tables(), [label])
    duplicate = tables()
    duplicate["tableA.csv"].append(duplicate["tableA.csv"][0])
    with pytest.raises(ValueError, match="Duplicate source record"):
        adapt_tables("fodors_zagats", duplicate, [])


def test_title_only_projection_does_not_claim_authors_are_addresses():
    rows = {"tableA.csv": [{"id": "a", "title": "A useful paper", "authors": "Hidden Author", "year": "2000"}],
            "tableB.csv": [{"id": "b", "title": "A useful paper", "venue": "Secret Venue"}]}
    labels = [{"ltable_id": "a", "rtable_id": "b", "label": 1, "original_split": "test"}]
    result = adapt_tables("dblp_acm", rows, labels)
    record = next(row for rows in result["matcher"].values() for row in rows)
    assert record["name"] == "A USEFUL PAPER"
    assert record["address"] is None and record["city"] is None
    assert "Hidden Author" not in str(result["matcher"])


def test_cross_split_negatives_are_discarded_counted_and_never_moved_or_relabelled():
    source_tables = {"tableA.csv": [{"id": f"a{i}", "name": f"Restaurant {i}"} for i in range(15)],
                     "tableB.csv": [{"id": f"b{i}", "name": f"Restaurant {i}"} for i in range(15)]}
    labels = [{"ltable_id": f"a{i}", "rtable_id": f"b{j}", "label": int(i == j), "original_split": "train"}
              for i in range(15) for j in range(15)]
    result = adapt_tables("fodors_zagats", source_tables, labels)
    assert result["statistics"]["cross_split_pairs_dropped"].get("1", 0) == 0
    assert result["statistics"]["cross_split_pairs_dropped"]["0"] > 0
    assert len(result["labels"]) + sum(result["statistics"]["cross_split_pairs_dropped"].values()) == len(labels)
    assert sum(row["label"] for row in result["labels"]) == 15
    endpoint_groups = {}
    for pair in result["labels"]:
        for endpoint in (pair["left_id"], pair["right_id"]):
            assert endpoint_groups.setdefault(endpoint, pair["group_id"]) == pair["group_id"]


def converted_fixture(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    fixture_rows = tables() | {
        "train.csv": [{"ltable_id": "A001", "rtable_id": "B001", "label": 1}],
        "valid.csv": [{"ltable_id": "A002", "rtable_id": "B002", "label": 1}],
        "test.csv": [{"ltable_id": "A001", "rtable_id": "B002", "label": 0}],
    }
    for filename, rows in fixture_rows.items():
        with (raw / filename).open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=sorted(set().union(*(row.keys() for row in rows))))
            writer.writeheader()
            writer.writerows(rows)
    monkeypatch.setitem(CATALOG["fodors_zagats"], "source_sha256", {name: file_hash(raw / name) for name in fixture_rows})
    output = tmp_path / "converted"
    convert_benchmark(raw, output, "fodors_zagats")
    return output


def test_converter_and_preflight_enforce_hashes_and_refuse_output_reuse(tmp_path, monkeypatch):
    output = converted_fixture(tmp_path, monkeypatch)
    report = verify_benchmark(output)
    assert report["records"] == 4 and report["unknown_pairs_are_not_negatives"]
    with pytest.raises(FileExistsError):
        convert_benchmark(tmp_path / "raw", output, "fodors_zagats")
    with (output / "matcher/train.parquet").open("ab") as target:
        target.write(b"changed")
    with pytest.raises(ValueError, match="hash"):
        verify_benchmark(output)


def test_preflight_rejects_answer_column_even_after_manifest_hash_is_updated(tmp_path, monkeypatch):
    output = converted_fixture(tmp_path, monkeypatch)
    path = output / "matcher/train.parquet"
    table = pq.read_table(path)
    table = table.append_column("label", pa.array([1] * len(table), type=pa.int8()))
    pq.write_table(table, path)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["matcher/train.parquet"] = file_hash(path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="eight-column"):
        verify_benchmark(output)


def update_file_hash(output, relative):
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][relative] = file_hash(output / relative)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_preflight_does_not_materialise_test_or_other_matcher_text_features(tmp_path, monkeypatch):
    output = converted_fixture(tmp_path, monkeypatch)
    original = pq.read_table
    feature_reads = []
    def checked_read(path, *args, **kwargs):
        if "matcher" in str(path):
            feature_reads.append(str(path))
            assert kwargs.get("columns") == ["record_id", "record_version_id", "source"]
        return original(path, *args, **kwargs)
    monkeypatch.setattr(pq, "read_table", checked_read)
    verify_benchmark(output)
    assert len(feature_reads) == 3


@pytest.mark.parametrize("relative,column", [("evaluator/labelled_pairs.parquet", "label"),
                                             ("evaluator/record_groups.parquet", "record_id")])
def test_preflight_checks_evaluator_schema_types(tmp_path, monkeypatch, relative, column):
    output = converted_fixture(tmp_path, monkeypatch)
    path = output / relative
    table = pq.read_table(path)
    number = table.schema.get_field_index(column)
    table = table.set_column(number, column, pa.array([0] * len(table), type=pa.int64()))
    pq.write_table(table, path)
    update_file_hash(output, relative)
    with pytest.raises(ValueError, match="schema mismatch"):
        verify_benchmark(output)


def test_preflight_rejects_a_reversed_duplicate_pair(tmp_path, monkeypatch):
    output = converted_fixture(tmp_path, monkeypatch)
    relative = "evaluator/labelled_pairs.parquet"
    table = pq.read_table(output / relative)
    rows = table.to_pylist()
    first = rows[0]
    rows.append(first | {"left_id": first["right_id"], "right_id": first["left_id"],
                         "left_group_id": first["right_group_id"], "right_group_id": first["left_group_id"]})
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), output / relative)
    update_file_hash(output, relative)
    with pytest.raises(ValueError, match="duplicated"):
        verify_benchmark(output)
