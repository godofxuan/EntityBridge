"""Startup-selected workspaces with separate databases, artifacts and identity scopes.

No HTTP path, header, or claim can switch an application's Store. Run one API and
worker deployment per selected workspace; this is isolation, not a tenant portal.
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


@dataclass(frozen=True)
class Workspace:
    name: str
    database_url: object
    artifact_root: Path
    tokens: dict
    oidc: dict | None


def load_workspace(path, name, *, environ=None):
    environment = os.environ if environ is None else environ
    path = Path(path).resolve()
    document = json.loads(path.read_text(encoding="utf-8"))
    if set(document) != {"version", "workspaces"} or document["version"] != 1:
        raise ValueError("Expected workspace configuration version 1")
    definitions = document["workspaces"]
    if not isinstance(definitions, dict) or name not in definitions:
        raise ValueError("Select a configured workspace at process startup")
    identities, artifacts, loaded, used_tokens = set(), [], {}, set()
    for key, value in definitions.items():
        if not isinstance(key, str) or not key or not isinstance(value, dict):
            raise ValueError("Invalid workspace definition")
        if set(value) - {"database_env", "artifact_root", "tokens_env", "oidc"}:
            raise ValueError("Unknown workspace configuration key")
        variable = value.get("database_env")
        if not isinstance(variable, str) or not environment.get(variable):
            raise ValueError("Every configured workspace requires its database environment variable")
        try:
            url = make_url(environment[variable])
        except (ArgumentError, ValueError, TypeError):
            raise ValueError("Invalid workspace database URL; details suppressed") from None
        if {"dbname", "database"} & url.query.keys():
            raise ValueError("Database overrides are not permitted in workspace URLs")
        if url.get_backend_name() == "sqlite":
            if not url.database or url.database == ":memory:" or url.query:
                raise ValueError("Workspace SQLite databases must be persistent files without URL overrides")
            database = Path(url.database)
            database = (path.parent / database).resolve() if not database.is_absolute() else database.resolve()
            url = url.set(database=str(database))
            identity = ("sqlite", os.path.normcase(str(database)))
        elif url.get_backend_name() == "postgresql" and url.database:
            if set(url.query) - {"connect_timeout", "sslmode", "sslrootcert", "sslcert", "sslkey"}:
                raise ValueError("Workspace PostgreSQL URLs only allow timeout and explicit TLS parameters")
            identity = ("postgresql", (url.host or "localhost").lower(), url.port or 5432, url.database)
        else:
            raise ValueError("Workspaces require SQLite files or PostgreSQL databases")
        if identity in identities:
            raise ValueError("Workspaces must not share a database, even under different credentials")
        identities.add(identity)
        raw_root = value.get("artifact_root")
        if not isinstance(raw_root, str) or not raw_root:
            raise ValueError("Every workspace needs its own artifact directory")
        root = (path.parent / raw_root).resolve()
        if any(root == old or root.is_relative_to(old) or old.is_relative_to(root) for old in artifacts):
            raise ValueError("Workspace artifact directories must be disjoint")
        artifacts.append(root)
        tokens = json.loads(environment.get(value.get("tokens_env", ""), "{}"))
        if not isinstance(tokens, dict) or used_tokens.intersection(tokens):
            raise ValueError("Static bearer tokens must be distinct across configured workspaces")
        used_tokens.update(tokens)
        oidc = value.get("oidc")
        if oidc is not None:
            if not isinstance(oidc, dict) or oidc.get("workspace") != key:
                raise ValueError("OIDC workspace scope must equal the startup workspace name")
            oidc = dict(oidc)
        loaded[key] = Workspace(key, url, root, tokens, oidc)
    return loaded[name]


def create_workspace_app(path, name, *, environ=None, model_path=None, candidate_model_path=None):
    from .api import create_app
    from .store import Store
    selected = load_workspace(path, name, environ=environ)
    verifier = None
    if selected.oidc:
        from .auth import OidcConfig, OidcVerifier
        verifier = OidcVerifier(OidcConfig(**selected.oidc))
    if not selected.tokens and verifier is None:
        raise ValueError("Workspace service requires configured authentication")
    store = Store(selected.database_url, selected.artifact_root)
    store.initialize()
    from .workspace_binding import bind_workspace
    bind_workspace(store, selected.name)
    return create_app(store, tokens=selected.tokens, oidc_verifier=verifier,
                      model_path=model_path, candidate_model_path=candidate_model_path)
