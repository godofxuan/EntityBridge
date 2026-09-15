"""Authenticated HTTP + independent CLI worker workflow, optionally from an installed wheel.

Synthetic governance/capacity evidence only. PostgreSQL uses a disposable schema
in an explicit _test database. Child connection settings travel through stdin or
environment, never command arguments or the evidence transcript.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from entitybridge.operations import create_backup, restore_backup
from entitybridge.store import Store, digest
from entitybridge.workspace_binding import bind_workspace

ROOT = Path(__file__).resolve().parents[1]


def server():
    import uvicorn

    import entitybridge
    from entitybridge.api import create_app
    config = json.loads(sys.stdin.readline())
    if config.get('installed_root'):
        assert Path(entitybridge.__file__).resolve().is_relative_to(Path(config['installed_root']).resolve())
    store = Store(config['url'], config['artifacts'])
    store.initialize()
    bind_workspace(store, 'synthetic-company-workflow')
    original = store.prepare_revision
    fault = Path(config['fault'])

    def prepare(*args, **kwargs):
        if fault.exists():
            fault.unlink()
            raise OSError('injected candidate persistence failure')
        return original(*args, **kwargs)

    store.prepare_revision = prepare
    app = create_app(store, tokens=config['tokens'])
    uvicorn.run(app, host='127.0.0.1', port=config['port'], access_log=False, log_level='error')


def process_tree_rss(pid):
    """Sample RSS of the launcher and descendants; not unique/PSS memory."""
    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.Error:
        return 0
    total = 0
    for process in processes:
        try:
            total += process.memory_info().rss
        except psutil.Error:
            pass
    return total


def worker(environment, cwd):
    started = time.perf_counter()
    process = subprocess.Popen([sys.executable, '-m', 'entitybridge.cli', 'worker', '--once',
                                '--artifact-root', environment['ENTITYBRIDGE_PROBE_ARTIFACTS']],
        cwd=cwd, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    peak = 0
    while process.poll() is None:
        peak = max(peak, process_tree_rss(process.pid))
        if time.perf_counter() - started > 180:
            process.kill()
            process.communicate()
            raise TimeoutError('Independent worker exceeded 180 seconds')
        time.sleep(.01)
    stdout, _stderr = process.communicate()
    if process.returncode:
        raise RuntimeError(f'Worker failed ({process.returncode}); diagnostics withheld')
    return json.loads(stdout.decode('utf-8')), {'wall_seconds': time.perf_counter() - started,
                                               'peak_process_tree_rss_bytes_sampled': peak}


class HTTPWorkspace:
    def __init__(self, url, directory, installed_root):
        self.directory, self.url = Path(directory), url
        self.artifacts, self.fault = self.directory / 'artifacts', self.directory / 'fail-next-prepare'
        self.environment = {**os.environ, 'DATABASE_URL': make_url(url).render_as_string(hide_password=False),
                            'PYTHONUTF8': '1'}
        # CLI artifact root is a nonsecret explicit argument in its working directory.
        self.environment['PYTHONPATH'] = str(installed_root or ROOT / 'src')
        self.admin, self.reviewer, self.viewer = (secrets.token_urlsafe(24) for _ in range(3))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        config = {'url': self.environment['DATABASE_URL'], 'artifacts': str(self.artifacts),
                  'fault': str(self.fault), 'port': port, 'installed_root': str(installed_root) if installed_root else None,
                  'tokens': {self.admin: ['administrator', 'admin'], self.reviewer: ['reviewer', 'reviewer'],
                             self.viewer: ['viewer', 'viewer']}}
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--server'],
            cwd=directory, env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.process.stdin.write(json.dumps(config) + '\n')
        self.process.stdin.flush()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=120)
        self.trace = []
        deadline = time.monotonic() + 30
        while True:
            try:
                if self.client.get('/health').status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if self.process.poll() is not None or time.monotonic() > deadline:
                self.close()
                raise RuntimeError('HTTP server did not become ready; private diagnostics withheld')
            time.sleep(.05)
        self.store = Store(url, self.artifacts)

    def request(self, method, path, body=None, *, role='admin', expected=200, key=None):
        token = {'admin': self.admin, 'reviewer': self.reviewer, 'viewer': self.viewer, 'invalid': 'invalid'}[role]
        headers = {'Authorization': 'Bearer ' + token}
        if key:
            headers['Idempotency-Key'] = key
        response = self.client.request(method, path, json=body, headers=headers)
        try:
            content = response.json()
        except ValueError:
            content = {'body_sha256': hashlib.sha256(response.content).hexdigest(), 'bytes': len(response.content)}
        self.trace.append({'method': method, 'path': path, 'role': role, 'status': response.status_code,
                           'response': content})
        assert response.status_code == expected, f'{method} {path}: expected {expected}, got {response.status_code}'
        return content

    def match(self, *, method='fuzzy', threshold=1.0, review_threshold=.2):
        job = self.request('POST', '/jobs', {'idempotency_key': str(uuid4()), 'settings': {
            'method': method, 'threshold': threshold, 'review_threshold': review_threshold, 'incremental': False}}, expected=202)
        # The real CLI uses cwd/artifacts/revisions by default. Supply the
        # nonsecret artifact-root explicitly instead of bypassing its entrypoint.
        environment = dict(self.environment)
        result, measure = run_worker(environment, self.directory, self.artifacts)
        assert result['status'] == 'succeeded', result.get('last_error')
        assert self.store.current_revision() == job['parent_revision']
        current = self.request('GET', '/jobs/' + job['job_id'])
        return current['result_revision'], measure, current

    def publish(self, revision, parent):
        self.request('POST', f'/revisions/{revision}/publish', {'expected_parent': parent})

    def close(self):
        if getattr(self, 'client', None):
            self.client.close()
        if getattr(self, 'store', None):
            self.store.engine.dispose()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()


def run_worker(environment, directory, artifacts):
    # Keep worker() a reusable process sampler; the CLI is selected through a
    # short wrapper environment value consumed only by the helper below.
    environment = dict(environment)
    environment['ENTITYBRIDGE_PROBE_ARTIFACTS'] = str(artifacts)
    return worker(environment, directory)


def workflow(workspace, fixture):
    rows = {}
    for source in fixture['sources']:
        imported = workspace.request('POST', '/imports', source)
        assert workspace.request('POST', '/imports', source) == imported
        for definition, identity in zip(source['rows'], imported['records'], strict=True):
            rows[source['source'] + ':' + definition['source_key']] = identity
    initial, measure, _ = workspace.match()
    assert workspace.request('GET', '/entities') == []
    workspace.publish(initial, None)
    workspace.request('GET', '/review-operations', role='viewer', expected=403)
    workspace.request('GET', '/entities', role='invalid', expected=401)

    def decision(pair, action, parent, key, expected=200):
        left, right = (rows[name] for name in pair)
        body = {'left': left['record_id'], 'right': right['record_id'], 'action': action,
                'reason': 'Synthetic exercise: ' + action, 'base_revision': parent,
                'left_version': left['record_version_id'], 'right_version': right['record_version_id'],
                'policy_version': workspace.store._payload(parent)['policy_version']}
        return workspace.request('POST', '/reviews/decision', body, role='reviewer', expected=expected, key=key), body

    rejected, _ = decision(fixture['scenario']['different'], 'reject', initial, 'different')
    workspace.publish(rejected['revision_id'], initial)
    workspace.fault.write_text('fail once', encoding='utf-8')
    accepted, body = decision(fixture['scenario']['same'], 'accept', rejected['revision_id'], 'same', expected=202)
    assert accepted['status'] == 'build_failed'
    assert workspace.request('POST', '/reviews/decision', body, role='reviewer', expected=202, key='same') == accepted
    workspace.request('GET', accepted['status_url'], role='reviewer')
    accepted = workspace.request('POST', accepted['retry_url'], role='reviewer')
    workspace.request('GET', '/review-operation/' + accepted['operation_id'], role='reviewer')
    workspace.publish(accepted['revision_id'], rejected['revision_id'])
    before = len(workspace.store.decision_history())
    decision(fixture['scenario']['forbidden_transitive'], 'accept', accepted['revision_id'], 'forbidden', expected=422)
    assert len(workspace.store.decision_history()) == before
    same_ids = {rows[name]['record_id'] for name in fixture['scenario']['same']}
    merged = next(entity['entity_id'] for entity in workspace.request('GET', '/entities') if set(entity['members']) == same_ids)
    update_source, update_key = fixture['scenario']['attribute_update'].split(':')
    update_row = next(row for source in fixture['sources'] if source['source'] == update_source
                      for row in source['rows'] if row['source_key'] == update_key)
    previous_ids = {tuple(item['members']): item['entity_id'] for item in workspace.request('GET', '/entities')}
    workspace.request('POST', '/imports', {'source': update_source, 'rows': [update_row | {'address': '30 NEW SILVER ROAD'}]})
    changed, _, _ = workspace.match()
    workspace.publish(changed, accepted['revision_id'])
    assert previous_ids == {tuple(item['members']): item['entity_id'] for item in workspace.request('GET', '/entities')}
    preview = workspace.request('POST', f"/decisions/{accepted['decision_id']}/revoke-preview", {'base_revision': changed}, role='reviewer')
    revoked = workspace.request('POST', f"/decisions/{accepted['decision_id']}/revoke",
        {'base_revision': changed, 'reason': 'Synthetic correction: withdraw support', 'preview_cutoff': preview['event_cutoff']},
        role='reviewer', key='revoke')
    workspace.request('POST', f"/revisions/{revoked['revision_id']}/publish", {'expected_parent': initial}, expected=409)
    workspace.publish(revoked['revision_id'], changed)
    retired = workspace.request('GET', f'/entities/{merged}')
    assert retired['status'] == 'retired' and len(retired['destinations']) == 2
    assert len(workspace.request('GET', f'/entities/{merged}?revision={accepted["revision_id"]}')['members']) == 2
    for page in ('/', '/reviews', '/history', '/tasks', '/exports?revision=' + revoked['revision_id']):
        workspace.request('GET', page)
    workspace.request('GET', '/exports?revision=missing', expected=404)
    backup = workspace.directory / 'backup'
    snapshot = create_backup(workspace.store, backup)
    base_url = make_url(workspace.url)
    if base_url.get_backend_name() == 'sqlite':
        target_url = URL.create('sqlite', database=str(workspace.directory / 'restored.db'))
    else:
        target_url = base_url.difference_update_query(['options'])
    restored = restore_backup(backup, target_url, workspace.directory / 'restored-artifacts')
    actual_url = target_url.update_query_dict({'options': '-csearch_path=' + restored['postgres_schema']}) if restored['postgres_schema'] else target_url
    target = Store(actual_url, workspace.directory / 'restored-artifacts')
    try:
        assert target.entities() == workspace.store.entities()
        assert target.decision_history() == workspace.store.decision_history()
        from entitybridge.review_operations import ReviewOperations
        assert ReviewOperations(target).list() == ReviewOperations(workspace.store).list()
    finally:
        target.engine.dispose()
        if restored['postgres_schema']:
            cleanup_schema(target_url, restored['postgres_schema'])
    return {'checks': {'import_idempotency': True, 'real_http_authentication': True, 'independent_cli_worker': True,
                       'saved_intent_retry': True, 'transitive_conflict_rejected': True, 'attribute_change_keeps_id': True,
                       'explicit_publish_conflict': True, 'revoke_splits_and_preserves_history': True,
                       'backup_receipts_and_queries': True},
            'initial_worker_measurement': measure, 'initial': initial, 'current': revoked['revision_id'],
            'merged_historical_id': merged, 'current_destinations': retired['destinations'],
            'backup_summary': snapshot['summary'], 'restore': restored, 'http_transcript': workspace.trace}


def cleanup_schema(url, schema):
    assert schema.startswith(('workflow_', 'restore_')) and schema.replace('_', '').isalnum()
    engine = create_engine(url)
    try:
        with engine.begin() as con:
            con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()


def capacity(workspace, size):
    """One warmup + three measured full runs; equal one-pair buckets at all sizes."""
    import_seconds = 0
    source_hashes = {}
    for source in ('capacity_a', 'capacity_b'):
        rows = [{'source_key': str(i), 'name': f'{i:08X} SUPPLY LIMITED', 'country': 'GB',
                 'postcode': f'P{i:07}', 'address': f'{i} SYNTHETIC ROAD'} for i in range(size // 2)]
        source_hashes[source] = digest(rows)
        started = time.perf_counter()
        workspace.request('POST', '/imports', {'source': source, 'rows': rows})
        import_seconds += time.perf_counter() - started
    observations = []
    parent = None
    for repeat in range(4):
        started = time.perf_counter()
        revision, measure, job = workspace.match(method='exact', threshold=1.0, review_threshold=.5)
        workspace.publish(revision, parent)
        elapsed = time.perf_counter() - started
        payload = workspace.store._payload(revision)
        assert len(payload['records']) == size and len(payload['edges']) == size // 2
        assert max(len(item['members']) for item in payload['entities'].values()) == 2
        times = []
        for _ in range(30):
            tick = time.perf_counter()
            workspace.request('GET', f'/entities?revision={revision}&limit=50')
            times.append((time.perf_counter() - tick) * 1000)
        claim = next(event['created_at'] for event in job['events'] if event['action'] == 'CLAIM')
        ordered = sorted(times)
        observations.append({'repeat': repeat, 'warmup': repeat == 0, 'revision': revision,
            'records': size, 'candidate_pairs': len(payload['edges']), 'max_cluster_size': 2,
            'enqueue_worker_publish_wall_seconds': elapsed, **measure,
            'queue_wait_seconds_including_worker_startup': claim - job['created_at'],
            'query_p50_ms': ordered[math.ceil(.5 * len(times)) - 1],
            'query_p95_ms': ordered[math.ceil(.95 * len(times)) - 1], 'query_times_ms': times})
        parent = revision
    return {'scope': 'fixed synthetic one-pair candidate buckets; no concurrency or production SLA inference',
            'dataset': {'generator': 'one-pair-buckets-v1', 'records': size, 'source_sha256': source_hashes},
            'import_http_seconds': import_seconds, 'observations': observations,
            'lock_wait_seconds': None, 'lock_wait_note': 'No separate lock-wait instrumentation; queue wait measured',
            'memory_scope': '10ms sampled sum of launcher/descendant RSS; shared pages may be counted more than once; excludes separate API/DB processes',
            'cpu': platform.processor(), 'logical_cpus': psutil.cpu_count(),
            'memory_total_bytes': psutil.virtual_memory().total, 'python': platform.python_version()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--installed-root', type=Path)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--capacity-size', type=int, choices=[100, 1000, 10000])
    args = parser.parse_args()
    import entitybridge
    if args.installed_root and not Path(entitybridge.__file__).resolve().is_relative_to(args.installed_root.resolve()):
        parser.error('Set PYTHONPATH to installed-root so the entire harness uses the installed wheel')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixture = json.loads((ROOT / 'examples/company_workflow/sources.json').read_text(encoding='utf-8'))
    admin_url = None
    if args.postgres:
        if os.environ.get('ENTITYBRIDGE_TEST_DATABASE_URL'):
            admin_url = make_url(os.environ['ENTITYBRIDGE_TEST_DATABASE_URL'])
        else:
            config = json.loads((ROOT / '.tools/database.json').read_text(encoding='utf-8'))
            admin_url = URL.create('postgresql+psycopg', username=config['user'], password=config['password'],
                host=config['host'], port=config['port'], database='entitybridge_test', query={'connect_timeout': '8'})
        if not (admin_url.database or '').endswith('_test') or 'options' in admin_url.query:
            raise ValueError('Workflow requires an explicit _test database without schema overrides')
    with tempfile.TemporaryDirectory(prefix='entitybridge-workflow-') as temporary:
        schema = None
        if admin_url:
            schema = 'workflow_' + uuid4().hex
            engine = create_engine(admin_url)
            with engine.begin() as con:
                con.execute(text(f'CREATE SCHEMA "{schema}"'))
            engine.dispose()
            url = admin_url.update_query_dict({'options': '-csearch_path=' + schema})
        else:
            url = URL.create('sqlite', database=str(Path(temporary) / 'state.db'))
        workspace = None
        try:
            workspace = HTTPWorkspace(url, temporary, args.installed_root.resolve() if args.installed_root else None)
            report = capacity(workspace, args.capacity_size) if args.capacity_size else workflow(workspace, fixture)
            if not args.capacity_size:
                report.update(scope='synthetic automated HTTP replay; not human user acceptance or business quality',
                              dataset={'fixture_sha256': digest(fixture), 'records': 6})
            report.update(backend=make_url(url).get_backend_name(), package_version=entitybridge.__version__,
                          installed_wheel=bool(args.installed_root), execution_cwd_outside_source=True)
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'backend': report['backend'], 'checks': report.get('checks'),
                              'capacity_size': args.capacity_size, 'report': str(output / 'report.json')}))
        finally:
            if workspace:
                (output / 'http_trace.json').write_text(json.dumps(workspace.trace, ensure_ascii=False, indent=2), encoding='utf-8')
                workspace.close()
            if schema:
                cleanup_schema(admin_url, schema)


if __name__ == '__main__':
    if '--server' in sys.argv:
        server()
    else:
        try:
            main()
        except SQLAlchemyError:
            raise SystemExit('Database validation failed; connection details withheld') from None
