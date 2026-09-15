"""Frozen, staged company research; test features enter only the final stage."""
from __future__ import annotations

import gc
import json
import platform
import time
import zipfile
from collections import Counter
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from .benchmark_metrics import evaluate_labeled_pairs, group_bootstrap_intervals, review_budget_curve
from .benchmarks import file_hash, verify_benchmark
from .candidates import FrozenNameRetriever, generate_candidates
from .ditto_training import TrainingConfig, fit_ditto, select_policies
from .madi_benchmark import SOURCE_PAIRS
from .matching import SplinkMatcher, score_baselines
from .supervised import FrozenPairClassifier

CPU_METHODS = ('exact', 'fuzzy', 'splink', 'supervised_logistic')
TRACKS = ('fixed', 'hybrid_strict_country', 'hybrid_soft_country', 'supplied_pairs_only')


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def now():
    return datetime.now(UTC).isoformat()


def pair(row):
    return tuple(sorted((row.get('left', row.get('left_id')), row.get('right', row.get('right_id')))))


def read_partition(dataset, split):
    if split not in ('train', 'validation', 'test'):
        raise ValueError('Unknown partition')
    return (pq.read_table(dataset / 'matcher' / f'{split}.parquet').to_pylist(),
            pq.read_table(dataset / 'evaluator/labelled_pairs.parquet', filters=[('split', '=', split)]).to_pylist())


def candidates(records, labels, retriever):
    tracks = {name: [] for name in TRACKS}
    for a, b in SOURCE_PAIRS:
        subset = [r for r in records if r['source'] in (a, b)]
        tracks['fixed'].extend(generate_candidates(subset))
        tracks['hybrid_strict_country'].extend(generate_candidates(subset, retriever=retriever))
        tracks['hybrid_soft_country'].extend(generate_candidates(
            [r | {'country': None} for r in subset], retriever=retriever))
    tracks['supplied_pairs_only'] = [{'left': a, 'right': b, 'rules': ['supplied_pair_diagnostic']}
                                      for a, b in sorted({pair(r) for r in labels})]
    return {name: sorted(rows, key=pair) for name, rows in tracks.items()}


def candidate_union(tracks):
    unique = {pair(row): row for rows in tracks.values() for row in rows}
    return [unique[key] for key in sorted(unique)]


def values(edges):
    return {pair(row): float(row['score']) for row in edges}


def cpu_scores(records, tracks, output):
    union = candidate_union(tracks)
    scores = {name: values(rows) for name, rows in score_baselines(records, union).items()}
    scores['supervised_logistic'] = values(FrozenPairClassifier.load(output / 'logistic').score(records, union))
    index = {r['record_id']: r for r in records}
    scores['splink'] = {}
    for a, b in SOURCE_PAIRS:
        subset = [r for r in records if r['source'] in (a, b)]
        edges = [r for r in union if {index[x]['source'] for x in pair(r)} == {a, b}]
        model = SplinkMatcher.load(output / f'splink_{a}_{b}')
        scores['splink'].update(values(model.score(subset, edges)))
    expected = {pair(r) for r in union}
    if any(set(s) != expected for s in scores.values()):
        raise ValueError('All models must score exactly the common candidate union')
    return scores


def policies(labels, tracks, scores):
    return {track: {name: select_policies(labels, {pair(r): data[pair(r)] for r in rows}, exact=name == 'exact')
                    for name, data in scores.items()} for track, rows in tracks.items()}


def packed(scores):
    return {name: [[*key, score] for key, score in sorted(data.items())] for name, data in scores.items()}


def file_map(directory):
    return {p.relative_to(directory).as_posix(): file_hash(p) for p in sorted(directory.rglob('*')) if p.is_file()}


def check_hashes(root, expected):
    for relative, sha in expected.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or file_hash(path) != sha:
            raise ValueError(f'Frozen input changed: {relative}')


def guard(root, dataset, output):
    receipt = read_json(output / 'prepared.json')
    check_hashes(root, receipt['source_files'])
    check_hashes(dataset, receipt['dataset_files'])
    check_hashes(output, receipt['frozen_development_files'])
    return receipt


