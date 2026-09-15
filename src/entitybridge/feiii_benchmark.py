"""NIST expert-adjudicated financial-company pairs for zero-shot evaluation.

This is an adjudication-selected pair cohort, not a complete entity population.
The official archive is read as data only. Ambiguous and unjudged pairs are not
negative labels. Identifier columns are used only for joining opaque records.
"""
import csv
import hashlib
import io
import zipfile
from collections import Counter
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree

from .benchmarks import _Components, file_hash
from .normalization import MATCHER_COLUMNS, normalize_postcode, normalize_text
from .store import digest

ARCHIVE_SHA256 = "13e0a099ddaa32950f27267d5a7327f1a4c00a72585400cd0c8b1061db899413"
URL = "https://ir.nist.gov/feiii/data/feiii-data-2016-final.zip"
SOURCE_IDS = {"ffiec": "IDRSSD", "lei": "LEI", "sec": "CIK"}
PROJECTION = {
    "ffiec": {"name": "Financial Institution Name", "address": "Financial Institution Address",
              "city": "Financial Institution City", "postcode": "Financial Institution Zip Code"},
    "lei": {"name": "LegalName", "address": "LegalAddress_Line1", "city": "LegalAddress_City",
            "postcode": "LegalAddress_PostalCode", "country": "LegalAddress_Country"},
    "sec": {"name": "CONFORMED_NAME", "address": "B_STREET", "city": "B_CITY", "postcode": "B_POSTAL",
            "country": "B_COUNTRY"}}


def opaque(*values):
    return str(uuid5(NAMESPACE_URL, digest(["feiii2016-v1", *values])))


def adapt(tables, judgments):
    needed = {s: set() for s in SOURCE_IDS}
    for row in judgments:
        if row["type"] not in {"TP", "TN", "Ambiguous"} or row["right_source"] not in {"lei", "sec"}:
            raise ValueError("Undeclared FEIII judgment type/source")
        needed["ffiec"].add(row["left_id"])
        needed[row["right_source"]].add(row["right_id"])
    rows, identifiers, source_counts, duplicate_records = {}, {}, {}, 0
    for source, originals in tables.items():
        count = 0
        for original in originals:
            count += 1
            key = original[SOURCE_IDS[source]]
            if key not in needed[source]:
                continue
            projected = {field: normalize_text(original.get(column)) for field, column in PROJECTION[source].items()}
            if source == "lei":
                projected["address"] = normalize_text(" ".join(original.get(f"LegalAddress_Line{i}") or "" for i in range(1, 5)))
            projected["postcode"] = normalize_postcode(projected.get("postcode"))
            record = dict.fromkeys(MATCHER_COLUMNS)
            record_id = opaque("record", source, key)
            record.update(projected, record_id=record_id, source=source,
                          record_version_id=opaque("version", record_id, projected))
            if record_id in rows:
                if rows[record_id] != record:
                    raise ValueError("Different FEIII records share a source ID")
                duplicate_records += 1
            rows[record_id] = record
            identifiers[source, key] = record_id
        source_counts[source] = count
    binary, ambiguous, missing = {}, [], []
    conflicting = set()
    for original in judgments:
        a = identifiers.get(("ffiec", original["left_id"]))
        b = identifiers.get((original["right_source"], original["right_id"]))
        if a is None or b is None:
            missing.append(digest(original))
            continue
        a, b = sorted((a, b))
        item = {"left_id": a, "right_id": b, "source_pair": "ffiec_" + original["right_source"]}
        if original["type"] == "Ambiguous":
            ambiguous.append(item)
            continue
        item.update(label=int(original["type"] == "TP"), split="test", original_split="official_adjudicated")
        if (a, b) in binary and binary[a, b]["label"] != item["label"]:
            conflicting.update((a, b))
        binary[a, b] = item
    positive = _Components(rows)
    for item in binary.values():
        if item["label"]:
            positive.union(item["left_id"], item["right_id"])
    bad = {positive.find(key) for key in conflicting}
    for item in binary.values():
        if not item["label"] and positive.find(item["left_id"]) == positive.find(item["right_id"]):
            bad.add(positive.find(item["left_id"]))
    ambiguous_keys = {(r["left_id"], r["right_id"]) for r in ambiguous}
    labels = [r for key, r in binary.items() if key not in ambiguous_keys and not any(positive.find(k) in bad for k in key)]
    dependencies = _Components(rows)
    for row in labels:
        dependencies.union(row["left_id"], row["right_id"])
    for row in labels:
        row["group_id"] = opaque("dependency", dependencies.find(row["left_id"]))
    return {"records": sorted(rows.values(), key=lambda r: r["record_id"]),
        "labels": sorted(labels, key=lambda r: (r["left_id"], r["right_id"])), "ambiguous_pairs": ambiguous,
        "audit": {"source_records": source_counts, "projected_cohort_records": len(rows),
            "identical_source_duplicates": duplicate_records, "missing_endpoint_pairs": len(missing),
            "missing_endpoint_hashes": missing, "ambiguous_pairs": len(ambiguous),
            "quarantined_binary_pairs": len(binary) - len(labels),
            "positive_closure_bad_components": len(bad),
            "retained_labels": {s: dict(Counter(str(r["label"]) for r in labels if r["source_pair"] == s))
                                for s in ("ffiec_lei", "ffiec_sec")},
            "scope": "Supplied expert-adjudicated pairs; unknowns not converted to negatives"}}


