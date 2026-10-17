"""Issue #204: the coding demo must not silently deny every run_command.

``scripts/run_demo.sh --coding`` seeds scenarios 10-13, and two of those
scenarios submit ``run_command`` actions. The deterministic policy refuses
``run_command`` for every executor backend except Docker
(``policy.py`` ``RULE_RUN_COMMAND_REQUIRES_DOCKER``), while the launcher never
selected one, so a judge running the quick-start saw two
``DENY [UNSUPPORTED_OPERATION]`` receipts and concluded the coding track was
broken. The gateway was failing closed correctly; the demo was lying by
omission.

These tests drive the real launcher in ``--seed-only`` mode against a seeded
evidence database, with a stubbed ``docker`` CLI on ``PATH``, so both branches
of the preflight run on a machine with no daemon:

* no daemon -> a loud warning names the reason and the denial code, and
* a reachable daemon -> the Docker executor is selected and the allowlisted
  ``python -m pytest`` actions execute instead of being denied.

No Docker daemon is required: the stub answers ``docker info`` and emits the
in-container helper's JSON contract for ``docker run``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_DEMO = REPO_ROOT / "scripts" / "run_demo.sh"

# The coding scenarios whose run_command actions must execute on a daemon.
CODING_RUN_NAME = "Coding Fix: Auth Boundary Test"
EXFIL_RUN_NAME = "Coding Blocked Network Exfiltration"

# `docker info` reports a reachable daemon; `docker run` emits the contract the
# in-container helper (scopewatch/docker_job.py::helper_code) prints.
DOCKER_STUB = """#!/bin/sh
if [ "$1" = "info" ]; then
  exit 0
fi
if [ "$1" = "run" ]; then
  printf '%s' '{"status":"EXECUTED","result":{"operation":"run_command","argv":["python","-m","pytest"],"exit_code":0,"stdout":"2 passed in stub","stderr":"","truncated_stdout":false,"truncated_stderr":false,"timed_out":false},"error_code":null}'
