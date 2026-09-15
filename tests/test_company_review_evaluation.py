from copy import deepcopy

import pytest

from entitybridge.company_review_evaluation import evaluate_company_review


def fixture():
    dataset = {'domain': 'company', 'synthetic': True, 'holdout_status': 'independent_frozen_before_scoring',
        'annotation_provenance': {key: 'synthetic test only' for key in
            ('source_reference', 'usage_terms', 'annotation_method', 'review_record_reference')},
        'partitions': {'train': [{'record_id': 'train', 'entity_id': 'train'}],
                       'development': [{'record_id': 'dev', 'entity_id': 'dev'}],
                       'holdout': [{'record_id': str(i), 'entity_id': str(i)} for i in range(5)]},
        'labels': [{'left_id': '0', 'right_id': '1', 'label': 1},
                   {'left_id': '0', 'right_id': '2', 'label': 0},
                   {'left_id': '0', 'right_id': '3', 'label': None},
                   {'left_id': '1', 'right_id': '4', 'label': 1}]}
    scores = [{'left_id': '0', 'right_id': str(i), 'score': i / 4} for i in range(1, 4)]
    predictions = {'baseline': scores, 'ditto': deepcopy(scores)}
    protocol = {'auto_merge': False, 'primary_budget': 100, 'secondary_budgets': [25, 50]}
    return dataset, predictions, protocol


def test_unknown_consumes_budget_and_positive_outside_candidates_is_missed():
    data, predictions, protocol = fixture()
    result = evaluate_company_review(data, predictions, protocol, allow_synthetic=True)
    model = result['models']['baseline']
    assert model['candidate_known_positive_recall'] == .5
    assert model['budgets'][0]['unknown_reviewed_pairs'] == 1
    assert model['budgets'][0]['known_positive_recall'] == .5
    assert result['primary_budget_known_positive_delta'] == 0
    assert result['human_time_saved'] is None and result['cluster_metrics'] is None


@pytest.mark.parametrize('damage', ['record_leak', 'entity_leak', 'duplicate', 'candidates', 'score', 'replay'])
def test_invalid_comparisons_are_rejected(damage):
    data, predictions, protocol = fixture()
    if damage == 'record_leak':
        data['partitions']['train'][0]['record_id'] = '0'
    elif damage == 'entity_leak':
        data['partitions']['train'][0]['entity_id'] = '0'
    elif damage == 'duplicate':
        data['labels'].append(data['labels'][0])
    elif damage == 'candidates':
        predictions['ditto'].pop()
    elif damage == 'score':
        predictions['ditto'][0]['score'] = float('nan')
    else:
        data['holdout_status'] = 'exploratory-replayed'
    with pytest.raises(ValueError):
        evaluate_company_review(data, predictions, protocol, allow_synthetic=True)


def test_synthetic_provenance_cannot_pass_real_quality_gate():
    with pytest.raises(ValueError, match='Synthetic'):
        evaluate_company_review(*fixture())