def read_archive(path):
    if file_hash(path) != ARCHIVE_SHA256:
        raise ValueError("FEIII source archive hash mismatch")
    with zipfile.ZipFile(path) as archive:
        judgments = []
        for target in ("lei", "sec"):
            raw = archive.read(f"Ground Truth/FFIEC-{target.upper()}-GroundTruth.txt").decode("utf-8-sig")
            for row in csv.DictReader(io.StringIO(raw), delimiter="\t"):
                judgments.append({"left_id": row["FFIEC_ID"], "right_id": row[target.upper() + "_ID"],
                                  "right_source": target, "type": row["TYPE"]})
        streams = {source: io.TextIOWrapper(archive.open(f"Data and Metadata/{source.upper()}.csv"),
                                           encoding="utf-8-sig") for source in ("ffiec", "sec")}
        try:
            tables = {source: csv.DictReader(stream) for source, stream in streams.items()}
            # The official LEI CSV is not valid UTF-8 or Windows-1252. Use the
            # Unicode workbook in the SAME pinned NIST archive, never replacement
            # characters or guessed decoding of company names/addresses.
            tables["lei"] = workbook_rows(archive.read("Data and Metadata/LEI.xlsx"))
            result = adapt(tables, judgments)
        finally:
            for stream in streams.values():
                stream.close()
        result["audit"].update(archive_sha256=ARCHIVE_SHA256, url=URL, projection=PROJECTION,
            source_formats={"ffiec": "UTF-8 CSV", "sec": "UTF-8 CSV", "lei": "official Unicode XLSX"},
            member_sha256={name: hashlib.sha256(archive.read(name)).hexdigest() for name in ("Ground Truth/README.txt",)})
        return result


def workbook_rows(content):
    """Read the official single-sheet workbook with standard-library XML only."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(content)) as workbook:
        strings = []
        with workbook.open("xl/sharedStrings.xml") as stream:
            for _, elem in ElementTree.iterparse(stream, events=("end",)):
                if elem.tag == ns + "si":
                    strings.append("".join(t.text or "" for t in elem.iter(ns + "t")))
                    elem.clear()
        header = None
        with workbook.open("xl/worksheets/sheet1.xml") as stream:
            for _, elem in ElementTree.iterparse(stream, events=("end",)):
                if elem.tag != ns + "row":
                    continue
                cells = {}
                for c in elem.findall(ns + "c"):
                    column = "".join(ch for ch in c.attrib["r"] if ch.isalpha())
                    value = c.findtext(ns + "v") or ""
                    if c.attrib.get("t") == "s":
                        value = strings[int(value)]
                    elif c.attrib.get("t") == "inlineStr":
                        value = "".join(t.text or "" for t in c.iter(ns + "t"))
                    cells[column] = value
                if header is None:
                    header = cells
                elif cells:
                    yield {name: cells.get(column, "") for column, name in header.items()}
                elem.clear()
