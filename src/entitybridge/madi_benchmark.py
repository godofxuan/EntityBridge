"""Published company labels with protected official test entities and explicit quarantine.

The adapter never derives labels from names or URLs. It uses known-positive
components for split isolation, not as an exhaustive real-world truth map.
"""

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pyarrow as pa
import pyarrow.parquet as pq

from .benchmarks import GROUP_COLUMNS, LABEL_COLUMNS, SPLITS, _Components, _split, file_hash, verify_benchmark
from .normalization import MATCHER_COLUMNS, normalize_text
from .store import digest

VERSION = 'madi-protected-test-positive-components-v1'
SOURCE_IDS = {'forbes': 'forbes_url', 'dbpedia': 'entity_uri', 'fullcontact': 'Attribute_1'}
PROJECTION = {'forbes': {'name': 'company', 'country': 'region'},
              'dbpedia': {'name': 'org_name', 'country': 'nation', 'city': 'headquarters'},
              'fullcontact': {'name': 'Attribute_2', 'country': 'Attribute_3', 'city': 'Attribute_4'}}
SOURCE_PAIRS = (('forbes', 'dbpedia'), ('forbes', 'fullcontact'))


def opaque(*parts):
    return str(uuid5(NAMESPACE_URL, digest(['entitybridge-madi-v1', *parts])))


def adapt_sources(tables, labels, *, seed=20260915):
    if set(tables) != set(SOURCE_IDS):
        raise ValueError('All three declared company sources are required')
    records, identifiers = {}, {}
    for source, rows in tables.items():
        for original in rows:
            key = original.get(SOURCE_IDS[source])
            if not isinstance(key, str) or not key or (source, key) in identifiers:
                raise ValueError('Source identifiers must be present and unique')
            identifier = opaque('record', source, key)
            identifiers[source, key] = identifier
            projected = {field: normalize_text(original.get(column)) for field, column in PROJECTION[source].items()}
            row = dict.fromkeys(MATCHER_COLUMNS)
            row.update(projected, record_id=identifier, source=source,
                       record_version_id=opaque('version', identifier, projected))
            records[identifier] = row
    components = _Components(records)
    valid, missing, original_counts, record_splits = [], [], defaultdict(Counter), defaultdict(set)
    for index, original in enumerate(labels):
        split, label = original['original_split'], original['label']
        if split not in SPLITS or type(label) is not int or label not in (0, 1):
            raise ValueError('Explicit binary labels and a declared original split are required')
        if (original['left_source'], original['right_source']) not in SOURCE_PAIRS:
            raise ValueError('Unknown company source pair')
        original_counts[split][str(label)] += 1
        a = identifiers.get((original['left_source'], original['left_id']))
        b = identifiers.get((original['right_source'], original['right_id']))
        if a is None or b is None:
            missing.append({'index': index, 'original_split': split, 'label': label, 'row_sha256': digest(original)})
            continue
        a, b = sorted((a, b))
        row = {'left_id': a, 'right_id': b, 'label': label, 'original_split': split}
        valid.append(row)
        record_splits[a].add(split)
        record_splits[b].add(split)
        if label:
            components.union(a, b)
    contradictions = [r for r in valid if not r['label'] and components.find(r['left_id']) == components.find(r['right_id'])]
    bad_groups = {components.find(r['left_id']) for r in contradictions}
    bad_records = {key for key in records if components.find(key) in bad_groups}
    quarantine = [r for r in valid if {r['left_id'], r['right_id']} & bad_records]
    clean = [r for r in valid if not {r['left_id'], r['right_id']} & bad_records]
    unique = {}
    priority = {'train': 0, 'validation': 1, 'test': 2}
    for row in clean:
        pair = row['left_id'], row['right_id']
        old = unique.get(pair)
        if old and old['label'] != row['label']:
            raise ValueError('Unresolved contradictory labels')
        if old is None or priority[row['original_split']] > priority[old['original_split']]:
            unique[pair] = row
    retained, occupied, assignment = [], set(), {}
    for split in ('test', 'validation', 'train'):
        selected = [r for r in unique.values() if r['original_split'] == split and not any(
            components.find(r[key]) in occupied for key in ('left_id', 'right_id'))]
        groups = {components.find(r[key]) for r in selected for key in ('left_id', 'right_id')}
        assignment.update(dict.fromkeys(groups, split))
        occupied.update(groups)
        retained.extend(r | {'split': split} for r in selected)
    for key in bad_records:
        records.pop(key)
    groups = {key: opaque('entity-group', components.find(key)) for key in records}
    record_split = {key: assignment.get(components.find(key), _split(groups[key], seed)) for key in records}
    dependencies = _Components(records)
    # Known positive relations remain dependent even when a redundant original
    # training pair was purged because its entity belongs to the protected test.
    for row in clean:
        if row['label']:
            dependencies.union(row['left_id'], row['right_id'])
    for row in retained:
        dependencies.union(row['left_id'], row['right_id'])
    for row in retained:
        a, b = row['left_id'], row['right_id']
        row.update(left_group_id=groups[a], right_group_id=groups[b],
                   group_id=opaque('dependency-group', dependencies.find(a)))
    retained.sort(key=lambda r: (r['split'], r['left_id'], r['right_id']))
    statistics = {'original_records': len(identifiers), 'records': len(records), 'original_label_counts': dict(original_counts),
        'original_records_in_multiple_splits': sum(len(value) > 1 for value in record_splits.values()),
        'missing_endpoint_pairs': missing, 'positive_closure_contradictions': len(contradictions),
        'quarantined_records': len(bad_records), 'quarantined_labels_by_original_split': dict(Counter(r['original_split'] for r in quarantine)),
        'duplicate_pairs_collapsed': len(clean) - len(unique),
        'retained': {split: {'records': sum(value == split for value in record_split.values()),
            'labels': dict(Counter(str(r['label']) for r in retained if r['split'] == split)),
            'label_pairs': sum(r['split'] == split for r in retained)} for split in SPLITS},
        'purged_for_split_isolation': len(unique) - len(retained),
        'quarantine_rule': 'bad positive components and all incident pairs, before fitting; no label relabelling'}
    return {'records': records, 'labels': retained, 'record_groups': [
        {'record_id': key, 'entity_group_id': groups[key], 'split': record_split[key]} for key in sorted(records)],
        'statistics': statistics}


