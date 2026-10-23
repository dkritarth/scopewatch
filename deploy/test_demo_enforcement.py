"""Native HTTP guards on the direct Caddy-to-gateway deployment path.

Synthetic fixtures, no external network or model calls. The independent
Compose checks verify that this is the only service enforcing demo limits.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from scopewatch.app import create_app
from scopewatch.db import init_db


@pytest.mark.parametrize("control", [
    "DEMO_MAX_CONCURRENT_RUNS",
    "DEMO_MAX_TURNS_PER_RUN",
    "DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP",
    "DEMO_MAX_RUNS_PER_DAY",
    "DEMO_MAX_ACTIONS_PER_DAY",
    "DEMO_RATE_LIMIT_PER_MIN",
    "DEMO_DAILY_RUN_BUDGET",
    "DEMO_DAILY_ACTION_BUDGET",
])
@pytest.mark.parametrize("malformed", ["not-a-number", "1.5", "-1", "NaN", "inf"])
def test_bad_numeric_refuses_runs_and_actions_without_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, control: str, malformed: str
) -> None:
    # Create a real run before changing the configuration, so the action
    # refusal cannot pass merely because the addressed run does not exist.
    for name in (
        "DEMO_MAX_CONCURRENT_RUNS", "DEMO_MAX_TURNS_PER_RUN",
        "DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP", "DEMO_MAX_RUNS_PER_DAY",
        "DEMO_MAX_ACTIONS_PER_DAY", "DEMO_RATE_LIMIT_PER_MIN",
        "DEMO_DAILY_RUN_BUDGET", "DEMO_DAILY_ACTION_BUDGET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEMO_TOKEN", "synthetic-deployment-token")
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "mock")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_PROFILE", "mock")
    monkeypatch.setenv("SCOPEWATCH_RUN_WORKSPACES_DIR", str(tmp_path / "runs"))
    database = tmp_path / "evidence.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    init_db(database)
    headers = {"X-Demo-Token": "synthetic-deployment-token"}
    payload = {
        "name": "Synthetic deployment check",
        "task_scope": {
            "task_description": "Write a synthetic output",
            "allowed_paths": ["."],
            "allowed_tools": ["workspace"],
            "allowed_operations": ["write_text"],
            "created_at": "2026-10-23T00:00:00Z",
        },
    }
    with TestClient(create_app(db_path=database, workspace_root=workspace)) as client:
        created = client.post("/api/v1/runs", json=payload, headers=headers)
        assert created.status_code == 201
        run_id = created.json()["id"]
        before = client.get(f"/api/v1/runs/{run_id}/events").json()

    monkeypatch.setenv(control, malformed)
    with TestClient(create_app(db_path=database, workspace_root=workspace)) as client:
        responses = [
            client.post("/api/v1/runs", json=payload, headers=headers),
            client.post(f"/api/v1/runs/{run_id}/actions", headers=headers, json={
                "tool": "workspace", "operation": "write_text",
                "resource": "must-not-exist.txt", "arguments": {"content": "synthetic"},
            }),
        ]
        for response in responses:
            assert response.status_code == 503
            assert set(response.json()) == {"error", "message"}
            assert response.json()["error"] == "demo_guard_misconfigured"
        assert client.get("/api/v1/health").status_code == 200
        runs = client.get("/api/v1/runs")
        assert runs.status_code == 200
        assert len(runs.json()) == 1
        assert client.get(f"/api/v1/runs/{run_id}/events").json() == before
    assert not list((tmp_path / "runs").rglob("must-not-exist.txt"))


def _run_hosted_entrypoint(token: str | None, locale: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("DEMO_TOKEN", None)
    env["LC_ALL"] = locale
    env["DEMO_MAX_RUNS_PER_DAY"] = "bad"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "backend")
    env["ENTRYPOINT_TEST_PYTHON"] = sys.executable
    if token is not None:
        env["DEMO_TOKEN"] = token
    # Stub only container seeding/marker writes. Validation and the final
    # command use the real Python interpreter and native guard configuration.
    return subprocess.run(
        ["bash", "-c", '''
python() {
  if [[ "$1" == /app/scripts/seed_demo.py ]]; then return 0; fi
  command "$ENTRYPOINT_TEST_PYTHON" "$@"
}
touch() { return 0; }
source "$@"
''', "entrypoint-test", str(Path(__file__).resolve().parent / "docker-entrypoint.sh"),
         sys.executable, "-c",
         "from scopewatch.demo_guards import DemoGuardConfig; import json; "
         "config = DemoGuardConfig.from_env(); "
         "print(json.dumps([config.enabled, config.valid, config.demo_token]))"],
        env=env, capture_output=True, text=True, timeout=5,
    )


# Cover every character Python classifies as whitespace, including the
# review's U+00A0, U+0085 and U+001C bypasses, under both container locales.
PYTHON_WHITESPACE = "".join(chr(code) for code in range(sys.maxunicode + 1) if chr(code).isspace())


@pytest.mark.parametrize("locale", ["C", "C.UTF-8"])
@pytest.mark.parametrize("token", [
    None, "", "\t\r\n", PYTHON_WHITESPACE,
    *(pytest.param(char, id=f"U+{ord(char):04X}") for char in PYTHON_WHITESPACE),
])
def test_hosted_entrypoint_refuses_tokens_that_disable_native_guards(
    token: str | None, locale: str
) -> None:
    proc = _run_hosted_entrypoint(token, locale)
    assert proc.returncode == 1
    assert "DEMO_TOKEN must contain non-whitespace characters" in proc.stderr
    assert proc.stdout == ""  # Neither seeding nor the gateway command ran.


@pytest.mark.parametrize("locale", ["C", "C.UTF-8"])
@pytest.mark.parametrize("token", [
    "synthetic-deployment-token",
    " \tsynthetic-deployment-token\r\n",
    "\u00a0synthetic-deployment-token\u0085",
])
def test_hosted_entrypoint_preserves_enabled_guards_and_malformed_budget(
    token: str, locale: str
) -> None:
    proc = _run_hosted_entrypoint(token, locale)
    assert proc.returncode == 0, proc.stderr
    # The real gateway config keeps the token as supplied and marks the bad
    # numeric invalid, so the existing HTTP guards still refuse mutations.
    assert json.loads(proc.stdout.splitlines()[-1]) == [True, False, token]
