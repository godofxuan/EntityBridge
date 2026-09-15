"""Run the predeclared finite company optimization protocol in two stages."""
import argparse
from pathlib import Path

from entitybridge.company_study import evaluate, prepare, resume_external


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "evaluate", "resume-external"))
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--previous-study", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--feiii-archive", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.stage == "prepare":
        if args.previous_study is None:
            parser.error("prepare needs --previous-study")
        prepare(root, args.dataset, args.previous_study, args.output)
    else:
        if args.feiii_archive is None:
            parser.error("evaluate needs --feiii-archive")
        action = resume_external if args.stage == "resume-external" else evaluate
        action(root, args.dataset, args.output, args.feiii_archive)


if __name__ == "__main__":
    main()
