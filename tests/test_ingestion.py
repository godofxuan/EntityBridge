import csv
import io
import json
import zipfile

from entitybridge.ingestion import iter_ch_zip


def test_ch_zip_preserves_leading_zeros_and_quarantines_invalid_rows(tmp_path):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['CompanyName', ' CompanyNumber', 'RegAddress.AddressLine1', 'RegAddress.PostCode'])
    writer.writerow(['Example Limited', '00123456', '1 Road', 'AB1 2CD'])
    writer.writerow(['', '00234567', '', ''])
    writer.writerow(['Broken', '123', '', ''])
    path = tmp_path / 'companies.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('companies.csv', buffer.getvalue())
    rejected = []
    records = list(iter_ch_zip(path, rejected.append))
    assert len(records) == 1
    assert records[0]['source_key'] == '00123456'
    assert records[0]['registration_number'] == '00123456'
    assert records[0]['name'] == 'Example Limited'
    assert {item['reason'] for item in rejected} == {'missing_name', 'invalid_company_number'}


def test_gleif_filter_keeps_only_explicit_unambiguous_registry_scope():
    from entitybridge.ingestion import gold_exclusion, parse_gleif
    obj = {'id': 'EXAMPLE_LEI', 'attributes': {
        'entity': {'legalName': {'name': 'Example Limited'},
                   'legalAddress': {'country': 'GB', 'addressLines': ['1 Road']},
                   'registeredAt': {'id': 'RA000585'}, 'registeredAs': '00123456',
                   'category': 'GENERAL', 'status': 'ACTIVE'},
        'registration': {'status': 'ISSUED'}}}
    record = parse_gleif(obj, 'page:1')
    assert record['registration_number'] == '00123456'
    assert gold_exclusion(record) is None
    obj['attributes']['registration']['status'] = 'DUPLICATE'
    assert gold_exclusion(parse_gleif(obj, 'page:1')) == 'registration_status:DUPLICATE'


def test_opaque_registry_reuses_import_but_changes_version_for_source_change(tmp_path):
    from uuid import UUID

    from entitybridge.ingestion import OpaqueRegistry
    path = tmp_path / 'private.json'
    registry = OpaqueRegistry(path)
    a = registry.ids('gleif', '00123456', 'content-a')
    b = registry.ids('companies_house', '00123456', 'content-b')
    registry.save()
    again = OpaqueRegistry(path)
    assert again.ids('gleif', '00123456', 'content-a') == a
    changed = again.ids('gleif', '00123456', 'content-changed')
    assert changed[0] == a[0] and changed[1] != a[1]
    assert len(set(a + b)) == 4
    assert all(UUID(value).version == 4 for value in a + b + changed)


def test_entity_split_is_stable_for_all_source_versions():
    from entitybridge.ingestion import entity_split
    assignments = [entity_split('RA000585:' + str(i), seed=42) for i in range(1000)]
    assert assignments == [entity_split('RA000585:' + str(i), seed=42) for i in range(1000)]
    assert set(assignments) == {'train', 'validation', 'test'}
    assert 500 < assignments.count('train') < 700


def test_download_accepts_compressed_http_and_reuses_hash_verified_snapshot(tmp_path):
    import gzip
    import runpy
    from pathlib import Path

    import httpx
    download = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/fetch_public_data.py'))['download']
    payload = b'{"official":true}'
    compressed = gzip.compress(payload)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, headers={'content-encoding':'gzip', 'content-length': str(len(compressed))}, content=compressed)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        target = tmp_path / 'source.json'
        first = download(client, 'https://example.test/file', target, max_bytes=1000, terms_url='https://example.test/terms')
        second = download(client, 'https://example.test/file', target, max_bytes=1000, terms_url='https://example.test/terms')
    assert first == second and len(calls) == 1
    assert target.read_bytes() == payload


def test_golden_copy_csv_stream_filters_gb_and_keeps_raw_registration_fields(tmp_path):
    from entitybridge.ingestion import iter_gleif_csv_zip
    buffer=io.StringIO()
    writer=csv.writer(buffer)
    writer.writerow(['LEI','Entity.LegalName','Entity.LegalAddress.Country','Entity.RegistrationAuthority.RegistrationAuthorityID','Entity.RegistrationAuthority.RegistrationAuthorityEntityID','Entity.EntityStatus','Entity.EntityCategory','Registration.RegistrationStatus'])
    writer.writerow(['ONE','Example Limited','GB','RA000585','00123456','ACTIVE','GENERAL','ISSUED'])
    writer.writerow(['TWO','Other Corp','US','RA_US','00000123','ACTIVE','GENERAL','ISSUED'])
    path=tmp_path/'golden.zip'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('golden.csv',buffer.getvalue())
    rows=list(iter_gleif_csv_zip(path,lambda _:None))
    assert len(rows)==1
    assert rows[0]['registration_number']=='00123456'
    assert rows[0]['name']=='Example Limited'


