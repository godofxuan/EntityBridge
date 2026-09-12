"""Bind one database to one startup-selected workspace, including across aliases.

Only the logical name is durable: paths and hostnames can change on restore.
Call after Store.initialize, before a configured API or worker serves the store.
Plain Store users do not acquire an implicit workspace binding.
"""

from sqlalchemy import insert, select

from . import schema as s


class WorkspaceBindingConflict(ValueError):
    """This database is already owned by another configured workspace."""


def bind_workspace(store, name):
    if (not isinstance(name, str) or not name.strip() or len(name) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError("Workspace name must contain 1 to 200 characters without control characters")
    with store.engine.begin() as con:
        # Use the same database row lock as import, review, publication and jobs;
        # two first binders serialize even with distinct engines or URL aliases.
        store._lock(con)
        rows = con.execute(select(s.workspace_binding)).mappings().all()
        if not rows:
            con.execute(insert(s.workspace_binding).values(singleton=1, workspace_name=name))
        elif len(rows) != 1 or rows[0]["singleton"] != 1:
            raise WorkspaceBindingConflict("Invalid database workspace ownership record")
        elif rows[0]["workspace_name"] != name:
            raise WorkspaceBindingConflict("Database is already bound to a different workspace")
    return name