def prepare_dataset(raw, output, snapshot, *, seed=20260915):
    raw, output = Path(raw), Path(output)
    if output.exists():
        raise ValueError('MADI dataset output must be new')
    for relative, information in snapshot['files'].items():
        path = (raw / relative).resolve()
        if not path.is_relative_to(raw.resolve()) or file_hash(path) != information['sha256']:
            raise ValueError('Pinned MADI source hash/path mismatch')
    tables = {}
    for source in SOURCE_IDS:
        with (raw / 'data' / f'{source}.csv').open(encoding='utf-8-sig', newline='') as stream:
            tables[source] = list(csv.DictReader(stream))
    labels = []
    for a, b in SOURCE_PAIRS:
        for split, suffix in [('train', 'train'), ('validation', 'val'), ('test', 'test')]:
            with (raw / 'entitymatching' / f'{a}_2_{b}_{suffix}.csv').open(encoding='utf-8-sig', newline='') as stream:
                for row in csv.reader(stream):
                    if len(row) != 3 or row[2].lower() not in {'true', 'false'}:
                        raise ValueError('Unexpected headerless MADI label format')
                    labels.append({'left_source': a, 'right_source': b, 'left_id': row[0], 'right_id': row[1],
                                   'label': int(row[2].lower() == 'true'), 'original_split': split})
    result = adapt_sources(tables, labels, seed=seed)
    output.mkdir(parents=True)
    (output / 'matcher').mkdir()
    (output / 'evaluator').mkdir()
    for split in SPLITS:
        rows = [result['records'][r['record_id']] for r in result['record_groups'] if r['split'] == split]
        pq.write_table(pa.Table.from_pylist(rows, schema=pa.schema([(field, pa.string()) for field in MATCHER_COLUMNS])),
                       output / 'matcher' / f'{split}.parquet')
    pq.write_table(pa.Table.from_pylist(result['labels'], schema=pa.schema([
        (field, pa.int8() if field == 'label' else pa.string()) for field in LABEL_COLUMNS])), output / 'evaluator/labelled_pairs.parquet')
    pq.write_table(pa.Table.from_pylist(result['record_groups'], schema=pa.schema([
        (field, pa.string()) for field in GROUP_COLUMNS])), output / 'evaluator/record_groups.parquet')
    manifest = {'dataset': 'madi_companies_base', 'domain': 'company', 'adapter_version': VERSION,
        'records': len(result['records']), 'partial_labels_only': True, 'snapshot': snapshot,
        'statistics': result['statistics'], 'field_projection': PROJECTION, 'split_seed': seed,
        'files': {p.relative_to(output).as_posix(): file_hash(p) for p in sorted(output.rglob('*.parquet'))}}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    verify_benchmark(output)
    return manifest