def test_benchmark_export_is_entity_split_and_identifier_isolated(tmp_path):
    import runpy
    from pathlib import Path

    import pyarrow.parquet as pq
    import pytest

    from entitybridge.ingestion import OpaqueRegistry
    from entitybridge.normalization import MATCHER_COLUMNS
    export = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/build_benchmark.py'))['export_benchmark']
    pairs = {f'{i:08d}': tuple({'source': source, 'source_key': f'{source}-{i:08d}',
                              'registration_number': f'{i:08d}', 'name': f'Company {i} Limited'}
                             for source in ('gleif','companies_house')) for i in range(100)}
    registry=OpaqueRegistry(tmp_path/'private.json')
    output=tmp_path/'benchmark'
    manifest=export(pairs, output, registry, 200, 42)
    assert manifest['records']==200
    truth=pq.read_table(output/'evaluator/truth_map.parquet').to_pylist()
    by_entity={}
    ids=set()
    for split in ('train','validation','test'):
        table=pq.read_table(output/'matcher'/f'{split}.parquet')
        assert set(table.column_names)==set(MATCHER_COLUMNS)
        ids.update(table.column('record_id').to_pylist())
    assert len(ids)==200
    for item in truth:
        by_entity.setdefault(item['true_entity_id'],set()).add(item['split'])
    assert len(by_entity)==100 and all(len(splits)==1 for splits in by_entity.values())
    assert not any(path.name=='truth_map.parquet' for path in (output/'matcher').rglob('*'))
    with pytest.raises(ValueError,match='overwrite frozen'):
        export(pairs,output,registry,200,42)


def test_snapshot_integrity_prevents_silent_raw_file_replacement(tmp_path):
    import pytest

    from entitybridge.ingestion import file_sha256, verify_snapshot
    path=tmp_path/'source.csv'
    path.write_text('original',encoding='utf-8')
    path.with_suffix('.csv.manifest.json').write_text(json.dumps({'sha256':file_sha256(path),'source_url':'https://example.test/data'}),encoding='utf-8')
    assert verify_snapshot(path)['sha256']==file_sha256(path)
    path.write_text('modified',encoding='utf-8')
    with pytest.raises(ValueError,match='hash mismatch'):
        verify_snapshot(path)


def test_company_numbers_keep_valid_society_and_alphanumeric_suffixes():
    from entitybridge.ingestion import company_number
    assert company_number('IP10067R')=='IP10067R'
    assert company_number('RS00157S')=='RS00157S'
    assert company_number('R0000286')=='R0000286'
    assert company_number('00123456')=='00123456'
    assert company_number('123') is None


def test_ch_missing_country_stays_missing_instead_of_becoming_gb():
    from entitybridge.ingestion import ch_country
    assert ch_country('') is None
    assert ch_country('ENGLAND')=='GB'
    assert ch_country('UNITED KINGDOM')=='GB'
    assert ch_country('JERSEY')=='JE'
    assert ch_country('Unrecognized place') is None


def test_real_alias_derivation_keeps_entity_split_and_labels_borrowed_address(tmp_path):
    import runpy
    from pathlib import Path

    import pyarrow.parquet as pq
    module=runpy.run_path(str(Path(__file__).parents[1]/'scripts/build_benchmark.py'))
    records=[{'source':'gleif','source_key':'LEI-ONE','registration_number':'00123456','name':'New Name Limited','address':'Current Road','postcode':'AB12CD','city':'Town','country':'GB','other_names':[{'name':'Former Name Limited','type':'PREVIOUS_LEGAL_NAME'}]},
             {'source':'companies_house','source_key':'00123456','registration_number':'00123456','name':'New Name Limited','address':'Current Road','postcode':'AB12CD','city':'Town','country':'GB'}]
    rich=tmp_path/'rich.jsonl'
    rich.write_text('\n'.join(json.dumps(row) for row in records),encoding='utf-8')
    outputs=module['build_alias_benchmarks'](rich,tmp_path/'aliases',42)
    assert len(outputs)==3
    files=list((tmp_path/'aliases'/'aliases_name_only'/'matcher').glob('*.parquet'))
    rows=[row for path in files for row in pq.read_table(path).to_pylist()]
    query=next(row for row in rows if row['source']=='gleif')
    assert query['name']=='FORMER NAME LIMITED'
    assert query['address'] is None and query['postcode'] is None
    manifest=json.loads((tmp_path/'aliases'/'aliases_current_address'/'manifest.json').read_text())
    assert manifest['kind']=='B_real_previous_name_derived'
    assert 'not historical' in manifest['provenance']['address_interpretation']


def test_release_holdout_excludes_every_old_entity_and_preserves_train_validation(tmp_path):
    import runpy
    from pathlib import Path

    import pyarrow.parquet as pq

    from entitybridge.ingestion import OpaqueRegistry, file_sha256
    module=runpy.run_path(str(Path(__file__).parents[1]/'scripts/build_benchmark.py'))
    pairs={f'{i:08d}':tuple({'source':source,'source_key':f'{source}-{i:08d}',
                            'registration_number':f'{i:08d}','name':f'Firm {i} Limited'}
                           for source in ('gleif','companies_house')) for i in range(110)}
    old=tmp_path/'old'
    module['export_benchmark']({k:v for k,v in pairs.items() if int(k)<100},old,OpaqueRegistry(tmp_path/'old_registry.json'),200,42)
    output=tmp_path/'release'
    manifest=module['export_release_holdout'](pairs,old,output,provenance={})
    assert manifest['split_entities']['test']==10
    for split in ('train','validation'):
        assert file_sha256(output/'matcher'/f'{split}.parquet')==file_sha256(old/'matcher'/f'{split}.parquet')
    previous={r['registration_number'] for r in map(json.loads,(old/'evaluator/rich_records.jsonl').open(encoding='utf-8'))}
    rows=[json.loads(line) for line in (output/'evaluator/rich_records.jsonl').open(encoding='utf-8')]
    test_numbers={r['registration_number'] for r in rows if r['split']=='test'}
    assert len(test_numbers)==10 and not test_numbers.intersection(previous)
    old_ids=set(pq.read_table(old/'evaluator/truth_map.parquet')['record_id'].to_pylist())
    test_ids=set(pq.read_table(output/'matcher/test.parquet')['record_id'].to_pylist())
    assert not test_ids.intersection(old_ids)
    assert manifest['holdout_verification']['new_test_overlap_with_previous_entities']==0
