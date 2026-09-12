import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from entitybridge.workspaces import create_workspace_app, load_workspace


def configuration(tmp_path):
    data = {"version": 1, "workspaces": {
        name: {"database_env": name.upper() + "_DB", "tokens_env": name.upper() + "_TOKENS",
               "artifact_root": name + "/revisions"} for name in ("alpha", "beta")}}
    env = {"ALPHA_DB": "sqlite:///alpha.db", "BETA_DB": "sqlite:///beta.db",
           "ALPHA_TOKENS": json.dumps({"alpha-secret": ["alpha-user", "admin"]}),
           "BETA_TOKENS": json.dumps({"beta-secret": ["beta-user", "admin"]})}
    path = tmp_path / "workspaces.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data, env


def test_two_actual_services_keep_records_jobs_artifacts_and_tokens_separate(tmp_path):
    path, _, env = configuration(tmp_path)
    alpha = TestClient(create_workspace_app(path, "alpha", environ=env))
    beta = TestClient(create_workspace_app(path, "beta", environ=env))
    alpha.headers["Authorization"] = "Bearer alpha-secret"
    beta.headers["Authorization"] = "Bearer beta-secret"
    assert alpha.post("/imports", json={"source": "a", "rows": [{"source_key": "1", "name": "ALPHA ONLY"}]}).status_code == 200
    revision = alpha.post("/match-runs", json={"method": "exact"}).json()["revision_id"]
    assert alpha.post(f"/revisions/{revision}/publish", json={"expected_parent": None}).status_code == 200
    entity = alpha.get("/entities").json()[0]
    assert beta.get("/entities", headers={"X-Workspace": "alpha"}, params={"workspace": "alpha"}).json() == []
    assert beta.get(f"/entities/{entity['entity_id']}").status_code == 404
    submitted = alpha.post("/jobs", json={"settings": {"method": "exact"}, "idempotency_key": "same-key"}).json()
    assert beta.get(f"/jobs/{submitted['job_id']}").status_code == 404
    assert beta.get("/jobs").json() == []
    beta.headers["Authorization"] = "Bearer alpha-secret"
    assert beta.get("/jobs").status_code == 401
    assert list((tmp_path / "alpha/revisions").glob("*.json"))
    assert not list((tmp_path / "beta/revisions").glob("*.json"))


@pytest.mark.parametrize("change", ["same_database", "nested_artifacts", "scope", "override", "missing_env", "host_override", "shared_token"])
def test_workspace_misconfiguration_fails_before_creating_data(tmp_path, change):
    path, data, env = configuration(tmp_path)
    if change == "same_database":
        env["BETA_DB"] = "sqlite:///./alpha.db"
    elif change == "nested_artifacts":
        data["workspaces"]["beta"]["artifact_root"] = "alpha/revisions/nested"
    elif change == "scope":
        data["workspaces"]["alpha"]["oidc"] = {"workspace": "beta"}
    elif change == "override":
        env["BETA_DB"] = "postgresql+psycopg://localhost/beta?dbname=alpha"
    elif change == "host_override":
        env["BETA_DB"] = "postgresql+psycopg://shown/beta?host=actual"
    elif change == "shared_token":
        env["BETA_TOKENS"] = env["ALPHA_TOKENS"]
    else:
        del env["BETA_DB"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        create_workspace_app(path, "alpha", environ=env)
    assert not (tmp_path / "alpha.db").exists()


def test_different_postgresql_users_cannot_disguise_shared_database(tmp_path):
    path, _, env = configuration(tmp_path)
    env.update(ALPHA_DB="postgresql+psycopg://first@localhost/common", BETA_DB="postgresql+psycopg://second@localhost:5432/common")
    with pytest.raises(ValueError, match="must not share"):
        load_workspace(path, "alpha", environ=env)


def test_cli_backup_cannot_export_a_differently_bound_database_or_migrate_it(tmp_path):
    path, data, env = configuration(tmp_path)
    create_workspace_app(path, "alpha", environ=env)
    # A separate configuration hides the alias from configuration-wide checks.
    data["workspaces"] = {"beta": data["workspaces"]["beta"]}
    path.write_text(json.dumps(data), encoding="utf-8")
    env["BETA_DB"] = "sqlite:///./alpha.db"
    database = tmp_path / "alpha.db"
    before = database.read_bytes()
    output = tmp_path / "backup"
    result = subprocess.run([sys.executable, "-m", "entitybridge.cli", "backup", "--workspace-config", str(path),
                             "--workspace", "beta", "--output", str(output)], cwd=tmp_path,
                            env={**os.environ, **env, "PYTHONUTF8": "1",
                                 "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
                            capture_output=True, timeout=30, check=False)
    assert result.returncode != 0 and b"existing binding" in result.stderr
    assert not output.exists() and database.read_bytes() == before
