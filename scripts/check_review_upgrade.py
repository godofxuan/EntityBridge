"""Exercise old/new installed wheels and a pre-upgrade rollback copy outside checkout."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def child():
    from sqlalchemy import text

    import entitybridge
    from entitybridge.store import Store

    config = json.loads(sys.stdin.readline())
    assert Path(entitybridge.__file__).resolve().is_relative_to(Path(config['installed_root']))
    root = Path(config['directory'])
    store = Store(f"sqlite:///{root / 'state.db'}", root / 'artifacts')
    store.initialize()
    try:
        if config['phase'] == 'old':
            a, b = store.import_records('synthetic-upgrade', [
                {'source_key': x, 'name': x} for x in ('ALPHA', 'BRAVO')])['records']
            base = store.prepare_revision([])['revision_id']
            store.publish(base, expected_parent=None)
            candidate = store.decide(a['record_id'], b['record_id'], action='accept', reason='Synthetic old decision',
                reviewer='upgrade-reviewer', base_revision=base, left_version=a['record_version_id'],
                right_version=b['record_version_id'], policy_version='default-v1')
            store.publish(candidate['revision_id'], expected_parent=base)
        snapshot = {'records': store.active_records(), 'entities': store.entities(),
                    'history': store.decision_history(), 'published': store.current_revision()}
        if config['phase'] in {'new', 'rollback'}:
            assert snapshot == config['before'], 'Upgrade or rollback changed existing state'
        if config['phase'] == 'new':
            from entitybridge.review_operations import ReviewOperations
            assert ReviewOperations(store).list() == [], 'Old decisions must not gain fabricated receipts'
            decision = snapshot['history'][0]['decision_id']
            preview = store.revoke_preview(decision, base_revision=snapshot['published'])
            receipt = store.revoke(decision, base_revision=snapshot['published'], reviewer='upgrade-reviewer',
                reason='Synthetic upgrade correction', preview_cutoff=preview['event_cutoff'], idempotency_key='upgrade-revoke')
            assert receipt['status'] == 'prepared'
            assert store.current_revision() == snapshot['published']
            store.publish(receipt['revision_id'], expected_parent=snapshot['published'])
            assert len(store.entities()) == 2
            assert store.entities(revision=snapshot['published']) == snapshot['entities']
            assert len(ReviewOperations(store).list()) == 1
        with store.engine.connect() as con:
            schema = con.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
        print(json.dumps({'phase': config['phase'], 'version': entitybridge.__version__, 'schema': schema,
                          'snapshot': snapshot, 'checks_passed': True}))
    finally:
        store.engine.dispose()


def run(root, installed_root, phase, before=None):
    config = {'directory': str(root), 'installed_root': str(installed_root), 'phase': phase, 'before': before}
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child'], cwd=root,
        env={**os.environ, 'PYTHONPATH': str(installed_root), 'PYTHONUTF8': '1'}, input=json.dumps(config) + '\n',
        capture_output=True, text=True, encoding='utf-8', timeout=120, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError(f'Installed-wheel {phase} verification failed; private diagnostics withheld')
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-root', type=Path, required=True)
    parser.add_argument('--new-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output must be new')
    with tempfile.TemporaryDirectory(prefix='entitybridge-upgrade-') as directory:
        base = Path(directory)
        active, rollback = base / 'active', base / 'rollback'
        active.mkdir()
        old = run(active, args.old_root.resolve(), 'old')
        assert old['version'] == '0.5.0' and old['schema'] == '0005'
        # Both process and engine have closed; copy DB plus immutable artifacts.
        shutil.copytree(active, rollback)
        upgraded = run(active, args.new_root.resolve(), 'new', old['snapshot'])
        assert upgraded['schema'] == '0006'
        rolled_back = run(rollback, args.old_root.resolve(), 'rollback', old['snapshot'])
        assert rolled_back['schema'] == '0005'
        report = {'scope': 'synthetic SQLite old-wheel migration and pre-upgrade-copy rollback; outside checkout',
                  'old_version': old['version'], 'new_version': upgraded['version'], 'schema': upgraded['schema'],
                  'old_snapshot_sha256': hashlib.sha256(json.dumps(old['snapshot'], sort_keys=True).encode()).hexdigest(),
                  'checks': {'old_data_and_identity_preserved': True, 'old_events_preserved': True,
                    'no_fabricated_old_receipts': True, 'new_revoke_receipt': True, 'explicit_publish': True,
                    'historical_query': True, 'pre_upgrade_backup_rollback': True}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    if '--child' in sys.argv:
        child()
    else:
        main()
