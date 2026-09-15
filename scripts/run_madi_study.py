"""Execute the preregistered MaDI study without overwriting earlier stages."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from entitybridge.madi_study import evaluate, prepare, train_neural

parser = argparse.ArgumentParser()
parser.add_argument('stage', choices=['prepare', 'train', 'evaluate'])
parser.add_argument('--dataset', type=Path, default=ROOT / 'artifacts/research_20260915/dataset')
parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/research_20260915/study')
parser.add_argument('--seed', type=int)
parser.add_argument('--base-model', type=Path, default=ROOT / 'artifacts/models/roberta-base')
parser.add_argument('--gpu-confirmed', action='store_true')
args = parser.parse_args()
if args.stage == 'prepare':
    prepare(ROOT, args.dataset, args.output)
elif args.stage == 'train':
    train_neural(ROOT, args.dataset, args.output, seed=args.seed, base_model=args.base_model,
                 gpu_confirmed=args.gpu_confirmed)
else:
    evaluate(ROOT, args.dataset, args.output)
