"""Run an existing portable PostgreSQL archive inside this project on Windows.

No installer, system service, PATH edit, or administrator rights required.
Download the ZIP linked by postgresql.org/download/windows/ into .tools first.
"""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--port", type=int, default=55432)
    parser.add_argument("--ascii-root", type=Path, help="Optional subst drive alias for a non-ASCII Windows project path")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.ascii_root:
        if not args.ascii_root.exists() and os.name == "nt" and args.ascii_root.parent == args.ascii_root:
            subprocess.run(["subst", args.ascii_root.drive, str(root)], check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        if not args.ascii_root.samefile(root):
            raise ValueError("ASCII alias must refer to this exact project directory")
        root = args.ascii_root
    tools = root / ".tools"
    archive = tools / "postgresql-windows.zip"
    binary = tools / "pgsql" / "bin"
    data = tools / "pgdata"
    credential = tools / "database.json"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def run(executable, *arguments, check=True):
        # A daemon can inherit PIPE handles on Windows, keeping communicate()
        # waiting after pg_ctl exits. A file avoids waiting for daemon EOF.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([str(binary / executable), *map(str, arguments)], cwd=root,
                                    creationflags=flags, check=False, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=60)
            output.seek(0)
            result.stdout = output.read().decode("utf-8", errors="replace")
            result.stderr = ""
        if check and result.returncode:
            raise RuntimeError(f"{executable} exited {result.returncode}: {result.stdout}\n{result.stderr}")
        return result

    if args.action == "start":
        if not (binary / "pg_ctl.exe").exists():
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    if not member.filename.startswith(("pgsql/bin/", "pgsql/lib/", "pgsql/share/")):
                        continue
                    target = (tools / member.filename).resolve()
                    if not target.is_relative_to(tools.resolve()):
                        raise ValueError("Unsafe ZIP member")
                    bundle.extract(member, tools)
            (tools / "postgresql-manifest.json").write_text(json.dumps({
                "source": "https://sbp.enterprisedb.com/getfile.jsp?fileid=1260491",
                "sha256": hashlib.file_digest(archive.open("rb"), "sha256").hexdigest(),
                "bytes": archive.stat().st_size,
            }, indent=2), encoding="utf-8")
        if not data.exists():
            password = secrets.token_urlsafe(32)
            credential.write_text(json.dumps({"user": "entitybridge", "password": password,
                                              "host": "127.0.0.1", "port": args.port}), encoding="utf-8")
            password_file = tools / "pg-init-password"
            password_file.write_text(password, encoding="utf-8")
            try:
                result = run("initdb.exe", "-D", data, "-U", "entitybridge", "-A", "scram-sha-256",
                             "--pwfile", password_file, "--encoding=UTF8", "--locale=C")
                print(result.stdout)
            finally:
                password_file.unlink(missing_ok=True)
        config = json.loads(credential.read_text(encoding="utf-8"))
        if run("pg_ctl.exe", "-D", data, "status", check=False).returncode != 0:
            result = run("pg_ctl.exe", "-D", data, "-l", tools / "postgres.log", "-w", "start",
                         "-o", f"-h 127.0.0.1 -p {config['port']}")
            print(result.stdout)
        import psycopg
        from psycopg import sql
        with psycopg.connect(**config, dbname="postgres", autocommit=True, connect_timeout=10) as connection:
            for database in ("entitybridge", "entitybridge_test"):
                if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
                    connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
            print(connection.execute("SELECT version()").fetchone()[0])
        print("PostgreSQL listening on loopback; credentials retained only in ignored .tools/database.json")
    else:
        result = run("pg_ctl.exe", "-D", data, "-w", args.action, check=False)
        print(result.stdout or result.stderr)
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
