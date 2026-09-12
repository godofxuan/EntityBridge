"""Explicit identifier-rich linkage; never called by identifier-missing evaluation."""
from collections import defaultdict
from itertools import combinations


def trusted_registry_edges(records):
    """Return registry-backed links and conflicts, without inferring identity from LEI inequality."""
    buckets = defaultdict(list)
    excluded = []
    for record in records:
        valid = record.get("registration_authority") == "RA000585" and record.get("registration_number")
        valid = valid and not record.get("relationships")
        if record.get("source") == "gleif":
            valid = valid and record.get("category") == "GENERAL" and record.get("status") == "ACTIVE" and record.get("registration_status") == "ISSUED"
            valid = valid and not record.get("successor_entities") and not record.get("associated_entity", {}).get("lei")
        elif record.get("source") == "companies_house":
            valid = valid and record.get("status", "").upper() == "ACTIVE"
        else:
            valid = False
        if not valid:
            excluded.append(record["record_id"])
            continue
        buckets[record["registration_number"]].append(record)
    edges, conflicts = [], []
    for registry_number, members in buckets.items():
        counts = defaultdict(int)
        for record in members:
            counts[record["source"]] += 1
        if any(count > 1 for count in counts.values()):
            conflicts.append({"registry_number": registry_number, "records": [r["record_id"] for r in members],
                              "reason": "Multiple records in one source require review"})
            continue
        for left, right in combinations(members, 2):
            if left["source"] == right["source"]:
                continue
            edges.append({"left": left["record_id"], "right": right["record_id"],
                "left_version": left["record_version_id"], "right_version": right["record_version_id"], "score": 1.0,
                "model": "trusted-registry-v1", "candidate_rules": ["RA000585_verified_registry_number"],
                "evidence": {"registration_authority": "RA000585", "registration_number": registry_number,
                    "semantics": "identifier-rich deterministic evidence, not a name-matching score"}})
    return {"edges": edges, "conflicts": conflicts, "excluded_record_ids": excluded}
