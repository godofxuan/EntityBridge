"""Local demo and API startup; no external deployment or account required."""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy.engine import URL

from .api import create_app
from .store import Store


def database_url(root, *, portable=False):
    if portable:
        config = json.loads((root / ".tools/database.json").read_text())
        return URL.create("postgresql+psycopg", username=config["user"], password=config["password"],
            host=config["host"], port=config["port"], database="entitybridge")
    return os.environ.get("DATABASE_URL", f"sqlite:///{(root / 'artifacts/demo.sqlite').as_posix()}")


def seed_demo(store):
    """Deliberately synthetic company names and addresses, isolated from benchmarks."""
    if store.current_revision():
        return store.current_revision()
    a = store.import_records("demo_erp", [
        {"source_key": "a", "name": "NORTHSTAR ENERGY LIMITED", "address": "10 EXAMPLE ROAD", "postcode": "ZZ1 1ZZ", "country": "GB"},
        {"source_key": "c", "name": "NORTHSTAR ENERGY SERVICES LIMITED", "address": "10 EXAMPLE ROAD", "postcode": "ZZ1 1ZZ", "country": "GB"},
        {"source_key": "d", "name": "HARBOUR DIGITAL LIMITED", "address": "22 DEMO STREET", "postcode": "ZZ2 2ZZ", "country": "GB"},
    ])["records"]
    b = store.import_records("demo_contracts", [
        {"source_key": "b", "name": "NORTHSTAR ENERGY LTD", "address": "10 EXAMPLE ROAD", "postcode": "ZZ1 1ZZ", "country": "GB"},
        {"source_key": "e", "name": "HARBOUR DIGITAL LTD", "address": "22 DEMO STREET", "postcode": "ZZ2 2ZZ", "country": "GB"},
    ])["records"]
    raw = store.active_records()
    edges = []
    for left, right, score in [(a[0], b[0], .99), (a[1], b[0], .96), (a[2], b[1], .78)]:
        l, r = left["record_id"], right["record_id"]
        edges.append({"left": l, "right": r, "left_version": left["record_version_id"],
            "right_version": right["record_version_id"], "score": score, "candidate_rules": ["synthetic_governance_challenge"],
            "evidence": {"kind": "synthetic_demo_score_not_a_model_measurement",
                "name": {"left": raw[l]["name"], "right": raw[r]["name"]},
                "address": {"left": raw[l]["address"], "right": raw[r]["address"]}}})
    first = store.prepare_revision(edges, policy_version="synthetic-governance-v1")["revision_id"]
    store.publish(first, expected_parent=None)
    return first


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["demo", "serve", "init-db"])
    parser.add_argument("--portable-postgres", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--candidate-model", type=Path, help="Frozen training-only retriever for optional hybrid review candidates")
    args = parser.parse_args()
    root = Path.cwd()
    (root / "artifacts").mkdir(exist_ok=True)
    store = Store(database_url(root, portable=args.portable_postgres), root / "artifacts/revisions")
    store.initialize()
    if args.command == "init-db":
        print("Database initialized")
        return
    if args.command == "demo":
        seed_demo(store)
    token_config = json.loads(os.environ.get("ENTITYBRIDGE_TOKENS", "{}"))
    if args.command == "serve" and not token_config:
        raise SystemExit("Set ENTITYBRIDGE_TOKENS to a JSON object of token: [reviewer_name, role] before serving")
    import uvicorn
    print(f"EntityBridge local URL: http://127.0.0.1:{args.port}")
    uvicorn.run(create_app(store, tokens=token_config, model_path=args.model, candidate_model_path=args.candidate_model,
                         local_demo=args.command == "demo"),
                host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
