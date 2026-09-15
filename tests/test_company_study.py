import copy

import pytest

from entitybridge.company_study import development_split, paired_comparison
from entitybridge.feiii_benchmark import adapt


def test_feiii_keeps_ambiguous_unknown_and_quarantines_conflicting_positive_closure():
    tables = {
        "ffiec": [{"IDRSSD": k, "Financial Institution Name": "SYNTHETIC " + k} for k in ("1", "2", "3")],
        "lei": [{"LEI": k, "LegalName": "SYNTHETIC " + k, "LegalAddress_Country": "US"} for k in ("a", "b", "c")],
        "sec": [{"CIK": "x", "CONFORMED_NAME": "SYNTHETIC X"}]}
    judgments = [{"left_id": "1", "right_id": "a", "right_source": "lei", "type": "TP"},
                 {"left_id": "1", "right_id": "b", "right_source": "lei", "type": "Ambiguous"},
                 {"left_id": "2", "right_id": "b", "right_source": "lei", "type": "TN"},
                 {"left_id": "3", "right_id": "c", "right_source": "lei", "type": "TP"},
                 {"left_id": "3", "right_id": "x", "right_source": "sec", "type": "TN"},
                 {"left_id": "missing", "right_id": "a", "right_source": "lei", "type": "TP"}]
    result = adapt(tables, judgments)
    assert len(result["labels"]) == 4 and len(result["ambiguous_pairs"]) == 1
    assert result["audit"]["missing_endpoint_pairs"] == 1
    assert {r["label"] for r in result["labels"]} == {0, 1}
    assert all(not r["record_id"].startswith("LEI") and "LEI" not in r for r in result["records"])
    conflicting = adapt(tables, judgments + [judgments[3] | {"type": "TN"}])
    assert conflicting["audit"]["quarantined_binary_pairs"] == 2


def test_calibration_split_keeps_dependency_groups_and_rejects_shared_records():
    labels = [{"left_id": f"a{i}", "right_id": f"b{i}", "label": i % 2,
               "group_id": f"g{i}", "split": "validation"} for i in range(100)]
    a, b = development_split(labels)
    assert len(a) + len(b) == 100
    assert {r["group_id"] for r in a}.isdisjoint({r["group_id"] for r in b})
    bad = copy.deepcopy(labels)
    first = next(r for r in bad if r["group_id"] == a[0]["group_id"])
    second = next(r for r in bad if r["group_id"] == b[0]["group_id"])
    second["left_id"] = first["left_id"]
    with pytest.raises(ValueError, match="share records"):
        development_split(bad)


def test_paired_bootstrap_identical_models_have_zero_difference():
    labels = [{"left_id": f"a{i}", "right_id": f"b{i}", "label": i % 2,
               "group_id": f"g{i}"} for i in range(40)]
    values = {(r["left_id"], r["right_id"]): .9 if r["label"] else .1 for r in labels}
    scores = {"legacy_lr": values, "company_lr": values}
    selections = {name: {p: {"threshold": .5} for p in ("f1", "cost_10_1")} for name in scores}
    result = paired_comparison(labels, scores, selections, "company_lr", n_resamples=100)
    assert all(v == {"estimate": 0., "interval": [0., 0.]} for v in result["intervals"].values())


def test_official_workbook_reader_preserves_unicode_missing_cells_and_string_ids():
    import io
    import zipfile

    from entitybridge.feiii_benchmark import workbook_rows
    content = io.BytesIO()
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(content, "w") as z:
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}><si><t>LEI</t></si><si><t>LegalName</t></si>'
                   '<si><t>LegalAddress_Line1</t></si><si><t>001ABC</t></si>'
                   '<si><r><t>Crédit </t></r><r><t>银行</t></r></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
                   '<c r="C1" t="s"><v>2</v></c></row><row r="2"><c r="A2" t="s"><v>3</v></c>'
                   '<c r="B2" t="s"><v>4</v></c></row></sheetData></worksheet>')
    assert list(workbook_rows(content.getvalue())) == [
        {"LEI": "001ABC", "LegalName": "Crédit 银行", "LegalAddress_Line1": ""}]
