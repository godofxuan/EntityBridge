"""The same real transaction and concurrency cases on SQLite and PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, select, update
from test_recovery import isolated_database  # noqa: F401

from entitybridge import schema as s
from entitybridge.operations import create_backup, restore_backup
from entitybridge.review_operations import ReviewOperations
from entitybridge.store import Store, VersionConflict


@pytest.fixture
def review_store(isolated_database, tmp_path):  # noqa: F811
    _engine, url = isolated_database
    store = Store(url, tmp_path / 'artifacts')
    store.initialize()
    a, b, _c = store.import_records('synthetic', [{'source_key': x, 'name': x} for x in 'abc'])['records']
    base = store.prepare_revision([])['revision_id']
    store.publish(base, expected_parent=None)
    request = {'left': a['record_id'], 'right': b['record_id'], 'action': 'accept', 'reason': 'Synthetic fixture',
                   'reviewer': 'tester', 'base_revision': base, 'left_version': a['record_version_id'],
                   'right_version': b['record_version_id'], 'policy_version': 'default-v1'}
    yield store, request
    store.engine.dispose()


def counts(store):
    with store.engine.connect() as con:
        return tuple(con.execute(select(func.count()).select_from(table)).scalar_one()
                     for table in (s.decisions, s.events, s.review_operations, s.revisions))


@pytest.mark.parametrize('action', ['accept', 'reject', 'abstain'])
def test_atomic_decision_replay_and_revocation(review_store, action):
    store, request = review_store
    request['action'] = action
    first = store.decide(**request, idempotency_key='decision')
    assert first['status'] == 'prepared'
    assert counts(store) == (1, 1, 1, 2)
    reverse = request | {'left': request['right'], 'right': request['left'],
                             'left_version': request['right_version'], 'right_version': request['left_version']}
    assert store.decide(**reverse, idempotency_key='decision') == first
    with pytest.raises(VersionConflict):
        store.decide(**(request | {'reviewer': 'other'}), idempotency_key='decision')
    store.publish(first['revision_id'], expected_parent=request['base_revision'])
    preview = store.revoke_preview(first['decision_id'], base_revision=first['revision_id'])
    revoke = {'base_revision': first['revision_id'], 'reviewer': 'tester', 'reason': 'Synthetic correction',
                  'preview_cutoff': preview['event_cutoff'], 'idempotency_key': 'revoke'}
    with patch.object(store, 'prepare_revision', side_effect=OSError('injected')):
        failed = store.revoke(first['decision_id'], **revoke)
    assert failed['status'] == 'build_failed'
    assert store.revoke(first['decision_id'], **revoke) == failed
    recovered = ReviewOperations(store).retry(failed['operation_id'])
    assert recovered['status'] == 'prepared'
    assert counts(store) == (1, 2, 2, 3)
    assert store.current_revision() == first['revision_id']
    store.publish(recovered['revision_id'], expected_parent=first['revision_id'])


@pytest.mark.parametrize('boundary', ['receipt_insert', 'file_write', 'before_rename', 'after_rename', 'after_bind'])
def test_fault_boundaries_preserve_saved_intent_and_publication(review_store, boundary):
    store, request = review_store
    original_replace, original_bind = Path.replace, ReviewOperations.bind

    def fail_insert(con, cursor, statement, *args):
        if statement.lstrip().upper().startswith('INSERT INTO REVIEW_OPERATION'):
            raise RuntimeError('injected transaction fault')

    def replace(path, target):
        if path.suffix == '.partial':
            if boundary == 'after_rename':
                original_replace(path, target)
            raise OSError('injected file fault')
        return original_replace(path, target)

    def bind(service, *args):
        original_bind(service, *args)
        raise RuntimeError('injected after bind before commit')

    if boundary == 'receipt_insert':
        event.listen(store.engine, 'before_cursor_execute', fail_insert)
        try:
            with pytest.raises(RuntimeError, match='transaction fault'):
                store.decide(**request, idempotency_key='fault')
        finally:
            event.remove(store.engine, 'before_cursor_execute', fail_insert)
        assert counts(store) == (0, 0, 0, 1)
        recovered = store.decide(**request, idempotency_key='fault')
    else:
        target, replacement = (('entitybridge.store.os.fsync', OSError('injected write fault'))
                               if boundary == 'file_write' else
                               ('entitybridge.review_operations.ReviewOperations.bind', bind)
                               if boundary == 'after_bind' else ('pathlib.Path.replace', replace))
        options = {'side_effect': replacement} if isinstance(replacement, Exception) else {'new': replacement}
        with patch(target, **options):
            failed = store.decide(**request, idempotency_key='fault')
        assert failed['status'] == 'build_failed'
        assert counts(store) == (1, 1, 1, 1)
        recovered = ReviewOperations(store).retry(failed['operation_id'])
    assert recovered['status'] == 'prepared'
    assert counts(store) == (1, 1, 1, 2)
    assert store.current_revision() == request['base_revision']


def test_concurrent_same_key_and_conflicting_reviewers(review_store):
    store, request = review_store
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: store.decide(**request, idempotency_key='same'), range(2)))
    assert len({result['operation_id'] for result in results}) == 1
    assert counts(store) == (1, 1, 1, 2)
    with pytest.raises(ValueError):
        store.decide(**(request | {'action': 'reject', 'reviewer': 'second'}), idempotency_key='contradict')
    assert counts(store) == (1, 1, 1, 2)


def test_two_recoverers_and_old_execution_cannot_bind_over_new_result(review_store):
    store, request = review_store
    with patch.object(store, 'prepare_revision', side_effect=OSError('injected')):
        failed = store.decide(**request, idempotency_key='recover')
    service, entered, release = ReviewOperations(store), Event(), Event()
    original = store.prepare_revision

    def pause_first(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return original(*args, **kwargs)

    with patch.object(store, 'prepare_revision', side_effect=pause_first), ThreadPoolExecutor(1) as pool:
        old = pool.submit(service.retry, failed['operation_id'])
        try:
            assert entered.wait(5)
            with store.engine.begin() as con:
                store._lock(con)
                con.execute(update(s.review_operations).where(
                    s.review_operations.c.operation_id == failed['operation_id']).values(lease_until=0))
            new = service.retry(failed['operation_id'])
            assert new['status'] == 'prepared'
        finally:
            release.set()
        assert old.result()['candidate_revision_id'] == new['candidate_revision_id']
    assert counts(store) == (1, 1, 1, 2)
    assert service.get(failed['operation_id'])['attempt'] == 3


@pytest.mark.parametrize('change', ['source', 'event', 'publish'])
def test_recovery_refuses_changed_frozen_basis(review_store, change):
    store, request = review_store
    with patch.object(store, 'prepare_revision', side_effect=OSError('injected')):
        failed = store.decide(**request, idempotency_key='frozen')
    if change == 'source':
        store.import_records('synthetic', [{'source_key': 'c', 'name': 'changed'}])
    elif change == 'event':
        store.decide(**(request | {'reason': 'another reviewer intent'}), idempotency_key='next')
    else:
        revision = store.prepare_revision([])['revision_id']
        store.publish(revision, expected_parent=request['base_revision'])
    result = ReviewOperations(store).retry(failed['operation_id'])
    assert result['status'] == 'stale_basis' and result['candidate_revision_id'] is None
    assert store.decide(**request, idempotency_key='frozen') == result


def test_backup_restores_receipts_and_interrupts_inflight_builds(review_store, tmp_path):
    store, request = review_store
    with patch.object(store, 'prepare_revision', side_effect=OSError('injected')):
        saved = store.decide(**request, idempotency_key='restore')
    with store.engine.begin() as con:
        con.execute(update(s.review_operations).values(status='building', lease_token='old', lease_until=9999999999.0))
    backup = tmp_path / 'backup'
    create_backup(store, backup)
    url, artifacts = f"sqlite:///{tmp_path / 'restored.db'}", tmp_path / 'restored_artifacts'
    restored = restore_backup(backup, url, artifacts)
    assert restored['recovery_interrupted_reviews'] == 1
    target = Store(url, artifacts)
    try:
        receipt = ReviewOperations(target).get(saved['operation_id'])
        assert receipt['status'] == 'build_failed' and receipt['safe_error_code'] == 'restore_interrupted'
        assert target.decision_history() == store.decision_history()
        assert ReviewOperations(target).retry(saved['operation_id'])['status'] == 'prepared'
        assert target.current_revision() == request['base_revision']
    finally:
        target.engine.dispose()


def test_source_changes_after_artifact_write_cannot_register_candidate(review_store):
    store, request = review_store
    original_replace = Path.replace

    def replace(path, target):
        result = original_replace(path, target)
        if path.suffix == '.partial':
            store.import_records('synthetic', [{'source_key': 'c', 'name': 'changed during build'}])
        return result

    with patch('pathlib.Path.replace', new=replace):
        receipt = store.decide(**request, idempotency_key='late-source-change')
    assert receipt['status'] == 'stale_basis'
    assert receipt['candidate_revision_id'] is None
    assert counts(store) == (1, 1, 1, 1)
    assert store.current_revision() == request['base_revision']


def test_backup_rejects_receipt_request_corruption(review_store, tmp_path):
    store, request = review_store
    store.decide(**request, idempotency_key='corrupt-backup')
    with store.engine.begin() as con:
        con.execute(update(s.review_operations).values(request_hash='corrupted'))
    with pytest.raises(ValueError, match='receipt disagrees'):
        create_backup(store, tmp_path / 'bad-backup')


@pytest.mark.parametrize('boundary', ['saved_intent', 'after_rename'])
def test_actual_process_kill_recovers_saved_review(isolated_database, tmp_path, boundary):  # noqa: F811
    import importlib.util
    path = Path(__file__).resolve().parents[1] / 'scripts/check_review_recovery.py'
    spec = importlib.util.spec_from_file_location('review_crash', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _engine, url = isolated_database
    result = module.check_review_recovery(url, tmp_path / boundary, boundary=boundary)
    assert result['child_killed'] and result['single_saved_event'] and result['receipt_recovered']
