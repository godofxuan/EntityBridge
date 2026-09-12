"""Database-backed, at-least-once preparation jobs with fenced result commits.

Execution may repeat after a crash. Only the current lease holder can atomically
bind one prepared revision; publication remains an independent human action.
"""

import json
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import insert, or_, select, text, update

from . import schema as s
from .store import digest

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_CLEAR_LEASE = {"lease_owner": None, "lease_token": None, "lease_until": None}


class JobConflict(ValueError):
    """An idempotency key or transition conflicts with durable state."""


class LeaseLost(JobConflict):
    """This execution no longer owns a live lease."""


class JobCancelled(JobConflict):
    """Cancellation prevents this execution from committing a result."""


class StaleJob(JobConflict):
    """Source facts, published parent or review events changed after submission."""


@dataclass(frozen=True)
class Lease:
    job_id: str
    token: str
    owner: str
    attempt: int
    payload: dict
    source_hash: str
    parent_revision: str | None
    event_cutoff: int
    lease_until: float


def _now(con):
    # PostgreSQL now() is the transaction's start, possibly before a lock wait.
    expression = ("EXTRACT(EPOCH FROM clock_timestamp())" if con.dialect.name == "postgresql"
                  else "(julianday('now') - 2440587.5) * 86400.0")
    return float(con.execute(text("SELECT " + expression)).scalar_one())


def _event(con, job, action, now, detail=None):
    con.execute(insert(s.job_events).values(job_id=job["job_id"], attempt=job["attempt"],
        action=action, created_at=now, detail=detail or {}))


def _public(row):
    result = dict(row)
    result.pop("lease_token", None)
    return result


def _json_copy(value):
    # Also prevents the caller mutating nested payload data after submission.
    return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))


def _duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("lease duration must be a positive finite number")
    return float(value)


def cancel_restored_jobs(con):
    """Cancel unfinished jobs inside a restore transaction; never reuse a lease."""
    now = _now(con)
    rows = con.execute(select(s.jobs).where(s.jobs.c.status.in_(["queued", "running"]))).mappings().all()
    for row in rows:
        con.execute(update(s.jobs).where(s.jobs.c.job_id == row["job_id"]).values(
            status="cancelled", updated_at=now, cancel_requested=True, **_CLEAR_LEASE))
        _event(con, row, "RESTORE_CANCEL", now, {"reason": "Restored jobs require an explicit new submission"})
    return len(rows)


