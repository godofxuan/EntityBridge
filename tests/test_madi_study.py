import pytest

from entitybridge.candidates import FrozenNameRetriever
from entitybridge.madi_study import candidates, guard, pair, paired_intervals, save, train_neural


def record(key, source, name, country='US'):
    return {'record_id': key, 'record_version_id': key + 'v', 'source': source, 'name': name,
            'country': country, 'city': None, 'address': None, 'postcode': None}


def test_country_ablation_preserves_original_features_and_source_scope():
    records = [record('a', 'forbes', 'ACME', 'UNITED STATES'), record('b', 'dbpedia', 'ACME'),
               record('c', 'fullcontact', 'ACME')]
    retriever = FrozenNameRetriever.fit(records, top_k=10, min_similarity=.2)
    tracks = candidates(records, [], retriever)
    assert tracks['fixed'] == tracks['hybrid_strict_country'] == []
    assert {pair(r) for r in tracks['hybrid_soft_country']} == {('a', 'b'), ('a', 'c')}
    assert records[0]['country'] == 'UNITED STATES'
    assert records[1]['country'] == 'US'


def test_paired_bootstrap_uses_shared_draws_and_mean_of_every_seed():
    labels = [{'left_id': f'a{i}', 'right_id': f'b{i}', 'label': i % 2, 'group_id': str(i)} for i in range(120)]
    perfect = {pair(r): float(r['label']) for r in labels}
    bad = {key: 1 - value for key, value in perfect.items()}
    scores = {'supervised_logistic': bad, 'ditto_seed11': perfect, 'ditto_seed23': perfect, 'ditto_seed47': perfect}
    selections = {name: {p: {'threshold': .5} for p in ('f1', 'cost_10_1')} for name in scores}
    result = paired_intervals(labels, scores, selections, n_resamples=100)
    assert result['intervals']['known_positives_at100']['estimate'] == 20
    assert result['intervals']['f1']['estimate'] == 1
    assert result['intervals']['f1']['interval'] == [1, 1]
    assert result['intervals']['cost_10FP_FN']['estimate'] == -660
    identical = {name: perfect for name in scores}
    zero = paired_intervals(labels, identical, selections, n_resamples=100)
    assert all(v['interval'] == [0, 0] and v['estimate'] == 0 for v in zero['intervals'].values())
    assert result == paired_intervals(labels, scores, selections, n_resamples=100)


def test_paired_bootstrap_refuses_large_dependency_component_and_unknowns():
    labels = [{'left_id': f'a{i}', 'right_id': f'b{i}', 'label': i % 2, 'group_id': 'same'} for i in range(4)]
    data = {pair(r): .5 for r in labels}
    assert paired_intervals(labels, {'supervised_logistic': data}, {})['status'] == 'insufficient_independent_groups'
    with pytest.raises(ValueError, match='supplied labeled'):
        paired_intervals(labels, {'supervised_logistic': data | {('x', 'y'): .8}}, {})


def test_guard_detects_changed_frozen_inputs(tmp_path):
    root, dataset, output = [tmp_path / n for n in ('root', 'data', 'study')]
    for directory in (root, dataset, output):
        directory.mkdir()
    from entitybridge.benchmarks import file_hash
    (root / 'source.py').write_text('before', encoding='utf-8')
    save(output / 'prepared.json', {'source_files': {'source.py': file_hash(root / 'source.py')},
        'dataset_files': {}, 'frozen_development_files': {}})
    guard(root, dataset, output)
    (root / 'source.py').write_text('after', encoding='utf-8')
    with pytest.raises(ValueError, match='Frozen input changed'):
        guard(root, dataset, output)


def test_training_stops_before_reading_data_after_test_freeze(tmp_path, monkeypatch):
    import entitybridge.madi_study as study
    monkeypatch.setattr(study, 'guard', lambda *a: {})
    monkeypatch.setattr(study, 'read_partition', lambda *a: pytest.fail('read held-out data'))
    (tmp_path / 'frozen_config.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='No training after test freeze'):
        train_neural(tmp_path, tmp_path, tmp_path, seed=11, base_model=tmp_path, gpu_confirmed=True)
