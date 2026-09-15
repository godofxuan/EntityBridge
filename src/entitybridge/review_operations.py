"""Durable human intent receipts; synchronous derived builds with fenced retries.

The event and receipt share one transaction. Candidate construction is outside
it; a second transaction atomically registers the candidate and its receipt.
No system failure manufactures a human revocation or publishes a revision.
"""

from uuid import uuid4

from sqlalchemy import insert, select, update

from . import schema as s
from .jobs import _duration, _now
from .resolution import Edge, resolve
from .store import VersionConflict, digest, now


class StaleReview(VersionConflict):
    """The receipt's frozen source, parent or event frontier has changed."""


class ReviewLeaseLost(VersionConflict):
    """A previous build cannot bind a result after its lease was replaced."""


def _public(row):
    result = {key: value for key, value in dict(row).items()
              if key not in {'lease_token', 'payload', 'request_hash', 'idempotency_key'}}
    identifier = row['operation_id']
    result.update(revision_id=row['candidate_revision_id'], parent_revision=row['base_revision'],
                  status_url=f'/review-operations/{identifier}',
                  retry_url=f'/review-operations/{identifier}/retry')
    return result


def interrupt_restored_reviews(con):
    """Restored unfinished builds require explicit recovery with a new token."""
    rows = con.execute(select(s.review_operations.c.operation_id).where(
        s.review_operations.c.status.in_(['accepted', 'building']))).scalars().all()
    for identifier in rows:
        con.execute(update(s.review_operations).where(s.review_operations.c.operation_id == identifier).values(
            status='build_failed', lease_token=None, lease_until=None,
            safe_error_code='restore_interrupted', updated_at=_now(con)))
    return len(rows)


def validate_snapshot(tables):
    """Validate receipt/event/result relationships in a portable backup."""
    rows = tables[s.review_operations.name]
    for column in ('idempotency_key', 'event_seq', 'candidate_revision_id'):
        values = [row[column] for row in rows if row[column] is not None]
        if len(values) != len(set(values)):
            raise ValueError('Backup contains duplicate review operation bindings')
    events = {row['seq']: row for row in tables[s.events.name]}
    revisions = {row['revision_id']: row for row in tables[s.revisions.name]}
    for row in rows:
        event, base = events[row['event_seq']], revisions[row['base_revision']]
        if (row['kind'] not in {'decision', 'revoke'} or row['status'] not in {
                'accepted', 'building', 'prepared', 'build_failed', 'stale_basis'}
                or row['request_hash'] != digest({'kind': row['kind'], 'request': row['payload']})
                or event['decision_id'] != row['decision_id'] or event['reviewer'] != row['reviewer']
                or row['payload'].get('reviewer') != row['reviewer']
                or event['reason'] != row['payload'].get('reason')
                or event['action'] != ('CREATE' if row['kind'] == 'decision' else 'REVOKE')
                or base['status'] != 'published' or base['input_hash'] != row['source_hash']
                or base['policy_version'] != row['policy_version'] or row['event_seq'] <= base['event_cutoff']):
            raise ValueError('Backup review receipt disagrees with its saved event or basis')
        if row['status'] == 'prepared':
            candidate = revisions.get(row['candidate_revision_id'])
            if not candidate or (candidate['parent_revision'], candidate['input_hash'], candidate['event_cutoff'],
                                  candidate['policy_version']) != (row['base_revision'], row['source_hash'],
                                                                  row['event_seq'], row['policy_version']):
                raise ValueError('Backup review receipt disagrees with its candidate')
        elif row['candidate_revision_id'] is not None:
            raise ValueError('Unprepared review receipt cannot refer to a candidate')
        if row['status'] == 'building':
            if not row['lease_token'] or row['lease_until'] is None:
                raise ValueError('Building review receipt requires a lease')
        elif row['lease_token'] is not None or row['lease_until'] is not None:
            raise ValueError('Inactive review receipt cannot retain a lease')