fi
exit 0
"""

# `docker info` fails: the CLI is installed but no daemon answers.
DOCKER_DAEMON_DOWN_STUB = """#!/bin/sh
echo "Cannot connect to the Docker daemon" >&2
exit 1
"""


def _bin_dir(tmp_path: Path, name: str, script: str) -> Path:
    """Create a directory holding one executable stub called ``name``."""
    bin_dir = tmp_path / f"bin-{name}"
    bin_dir.mkdir()
    stub = bin_dir / name
    stub.write_text(script, encoding="utf-8")
    stub.chmod(0o755)
    return bin_dir


def _seed_dirs(tmp_path: Path) -> dict[str, Path]:
    """Isolated database, workspace, run-workspace root and PATH prefix."""
    root = tmp_path / "demo"
    (root / "run-workspaces").mkdir(parents=True, exist_ok=True)
    return {
        "db": root / "scopewatch-coding.db",
        "workspace": root / "coding-workspace",
        "run_workspaces": root / "run-workspaces",
    }


def _run_coding_demo(
    tmp_path: Path, docker_stub: str | None
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    """Run the launcher's coding preflight without starting the gateway."""
    paths = _seed_dirs(tmp_path)
    env = dict(os.environ)
    # Never inherit an executor choice: the preflight has to make its own.
    env.pop("SCOPEWATCH_EXECUTOR", None)
    env["SCOPEWATCH_RUN_WORKSPACES_DIR"] = str(paths["run_workspaces"])
    env["SCOPEWATCH_CAPTURE_TOKEN"] = "synthetic-capture-token"
    if docker_stub is None:
        # No `docker` anywhere on PATH. The launcher still needs a shell,
        # coreutils, and this interpreter, so keep only what it can reach
        # through symlinks rather than the inherited PATH.
        minimal = tmp_path / "bin-minimal"
        minimal.mkdir(exist_ok=True)
        for tool in ("bash", "sh", "mkdir", "dirname"):
            found = shutil.which(tool)
            if found:
                (minimal / tool).symlink_to(found)
        python_link = minimal / "python3"
        if not python_link.exists():
            python_link.symlink_to(Path(sys.executable))
        env["PATH"] = str(minimal)
    else:
        env["PATH"] = f"{_bin_dir(tmp_path, 'docker', docker_stub)}:{env['PATH']}"

    proc = subprocess.run(
        [
            "bash",
            str(RUN_DEMO),
            "--coding",
            "--seed-only",
            "--db-path",
            str(paths["db"]),
            "--workspace-root",
            str(paths["workspace"]),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return proc, paths


def _run_command_rows(db_path: Path) -> dict[str, list[tuple[str, str, str]]]:
    """Map each coding run name to its (outcome, reason_code, receipt) rows."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT runs.name, decisions.outcome, decisions.reason_code,
                   COALESCE(receipts.status, 'NONE')
            FROM action_requests AS actions
            JOIN runs ON runs.id = actions.run_id
            JOIN policy_decisions AS decisions
                 ON decisions.action_request_id = actions.id
            LEFT JOIN execution_receipts AS receipts
                 ON receipts.action_request_id = actions.id
            WHERE actions.operation = 'run_command'
            """
        ).fetchall()
    finally:
        conn.close()
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for run_name, outcome, reason_code, receipt_status in rows:
        grouped.setdefault(run_name, []).append(
            (outcome, reason_code, receipt_status)
        )
    return grouped


def test_coding_help_states_the_docker_requirement() -> None:
    """``--coding`` help must name the executor requirement up front."""
    proc = subprocess.run(
        ["bash", str(RUN_DEMO), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0
    assert "--coding" in proc.stdout
    assert "Docker" in proc.stdout
    # The requirement must be visible, not a silent dependency: the operator
    # can read which variable the launcher will set.
    assert "SCOPEWATCH_EXECUTOR=docker" in proc.stdout


@pytest.mark.parametrize(
    "stub",
    [None, DOCKER_DAEMON_DOWN_STUB],
    ids=["no-docker-cli", "daemon-unreachable"],
)
def test_coding_without_a_daemon_warns_instead_of_denying_silently(
    tmp_path: Path, stub: str | None
) -> None:
    """No daemon: the reason is printed before seeding, and the DB agrees."""
    proc, paths = _run_coding_demo(tmp_path, stub)
    assert proc.returncode == 0, proc.stderr

    output = proc.stdout + proc.stderr
    assert "WARNING: --coding needs the Docker executor" in output
    assert "DENY [UNSUPPORTED_OPERATION]" in output

    # The warning must precede seeding, not follow it.
    rows = _run_command_rows(paths["db"])
    denied = rows.get(CODING_RUN_NAME, [])
    assert denied, "expected the coding fix scenario's run_command actions"
    for outcome, reason_code, receipt in denied:
        assert outcome == "DENY"
        assert reason_code == "UNSUPPORTED_OPERATION"
        assert receipt == "NOT_EXECUTED"


def test_coding_with_a_daemon_executes_the_allowlisted_run_command(
    tmp_path: Path,
) -> None:
    """A reachable daemon selects Docker, and the pytest run executes."""
    proc, paths = _run_coding_demo(tmp_path, DOCKER_STUB)
    assert proc.returncode == 0, proc.stderr
    assert "WARNING: --coding needs the Docker executor" not in (
        proc.stdout + proc.stderr
    )
    assert "Executor: docker" in proc.stdout

    rows = _run_command_rows(paths["db"])
    fix_rows = rows.get(CODING_RUN_NAME, [])
    assert len(fix_rows) == 2, f"scenario 10 submits two run_command actions: {fix_rows}"
    for outcome, reason_code, receipt in fix_rows:
        assert (outcome, reason_code, receipt) == (
            "ALLOW",
            "ALLOWED_TOOL_AND_RESOURCE",
            "EXECUTED",
        )

    # Scenario 13's curl is still refused: the scope allows only
    # `python -m pytest`. The refusal is a policy decision, not a missing daemon.
    exfil_rows = rows.get(EXFIL_RUN_NAME, [])
    assert len(exfil_rows) == 1
    assert exfil_rows[0][1] == "COMMAND_NOT_ALLOWED"


def test_an_explicit_executor_choice_is_reported_not_overridden(
    tmp_path: Path,
) -> None:
    """SCOPEWATCH_EXECUTOR wins, and the launcher says which one is in use."""
    paths = _seed_dirs(tmp_path)
    env = dict(os.environ)
    env["SCOPEWATCH_EXECUTOR"] = "local"
    env["SCOPEWATCH_RUN_WORKSPACES_DIR"] = str(paths["run_workspaces"])
    env["SCOPEWATCH_CAPTURE_TOKEN"] = "synthetic-capture-token"
    env["PATH"] = f"{_bin_dir(tmp_path, 'docker', DOCKER_STUB)}:{env['PATH']}"

    proc = subprocess.run(
        [
            "bash",
            str(RUN_DEMO),
            "--coding",
            "--seed-only",
            "--db-path",
            str(paths["db"]),
            "--workspace-root",
            str(paths["workspace"]),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    assert "WARNING: --coding needs the Docker executor" not in (
        proc.stdout + proc.stderr
    )
    assert "Executor: local (from the environment)" in proc.stdout


def test_coding_scenario_command_allowlist_reaches_the_store(
    tmp_path: Path,
) -> None:
    """The persisted task scope keeps ``allowed_commands`` verbatim.

    ``seed_demo.py`` used to restate the task scope field by field and omitted
    ``allowed_commands``, so every seeded coding scenario carried an empty
    allowlist and denied its own ``python -m pytest`` with
    ``COMMAND_NOT_ALLOWED`` even on the Docker executor (#204).
    """
    proc, paths = _run_coding_demo(tmp_path, DOCKER_STUB)
    assert proc.returncode == 0, proc.stderr

    conn = sqlite3.connect(paths["db"])
    try:
        row = conn.execute(
            "SELECT task_scope_json FROM runs WHERE name = ?", (CODING_RUN_NAME,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"run {CODING_RUN_NAME!r} was not seeded"
    scope = json.loads(row[0])
    assert scope["allowed_commands"] == [["python", "-m", "pytest"]]
