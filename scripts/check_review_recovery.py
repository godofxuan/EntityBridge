"""Kill an actual child at saved-intent or file/DB boundaries in an isolated DB."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from entitybridge import schema as s
from entitybridge.review_operations import ReviewOperations
from entitybridge.store import Store


def _worker():
    settings = json.loads(sys.stdin.readline())
    store = Store(settings['url'], settings['artifacts'])
    marker = Path(settings['marker'])

    def pause(detail):
        temporary = marker.with_suffix('.partial')
        temporary.write_text(json.dumps(detail), encoding='utf-8')
        temporary.replace(marker)
        threading.Event().wait(40)
        raise RuntimeError('Parent failed to kill child at the declared boundary')

    if settings['boundary'] == 'saved_intent':
        ReviewOperations.retry = lambda _self, identifier: pause({'operation_id': identifier})
    else:
        replace = Path.replace

        def after_replace(path, target):
            result = replace(path, target)
            if path.suffix == '.partial' and Path(target).parent == Path(settings['artifacts']):
                pause({'orphan': Path(target).name})
            return result

        Path.replace = after_replace
    ReviewOperations(store, lease_seconds=1).submit('decision', settings['request'], idempotency_key='crash-intent')


def check_review_recovery(url, directory, *, boundary):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    artifacts, marker = directory / 'artifacts', directory / 'boundary.json'
    store = Store(url, artifacts)
    store.initialize()
    if store.current_revision() is not None or store.active_records():
        raise ValueError('Crash probe requires an empty isolated database')
    a, b = store.import_records('synthetic', [{'source_key': x, 'name': x} for x in 'ab'])['records']
    base = store.prepare_revision([])['revision_id']
    store.publish(base, expected_parent=None)
    request = {'left': a['record_id'], 'right': b['record_id'], 'left_version': a['record_version_id'],
        'right_version': b['record_version_id'], 'base_revision': base, 'action': 'accept', 'reason': 'Synthetic crash probe',
        'reviewer': 'probe', 'policy_version': 'default-v1'}
    settings = {'url': make_url(url).render_as_string(hide_password=False), 'artifacts': str(artifacts),
                'marker': str(marker), 'boundary': boundary, 'request': request}
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        process.stdin.write(json.dumps(settings) + '\n')
        process.stdin.flush()
        deadline = time.monotonic() + 25
        while not marker.exists():
            if process.poll() is not None:
                raise RuntimeError(f'Child exited before boundary (code {process.returncode}; diagnostics suppressed)')
            if time.monotonic() > deadline:
                raise TimeoutError('Child did not reach fault boundary')
            time.sleep(.02)
        process.kill()
        process.wait(timeout=10)
        assert process.returncode != 0
        service = ReviewOperations(store)
        saved = service.list()[0]
        assert saved['status'] == ('accepted' if boundary == 'saved_intent' else 'building')
        assert store.current_revision() == base
        with store.engine.connect() as con:
            assert con.execute(select(func.count()).select_from(s.events)).scalar_one() == 1
            assert con.execute(select(func.count()).select_from(s.revisions)).scalar_one() == 1
        # No forced takeover: wait for the real one-second test lease to expire.
        deadline = time.monotonic() + 5
        while True:
            recovered = service.retry(saved['operation_id'])
            if recovered['status'] != 'building':
                break
            if time.monotonic() > deadline:
                raise TimeoutError('The crash lease did not expire')
            time.sleep(.05)
        assert recovered['status'] == 'prepared'
        assert store.decide(**request, idempotency_key='crash-intent') == recovered
        assert store.current_revision() == base
        store.publish(recovered['revision_id'], expected_parent=base)
        assert len(store.entities()) == 1 and len(store.entities(revision=base)) == 2
        detail = json.loads(marker.read_text(encoding='utf-8'))
        if 'orphan' in detail:
            assert (artifacts / detail['orphan']).is_file()
            assert all(row['artifact_path'] != detail['orphan'] for row in store.history())
        return {'backend': store.engine.dialect.name, 'boundary': boundary, 'child_killed': True,
                'single_saved_event': True, 'no_implicit_publication': True, 'receipt_recovered': True,
                'old_history_readable': True, 'orphan_unregistered': 'orphan' in detail,
                'operation_id': saved['operation_id'], 'candidate_revision': recovered['revision_id']}
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        store.engine.dispose()


if __name__ == '__main__' and '--worker' in sys.argv:
    _worker()