class JobQueue:
    def __init__(self, store):
        self.store = store

    def _row(self, con, job_id):
        row = con.execute(select(s.jobs).where(s.jobs.c.job_id == job_id)).mappings().first()
        if row is None:
            raise KeyError(job_id)
        return row

    def _basis(self, con):
        return (digest(self.store.active_records(con)), self.store.current_revision(con),
                self.store._event_cutoff(con))

    @staticmethod
    def _same_basis(row, basis):
        return (row["source_hash"], row["parent_revision"], row["event_cutoff"]) == basis

    @staticmethod
    def _owned(row, lease, now):
        if (row["status"] != "running" or row["lease_token"] != lease.token
                or row["lease_owner"] != lease.owner or row["attempt"] != lease.attempt
                or row["lease_until"] is None or row["lease_until"] <= now):
            raise LeaseLost("Job lease expired or was replaced")
        if row["cancel_requested"]:
            raise JobCancelled("Job cancellation requested")

    def enqueue(self, payload, *, idempotency_key, max_attempts=3):
        if not isinstance(payload, dict):
            raise TypeError("job payload must be an object")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ValueError("idempotency key must contain 1 to 200 characters")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be an integer from 1 to 20")
        payload = _json_copy(payload)
        payload_hash = digest(payload)
        with self.store.engine.begin() as con:
            self.store._lock(con)
            existing = con.execute(select(s.jobs).where(
                s.jobs.c.idempotency_key == idempotency_key)).mappings().first()
            if existing:
                if existing["payload_hash"] != payload_hash or existing["max_attempts"] != max_attempts:
                    raise JobConflict("Idempotency key was already used for another request")
                return _public(existing)
            source_hash, parent, cutoff = self._basis(con)
            now = _now(con)
            row = dict(job_id=str(uuid4()), idempotency_key=idempotency_key, payload=payload,
                payload_hash=payload_hash, source_hash=source_hash, parent_revision=parent, event_cutoff=cutoff,
                status="queued", attempt=0, max_attempts=max_attempts, created_at=now, updated_at=now,
                cancel_requested=False, result_revision=None, last_error=None, progress=None, **_CLEAR_LEASE)
            con.execute(insert(s.jobs).values(**row))
            _event(con, row, "SUBMIT", now)
            return _public(row)

    def get(self, job_id, *, include_events=False):
        with self.store.engine.connect() as con:
            result = _public(self._row(con, job_id))
            if include_events:
                result["events"] = [dict(row) for row in con.execute(select(s.job_events).where(
                    s.job_events.c.job_id == job_id).order_by(s.job_events.c.seq)).mappings()]
            return result

    def list_jobs(self, *, limit=100, offset=0, status=None):
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1 to 200 and offset must be nonnegative")
        if status is not None and status not in TERMINAL | {"queued", "running"}:
            raise ValueError("unknown job status")
        statement = select(s.jobs)
        if status is not None:
            statement = statement.where(s.jobs.c.status == status)
        with self.store.engine.connect() as con:
            return [_public(row) for row in con.execute(statement.order_by(
                s.jobs.c.created_at.desc(), s.jobs.c.job_id.desc()).limit(limit).offset(offset)).mappings()]

    def claim(self, owner, *, lease_seconds=60.0):
        lease_seconds = _duration(lease_seconds)
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 100:
            raise ValueError("worker owner must contain 1 to 100 characters")
        with self.store.engine.begin() as con:
            self.store._lock(con)
            now = _now(con)
            rows = con.execute(select(s.jobs).where(or_(s.jobs.c.status == "queued",
                (s.jobs.c.status == "running") & (s.jobs.c.lease_until <= now))).order_by(
                    s.jobs.c.created_at, s.jobs.c.job_id)).mappings().all()
            basis = self._basis(con) if rows else None
            for row in rows:
                if row["cancel_requested"]:
                    self._terminal(con, row, "cancelled", now, "CANCEL")
                    continue
                if not self._same_basis(row, basis):
                    self._terminal(con, row, "failed", now, "FAIL", "stale_basis",
                                   "Job input basis changed; submit a new job")
                    continue
                if row["attempt"] >= row["max_attempts"]:
                    self._terminal(con, row, "failed", now, "FAIL", "attempts_exhausted",
                                   "Worker lease expired and attempt budget is exhausted")
                    continue
                if row["status"] == "running":
                    _event(con, row, "LEASE_EXPIRED", now, {"owner": row["lease_owner"]})
                token = str(uuid4())
                attempt = row["attempt"] + 1
                # The source-basis scan can be slow; grant the lease from fresh DB time.
                now = _now(con)
                until = now + lease_seconds
                con.execute(update(s.jobs).where(s.jobs.c.job_id == row["job_id"]).values(status="running",
                    attempt=attempt, lease_owner=owner, lease_token=token, lease_until=until, updated_at=now,
                    last_error=None, progress=None))
                _event(con, {**row, "attempt": attempt}, "CLAIM", now, {"owner": owner})
                return Lease(row["job_id"], token, owner, attempt, _json_copy(row["payload"]),
                             row["source_hash"], row["parent_revision"], row["event_cutoff"], until)
        return None

    @staticmethod
    def _terminal(con, row, status, now, action, code=None, message=None):
        error = {"code": code, "message": message} if code else None
        con.execute(update(s.jobs).where(s.jobs.c.job_id == row["job_id"]).values(
            status=status, updated_at=now, last_error=error, **_CLEAR_LEASE))
        _event(con, row, action, now, error)

    def check_active(self, lease):
        with self.store.engine.begin() as con:
            self.store._lock(con)
            self._owned(self._row(con, lease.job_id), lease, _now(con))

    def heartbeat(self, lease, *, lease_seconds=60.0, progress=None):
        lease_seconds = _duration(lease_seconds)
        progress = _json_copy(progress) if progress is not None else None
        cancelled = False
        with self.store.engine.begin() as con:
            self.store._lock(con)
            row = self._row(con, lease.job_id)
            if row["status"] == "succeeded":
                return False
            now = _now(con)
            try:
                self._owned(row, lease, now)
            except JobCancelled:
                self._terminal(con, row, "cancelled", now, "CANCEL")
                cancelled = True
            if not cancelled:
                values = {"lease_until": now + lease_seconds, "updated_at": now}
                if progress is not None:
                    values["progress"] = progress
                con.execute(update(s.jobs).where(s.jobs.c.job_id == lease.job_id).values(**values))
        if cancelled:
            raise JobCancelled("Job cancelled")
        return True

    def validate_basis(self, con, lease):
        """Guard a Store transaction with fresh ownership and server-side inputs."""
        self.store._lock(con)
        row = self._row(con, lease.job_id)
        self._owned(row, lease, _now(con))
        if not self._same_basis(row, self._basis(con)):
            raise StaleJob("Job source facts, published parent or review events changed")
        # The scan itself may cross the deadline. A stale holder must not commit.
        self._owned(row, lease, _now(con))

    def bind_prepared(self, con, lease, revision_id):
        """Called inside prepare_revision's final transaction, never afterwards."""
        self.validate_basis(con, lease)
        row = self._row(con, lease.job_id)
        revision = con.execute(select(s.revisions).where(
            s.revisions.c.revision_id == revision_id)).mappings().one()
        if (revision["status"] != "prepared" or revision["input_hash"] != row["source_hash"]
                or revision["parent_revision"] != row["parent_revision"]
                or revision["event_cutoff"] != row["event_cutoff"]):
            raise JobConflict("Prepared revision does not match the job's frozen input basis")
        now = _now(con)
        self._owned(row, lease, now)
        con.execute(update(s.jobs).where(s.jobs.c.job_id == lease.job_id).values(status="succeeded",
            result_revision=revision_id, updated_at=now, last_error=None, **_CLEAR_LEASE))
        _event(con, row, "SUCCEED", now, {"revision_id": revision_id})

    def cancel(self, job_id):
        with self.store.engine.begin() as con:
            self.store._lock(con)
            row = self._row(con, job_id)
            now = _now(con)
            if row["status"] in TERMINAL:
                return _public(row)
            if not row["cancel_requested"]:
                con.execute(update(s.jobs).where(s.jobs.c.job_id == job_id).values(
                    cancel_requested=True, updated_at=now))
                _event(con, row, "CANCEL_REQUEST", now)
            if row["status"] == "queued":
                self._terminal(con, row, "cancelled", now, "CANCEL")
            return _public(self._row(con, job_id))

    def retry(self, job_id):
        with self.store.engine.begin() as con:
            self.store._lock(con)
            row = self._row(con, job_id)
            if row["status"] != "failed" or row["attempt"] >= row["max_attempts"]:
                raise JobConflict("Only failed jobs with remaining attempts can be retried")
            if not self._same_basis(row, self._basis(con)):
                raise StaleJob("A stale job requires a new submission with a new idempotency key")
            now = _now(con)
            con.execute(update(s.jobs).where(s.jobs.c.job_id == job_id).values(status="queued", updated_at=now))
            _event(con, row, "RETRY", now)
            return _public(self._row(con, job_id))

    def fail(self, lease, error, *, code="execution_failed"):
        messages = {
            "execution_failed": "Worker execution failed; verify inputs and configuration before an explicit retry",
            "stale_basis": "Job source facts, published parent or review events changed; submit a new job",
            "missing_atomic_binding": "Execution returned without atomically binding a prepared revision",
        }
        if code not in messages:
            code = "execution_failed"
        with self.store.engine.begin() as con:
            self.store._lock(con)
            row = self._row(con, lease.job_id)
            now = _now(con)
            try:
                self._owned(row, lease, now)
            except JobCancelled:
                self._terminal(con, row, "cancelled", now, "CANCEL")
            else:
                # Exception strings may embed SQL, credentials or source data.
                self._terminal(con, row, "failed", now, "FAIL", code, messages[code])
            return _public(self._row(con, lease.job_id))

    def run_once(self, owner: str, execute: Callable[[Lease], object], *,
                 lease_seconds=60.0, heartbeat_interval=None):
        """Run at most one job; the callback must atomically bind its prepared result.

        Heartbeats cannot interrupt Python computation. Lease checks at both Store
        transaction boundaries fence a worker even when its callback ignores them.
        """
        lease_seconds = _duration(lease_seconds)
        interval = lease_seconds / 3 if heartbeat_interval is None else _duration(heartbeat_interval)
        if interval >= lease_seconds:
            raise ValueError("heartbeat_interval must be shorter than lease_seconds")
        lease = self.claim(owner, lease_seconds=lease_seconds)
        if lease is None:
            return None
        stop = threading.Event()
        failures = []

        def renew():
            while not stop.wait(interval):
                try:
                    if not self.heartbeat(lease, lease_seconds=lease_seconds):
                        return
                except Exception as error:  # noqa: BLE001 - worker boundary records arbitrary callback/DB failures
                    failures.append(error)
                    return

        thread = threading.Thread(target=renew, name="entitybridge-job-heartbeat", daemon=True)
        thread.start()
        try:
            execute(lease)
            current = self.get(lease.job_id)
            if current["status"] != "succeeded":
                if failures:
                    raise failures[0]
                self.fail(lease, "Execution returned without atomically binding a prepared revision",
                          code="missing_atomic_binding")
        except Exception as error:  # noqa: BLE001 - arbitrary user-supplied worker callback boundary
            try:
                self.fail(lease, error, code="stale_basis" if isinstance(error, StaleJob) else "execution_failed")
            except LeaseLost:
                pass  # A terminal result or replacement worker owns durable state.
        finally:
            stop.set()
            thread.join(timeout=min(lease_seconds, 5.0))
        return self.get(lease.job_id)
