from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from entitybridge import schema as s
from entitybridge.api import create_app
from entitybridge.store import Store


def setup_review(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'state.db'}", tmp_path / 'artifacts')
    store.initialize()
    a, b = store.import_records('synthetic', [{'source_key': x, 'name': x} for x in 'ab'])['records']
    base = store.prepare_revision([])['revision_id']
    store.publish(base, expected_parent=None)
    body = {"left": a['record_id'], "right": b['record_id'], "action": 'accept', "reason": 'Synthetic audit',
                "base_revision": base, "left_version": a['record_version_id'], "right_version": b['record_version_id'],
                "policy_version": 'default-v1'}
    return store, body


def test_saved_decision_failure_has_stable_receipt_and_explicit_recovery(tmp_path):
    store, body = setup_review(tmp_path)
    with TestClient(create_app(store, tokens={'token': ('auditor', 'reviewer')}),
                    raise_server_exceptions=False) as client:
        client.headers.update({'Authorization': 'Bearer token', 'Idempotency-Key': 'same-intent'})
        with patch.object(store, 'prepare_revision', side_effect=OSError('private path and credentials')):
            first = client.post('/reviews/decision', json=body)
        assert first.status_code == 202, first.text
        receipt = first.json()
        assert receipt['status'] == 'build_failed'
        assert 'private path' not in first.text
        assert client.post('/reviews/decision', json=body).json() == receipt
        assert client.post('/reviews/decision', json=body | {'reason': 'different'}).status_code == 409
        assert client.get(receipt['status_url']).json() == receipt
        recovered = client.post(receipt['retry_url'])
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()['status'] == 'prepared'
        assert recovered.json()['operation_id'] == receipt['operation_id']
        assert client.post(receipt['retry_url']).json() == recovered.json()
        assert store.current_revision() == body['base_revision']
        with store.engine.connect() as con:
            assert con.execute(select(func.count()).select_from(s.events)).scalar_one() == 1
        store.publish(recovered.json()['revision_id'], expected_parent=body['base_revision'])
        assert len(store.entities()) == 1
    store.engine.dispose()


def test_receipt_permissions_required_keys_ui_and_recovery_switch(tmp_path):
    store, body = setup_review(tmp_path)
    tokens = {'owner': ('owner', 'reviewer'), 'other': ('other', 'reviewer'),
              'viewer': ('viewer', 'viewer'), 'admin': ('admin', 'admin')}
    with TestClient(create_app(store, tokens=tokens)) as client:
        assert client.post('/reviews/decision', json=body).status_code == 401
        client.headers['Authorization'] = 'Bearer owner'
        assert client.post('/reviews/decision', json=body).status_code == 422
        with patch.object(store, 'prepare_revision', side_effect=OSError('injected')):
            result = client.post('/ui/decision', data=body | {'idempotency_key': 'ui-intent'}, follow_redirects=False)
        assert result.status_code == 303
        page = client.get(result.headers['location'])
        assert '判断已保存' in page.text and '候选尚未生成' in page.text
        operation = client.get('/review-operations').json()['items'][0]
        assert 'lease_token' not in operation and 'payload' not in operation
        assert operation['operation_id'] in client.get('/history').text
        client.headers['Authorization'] = 'Bearer other'
        assert client.post(operation['retry_url']).status_code == 403
        client.headers['Authorization'] = 'Bearer viewer'
        assert client.get(operation['status_url']).status_code == 403
        assert client.post(operation['retry_url']).status_code == 403
        client.headers['Authorization'] = 'Bearer admin'
        assert client.post(operation['retry_url']).json()['status'] == 'prepared'
    with TestClient(create_app(store, tokens=tokens, review_recovery_enabled=False)) as disabled:
        disabled.headers['Authorization'] = 'Bearer admin'
        assert disabled.post(operation['retry_url']).status_code == 503
        assert disabled.get(operation['status_url']).status_code == 200
    other_root = tmp_path / 'other'
    other_root.mkdir()
    other_store, _ = setup_review(other_root)
    with TestClient(create_app(other_store, tokens={'admin': ('admin', 'admin')})) as other:
        other.headers['Authorization'] = 'Bearer admin'
        assert other.get(operation['status_url']).status_code == 404
    other_store.engine.dispose()
    store.engine.dispose()
