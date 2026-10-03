"""Importing scopewatch.app must not create a database (#133)."""

import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _run(code: str, db_path: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env["SCOPEWATCH_DB_PATH"] = str(db_path)
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def test_importing_the_module_does_not_create_the_default_database(tmp_path: Path) -> None:
    db_path = tmp_path / "must-not-exist.db"
    proc = _run("import scopewatch.app; from scopewatch.app import create_app", db_path)
    assert proc.returncode == 0, proc.stderr
    assert not db_path.exists(), "importing scopewatch.app created a database as a side effect"


def test_default_app_is_built_on_first_access_and_cached(tmp_path: Path) -> None:
    db_path = tmp_path / "default.db"
    code = (
        "import scopewatch.app as m; "
        "a = m.app; b = m.app; "
        "assert a is b; "
        "print(type(a).__name__)"
    )
    proc = _run(code, db_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "FastAPI"
    assert db_path.exists(), "the default app should initialise its database when first used"


def test_uvicorn_style_import_string_still_resolves(tmp_path: Path) -> None:
    db_path = tmp_path / "uvicorn.db"
    code = (
        "from uvicorn.importer import import_from_string; "
        "app = import_from_string('scopewatch.app:app'); "
        "print(type(app).__name__)"
    )
    proc = _run(code, db_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "FastAPI"


def test_startup_uses_the_paths_given_to_create_app_not_the_default(tmp_path: Path) -> None:
    default_db = tmp_path / "default-must-not-exist.db"
    explicit_db = tmp_path / "explicit.db"
    code = (
        "import warnings; warnings.filterwarnings('ignore'); "
        "from fastapi.testclient import TestClient; "
        "from scopewatch.app import create_app; "
        f"app = create_app(db_path={str(explicit_db)!r}, workspace_root={str(tmp_path / 'ws')!r}); "
        "tc = TestClient(app); tc.__enter__(); "
        "assert tc.get('/api/v1/health').status_code == 200; "
        "tc.__exit__(None, None, None)"
    )
    proc = _run(code, default_db)
    assert proc.returncode == 0, proc.stderr
    assert explicit_db.exists()
    assert not default_db.exists(), "app startup initialised the default database instead of the one passed in"
