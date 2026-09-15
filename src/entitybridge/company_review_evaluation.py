"""Fixed-candidate company review-budget comparisons; no training or deployment.

This verifies supplied provenance assertions and split structure, not whether
human annotations are true. Unknown labels consume budget and remain unknown.
"""

import math

from .benchmark_metrics import evaluate_labeled_pairs, review_budget_curve


def evaluate_company_review(dataset, predictions, protocol, *, allow_synthetic=False):
    if dataset.get('domain') != 'company':
        raise ValueError('Company-domain data is required')
    if dataset.get('synthetic') is not False and not allow_synthetic:
        raise ValueError('Synthetic data cannot certify company model quality')
    provenance = dataset.get('annotation_provenance', {})
    if not all(provenance.get(key) for key in ('source_reference', 'usage_terms', 'annotation_method',
                                               'review_record_reference')):
        raise ValueError('Human annotation provenance and usage terms are required')
    if dataset.get('holdout_status') != 'independent_frozen_before_scoring':
        raise ValueError('Independent frozen company holdout is required; old tests remain replay')
    if (protocol.get('auto_merge') is not False or protocol.get('primary_budget') != 100
            or protocol.get('secondary_budgets') != [25, 50]):
        raise ValueError('This protocol fixes review-only and the primary/secondary budgets')
    splits = dataset['partitions']
    if set(splits) != {'train', 'development', 'holdout'}:
        raise ValueError('Declare train, development and holdout record/entity membership')
    prior_records, prior_entities = set(), set()
    for name in ('train', 'development', 'holdout'):
        rows = splits[name]
        identifiers = {row['record_id'] for row in rows}
        entities = {row['entity_id'] for row in rows}
        if len(identifiers) != len(rows) or not identifiers or any(not isinstance(x, str) or not x for x in identifiers | entities):
            raise ValueError('Every partition needs unique record IDs and explicit entity identifiers')
        if identifiers & prior_records or entities & prior_entities:
            raise ValueError('Record or entity leakage across company partitions')
        prior_records.update(identifiers)
        prior_entities.update(entities)
    holdout = {row['record_id'] for row in splits['holdout']}

    def pair(row):
        value = tuple(sorted((row['left_id'], row['right_id'])))
        if value[0] == value[1] or not set(value) <= holdout:
            raise ValueError('Every evaluated pair must have distinct declared holdout endpoints')
        return value

    labels, seen = [], set()
    for row in dataset['labels']:
        key = pair(row)
        if key in seen or row['label'] not in (0, 1, None) or isinstance(row['label'], bool):
            raise ValueError('Duplicate, conflicting or invalid company pair labels')
        seen.add(key)
        if row['label'] is not None:
            labels.append({'left_id': key[0], 'right_id': key[1], 'label': row['label']})
    if {row['label'] for row in labels} != {0, 1}:
        raise ValueError('Both positive and negative known holdout labels are required')
    if set(predictions) != {'baseline', 'ditto'}:
        raise ValueError('Declare exactly one frozen baseline and one Ditto ranking')
    models, candidate_sets = {}, []
    for name, rows in predictions.items():
        scores = {}
        for row in rows:
            key = pair(row)
            score = row['score']
            if key in scores or isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise ValueError('Duplicate pairs or invalid model scores')
            scores[key] = score
        candidate_sets.append(set(scores))
        metrics = evaluate_labeled_pairs(labels, scores, scores, threshold=None, score_kind='similarity')
        models[name] = {'budgets': review_budget_curve(labels, scores, [100, 25, 50]),
                        'candidate_known_positive_recall': metrics['candidate_known_positive_recall'],
                        'unknown_candidate_pairs': metrics['unknown_candidate_pairs'],
                        'ranking_on_known_candidates': metrics['ranking']}
    if candidate_sets[0] != candidate_sets[1]:
        raise ValueError('The two rankings must score exactly the same frozen candidate set')
    delta = models['ditto']['budgets'][0]['known_positive_pairs'] - models['baseline']['budgets'][0]['known_positive_pairs']
    return {'scope': 'synthetic_engineering_fixture' if allow_synthetic else 'supplied_frozen_company_holdout',
            'provenance_verification': 'assertions and structural checks only; human truth requires external review',
            'partition_overlap_records': 0, 'partition_overlap_entities': 0, 'auto_merge': False,
            'known_labels': len(labels), 'models': models, 'primary_budget_known_positive_delta': delta,
            'adoption': 'undetermined_pending_cost_and_label_review',
            'cluster_metrics': None, 'cluster_reason': 'No certified complete entity partition supplied',
            'confidence_intervals': None, 'interval_reason': 'Independent sampling blocks not certified',
            'calibration': 'not_fitted_here_requires_separate_development_freeze',
            'human_time_saved': None}
