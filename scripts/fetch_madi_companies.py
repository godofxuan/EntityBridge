"""Fetch only pinned MaDI company inputs; retain raw data locally and verify every hash."""

import argparse
import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

from entitybridge.madi_benchmark import prepare_dataset

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    snapshot = json.loads((ROOT / 'docs/data/MADI_COMPANIES_SNAPSHOT.json').read_text(encoding='utf-8'))
    args.raw.mkdir(parents=True, exist_ok=True)
    for name, item in snapshot['files'].items():
        target = (args.raw / name).resolve()
        if not target.is_relative_to(args.raw.resolve()):
            raise ValueError('Snapshot path escapes raw directory')
        if target.exists():
            content = target.read_bytes()
        else:
            url = ('https://raw.githubusercontent.com/wbsg-uni-mannheim/MaDI-Bench/' + snapshot['commit'] + '/'
                   + urllib.parse.quote(item['path'], safe='/'))
            request = urllib.request.Request(url, headers={'User-Agent': 'EntityBridge-benchmark'})
            with urllib.request.urlopen(request, timeout=45) as response:
                content = response.read(5_000_001)
        if len(content) > 5_000_000 or len(content) != item['bytes'] or hashlib.sha256(content).hexdigest() != item['sha256']:
            raise ValueError('Pinned source changed or exceeded its size bound')
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + '.partial')
            with temporary.open('xb') as stream:
                stream.write(content)
            temporary.replace(target)
    result = prepare_dataset(args.raw, args.output, snapshot)
    print(json.dumps(result['statistics'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
