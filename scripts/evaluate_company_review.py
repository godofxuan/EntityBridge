"""Verify frozen inputs and compare company review rankings; default run records the data block."""

import argparse
import hashlib
import json
from pathlib import Path

from entitybridge.company_review_evaluation import evaluate_company_review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--predictions', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output must be new; do not overwrite earlier results')
    protocol_bytes = args.protocol.read_bytes()
    protocol = json.loads(protocol_bytes)
    fingerprint = lambda content: hashlib.sha256(content).hexdigest()
    if args.dataset is None or args.predictions is None:
        result = {'status': 'blocked_missing_independent_company_labels_and_frozen_rankings',
                  'protocol_status': protocol['status'], 'protocol_sha256': fingerprint(protocol_bytes),
                  'training_runs': 0, 'quality_metrics': None, 'auto_merge': False,
                  'reason': 'No independently reviewed company holdout was supplied; synthetic/old WDC results cannot substitute.'}
    else:
        dataset_bytes, prediction_bytes = args.dataset.read_bytes(), args.predictions.read_bytes()
        if protocol.get('status') != 'frozen' or protocol.get('dataset_sha256') != fingerprint(dataset_bytes):
            parser.error('Freeze a protocol with the exact dataset identity before scoring the holdout')
        if not isinstance(protocol.get('adoption_threshold'), dict) or not protocol['adoption_threshold']:
            parser.error('Freeze explicit business cost and label coverage acceptance criteria before holdout scoring')
        document = json.loads(prediction_bytes)
        if document.get('protocol_sha256') != fingerprint(protocol_bytes):
            parser.error('Predictions must refer to the exact frozen protocol')
        if set(document.get('model_fingerprints', {})) != {'baseline', 'ditto'} or any(
                not isinstance(value, str) or len(value) != 64
                or any(character not in '0123456789abcdef' for character in value)
                for value in document['model_fingerprints'].values()):
            parser.error('Both frozen model SHA256 identities are required')
        result = evaluate_company_review(json.loads(dataset_bytes), document['scores'], protocol)
        result.update(dataset_sha256=fingerprint(dataset_bytes), predictions_sha256=fingerprint(prediction_bytes),
                      protocol_sha256=fingerprint(protocol_bytes), model_fingerprints=document['model_fingerprints'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'status': result.get('status', 'evaluated_supplied_inputs'), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