def prepare(root, dataset, output):
    if output.exists():
        raise ValueError('Study output must be new')
    protocol = read_json(root / 'docs/evaluation/MADI_COMPANIES_PROTOCOL.json')
    verification = verify_benchmark(dataset)
    output.mkdir(parents=True)
    source_files = list((root / 'src/entitybridge').rglob('*.py')) + [
        root / 'scripts/run_madi_study.py', root / 'scripts/fetch_madi_companies.py',
        root / 'docs/data/MADI_COMPANIES_SNAPSHOT.json', root / 'docs/evaluation/MADI_COMPANIES_PROTOCOL.json',
        root / 'docs/evaluation/MADI_IMPLEMENTATION_NOTES.md', root / 'requirements.lock',
        root / 'src/entitybridge/vendor/DITTO_LICENSE.md']
    source_hashes = {p.relative_to(root).as_posix(): file_hash(p) for p in sorted(source_files)}
    save(output / 'pre_fit.json', {'at': now(), 'test_features_read': False, 'source_files': source_hashes,
                                 'dataset_files': file_map(dataset), 'verification': verification})
    with zipfile.ZipFile(output / 'source.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for p in source_files:
            archive.write(p, p.relative_to(root).as_posix())
    train, train_labels = read_partition(dataset, 'train')
    validation, validation_labels = read_partition(dataset, 'validation')
    for split, labels in [('train', train_labels), ('validation', validation_labels)]:
        minimum = protocol['data_rules'][f'minimum_{split}']
        counts = Counter(r['label'] for r in labels)
        if len(labels) < minimum['pairs'] or any(counts[k] < minimum['each_class'] for k in (0, 1)):
            raise ValueError('Insufficient development labels')
    start = time.perf_counter()
    retriever = FrozenNameRetriever.fit(train, top_k=10, min_similarity=.2)
    retriever.save(output / 'retriever')
    logistic = FrozenPairClassifier.fit(train, train_labels)
    logistic.save(output / 'logistic')
    models = {'supervised_logistic': {'fingerprint': logistic.fingerprint, 'metadata': logistic.metadata}}
    for a, b in SOURCE_PAIRS:
        model = SplinkMatcher.fit([r for r in train if r['source'] in (a, b)])
        model.save(output / f'splink_{a}_{b}')
        models[f'splink_{a}_{b}'] = {'fingerprint': model.fingerprint, 'metadata': model.training_metadata}
    tracks = candidates(validation, validation_labels, retriever)
    save(output / 'validation_tracks.json', tracks)
    scores = cpu_scores(validation, tracks, output)
    save(output / 'validation_cpu_scores.json', packed(scores))
    save(output / 'validation_cpu_policies.json', policies(validation_labels, tracks, scores))
    save(output / 'prepared.json', {'prepared_at': now(), 'test_features_read': False,
        'source_files': source_hashes, 'dataset_files': file_map(dataset),
        'models': models, 'retriever_fingerprint': retriever.fingerprint,
        'candidate_counts': {name: len(rows) for name, rows in tracks.items()},
        'validation_union_pairs': len(candidate_union(tracks)), 'seconds': time.perf_counter() - start,
        'frozen_development_files': file_map(output)})
    print(json.dumps({'stage': 'prepared', 'counts': {n: len(r) for n, r in tracks.items()}}), flush=True)


def train_neural(root, dataset, output, *, seed, base_model, gpu_confirmed):
    guard(root, dataset, output)
    if (output / 'frozen_config.json').exists():
        raise ValueError('No training after test freeze')
    protocol = read_json(root / 'docs/evaluation/MADI_COMPANIES_PROTOCOL.json')
    if seed not in protocol['neural']['seeds'] or not gpu_confirmed:
        raise ValueError('Predeclared seed and explicit shared-GPU confirmation required')
    if file_hash(base_model / 'model.safetensors') != '5bde1d28afb363d0103324efeb5afc8b2b397fe5e04beabb9b1ef355255ade81':
        raise ValueError('Pinned base model differs')
    config = TrainingConfig(**{f.name: seed if f.name == 'seed' else protocol['neural'][f.name]
                              for f in fields(TrainingConfig)})
    train, labels = read_partition(dataset, 'train')
    validation, validation_labels = read_partition(dataset, 'validation')
    destination = output / f'ditto_seed{seed}'
    result = fit_ditto(train, labels, validation, validation_labels, domain='company', base_model=base_model,
        output=destination, config=config, device='cuda', provenance={
            'protocol_sha256': file_hash(root / 'docs/evaluation/MADI_COMPANIES_PROTOCOL.json'),
            'prepared_sha256': file_hash(output / 'prepared.json')})
    from .ditto import DittoMatcher
    matcher = DittoMatcher.load(destination / 'model', device='cuda', batch_size=16)
    tracks = read_json(output / 'validation_tracks.json')
    scores = {f'ditto_seed{seed}': matcher.score_pairs(validation, [pair(r) for r in candidate_union(tracks)], domain='company')}
    save(destination / 'validation_scores.json', packed(scores))
    save(destination / 'validation_policies.json', policies(validation_labels, tracks, scores))
    save(destination / 'ready.json', {'at': now(), 'training': result, 'files': file_map(destination),
                                    'test_features_read': False})
    print(json.dumps({'stage': 'neural_ready', 'seed': seed, 'training': result}), flush=True)


def paired_intervals(labels, scores, selections, *, n_resamples=1000, seed=20260915):
    """Paired cluster bootstrap on supplied labels; unknown pairs are inadmissible."""
    keys = [pair(r) for r in labels]
    if any(set(data) != set(keys) for data in scores.values()):
        raise ValueError('Paired review inference requires supplied labeled pairs only')
    group_names = sorted({r['group_id'] for r in labels})
    group_index = {g: i for i, g in enumerate(group_names)}
    groups = np.array([group_index[r['group_id']] for r in labels])
    group_sizes = np.bincount(groups)
    largest = int(max(group_sizes)) / len(labels)
    result = {'groups': len(group_names), 'largest_group_fraction': largest,
              'size_balance_effective_groups': float(len(labels)**2 / sum(group_sizes**2)),
              'replicates': n_resamples, 'seed': seed, 'confidence': .95,
              'scope': 'conditional fixed-pipeline paired dependency-component bootstrap'}
    if len(group_names) < 2 or largest > .5:
        return result | {'status': 'insufficient_independent_groups', 'intervals': {}}
    methods = list(scores)
    neural = [i for i, name in enumerate(methods) if name.startswith('ditto_seed')]
    if len(neural) != 3 or 'supervised_logistic' not in scores:
        raise ValueError('Primary comparison requires all three prespecified seeds')
    baseline = methods.index('supervised_logistic')
    y = np.array([r['label'] for r in labels])
    orders = [np.array(sorted(range(len(keys)), key=lambda i: (-scores[name][keys[i]], keys[i]))) for name in methods]
    predictions = {policy: np.array([[selections[name][policy]['threshold'] is not None and
        scores[name][key] >= selections[name][policy]['threshold'] for key in keys] for name in methods])
        for policy in ('f1', 'cost_10_1')}

    def metrics(weights):
        captures = []
        for order in orders:
            ordered_weights = weights[order]
            before = np.cumsum(ordered_weights) - ordered_weights
            used = np.minimum(ordered_weights, np.maximum(100 - before, 0))
            captures.append(float(used @ y[order]))
        totals = {'known_positives_at100': np.array(captures)}
        for policy, pred in predictions.items():
            tp = (pred * y) @ weights
            fp = (pred * (1 - y)) @ weights
            fn = ((1 - pred) * y) @ weights
            if policy == 'f1':
                denom = 2 * tp + fp + fn
                totals['f1'] = np.divide(2 * tp, denom, out=np.zeros_like(tp, dtype=float), where=denom != 0)
            else:
                totals['cost_10FP_FN'] = 10 * fp + fn
        return {name: float(np.mean(data[neural]) - data[baseline]) for name, data in totals.items()}

    random = np.random.default_rng(seed)
    draws = [metrics(np.bincount(random.integers(0, len(group_names), len(group_names)),
                                minlength=len(group_names))[groups]) for _ in range(n_resamples)]
    estimates = metrics(np.ones(len(labels), dtype=int))
    return result | {'status': 'estimated', 'difference': 'mean_three_ditto_seeds_minus_logistic',
        'intervals': {name: {'estimate': value, 'interval': np.quantile([r[name] for r in draws], [.025, .975]).tolist()}
                      for name, value in estimates.items()}}


def slice_predicates(index):
    def country_conflict(key):
        a, b = [index[k] for k in key]
        return bool(a.get('country') and b.get('country') and a['country'] != b['country'])
    return {**{f'{a}_to_{b}': lambda key, a=a, b=b: {index[k]['source'] for k in key} == {a, b}
               for a, b in SOURCE_PAIRS},
            'at_least_one_missing_city': lambda key: any(not index[k].get('city') for k in key),
            'both_city_present': lambda key: all(index[k].get('city') for k in key),
            'country_conflict': country_conflict,
            'no_country_conflict': lambda key: not country_conflict(key)}


def evaluate(root, dataset, output):
    prepared = guard(root, dataset, output)
    if (output / 'frozen_config.json').exists():
        raise ValueError('Test stage cannot be replayed into the original study')
    protocol = read_json(root / 'docs/evaluation/MADI_COMPANIES_PROTOCOL.json')
    thresholds = read_json(output / 'validation_cpu_policies.json')
    neural_receipts = {}
    for seed in protocol['neural']['seeds']:
        name = f'ditto_seed{seed}'
        destination = output / name
        ready = read_json(destination / 'ready.json')
        check_hashes(destination, ready['files'])
        neural_receipts[name] = file_hash(destination / 'ready.json')
        for track, data in read_json(destination / 'validation_policies.json').items():
            thresholds[track].update(data)
    freeze = {'at': now(), 'prepared_sha256': file_hash(output / 'prepared.json'),
              'neural_receipts': neural_receipts, 'thresholds': thresholds,
              'test_features_read_after_this_file': True, 'source_files': prepared['source_files']}
    save(output / 'frozen_config.json', freeze)
    # No train/validation model or threshold is selectable after this boundary.
    started = time.perf_counter()
    records, labels = read_partition(dataset, 'test')
    tracks = candidates(records, labels, FrozenNameRetriever.load(output / 'retriever'))
    save(output / 'test_tracks.json', tracks)
    begin = time.perf_counter()
    scores = cpu_scores(records, tracks, output)
    timings = {'cpu_all_methods_scoring_seconds': time.perf_counter() - begin}
    save(output / 'test_cpu_scores.json', packed(scores))
    import torch

    from .ditto import DittoMatcher
    for name in neural_receipts:
        begin = time.perf_counter()
        matcher = DittoMatcher.load(output / name / 'model', device='cuda', batch_size=16)
        scores[name] = matcher.score_pairs(records, [pair(r) for r in candidate_union(tracks)], domain='company')
        timings[f'{name}_load_and_score_seconds'] = time.perf_counter() - begin
        save(output / f'test_{name}_scores.json', packed({name: scores[name]}))
        del matcher
        gc.collect()
        torch.cuda.empty_cache()
        print(json.dumps({'stage': 'test_scored', 'method': name, 'pairs': len(scores[name])}), flush=True)
    source_counts = Counter(r['source'] for r in records)
    universe = sum(source_counts[a] * source_counts[b] for a, b in SOURCE_PAIRS)
    index = {r['record_id']: r for r in records}
    slices = slice_predicates(index)
    results = {}
    for track, rows in tracks.items():
        keys = {pair(r) for r in rows}
        results[track] = {}
        for name, all_scores in scores.items():
            data = {key: all_scores[key] for key in keys}
            result = {'review_budget': review_budget_curve(labels, data, [25, 50, 100]), 'policies': {}}
            for policy, selected in thresholds[track][name].items():
                threshold = selected['threshold']
                metric = evaluate_labeled_pairs(labels, data, data, threshold=threshold, universe_pair_count=universe,
                    score_kind='similarity' if name in ('exact', 'fuzzy') else 'probability')
                metric['cost_10FP_FN'] = 10 * metric['end_to_end']['fp'] + metric['end_to_end']['fn']
                metric['group_bootstrap'] = group_bootstrap_intervals(labels, data, data, threshold=threshold,
                                                                     n_resamples=1000, seed=20260915)
                metric['slices'] = {}
                for slice_name, includes in slices.items():
                    subset = [r for r in labels if includes(pair(r))]
                    slice_scores = {key: value for key, value in data.items() if includes(key)}
                    metric['slices'][slice_name] = evaluate_labeled_pairs(subset, slice_scores, slice_scores, threshold=threshold)
                result['policies'][policy] = metric
            results[track][name] = result
    supplied = {pair(r) for r in tracks['supplied_pairs_only']}
    paired = paired_intervals(labels, {name: {key: data[key] for key in supplied} for name, data in scores.items()},
                              thresholds['supplied_pairs_only'])
    report = {'completed_at': now(), 'status': 'completed_all_prespecified_models',
              'test_status': 'first_scoring_after_global_freeze', 'protocol': protocol,
              'frozen_config_sha256': file_hash(output / 'frozen_config.json'),
              'test_records': len(records), 'test_label_counts': dict(Counter(str(r['label']) for r in labels)),
              'test_source_counts': dict(source_counts), 'candidate_counts': {n: len(r) for n, r in tracks.items()},
              'results': results, 'primary_paired_comparison': paired, 'timings': timings,
              'seconds': time.perf_counter() - started, 'platform': platform.platform(),
              'python': platform.python_version(), 'gpu': torch.cuda.get_device_name(),
              'limitations': ['Partial public labels, not production or legal entity ground truth',
                'Protected entity split and reduced features differ from paper leaderboard',
                'Unknown predictions excluded from labelled precision; no overall deployment precision claim',
                'Class prevalence changed after isolation; probability calibration is sample conditional',
                'Three seeds on one development split; fixed-pipeline intervals exclude training uncertainty',
                'Review budget is known-positive yield, not measured human time saved',
                'Published benchmark may be in foundation-model pretraining; not verified unseen']}
    save(output / 'report.json', report)
    save(output / 'evidence_sha256.json', file_map(output))
    print(json.dumps({'stage': 'completed', 'primary': paired, 'seconds': report['seconds']}), flush=True)
