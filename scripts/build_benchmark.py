"""Stream official sources, audit registration overlap, and isolate a frozen gold benchmark.

No matching module imports this script. All identifiers and provenance remain evaluator-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pyarrow as pa
import pyarrow.parquet as pq

from entitybridge.ingestion import (
    PARSER_VERSION,
    OpaqueRegistry,
    entity_split,
    file_sha256,
    gold_exclusion,
    iter_ch_zip,
    parse_gleif,
    verify_snapshot,
)
from entitybridge.normalization import (
    FEATURE_VIEW_VERSION,
    MATCHER_COLUMNS,
    NORMALIZATION_VERSION,
    matcher_view,
)


def content_hash(record):
    return hashlib.sha256(json.dumps({k: v for k,v in record.items() if k not in ('location', 'snapshot_sha256')}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def export_benchmark(pairs, output, registry, size, seed, provenance=None, kind='A_real_cross_source'):
    """A complete-truth, closed-world subset: two independent source records per company."""
    if (output / 'manifest.json').exists():
        raise ValueError(f"Refuse to overwrite frozen dataset: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / 'matcher').mkdir(exist_ok=True)
    (output / 'evaluator').mkdir(exist_ok=True)
    selected = sorted(pairs, key=lambda number: hashlib.sha256(f"sample:{seed}:{number}".encode()).digest())[:size // 2]
    rows = defaultdict(list)
    truth = []
    rich = []
    redactions = Counter()
    for number in selected:
        split = entity_split('RA000585:' + number, seed)
        entity_id, _ = registry.ids('truth', number, 'truth-v1')
        for record in pairs[number]:
            rid, version = registry.ids(record['source'], record['source_key'], content_hash(record))
            view = matcher_view(record, rid, version)
            rows[split].append(view)
            for field in ('name','address','city','postcode','country'):
                if record.get(field) and view[field] is None:
                    redactions[field] += 1
            truth.append({'record_id': rid, 'true_entity_id': entity_id, 'split': split})
            rich.append({**record, 'record_id': rid, 'record_version_id': version, 'true_entity_id': entity_id, 'split': split})
    schema = pa.schema([(column, pa.string()) for column in MATCHER_COLUMNS])
    for split in ('train', 'validation', 'test'):
        # Pair adjacency and source ordering cannot encode the answer.
        values = sorted(rows[split], key=lambda row: row['record_id'])
        pq.write_table(pa.Table.from_pylist(values, schema=schema), output / 'matcher' / f'{split}.parquet')
    pq.write_table(pa.Table.from_pylist(sorted(truth, key=lambda row: row['record_id']), schema=pa.schema([('record_id',pa.string()),('true_entity_id',pa.string()),('split',pa.string())])), output / 'evaluator' / 'truth_map.parquet')
    with (output / 'evaluator' / 'rich_records.jsonl').open('w', encoding='utf-8') as target:
        for row in rich:
            target.write(json.dumps(row, ensure_ascii=False) + '\n')
    registry.save()
    manifest = {'created_at': datetime.now(UTC).isoformat(), 'kind': kind,
                'scope': 'closed-world complete-truth subset, one clean GLEIF and one active CH record per known registry entity',
                'requested_records': size, 'records': len(truth), 'entities': len(selected),
                'split_records': {split:len(rows[split]) for split in ('train','validation','test')},
                'seed': seed, 'split_policy': 'SHA256(entity, seed), 60/20/20', 'selection': 'lowest seeded entity hash from complete overlap population',
                'features': list(MATCHER_COLUMNS), 'normalization_version': NORMALIZATION_VERSION,
                'feature_view_version': FEATURE_VIEW_VERSION, 'null_after_projection_from_nonempty': dict(redactions),
                'parser_version': PARSER_VERSION, 'provenance': provenance,
                'files': {str(path.relative_to(output)): file_sha256(path) for path in sorted(output.rglob('*')) if path.is_file()}}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def build_alias_benchmarks(rich_path, output_dir, seed):
    """Derived official previous-name queries. Never call copied current addresses historical."""
    grouped=defaultdict(dict)
    for line in rich_path.open(encoding='utf-8'):
        record=json.loads(line)
        grouped[record['registration_number']][record['source']]=record
    eligible={}
    from entitybridge.normalization import normalize_text
    for number,sources in grouped.items():
        if not {'gleif','companies_house'}.issubset(sources):
            continue
        origin=sources['gleif']
        names=sorted({item['name'] for item in origin.get('other_names',[]) if item.get('type')=='PREVIOUS_LEGAL_NAME' and normalize_text(item.get('name')) != normalize_text(origin['name'])})
        if names:
            eligible[number]=(sources,names[0])
    registry=OpaqueRegistry(output_dir/'aliases_private_registry.json')
    outputs=[]
    for condition in ('name_only','partial_address','current_address'):
        pairs={}
        for number,(sources,name) in eligible.items():
            original=sources['gleif']
            query={**original,'source_key':original['source_key']+':previous-name:'+name,'name':name,
                   'derivation':'official PREVIOUS_LEGAL_NAME with controlled current-address availability',
                   'address_condition':condition}
            if condition=='name_only':
                query.update({field:None for field in ('address','postcode','city','country')})
            elif condition=='partial_address':
                query.update({field:None for field in ('address','city','country')})
            pairs[number]=(query,sources['companies_house'])
        manifest=export_benchmark(pairs,output_dir/f'aliases_{condition}',registry,len(pairs)*2,seed,
                                 provenance={'derived_from_sha256':file_sha256(rich_path),'condition':condition,
                                             'address_interpretation':'copied current GLEIF address, not historical address; name_only removes all address fields; partial_address retains postcode only',
                                             'alias_selection':'lexicographically first distinct official PREVIOUS_LEGAL_NAME per entity'},
                                 kind='B_real_previous_name_derived')
        outputs.append(manifest)
    return outputs


def reexport_frozen(rich_path, output_dir, sizes, seed):
    """New immutable feature-view revision from the same selected real source population."""
    grouped=defaultdict(dict)
    for line in rich_path.open(encoding='utf-8'):
        record=json.loads(line)
        grouped[record['registration_number']][record['source']]=record
    pairs={number:(sources['gleif'],sources['companies_house']) for number,sources in grouped.items()}
    original=json.loads((rich_path.parent.parent/'manifest.json').read_text(encoding='utf-8'))
    provenance={'derived_from_dataset_sha256':file_sha256(rich_path.parent.parent/'manifest.json'),
                'parent_provenance':original.get('provenance'), 'note':'same frozen real entity population; strict text identifier redaction added'}
    registry=OpaqueRegistry(output_dir/'private_registry.json')
    return [export_benchmark(pairs,output_dir/f'real_{size}',registry,size,seed,provenance=provenance) for size in sizes]


def export_release_holdout(pairs, previous_dir, output, *, provenance):
    """Reuse train/validation bytes and reserve the complete previously unused entity complement."""
    if (output/'manifest.json').exists():
        raise ValueError(f'Refuse to overwrite frozen dataset: {output}')
    previous_manifest_path=previous_dir/'manifest.json'
    previous_manifest=json.loads(previous_manifest_path.read_text(encoding='utf-8'))
    for relative, expected in previous_manifest['files'].items():
        if file_sha256(previous_dir/relative)!=expected:
            raise ValueError(f'Previous dataset hash mismatch: {relative}')
    if previous_manifest.get('feature_view_version')!=FEATURE_VIEW_VERSION:
        raise ValueError('Previous train/validation feature view differs from the release feature view')
    prior_rich=[json.loads(line) for line in (previous_dir/'evaluator/rich_records.jsonl').open(encoding='utf-8')]
    prior_entities={record['registration_number'] for record in prior_rich}
    missing=prior_entities.difference(pairs)
    if missing:
        raise ValueError(f'Official-source population changed: {len(missing)} previous entities absent')
    fresh_numbers=set(pairs).difference(prior_entities)
    if not fresh_numbers:
        raise ValueError('No previously unused entities remain for a new test')
    retained=[record for record in prior_rich if record['split'] in ('train','validation')]
    previous_truth=pq.read_table(previous_dir/'evaluator/truth_map.parquet').to_pylist()
    truth=[record for record in previous_truth if record['split'] in ('train','validation')]
    if {row['record_id'] for row in truth}!={row['record_id'] for row in retained}:
        raise ValueError('Previous truth and rich records disagree')
    output.mkdir(parents=True,exist_ok=True)
    (output/'matcher').mkdir(exist_ok=True)
    (output/'evaluator').mkdir(exist_ok=True)
    for split in ('train','validation'):
        shutil.copyfile(previous_dir/'matcher'/f'{split}.parquet',output/'matcher'/f'{split}.parquet')
    registry=OpaqueRegistry(output/'evaluator/private_registry.json')
    fresh_features=[]
    fresh_rich=[]
    redactions=Counter()
    for number in sorted(fresh_numbers):
        entity_id,_=registry.ids('truth',number,'truth-v1')
        for record in pairs[number]:
            rid,version=registry.ids(record['source'],record['source_key'],content_hash(record))
            view=matcher_view(record,rid,version)
            fresh_features.append(view)
            for field in ('name','address','city','postcode','country'):
                if record.get(field) and view[field] is None:
                    redactions[field]+=1
            truth.append({'record_id':rid,'true_entity_id':entity_id,'split':'test'})
            fresh_rich.append({**record,'record_id':rid,'record_version_id':version,'true_entity_id':entity_id,'split':'test'})
    previous_ids={row['record_id'] for row in prior_rich}
    previous_source_keys={(row['source'],row['source_key']) for row in prior_rich}
    record_overlap=previous_ids.intersection(row['record_id'] for row in fresh_features)
    source_overlap=previous_source_keys.intersection((row['source'],row['source_key']) for row in fresh_rich)
    if record_overlap or source_overlap:
        raise ValueError('New test overlaps previous record IDs or source keys')
    schema=pa.schema([(column,pa.string()) for column in MATCHER_COLUMNS])
    pq.write_table(pa.Table.from_pylist(sorted(fresh_features,key=lambda row:row['record_id']),schema=schema),output/'matcher/test.parquet')
    pq.write_table(pa.Table.from_pylist(sorted(truth,key=lambda row:row['record_id']),schema=pa.schema([('record_id',pa.string()),('true_entity_id',pa.string()),('split',pa.string())])),output/'evaluator/truth_map.parquet')
    with (output/'evaluator/rich_records.jsonl').open('w',encoding='utf-8') as target:
        for row in sorted(retained+fresh_rich,key=lambda row:row['record_id']):
            target.write(json.dumps(row,ensure_ascii=False)+'\n')
    registry.save()
    split_records=dict(Counter(row['split'] for row in truth))
    split_entities={split:len({row['true_entity_id'] for row in truth if row['split']==split}) for split in ('train','validation','test')}
    verification={'excluded_previous_entities':len(prior_entities),'new_test_entities':len(fresh_numbers),
                  'new_test_overlap_with_previous_entities':len(fresh_numbers.intersection(prior_entities)),
                  'new_test_record_id_overlap_with_previous_records':len(record_overlap),
                  'new_test_source_key_overlap_with_previous_records':len(source_overlap),
                  'old_test_records_retained':0,
                  **{f'{split}_byte_identical':file_sha256(output/'matcher'/f'{split}.parquet')==file_sha256(previous_dir/'matcher'/f'{split}.parquet') for split in ('train','validation')}}
    (output/'evaluator/holdout_verification.json').write_text(json.dumps(verification,indent=2),encoding='utf-8')
    manifest={'created_at':datetime.now(UTC).isoformat(),'kind':'A_real_cross_source_fresh_release_holdout',
              'scope':'same-source closed-world complete truth; training/validation retained, previously unused full population complement reserved as test',
              'records':len(truth),'entities':sum(split_entities.values()),'split_records':split_records,'split_entities':split_entities,
              'selection':'exclude every entity from prior 50k-entity dataset, including its old test; use all remaining clean overlap entities as new test',
              'seed':previous_manifest['seed'],'split_policy':'prior train/validation unchanged; all previously unselected entities assigned test',
              'features':list(MATCHER_COLUMNS),'normalization_version':NORMALIZATION_VERSION,'feature_view_version':FEATURE_VIEW_VERSION,
              'parser_version':PARSER_VERSION,'new_test_redactions':dict(redactions),'holdout_verification':verification,
              'provenance':{**provenance,'previous_dataset_manifest_sha256':file_sha256(previous_manifest_path),
                            'previous_rich_sha256':file_sha256(previous_dir/'evaluator/rich_records.jsonl')},
              'files':{str(path.relative_to(output)):file_sha256(path) for path in sorted(output.rglob('*')) if path.is_file()}}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return manifest


def audit_and_build(raw_dir, output_dir, sizes, seed, *, release_exclude_from=None):
    if (output_dir/'manifest.json').exists():
        raise ValueError(f'Refuse to overwrite frozen dataset: {output_dir}')
    start = time.perf_counter()
    output_dir.mkdir(parents=True, exist_ok=True)
    audit = {'parser_version': PARSER_VERSION, 'created_at': datetime.now(UTC).isoformat(),
             'source_manifests': [], 'gleif': {'rows':0}, 'companies_house': {'rows':0}, 'gold_policy': 'GB + RA000585 + GENERAL + ACTIVE + ISSUED; one LEI per CH number; CH Active; exclude abnormal relationships'}
    counts = Counter()
    (output_dir / 'audit').mkdir(exist_ok=True)
    gleif_quarantine = output_dir / 'audit' / 'gleif_quarantine.jsonl'
    gleif_quarantine.write_text('', encoding='utf-8')
    def reject_gleif(item):
        counts['gleif_rejected:' + item['reason']] += 1
        with gleif_quarantine.open('a', encoding='utf-8') as target:
            target.write(json.dumps(item, ensure_ascii=False) + '\n')
    candidates = defaultdict(list)
    observed_lei = set()
    golden = sorted((raw_dir / 'gleif_golden').glob('*.csv.zip'))
    golden_stats = {}
    if golden:
        from entitybridge.ingestion import iter_gleif_csv_zip
        audit['gleif']['selection'] = 'complete Golden Copy stream filtered to legalAddress.country GB'
        streams = [(path, iter_gleif_csv_zip(path, reject_gleif, golden_stats)) for path in golden]
    else:
        api_files = sorted((raw_dir / 'gleif').glob('page-*.json'))
        audit['gleif']['selection'] = 'diagnostic partial API pages only; not population representative'
        streams = [(path, (parse_gleif(obj, f'{path.name}:{i}') for i,obj in enumerate(json.loads(path.read_text(encoding='utf-8'))['data']))) for path in api_files]
    for path, records in streams:
        metadata = verify_snapshot(path)
        snapshot = metadata['sha256']
        audit['source_manifests'].append({'path': str(path), **metadata})
        for record in records:
            audit['gleif']['rows'] += 1
            if record['source_key'] in observed_lei:
                counts['duplicate_lei'] += 1
                continue
            observed_lei.add(record['source_key'])
            reason = gold_exclusion(record)
            if reason:
                counts['gleif_excluded:' + reason] += 1
            else:
                record['snapshot_sha256'] = snapshot
                candidates[record['registration_number']].append(record)
    clean = {number:items[0] for number,items in candidates.items() if len(items)==1}
    counts['ambiguous_multiple_gleif_numbers'] = sum(len(items)>1 for items in candidates.values())
    counts['clean_gleif_entities_before_ch_join'] = len(clean)
    matches = {}
    seen = set()
    conflicting = set()
    ch_files = sorted((raw_dir / 'companies_house').glob('*.zip'))
    (output_dir / 'audit').mkdir(exist_ok=True)
    with (output_dir / 'audit' / 'quarantine.jsonl').open('w', encoding='utf-8') as rejected:
        def reject(item):
            counts['ch_quarantined:' + item['reason']] += 1
            rejected.write(json.dumps(item, ensure_ascii=False) + '\n')
        for path in ch_files:
            metadata = verify_snapshot(path)
            snapshot = metadata['sha256']
            audit['source_manifests'].append({'path': str(path), **metadata})
            before = audit['companies_house']['rows']
            for record in iter_ch_zip(path, reject):
                audit['companies_house']['rows'] += 1
                number = record['registration_number']
                if number in seen:
                    conflicting.add(number)
                    counts['ch_duplicate_number'] += 1
                seen.add(number)
                counts['ch_status:' + record['status']] += 1
                if number not in clean:
                    continue
                if record['status'] != 'Active':
                    counts['ch_overlap_excluded:status:' + record['status']] += 1
                    continue
                if record['category'] == 'Overseas Company':
                    counts['ch_overlap_excluded:overseas_company'] += 1
                    continue
                record['snapshot_sha256'] = snapshot
                matches[number] = (clean[number], record)
            print(json.dumps({'ch_file':path.name,'parsed_rows':audit['companies_house']['rows']-before,'cumulative_overlap':len(matches)}),flush=True)
    pairs = {number:records for number,records in matches.items() if number not in conflicting}
    audit['counts'] = dict(counts)
    audit['gleif']['full_file_scan'] = golden_stats
    audit['cross_source_entities'] = len(pairs)
    audit['companies_house']['files'] = len(ch_files)
    audit['companies_house']['raw_rows'] = audit['companies_house']['rows'] + sum(value for key,value in counts.items() if key.startswith('ch_quarantined:'))
    audit['gleif']['clean_unmatched_in_downloaded_ch'] = len(clean) - len(pairs)
    audit['field_missing'] = {source: dict(Counter(field for records in pairs.values() for record in records if record['source']==source for field in ('name','address','city','postcode','country') if not record.get(field))) for source in ('gleif','companies_house')}
    audit['overlap_differences'] = dict(Counter(field for records in pairs.values() for field in ('name','address','city','postcode') if matcher_view(records[0],'','')[field] != matcher_view(records[1],'','')[field]))
    audit['elapsed_seconds'] = round(time.perf_counter()-start,3)
    (output_dir / 'audit' / 'data_audit.json').write_text(json.dumps(audit,indent=2),encoding='utf-8')
    if release_exclude_from:
        result=export_release_holdout(pairs,release_exclude_from,output_dir,provenance={
            'sources':audit['source_manifests'],'audit_sha256':file_sha256(output_dir/'audit/data_audit.json'),
            'build_script_sha256':file_sha256(Path(__file__))})
        print(json.dumps({key:result[key] for key in ('records','entities','split_records','split_entities','holdout_verification')}),flush=True)
        return audit,[result]
    registry = OpaqueRegistry(output_dir / 'private_registry.json')
    results = []
    for size in sizes:
        result = export_benchmark(pairs, output_dir / f'real_{size}', registry, size, seed, provenance={'sources': audit['source_manifests'], 'audit_sha256': file_sha256(output_dir / 'audit' / 'data_audit.json'), 'build_script_sha256': file_sha256(Path(__file__))})
        results.append(result)
        print(json.dumps({key:result[key] for key in ('records','entities','split_records')}),flush=True)
    return audit,results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-dir',type=Path,default=Path('artifacts/raw'))
    parser.add_argument('--output-dir',type=Path,default=Path('artifacts/datasets'))
    parser.add_argument('--sizes',type=int,nargs='+',default=[10000,100000])
    parser.add_argument('--seed',type=int,default=20260912)
    parser.add_argument('--aliases-from',type=Path,help='Build separately labelled B datasets from a frozen evaluator rich file')
    parser.add_argument('--reexport-from',type=Path,help='Create a new feature-view revision from a frozen evaluator rich file')
    parser.add_argument('--release-exclude-from',type=Path,help='Rescan official sources; use prior train/validation and all previously unused entities as fresh test')
    args=parser.parse_args()
    if args.reexport_from:
        outputs=reexport_frozen(args.reexport_from,args.output_dir,args.sizes,args.seed)
        print(json.dumps([{'records':value['records'],'entities':value['entities'],'redactions':value['null_after_projection_from_nonempty']} for value in outputs],indent=2))
        return
    if args.aliases_from:
        outputs=build_alias_benchmarks(args.aliases_from,args.output_dir,args.seed)
        print(json.dumps([{'kind':value['kind'],'records':value['records'],'entities':value['entities']} for value in outputs],indent=2))
        return
    if any(size < 2 or size > 1000000 or size%2 for size in args.sizes):
        parser.error('Sizes must be positive even source-record counts <= 1m')
    audit, _ = audit_and_build(args.raw_dir,args.output_dir,args.sizes,args.seed,release_exclude_from=args.release_exclude_from)
    print(json.dumps(audit,indent=2),flush=True)


if __name__=='__main__':
    main()
