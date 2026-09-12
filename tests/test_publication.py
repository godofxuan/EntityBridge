"""Publication must never smuggle internal history/data or omit runtime templates."""

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("publication", Path(__file__).parents[1] / "scripts/check_publication.py")
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


def test_public_export_excludes_private_material_and_preserves_code(tmp_path):
    root, output = tmp_path / "source", tmp_path / "public"
    files = {
        "README.md": "[Guide](docs/planning/private.md)\n[Usage](src/entitybridge/cli.py)\n",
        "pyproject.toml": '[project]\nname="entitybridge"\nversion="0.1.0"\n',
        "src/entitybridge/cli.py": "print('synthetic fixture')\n",
        "docs/planning/private.md": "PRIVATE_PLAN",
        "docs/RELEASE_FACTS.md": "PRIVATE_TASK",
        "docs/evaluation/evidence/run_report.json": '{"score": 0.5}',
        "docs/evaluation/evidence/pytest.log": "PRIVATE_PATH",
        ".tools/database.json": "PRIVATE_CREDENTIAL",
        "artifacts/raw.csv": "PRIVATE_DATA",
    }
    for name, value in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    manifest = publication.export_public(root, output, "synthetic-revision")
    names = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
    assert "docs/planning/private.md" not in names
    assert not any("PRIVATE_" in path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file())
    assert (output / "src/entitybridge/cli.py").read_bytes() == (root / "src/entitybridge/cli.py").read_bytes()
    assert "[Usage](src/entitybridge/cli.py)" in (output / "README.md").read_text(encoding="utf-8")
    hashes = json.loads((output / "docs/evaluation/evidence/SHA256.json").read_text())
    assert set(hashes) == {"run_report.json"}
    assert manifest["source_revision"] == "synthetic-revision"
    with pytest.raises(ValueError, match="new or empty"):
        publication.export_public(root, output)


def test_publication_rejects_escaping_paths_credentials_and_unsafe_nested_archive():
    for path in ("../README.md", "/README.md", "src/../../.env", "docs/planning/plan.md", ".env"):
        assert not publication.public_path(path)
    with pytest.raises(ValueError, match="credential"):
        publication.inspect_content("README.md", b"token=" + b"ghp_" + b"a" * 36)
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("artifacts/raw.csv", "synthetic")
    with pytest.raises(ValueError, match="nested archive"):
        publication.inspect_content("docs/evaluation/evidence/example.zip", buffer.getvalue())


def test_wheel_without_web_resources_fails_publication_check(tmp_path):
    wheel = tmp_path / "entitybridge-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("entitybridge/__init__.py", "")
    with pytest.raises(ValueError, match="runtime resources"):
        publication.check_wheel(wheel)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("entitybridge/web/templates/app.html", "<main>Fixture</main>")
        archive.writestr("entitybridge/web/static/app.css", "main {color: black;}")
    assert publication.check_wheel(wheel) == 3
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("entitybridge/../../.env", "synthetic")
    with pytest.raises(ValueError, match="Unsafe wheel member"):
        publication.check_wheel(wheel)
