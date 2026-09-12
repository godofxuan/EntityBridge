"""Run the same public persistence tests against an isolated real PostgreSQL schema."""
import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import URL

root = Path(__file__).resolve().parents[1]
config = json.loads((root / ".tools/database.json").read_text())
url = URL.create("postgresql+psycopg", username=config["user"], password=config["password"],
                 host=config["host"], port=config["port"], database="entitybridge_test")
environment = dict(os.environ, ENTITYBRIDGE_TEST_DATABASE_URL=url.render_as_string(hide_password=False))
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "tests/test_store.py", "-q"],
                                cwd=root, env=environment))
