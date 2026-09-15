import copy

import pytest

from entitybridge.madi_benchmark import adapt_sources
from entitybridge.normalization import MATCHER_COLUMNS


def fixtures():
    tables = {'forbes': [{'forbes_url': x, 'company': x, 'region': 'GB', 'url': 'FORBIDDEN_URL'} for x in 'abcdef'],
              'dbpedia': [{'entity_uri': x, 'org_name': x, 'nation': 'GB', 'headquarters': 'EXAMPLE'} for x in 'uvwxyz'],
              'fullcontact': [{'Attribute_1': 'f1', 'Attribute_2': 'Synthetic company', 'Attribute_5': 'FORBIDDEN_PERSON'}]}

    def label(a, b, value, split):
        return {'left_source': 'forbes', 'right_source': 'dbpedia', 'left_id': a, 'right_id': b,
                'label': value, 'original_split': split}
    labels = [label('a', 'u', 1, 'test'), label('b', 'v', 1, 'validation'), label('c', 'w', 1, 'train'),
              label('a', 'x', 0, 'train'), label('b', 'y', 0, 'train'), label('d', 'z', 0, 'train')]
    return tables, labels, label


def test_official_test_priority_and_known_entity_purge_without_feature_leakage():
    tables, labels, _ = fixtures()
    result = adapt_sources(tables, labels)
    assert result['statistics']['purged_for_split_isolation'] == 2
    assert [r['label'] for r in result['labels'] if r['split'] == 'test'] == [1]
    assert sum(r['split'] == 'train' for r in result['labels']) == 2
    assert all(set(r) == set(MATCHER_COLUMNS) for r in result['records'].values())
    assert 'FORBIDDEN_' not in str(result['records'])
    memberships = {}
    for row in result['record_groups']:
        assert memberships.setdefault(row['entity_group_id'], row['split']) == row['split']
    shuffled = adapt_sources({k: list(reversed(v)) for k, v in tables.items()}, list(reversed(labels)))
    assert shuffled['labels'] == result['labels'] and shuffled['record_groups'] == result['record_groups']


def test_inconsistent_gold_quarantines_whole_component_and_incident_labels():
    tables, labels, make = fixtures()
    labels.extend([make('e', 'x', 1, 'train'), make('f', 'x', 1, 'validation'), make('e', 'x', 0, 'test')])
    result = adapt_sources(tables, labels)
    assert result['statistics']['positive_closure_contradictions'] == 1
    assert result['statistics']['quarantined_records'] == 3
    assert result['statistics']['quarantined_labels_by_original_split'] == {'train': 2, 'validation': 1, 'test': 1}
    assert len(result['records']) == 10


def test_missing_endpoints_are_recorded_and_never_invented():
    tables, labels, make = fixtures()
    result = adapt_sources(tables, [*labels, make('a', 'missing', 1, 'train')])
    assert len(result['statistics']['missing_endpoint_pairs']) == 1
    assert len(result['records']) == 13


def test_duplicates_preserve_test_and_reject_invalid_source_ids():
    tables, labels, _ = fixtures()
    result = adapt_sources(tables, [*labels, labels[0] | {'original_split': 'train'}, labels[0]])
    assert result['statistics']['duplicate_pairs_collapsed'] == 2
    assert len(result['labels']) == 4
    bad = copy.deepcopy(tables)
    bad['forbes'].append(bad['forbes'][0])
    with pytest.raises(ValueError, match='unique'):
        adapt_sources(bad, labels)


def test_labels_cannot_be_inferred_or_coerced():
    tables, labels, _ = fixtures()
    with pytest.raises(ValueError, match='binary'):
        adapt_sources(tables, [labels[0] | {'label': True}])