class ReviewOperations:
    def __init__(self, store, *, lease_seconds=300):
        self.store = store
        self.lease_seconds = _duration(lease_seconds)

    def _row(self, con, identifier):
        row = con.execute(select(s.review_operations).where(
            s.review_operations.c.operation_id == identifier)).mappings().first()
        if row is None:
            raise KeyError('Review operation not found')
        return row

    def get(self, identifier):
        with self.store.engine.connect() as con:
            return _public(self._row(con, identifier))

    def list(self, *, limit=100):
        if not 1 <= limit <= 200:
            raise ValueError('Operation list limit must be between 1 and 200')
        with self.store.engine.connect() as con:
            return [_public(row) for row in con.execute(select(s.review_operations).order_by(
                s.review_operations.c.created_at.desc(), s.review_operations.c.operation_id).limit(limit)).mappings()]

    def submit(self, kind, request, *, idempotency_key=None):
        request = dict(request)
        if kind not in {'decision', 'revoke'}:
            raise ValueError('Unknown review operation kind')
        if (not isinstance(request.get('reason'), str) or not request['reason'].strip()
                or not isinstance(request.get('reviewer'), str) or not request['reviewer'].strip()):
            raise ValueError('A reason and authenticated reviewer are required')
        request['reason'], request['reviewer'] = request['reason'].strip(), request['reviewer'].strip()
        if kind == 'decision' and request['left'] > request['right']:
            request['left'], request['right'] = request['right'], request['left']
            request['left_version'], request['right_version'] = request['right_version'], request['left_version']
        # Store-only compatibility: callers omitting a key create a new intent.
        # HTTP/UI require a client-retained key and never take this fallback.
        key = str(uuid4()) if idempotency_key is None else idempotency_key
        if not isinstance(key, str) or not 1 <= len(key) <= 200 or key != key.strip():
            raise ValueError('A nonblank idempotency key of at most 200 characters is required')
        request_hash = digest({'kind': kind, 'request': request})
        with self.store.engine.begin() as con:
            self.store._lock(con)
            prior = con.execute(select(s.review_operations).where(
                s.review_operations.c.idempotency_key == key)).mappings().first()
            if prior:
                if prior['request_hash'] != request_hash:
                    raise VersionConflict('Idempotency key already belongs to a different review request')
                return _public(prior)
            if kind == 'decision':
                payload, records, decision_id = self._decision(con, request)
                event_action = 'CREATE'
            else:
                payload, records, decision_id = self._revoke(con, request)
                event_action = 'REVOKE'
            event = con.execute(insert(s.events).values(decision_id=decision_id, action=event_action,
                created_at=now(), reason=request['reason'], reviewer=request['reviewer'],
                policy_version=payload['policy_version'])).inserted_primary_key[0]
            identifier, timestamp = str(uuid4()), _now(con)
            con.execute(insert(s.review_operations).values(operation_id=identifier, idempotency_key=key,
                request_hash=request_hash, payload=request, kind=kind, reviewer=request['reviewer'],
                decision_id=decision_id, event_seq=event, base_revision=request['base_revision'],
                source_hash=digest(records), policy_version=payload['policy_version'], status='accepted',
                attempt=0, created_at=timestamp, updated_at=timestamp))
        return self.retry(identifier)

    def _decision(self, con, request):
        action = request['action']
        if action not in {'accept', 'reject', 'abstain'}:
            raise ValueError('Decision requires accept/reject/abstain')
        payload, records = self.store._check_basis(con, request['base_revision'],
            left=request['left'], right=request['right'], left_version=request['left_version'],
            right_version=request['right_version'], policy=request['policy_version'])
        constraints = self.store._constraints(con, records, request['policy_version'], self.store._event_cutoff(con))
        constraints[{'accept': 'must_link', 'reject': 'cannot_link', 'abstain': 'suppressed'}[action]].append(
            (request['left'], request['right']))
        resolve(records, [Edge(e['left'], e['right'], e['score'], e.get('auto_merge', True)) for e in payload['edges']],
                threshold=payload['threshold'], review_threshold=payload['review_threshold'], **constraints)
        identifier = str(uuid4())
        con.execute(insert(s.decisions).values(decision_id=identifier, left_id=request['left'],
            right_id=request['right'], left_version=request['left_version'], right_version=request['right_version'],
            action=action, reason=request['reason'], reviewer=request['reviewer'],
            base_revision=request['base_revision'], policy_version=request['policy_version']))
        return payload, records, identifier

    def _revoke(self, con, request):
        # The workspace lock is already held. No competing writer can change
        # the preview between these reads and the event/receipt transaction.
        preview = self.store.revoke_preview(request['decision_id'], base_revision=request['base_revision'])
        if request['preview_cutoff'] != preview['event_cutoff']:
            raise VersionConflict('The displayed preview is stale; preview again')
        payload, records = self.store._check_basis(con, request['base_revision'])
        if self.store._event_cutoff(con) != preview['event_cutoff']:
            raise VersionConflict('Decisions changed since preview; preview again')
        return payload, records, request['decision_id']

    def _basis(self, con, row):
        if (self.store.current_revision(con) != row['base_revision']
                or self.store._event_cutoff(con) != row['event_seq']
                or digest(self.store.active_records(con)) != row['source_hash']):
            raise StaleReview('Saved review basis changed; inspect current facts before a new operation')

    def _owned(self, con, identifier, token):
        row = self._row(con, identifier)
        if (row['status'] != 'building' or row['lease_token'] != token
                or row['lease_until'] is None or row['lease_until'] <= _now(con)):
            raise ReviewLeaseLost('Review build lease expired or was replaced')
        return row

    def validate(self, con, identifier, token):
        row = self._owned(con, identifier, token)
        self._basis(con, row)
        return self._owned(con, identifier, token)  # Recheck time after source scan.

    def bind(self, con, identifier, token, revision_id):
        row = self.validate(con, identifier, token)
        revision = con.execute(select(s.revisions).where(s.revisions.c.revision_id == revision_id)).mappings().one()
        if (revision['status'], revision['parent_revision'], revision['input_hash'], revision['event_cutoff'],
                revision['policy_version']) != ('prepared', row['base_revision'], row['source_hash'],
                                                row['event_seq'], row['policy_version']):
            raise StaleReview('Candidate does not match the saved operation')
        con.execute(update(s.review_operations).where(s.review_operations.c.operation_id == identifier).values(
            status='prepared', candidate_revision_id=revision_id, lease_token=None, lease_until=None,
            safe_error_code=None, updated_at=_now(con)))

    def retry(self, identifier):
        with self.store.engine.begin() as con:
            self.store._lock(con)
            row = self._row(con, identifier)
            if row['status'] in {'prepared', 'stale_basis'}:
                return _public(row)
            if row['status'] == 'building' and row['lease_until'] > _now(con):
                return _public(row)
            try:
                self._basis(con, row)
            except StaleReview:
                con.execute(update(s.review_operations).where(s.review_operations.c.operation_id == identifier).values(
                    status='stale_basis', safe_error_code='stale_basis', lease_token=None, lease_until=None,
                    updated_at=_now(con)))
                return _public(self._row(con, identifier))
            token, timestamp = str(uuid4()), _now(con)
            con.execute(update(s.review_operations).where(s.review_operations.c.operation_id == identifier).values(
                status='building', lease_token=token, lease_until=timestamp + self.lease_seconds,
                attempt=row['attempt'] + 1, safe_error_code=None, updated_at=timestamp))
            parent, source_hash = row['base_revision'], row['source_hash']
        try:
            payload = self.store._payload(parent)
            self.store.prepare_revision(payload['edges'], policy_version=payload['policy_version'],
                threshold=payload['threshold'], review_threshold=payload['review_threshold'],
                expected_input_hash=source_hash,
                basis_guard=lambda con: self.validate(con, identifier, token),
                commit_hook=lambda con, revision: self.bind(con, identifier, token, revision))
        except Exception as error:  # noqa: BLE001 -- persist only a safe failure code after saved intent
            # Never persist raw exceptions: DB/file errors may contain secrets.
            with self.store.engine.begin() as con:
                self.store._lock(con)
                current = self._row(con, identifier)
                if current['status'] == 'building' and current['lease_token'] == token:
                    stale = isinstance(error, StaleReview)
                    con.execute(update(s.review_operations).where(s.review_operations.c.operation_id == identifier).values(
                        status='stale_basis' if stale else 'build_failed', lease_token=None, lease_until=None,
                        safe_error_code='stale_basis' if stale else 'candidate_build_failed', updated_at=_now(con)))
        return self.get(identifier)
