"""Check or export the explicit public source boundary; never publish or change Git.

Export into a new/empty directory, then initialize its independent public history.
All application code and dependency files remain byte-identical. Evidence hashes
are regenerated over the included evidence; a manifest records every source hash.
This is a publication hygiene check, not a complete secret scanner or license grant.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    "README.md", "THIRD_PARTY_NOTICES.md", "pyproject.toml", "requirements.lock", "requirements-neural.lock",
    "compose.yaml", "alembic.ini", ".gitignore", ".gitattributes", ".env.example", "LICENSE",
    "PUBLICATION_MANIFEST.json",
}
SOURCE_DIRS = {"src", "scripts", "tests", "migrations", ".github"}
DOC_DIRS = {"data", "decisions", "demo", "evaluation"}
PUBLIC_REVIEW = {
    "MATURITY_AND_POSITIONING.md", "MATCHING_AUDIT.md", "STORE_AUDIT.md", "SECURITY_AND_API.md",
}
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".hypothesis"}
BAD_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".parquet", ".csv", ".jsonl"}
REQUIRED_WHEEL = {"entitybridge/web/templates/app.html", "entitybridge/web/static/app.css"}
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub credential": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})"),
    "AWS access key": re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "personal workspace path": re.compile(rb"[A-Za-z]:[/\\](?:Users[/\\]|\xe6\x96\x87\xe6\xa1\xa3[/\\])"),
}


def public_path(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    parts = path.parts
    if not parts or path.is_absolute() or ".." in parts or ":" in parts[0]:
        return False
    if any(part in SKIP_PARTS or part.endswith(".egg-info") for part in parts):
        return False
    if path.suffix.lower() in BAD_SUFFIXES or path.name in {"database.json", "truth_map.json"}:
        return False
    if len(parts) == 1:
        return name in ROOT_FILES
    if parts[0] in SOURCE_DIRS:
        return path.suffix in {".py", ".html", ".css", ".mako", ".yml", ".yaml", ".md"}
    if parts[0] != "docs" or len(parts) < 3:
        return False
    if parts[1] == "review":
        return len(parts) == 3 and parts[2] in PUBLIC_REVIEW
    return parts[1] in DOC_DIRS and path.suffix.lower() in {
        ".md", ".json", ".html", ".png", ".mp4", ".srt", ".txt", ".zip",
    }


def inspect_content(name: str, content: bytes) -> None:
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(content):
            raise ValueError(f"Publication rejected: {label} in {name}; matched value suppressed")
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 50_000_000:
                raise ValueError(f"Unexpected nested archive size: {name}")
            for item in archive.infolist():
                if item.is_dir():
                    continue
                if not public_path(item.filename) or item.filename.endswith(".zip"):
                    raise ValueError(f"Disallowed nested archive member: {name}:{item.filename}")
                inspect_content(f"{name}:{item.filename}", archive.read(item))


def read_checked(root: Path, name: str) -> bytes:
    if not public_path(name):
        raise ValueError(f"Not in the public allowlist: {name}")
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Symlink or escaping path: {name}")
    content = path.read_bytes()
    inspect_content(name, content)
    return content


def export_public(root: Path, destination: Path, source_revision: str | None = None) -> dict:
    root, destination = root.resolve(), destination.resolve()
    if destination == root or (destination.exists() and any(destination.iterdir())):
        raise ValueError("Public export requires a new or empty directory distinct from the source")
    selected = []
    for top in sorted(ROOT_FILES | SOURCE_DIRS | {"docs"}):
        path = root / top
        if path.is_dir():
            selected.extend(p for p in path.rglob("*") if p.is_file() and public_path(p.relative_to(root).as_posix()))
        elif path.is_file() and top != "PUBLICATION_MANIFEST.json":
            selected.append(path)
    contents = {p.relative_to(root).as_posix(): read_checked(root, p.relative_to(root).as_posix())
                for p in sorted(selected)}
    if "README.md" not in contents or "pyproject.toml" not in contents:
        raise ValueError("Source root must contain README.md and pyproject.toml")
    source_hashes = {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
    # Remove links to excluded local-only material, retaining their readable labels.
    link = re.compile(r"(!?)\[([^\]]+)\]\(([^)]+)\)")
    transformations = []
    for name, content in list(contents.items()):
        if not name.endswith(".md"):
            continue
        def rewrite(match, document_name=name):
            target = match[3].strip("<>").split("#", 1)[0]
            if not target or "://" in target or target.startswith(("#", "mailto:")):
                return match[0]
            resolved = (root / PurePosixPath(document_name).parent / target).resolve()
            if not resolved.is_relative_to(root) or resolved.relative_to(root).as_posix() not in contents:
                return match[2] + "（本地材料，未包含在公开导出中）"
            return match[0]
        updated = link.sub(rewrite, content.decode("utf-8")).encode("utf-8")
        if updated != content:
            transformations.append(name)
            contents[name] = updated
    evidence_prefix = "docs/evaluation/evidence/"
    evidence_hashes = {name.removeprefix(evidence_prefix): hashlib.sha256(value).hexdigest()
                       for name, value in contents.items()
                       if name.startswith(evidence_prefix) and name != evidence_prefix + "SHA256.json"}
    if evidence_hashes:
        name = evidence_prefix + "SHA256.json"
        contents[name] = (json.dumps(evidence_hashes, indent=2, sort_keys=True) + "\n").encode()
        transformations.append(name)
    config = tomllib.loads(contents["pyproject.toml"].decode())
    manifest = {
        "format": "entitybridge-public-source-v1", "project_version": config["project"]["version"],
        "source_revision": source_revision, "history": "Exported working tree; no original Git history included",
        "license": "No license selected by exporter; see included LICENSE if the author provided one",
        "excluded": ["internal planning/status", "runtime data/databases/credentials", "test logs", "Git history"],
        "transformed_documentation": transformations,
        "files": {name: {"sha256": hashlib.sha256(value).hexdigest(),
                          "source_sha256": source_hashes.get(name), "bytes": len(value)}
                  for name, value in sorted(contents.items())},
    }
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in contents.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    (destination / "PUBLICATION_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def check_wheel(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        names = {item.filename for item in archive.infolist() if not item.is_dir()}
        missing = REQUIRED_WHEEL - names
        if "entitybridge/vendor/ditto_model.py" in names and "entitybridge/vendor/DITTO_LICENSE.md" not in names:
            missing.add("entitybridge/vendor/DITTO_LICENSE.md")
        if missing:
            raise ValueError(f"Wheel is missing runtime resources: {sorted(missing)}")
        for name in names:
            member = PurePosixPath(name)
            if member.is_absolute() or ".." in member.parts or "\\" in name or ":" in name:
                raise ValueError(f"Unsafe wheel member: {name}")
            first = member.parts[0]
            if first != "entitybridge" and not first.endswith(".dist-info"):
                raise ValueError(f"Unexpected wheel content: {name}")
            if first == "entitybridge" and member.suffix not in {".py", ".html", ".css"} and name != "entitybridge/vendor/DITTO_LICENSE.md":
                raise ValueError(f"Unexpected application data in wheel: {name}")
            content = archive.read(name)
            inspect_content(name, content)
            if name in REQUIRED_WHEEL and not content.strip():
                raise ValueError(f"Empty runtime resource: {name}")
        return len(names)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--export-dir", type=Path)
    parser.add_argument("--source-revision", help="Optional caller-supplied local source revision for provenance")
    parser.add_argument("--tracked", action="store_true", help="Read Git's tracked file list and check it")
    parser.add_argument("--wheel-dir", type=Path)
    args = parser.parse_args()
    if not any((args.export_dir, args.tracked, args.wheel_dir)):
        parser.error("Choose --export-dir, --tracked, or --wheel-dir")
    if args.export_dir:
        manifest = export_public(args.root, args.export_dir, args.source_revision)
        print(f"Exported {len(manifest['files'])} checked files; original Git history excluded")
    if args.tracked:
        output = subprocess.run(["git", "ls-files", "-z"], cwd=args.root, check=True, capture_output=True).stdout
        paths = [item.decode("utf-8") for item in output.split(b"\0") if item]
        if not paths:
            raise ValueError("No tracked files; cannot verify an empty repository")
        for name in paths:
            read_checked(args.root, name)
        print(f"Verified {len(paths)} tracked public files")
    if args.wheel_dir:
        wheels = sorted(args.wheel_dir.glob("entitybridge-*.whl"))
        if len(wheels) != 1:
            raise ValueError("Wheel directory must contain exactly one EntityBridge wheel")
        print(f"Verified wheel with {check_wheel(wheels[0])} files and runtime HTML/CSS resources")


if __name__ == "__main__":
    main()
