"""Transactional version publication and immutable source provenance."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, func, insert, select, update

from . import schema as s


class VersionConflict(ValueError):
    """The input, decision basis, or published parent has changed."""


def now():
    return datetime.now(UTC).isoformat()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class Store:
    def __init__(self, database_url: str, artifact_root: str | Path):
        self.engine = create_engine(database_url)
        self.artifact_root = Path(artifact_root).resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    def initialize(self):
        from .database import initialize_database
        initialize_database(self.engine)
        with self.engine.begin() as con:
            if con.execute(select(s.current)).first() is None:
                if con.dialect.name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as insert_once
                else:
                    from sqlalchemy.dialects.sqlite import insert as insert_once
                con.execute(insert_once(s.current).values(workspace="default", revision_id=None,
                    generation=0).on_conflict_do_nothing(index_elements=[s.current.c.workspace]))

    def current_revision(self, con=None):
        if con is None:
            with self.engine.connect() as connection:
                return self.current_revision(connection)
        return con.execute(select(s.current.c.revision_id)).scalar_one()

    def _lock(self, con):
        # All state writers acquire this row first. SQLite serializes writes;
        # PostgreSQL obtains a row lock, independent of connection/process.
        con.execute(update(s.current).values(generation=s.current.c.generation + 1))

    def import_records(self, source: str, rows: list[dict], *, manifest=None):
        if not source or len(source) > 80 or not rows:
            raise ValueError("A source and non-empty rows are required")
        keys = [row.get("source_key") for row in rows]
        if any(not isinstance(key, str) or not key or len(key) > 200 for key in keys):
            raise ValueError("source_key must be a non-empty string; preserve leading zeros")
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate source keys in a batch")
        if any(not row.get("name") and not row.get("tombstone") for row in rows):
            raise ValueError("Name required for a live record")
        content_hash = digest(sorted(rows, key=lambda row: row["source_key"]))
        with self.engine.begin() as con:
            self._lock(con)
            previous = con.execute(select(s.snapshots.c.result).where(
                s.snapshots.c.source_id == source, s.snapshots.c.content_sha256 == content_hash)).scalars()
            for result in previous:
                # A repeated observation is a no-op only while its exact record
                # versions remain current. A -> B -> A is a new observation,
                # including resurrection after a tombstone, not an old retry.
                expected = {item["record_id"]: item["record_version_id"] for item in result["records"]}
                actual = {}
                identifiers = list(expected)
                for start in range(0, len(identifiers), 1000):
                    actual.update(con.execute(select(s.records.c.record_id, s.records.c.current_version)
                        .where(s.records.c.record_id.in_(identifiers[start:start + 1000]))).all())
                if actual == expected:
                    return result
            if con.execute(select(s.sources).where(s.sources.c.source_id == source)).first() is None:
                con.execute(insert(s.sources).values(source_id=source, schema_version="1",
                    terms_url=(manifest or {}).get("terms_url")))
            snapshot_id = str(uuid4())
            result = {"snapshot_id": snapshot_id, "content_sha256": content_hash, "records": []}
            con.execute(insert(s.snapshots).values(snapshot_id=snapshot_id, source_id=source,
                content_sha256=content_hash, manifest={"observed_at": now(), **(manifest or {})}, result={}))
            for row in rows:
                existing = con.execute(select(s.records).where(s.records.c.source_id == source,
                    s.records.c.source_key == row["source_key"])).mappings().first()
                record_id = existing["record_id"] if existing else str(uuid4())
                version_id = str(uuid4())
                if existing:
                    old = con.execute(select(s.versions).where(
                        s.versions.c.record_version_id == existing["current_version"])).mappings().one()
                    if old["payload"] == row:
                        result["records"].append({"record_id": record_id,
                            "record_version_id": old["record_version_id"]})
                        continue
                    con.execute(update(s.records).where(s.records.c.record_id == record_id).values(current_version=version_id))
                    for decision in con.execute(select(s.decisions).where(
                        (s.decisions.c.left_id == record_id) | (s.decisions.c.right_id == record_id))).mappings():
                        state = con.execute(select(s.events.c.action).where(s.events.c.decision_id == decision["decision_id"])
                            .order_by(s.events.c.seq.desc()).limit(1)).scalar_one_or_none()
                        if state and state != "EXPIRE":
                            con.execute(insert(s.events).values(decision_id=decision["decision_id"], action="EXPIRE",
                                created_at=now(), reviewer="system", reason="Endpoint version changed; a new review is required"))
                else:
                    con.execute(insert(s.records).values(record_id=record_id, source_id=source,
                        source_key=row["source_key"], current_version=version_id))
                con.execute(insert(s.versions).values(record_version_id=version_id, record_id=record_id,
                    snapshot_id=snapshot_id, payload=row, tombstone=bool(row.get("tombstone"))))
                result["records"].append({"record_id": record_id, "record_version_id": version_id})
            con.execute(update(s.snapshots).where(s.snapshots.c.snapshot_id == snapshot_id).values(result=result))
            return result

    def active_records(self, con=None):
        if con is None:
            with self.engine.connect() as connection:
                return self.active_records(connection)
        rows = con.execute(select(s.records.c.source_id, s.versions).join(s.versions,
            s.records.c.current_version == s.versions.c.record_version_id).where(~s.versions.c.tombstone)).mappings()
        return {row["record_id"]: {**row["payload"], "record_id": row["record_id"],
            "record_version_id": row["record_version_id"], "source": row["source_id"]} for row in rows}

    def _event_cutoff(self, con):
        return con.execute(select(func.coalesce(func.max(s.events.c.seq), 0))).scalar_one()

    def prepare_revision(self, edges, *, policy_version="default-v1", threshold=0.9, review_threshold=0.5,
                         expected_input_hash=None, verify_full=False, max_incremental_records=10000,
                         force_full=False, force_full_reason="explicit_full_rebuild", basis_guard=None, commit_hook=None):
        from .identity import assign_identities
        from .resolution import Edge, EdgeDecision, Resolution, resolve

        with self.engine.connect() as con:
            # Internal worker hooks fence the frozen basis while reading it.
            if basis_guard is not None:
                self._lock(con)
                basis_guard(con)
            parent = self.current_revision(con)
            records = self.active_records(con)
            cutoff = self._event_cutoff(con)
            constraints = self._constraints(con, records, policy_version, cutoff)
        if expected_input_hash and digest(records) != expected_input_hash:
            raise VersionConflict("Source records changed during scoring; rerun matching")
        for edge in edges:
            for side in ("left", "right"):
                endpoint = records.get(edge[side])
                if not endpoint or edge.get(side + "_version") != endpoint["record_version_id"]:
                    raise VersionConflict("Scored edge endpoint version is stale or missing")
        prior_payload = self._payload(parent) if parent else None
        computation = {"mode": "full", "reason": force_full_reason if force_full else "initial_revision",
                       "affected_records": len(records)}
        new_edges = tuple(Edge(e["left"], e["right"], e["score"], e.get("auto_merge", True)) for e in edges)
        if prior_payload and not force_full:
            from .incremental import Snapshot, recompute
            def state(payload):
                return Snapshot(frozenset(payload["records"]), tuple(Edge(e["left"], e["right"], e["score"],
                    e.get("auto_merge", True)) for e in payload["edges"]),
                    **{key: tuple(tuple(pair) for pair in value) for key, value in payload["constraints"].items()},
                    threshold=payload["threshold"], review_threshold=payload["review_threshold"], policy_version=payload["policy_version"])
            prior = Resolution(tuple(sorted(tuple(entity["members"]) for entity in prior_payload["entities"].values())),
                tuple(EdgeDecision(**{**item, "conflicts": tuple(tuple(pair) for pair in item["conflicts"])}) for item in prior_payload["edge_decisions"]))
            new = Snapshot(frozenset(records), new_edges, **{key: tuple(value) for key, value in constraints.items()},
                threshold=threshold, review_threshold=review_threshold, policy_version=policy_version)
            changed = {key for key in records.keys() | prior_payload["records"].keys()
                if records.get(key) != prior_payload["records"].get(key)}
            update_result = recompute(state(prior_payload), new, prior, changed_records=changed, max_records=max_incremental_records)
            outcome = update_result.resolution
            computation = {"mode": update_result.mode, "reason": update_result.reason,
                "affected_records": len(update_result.affected_records), "full_equivalence_checked": verify_full}
            if verify_full and outcome.partitions != new.solve().partitions:
                raise RuntimeError("Incremental result differs from the full reference; refusing publication")
        else:
            outcome = resolve(records, new_edges, threshold=threshold, review_threshold=review_threshold, **constraints)
        previous = prior_payload["entities"] if prior_payload else {}
        identities = assign_identities(outcome.partitions, previous={key: val["members"] for key, val in previous.items()})
        entities = {}
        for entity_id, members in identities.entities.items():
            canonical = {}
            ordered = sorted(members, key=lambda member: (records[member]["source"] != "companies_house", member))
            for field in ("name", "address", "city", "postcode", "country"):
                chosen = next((records[member] for member in ordered if records[member].get(field)), None)
                if chosen:
                    canonical[field] = {"value": chosen[field], "record_version_id": chosen["record_version_id"],
                        "source": chosen["source"], "rule": "companies-house-first-then-stable-id-v1"}
            entities[entity_id] = {"entity_id": entity_id, "members": sorted(members), "canonical": canonical}
        from dataclasses import asdict
        revision_id = str(uuid4())
        payload = {"revision_id": revision_id, "parent_revision": parent, "records": records,
            "entities": entities, "edges": edges, "constraints": constraints, "event_cutoff": cutoff,
            "edge_decisions": [asdict(d) for d in outcome.decisions], "policy_version": policy_version,
            "threshold": threshold, "review_threshold": review_threshold,
            "computation": computation,
            "lineage": [asdict(item) for item in identities.lineage]}
        path = self.artifact_root / f"{revision_id}.json"
        content = encoded(payload)
        temporary = path.with_suffix(".partial")
        with temporary.open("xb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
        from . import query_projection
        with self.engine.begin() as con:
            if basis_guard is not None:
                self._lock(con)
                basis_guard(con)
            con.execute(insert(s.runs).values(run_id=revision_id, pipeline_version=policy_version, status="complete",
                manifest={"input_hash": digest(records), "edges": len(edges), "artifact_sha256": hashlib.sha256(content).hexdigest(),
                          "threshold": threshold, "review_threshold": review_threshold, "computation": computation,
                          "query_projection_version": query_projection.VERSION}))
            if edges:
                unique_edges = {}
                for edge in edges:
                    left, right = sorted((edge["left_version"], edge["right_version"]))
                    unique_edges[(left, right)] = {"run_id": revision_id, "left_version": left, "right_version": right,
                        "score": edge["score"], "evidence": {"fields": edge.get("evidence", {}),
                            "rules": edge.get("candidate_rules", []), "auto_merge": edge.get("auto_merge", True)}}
                con.execute(insert(s.edges), list(unique_edges.values()))
            con.execute(insert(s.revisions).values(revision_id=revision_id, parent_revision=parent, status="prepared",
                artifact_path=path.name, artifact_sha256=hashlib.sha256(content).hexdigest(), input_hash=digest(records),
                event_cutoff=cutoff, constraint_hash=digest(constraints), policy_version=policy_version, created_at=now()))
            if records:
                con.execute(insert(s.memberships), [{"revision_id": revision_id, "entity_id": entity_id,
                    "record_id": member, "record_version_id": records[member]["record_version_id"]}
                    for entity_id, entity in entities.items() for member in entity["members"]])
            if identities.lineage:
                con.execute(insert(s.lineage), [{"revision_id": revision_id, "from_entity": item.from_entity,
                    "to_entity": item.to_entity, "relation": item.kind} for item in identities.lineage])
            query_projection.write(con, payload, hashlib.sha256(content).hexdigest())
            if commit_hook is not None:
                commit_hook(con, revision_id)
        return {"revision_id": revision_id, "parent_revision": parent, "entity_count": len(entities), "computation": computation}

    def _payload(self, revision_id, *, published_only=True):
        with self.engine.connect() as con:
            row = con.execute(select(s.revisions).where(s.revisions.c.revision_id == revision_id)).mappings().one_or_none()
        if not row or (published_only and row["status"] != "published"):
            raise KeyError("Published revision not found")
        path = (self.artifact_root / row["artifact_path"]).resolve()
        if not path.is_relative_to(self.artifact_root):
            raise ValueError("Invalid artifact path")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != row["artifact_sha256"]:
            raise ValueError("Artifact checksum mismatch")
        return json.loads(content)

    def publish(self, revision_id, *, expected_parent):
        from . import query_projection
        payload = self._payload(revision_id, published_only=False)
        with self.engine.begin() as con:
            self._lock(con)
            row = con.execute(select(s.revisions).where(s.revisions.c.revision_id == revision_id)).mappings().one()
            actual = self.current_revision(con)
            if row["status"] == "published" and actual == revision_id:
                return {"revision_id": revision_id, "status": "published"}
            if actual != expected_parent or row["parent_revision"] != expected_parent:
                raise VersionConflict("Published parent changed; rebuild the candidate")
            if digest(self.active_records(con)) != row["input_hash"] or self._event_cutoff(con) != row["event_cutoff"]:
                raise VersionConflict("Source records or decisions changed; rebuild the candidate")
            if self._projection_state(con, row) is None and self._legacy_projection(con, revision_id):
                query_projection.write(con, payload, row["artifact_sha256"])
            query_projection.verify(con, payload, row["artifact_sha256"])
            con.execute(update(s.revisions).where(s.revisions.c.revision_id == revision_id).values(status="published", published_at=now()))
            con.execute(update(s.current).values(revision_id=revision_id))
        return {"revision_id": revision_id, "status": "published"}

    def _projection_state(self, con, revision):
        from .query_projection import VERSION
        state = con.execute(select(s.query_projections).where(
            s.query_projections.c.revision_id == revision["revision_id"])).mappings().one_or_none()
        if state and (state["projection_version"] != VERSION or state["artifact_sha256"] != revision["artifact_sha256"]):
            raise ValueError("Query projection version or artifact binding is invalid; rebuild the projection")
        return state

    def _legacy_projection(self, con, revision):
        manifest = con.execute(select(s.runs.c.manifest).where(s.runs.c.run_id == revision)).scalar_one()
        return "query_projection_version" not in manifest

    def _ensure_query_projection(self, revision):
        from . import query_projection
        with self.engine.connect() as con:
            row = con.execute(select(s.revisions).where(s.revisions.c.revision_id == revision,
                s.revisions.c.status == "published")).mappings().one_or_none()
            if not row:
                raise KeyError("Published revision not found")
            state = self._projection_state(con, row)
            if state:
                return dict(state)
            if not self._legacy_projection(con, revision):
                raise ValueError("Published query projection is missing; rebuild it from the verified artifact")
        payload = self._payload(revision)
        with self.engine.begin() as con:
            self._lock(con)
            state = self._projection_state(con, row)
            if state is None:
                query_projection.write(con, payload, row["artifact_sha256"])
                state = query_projection.verify(con, payload, row["artifact_sha256"])
            return dict(state)

    def rebuild_query_projection(self, revision=None):
        """Rebuild derived query rows atomically; never change identity/source history."""
        from . import query_projection
        revision = revision or self.current_revision()
        if not revision:
            raise KeyError("Published revision not found")
        payload = self._payload(revision)
        with self.engine.begin() as con:
            self._lock(con)
            row = con.execute(select(s.revisions).where(s.revisions.c.revision_id == revision)).mappings().one()
            query_projection.write(con, payload, row["artifact_sha256"])
            return query_projection.verify(con, payload, row["artifact_sha256"])

    def projection_status(self, revision=None, *, verify=False):
        """Inspect readiness; verify=True rechecks the artifact and every projected row."""
        from . import query_projection
        revision = revision or self.current_revision()
        if not revision:
            return {"revision_id": None, "ready": False, "status": "no_published_revision"}
        with self.engine.connect() as con:
            row = con.execute(select(s.revisions).where(s.revisions.c.revision_id == revision,
                s.revisions.c.status == "published")).mappings().one_or_none()
            if not row:
                raise KeyError("Published revision not found")
            state = self._projection_state(con, row)
            if state is None:
                return {"revision_id": revision, "ready": False, "status": "missing"}
            if verify:
                state = query_projection.verify(con, self._payload(revision), row["artifact_sha256"])
            return {**dict(state), "ready": True, "status": "ready"}

    def _entity_select(self, revision, query):
        statement = select(s.query_entities).where(s.query_entities.c.revision_id == revision)
        if query:
            matching = select(s.query_values.c.entity_id).where(s.query_values.c.revision_id == revision,
                s.query_values.c.entity_id == s.query_entities.c.entity_id,
                s.query_values.c.value_folded.contains(query.casefold(), autoescape=True)).exists()
            statement = statement.where(matching)
        return statement

    def entities_page(self, query="", *, revision=None, offset=0, limit=100):
        if not isinstance(query, str) or not isinstance(offset, int) or offset < 0:
            raise ValueError("query must be text and offset a non-negative integer")
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        revision = revision or self.current_revision()
        result = {"revision_id": revision, "total": 0, "offset": offset, "limit": limit, "items": []}
        if not revision:
            return result
        state = self._ensure_query_projection(revision)
        with self.engine.connect() as con:
            statement = self._entity_select(revision, query)
            total = con.execute(select(func.count()).select_from(statement.subquery())).scalar_one() if query else state["entity_count"]
            rows = con.execute(statement.order_by(s.query_entities.c.entity_id).offset(offset).limit(limit)).mappings().all()
            members = {row["entity_id"]: [] for row in rows}
            identifiers = list(members)
            for start in range(0, len(identifiers), 1000):
                for entity_id, record_id in con.execute(select(s.memberships.c.entity_id, s.memberships.c.record_id)
                    .where(s.memberships.c.revision_id == revision, s.memberships.c.entity_id.in_(identifiers[start:start + 1000]))
                    .order_by(s.memberships.c.entity_id, s.memberships.c.record_id)):
                    members[entity_id].append(record_id)
        result.update(total=total, items=[{"entity_id": row["entity_id"], "canonical": row["canonical"],
                                          "members": members[row["entity_id"]]} for row in rows])
        return result

    def entities(self, query="", *, revision=None):
        return self.entities_page(query, revision=revision, limit=2147483647)["items"]

    def _constraints(self, con, records, policy, cutoff, *, revoked=None):
        latest = {}
        for event in con.execute(select(s.events).where(s.events.c.seq <= cutoff).order_by(s.events.c.seq)).mappings():
            latest[event["decision_id"]] = (event["action"], event["policy_version"])
        if revoked:
            latest[revoked] = ("REVOKE", policy)
        result = {"must_link": [], "cannot_link": [], "suppressed": []}
        for decision in con.execute(select(s.decisions)).mappings():
            a, b = decision["left_id"], decision["right_id"]
            state, event_policy = latest.get(decision["decision_id"], (None, None))
            if not state or state == "EXPIRE":
                continue
            if (state == "REVOKE" or decision["action"] == "abstain") and (event_policy or decision["policy_version"]) != policy:
                continue
            if a not in records or b not in records:
                continue
            if (records[a]["record_version_id"], records[b]["record_version_id"]) != (
                    decision["left_version"], decision["right_version"]):
                continue
            kind = "suppressed" if state == "REVOKE" else {
                "accept": "must_link", "reject": "cannot_link", "abstain": "suppressed"
            }[decision["action"]]
            result[kind].append((a, b))
        return {key: sorted(set(values)) for key, values in result.items()}

    def _check_basis(self, con, base_revision, *, left=None, right=None, left_version=None,
                     right_version=None, policy=None):
        if not base_revision or self.current_revision(con) != base_revision:
            raise VersionConflict("Review is stale: published revision changed")
        payload = self._payload(base_revision)
        records = self.active_records(con)
        if digest(records) != digest(payload["records"]):
            raise VersionConflict("Review is stale: source records changed")
        if policy and policy != payload["policy_version"]:
            raise VersionConflict("Review is stale: scoring policy changed")
        if left is not None and (left == right or left not in records or right not in records):
            raise ValueError("Decision endpoints must be distinct live records")
        if left is not None and (records[left]["record_version_id"], records[right]["record_version_id"]) != (
                left_version, right_version):
            raise VersionConflict("Review is stale: endpoint versions changed")
        return payload, records

    def decide(self, left, right, *, action, reason, reviewer, base_revision, left_version,
               right_version, policy_version, idempotency_key=None):
        from .review_operations import ReviewOperations
        return ReviewOperations(self).submit("decision", {"left": left, "right": right, "action": action,
            "reason": reason, "reviewer": reviewer, "base_revision": base_revision, "left_version": left_version,
            "right_version": right_version, "policy_version": policy_version}, idempotency_key=idempotency_key)

    def revoke_preview(self, decision_id, *, base_revision):
        from .resolution import Edge, resolve

        with self.engine.connect() as con:
            payload, records = self._check_basis(con, base_revision)
            decision = con.execute(select(s.decisions).where(s.decisions.c.decision_id == decision_id)).mappings().one_or_none()
            if not decision:
                raise KeyError("Decision not found")
            latest = con.execute(select(s.events.c.action).where(s.events.c.decision_id == decision_id)
                .order_by(s.events.c.seq.desc()).limit(1)).scalar_one()
            if latest != "CREATE":
                raise VersionConflict("Decision is no longer active")
            self._check_basis(con, base_revision, left=decision["left_id"], right=decision["right_id"],
                left_version=decision["left_version"], right_version=decision["right_version"])
            cutoff = self._event_cutoff(con)
            constraints = self._constraints(con, records, payload["policy_version"], cutoff, revoked=decision_id)
        resolution = resolve(records, [Edge(e["left"], e["right"], e["score"], e.get("auto_merge", True)) for e in payload["edges"]],
            threshold=payload["threshold"], review_threshold=payload["review_threshold"], **constraints)
        return {"decision_id": decision_id, "base_revision": base_revision, "event_cutoff": cutoff,
            "partitions": resolution.partitions, "entity_count_before": len(payload["entities"]),
            "entity_count_after": len(resolution.partitions)}

    def revoke(self, decision_id, *, base_revision, reviewer, reason, preview_cutoff, idempotency_key=None):
        from .review_operations import ReviewOperations
        return ReviewOperations(self).submit("revoke", {"decision_id": decision_id, "base_revision": base_revision,
            "reviewer": reviewer, "reason": reason, "preview_cutoff": preview_cutoff}, idempotency_key=idempotency_key)

    def entity(self, entity_id, *, revision=None):
        selected = revision or self.current_revision()
        if not selected:
            raise KeyError("Entity not found")
        self._ensure_query_projection(selected)
        with self.engine.connect() as con:
            entity = con.execute(select(s.query_entities).where(s.query_entities.c.revision_id == selected,
                s.query_entities.c.entity_id == entity_id)).mappings().one_or_none()
            if entity:
                rows = con.execute(select(s.records.c.source_id, s.versions).select_from(s.memberships)
                    .join(s.versions, s.memberships.c.record_version_id == s.versions.c.record_version_id)
                    .join(s.records, s.memberships.c.record_id == s.records.c.record_id)
                    .where(s.memberships.c.revision_id == selected, s.memberships.c.entity_id == entity_id)
                    .order_by(s.memberships.c.record_id)).mappings().all()
                records = [{**row["payload"], "record_id": row["record_id"],
                            "record_version_id": row["record_version_id"], "source": row["source_id"]} for row in rows]
                return {"entity_id": entity_id, "canonical": entity["canonical"],
                    "members": [record["record_id"] for record in records], "revision_id": selected,
                    "status": "current" if not revision else "historical", "records": records}
        if revision:
            raise KeyError("Entity not present in that revision")
        with self.engine.connect() as con:
            known = con.execute(select(s.memberships.c.record_id).join(s.revisions,
                s.revisions.c.revision_id == s.memberships.c.revision_id).where(
                    s.memberships.c.entity_id == entity_id, s.revisions.c.status == "published")).scalars().all()
            if not known:
                raise KeyError("Entity not found")
            transitions = con.execute(select(s.lineage).join(s.revisions,
                s.revisions.c.revision_id == s.lineage.c.revision_id).where(s.revisions.c.status == "published")).mappings().all()
        graph = {}
        for transition in transitions:
            if transition["from_entity"] != transition["to_entity"]:
                graph.setdefault(transition["from_entity"], set()).add(transition["to_entity"])
        reached, queue = set(), [entity_id]
        while queue:
            node = queue.pop()
            if node in reached:
                continue
            reached.add(node)
            queue.extend(graph.get(node, ()))
        destinations = []
        identifiers = sorted(reached)
        with self.engine.connect() as con:
            for start in range(0, len(identifiers), 1000):
                destinations.extend(con.execute(select(s.query_entities.c.entity_id).where(
                    s.query_entities.c.revision_id == selected,
                    s.query_entities.c.entity_id.in_(identifiers[start:start + 1000]))).scalars())
        return {"entity_id": entity_id, "revision_id": selected, "status": "retired",
            "destinations": sorted(destinations)}

    def history(self):
        with self.engine.connect() as con:
            return [dict(row) for row in con.execute(select(s.revisions).order_by(s.revisions.c.created_at)).mappings()]

    def decision_history(self):
        with self.engine.connect() as con:
            items = [dict(row) for row in con.execute(select(s.decisions)).mappings()]
            for item in items:
                item["events"] = [dict(row) for row in con.execute(select(s.events)
                    .where(s.events.c.decision_id == item["decision_id"]).order_by(s.events.c.seq)).mappings()]
            items.sort(key=lambda item: (item['events'][0]['seq'] if item['events'] else 0, item['decision_id']))
            return items
