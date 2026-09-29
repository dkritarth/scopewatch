"""Gateway-native demo guards (#76): token, rate limit, caps, budgets.

These tests prove the gateway enforces the demo controls itself (no deploy
gate required). Guards engage only when DEMO_TOKEN is set (demo mode);
without it the gateway behaves exactly as before (local dev / tests).

All fixtures are synthetic. No network, no secrets.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scopewatch.app import create_app
from scopewatch.demo_guards import DemoGuardConfig, DemoGuardPolicy
from scopewatch.db import init_db

TOKEN = "test-demo-token-12345"


def _set_demo_env(monkeypatch: pytest.MonkeyPatch, extra: dict[str, str]) -> None:
    env = {"DEMO_TOKEN": TOKEN}
    env.update(extra)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _make_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra_env: dict[str, str] | None = None
) -> TestClient:
    _set_demo_env(monkeypatch, extra_env or {})
    db_file = tmp_path / "guards_test.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "outputs").mkdir(exist_ok=True)
    app = create_app(db_path=db_file, workspace_root=workspace)
    return TestClient(app, raise_server_exceptions=False)


def _run_payload(name: str = "Guarded run") -> dict:
    return {
        "name": name,
        "task_scope": {
            "schema_version": "1",
            "task_description": "Synthetic guarded task",
            "allowed_paths": ["outputs"],
            "blocked_paths": ["invoices/private"],
            "allowed_tools": ["workspace"],
            "allowed_operations": ["list_directory", "read_text", "write_text"],
            "allowed_network_destinations": [],
            "requires_approval": ["delete_path"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def _auth_headers(token: str = TOKEN) -> dict[str, str]:
    return {"X-Demo-Token": token}


def _deny_action(run_id: str, client: TestClient) -> object:
    # Blocked-path action: deterministic DENY, still counts as a turn attempt.
    return client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "read_text",
            "resource": "invoices/private/payroll.txt",
            "arguments": {},
        },
        headers=_auth_headers(),
    )


# ---------------------------------------------------------------------------
# Demo token (401 {error: demo_token_required})
# ---------------------------------------------------------------------------


def test_mutating_requires_token_missing_401(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _make_client(monkeypatch, tmp_path)
    resp = client.post("/api/v1/runs", json=_run_payload())
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"] == "demo_token_required"
    assert "message" in body


def test_mutating_wrong_token_401(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/v1/runs", json=_run_payload(), headers=_auth_headers("wrong-token")
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "demo_token_required"


def test_mutating_valid_token_allows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(monkeypatch, tmp_path)
    resp = client.post("/api/v1/runs", json=_run_payload(), headers=_auth_headers())
    assert resp.status_code == 201


def test_wrong_token_is_not_echoed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(monkeypatch, tmp_path)
    secret = "super-secret-wrong-token-xyz"
    resp = client.post("/api/v1/runs", json=_run_payload(), headers=_auth_headers(secret))
    assert resp.status_code == 401
    assert secret not in resp.text


def test_demo_reset_requires_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(monkeypatch, tmp_path)
    resp = client.post("/api/v1/demo/reset")
    assert resp.status_code == 401
    assert resp.json()["error"] == "demo_token_required"


def test_health_public_without_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(monkeypatch, tmp_path)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_reads_public_without_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(monkeypatch, tmp_path)
    created = client.post("/api/v1/runs", json=_run_payload(), headers=_auth_headers())
    assert created.status_code == 201
    run_id = created.json()["id"]

    assert client.get("/api/v1/runs").status_code == 200
    assert client.get(f"/api/v1/runs/{run_id}").status_code == 200
    assert client.get(f"/api/v1/runs/{run_id}/events").status_code == 200


def test_dashboard_index_public_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _make_client(monkeypatch, tmp_path)
    resp = client.get("/")
    # Static dashboard stays public; 404 only if frontend bundle is absent.
    assert resp.status_code in (200, 404)


def test_no_token_required_when_demo_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    db_file = tmp_path / "plain.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    app = create_app(db_path=db_file, workspace_root=workspace)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/v1/runs", json=_run_payload())
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Per-IP run-creation rate limit (429 + Retry-After)
# ---------------------------------------------------------------------------


def test_rate_limit_429_with_retry_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _make_client(
        monkeypatch, tmp_path, {"DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP": "1"}
    )
    first = client.post("/api/v1/runs", json=_run_payload("one"), headers=_auth_headers())
    assert first.status_code == 201
    second = client.post("/api/v1/runs", json=_run_payload("two"), headers=_auth_headers())
    assert second.status_code == 429
    body = second.json()
    assert body["error"] == "rate_limited"
    assert "Retry-After" in second.headers
    assert int(second.headers["Retry-After"]) >= 1


def test_rate_limit_alias_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(monkeypatch, tmp_path, {"DEMO_RATE_LIMIT_PER_MIN": "1"})
    assert (
        client.post("/api/v1/runs", json=_run_payload("one"), headers=_auth_headers()).status_code
        == 201
    )
    limited = client.post(
        "/api/v1/runs", json=_run_payload("two"), headers=_auth_headers()
    )
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"


def test_rate_limit_is_per_ip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(
        monkeypatch, tmp_path, {"DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP": "1"}
    )
    headers_a = {**_auth_headers(), "X-Forwarded-For": "203.0.113.10"}
    headers_b = {**_auth_headers(), "X-Forwarded-For": "203.0.113.11"}
    assert client.post("/api/v1/runs", json=_run_payload("a"), headers=headers_a).status_code == 201
    # A second IP is unaffected by the first IP's window.
    assert client.post("/api/v1/runs", json=_run_payload("b"), headers=headers_b).status_code == 201
    # Same IP again is limited.
    limited = client.post("/api/v1/runs", json=_run_payload("a2"), headers=headers_a)
    assert limited.status_code == 429


def test_rate_window_resets_unit() -> None:
    now = [1_000_000.0]
    config = DemoGuardConfig(
        enabled=True,
        demo_token="x",
        max_concurrent_runs=5,
        max_turns_per_run=40,
        rate_limit_runs_per_min_per_ip=1,
        max_runs_per_day=200,
        max_actions_per_day=5000,
        valid=True,
        error="",
    )
    policy = DemoGuardPolicy(config, now=lambda: now[0])
    assert policy.try_begin_run("10.0.0.1").allowed is True
    assert policy.try_begin_run("10.0.0.1").allowed is False
    now[0] += 61.0  # slide past the 60s window
    assert policy.try_begin_run("10.0.0.1").allowed is True


# ---------------------------------------------------------------------------
# Concurrent-run cap (429 concurrent_cap)
# ---------------------------------------------------------------------------


def test_concurrent_cap_429_then_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _make_client(monkeypatch, tmp_path, {"DEMO_MAX_CONCURRENT_RUNS": "1"})
    first = client.post("/api/v1/runs", json=_run_payload("one"), headers=_auth_headers())
    assert first.status_code == 201
    run_id = first.json()["id"]

    refused = client.post("/api/v1/runs", json=_run_payload("two"), headers=_auth_headers())
    assert refused.status_code == 429
    assert refused.json()["error"] == "concurrent_cap"

    # Completing the busy run frees capacity.
    assert (
        client.post(f"/api/v1/runs/{run_id}/complete", headers=_auth_headers()).status_code == 200
    )
    third = client.post("/api/v1/runs", json=_run_payload("three"), headers=_auth_headers())
    assert third.status_code == 201


def test_concurrency_race_single_winner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two simultaneous creations against cap 1: exactly one wins (fail closed)."""
    monkeypatch.setenv("DEMO_TOKEN", TOKEN)
    monkeypatch.setenv("DEMO_MAX_CONCURRENT_RUNS", "1")
    from scopewatch.service import ScopewatchService

    db_file = tmp_path / "race.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    service = ScopewatchService(db_path=db_file, workspace_root=workspace)

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def attempt(name: str) -> None:
        from scopewatch.schemas import TaskScope

        barrier.wait()
        try:
            scope = TaskScope(
                task_description="race",
                allowed_paths=["outputs"],
                blocked_paths=[],
                allowed_tools=["workspace"],
                allowed_operations=["read_text"],
                allowed_network_destinations=[],
                requires_approval=[],
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            service.create_run(name=name, task_scope=scope)
            outcomes.append("created")
        except Exception as exc:  # noqa: BLE001 - any refusal counts as denied
            outcomes.append(f"refused:{type(exc).__name__}")

    threads = [threading.Thread(target=attempt, args=(f"run-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["created", "refused:DemoGuardRefusal"]


# ---------------------------------------------------------------------------
# Per-run turn cap (429 turn_cap)
# ---------------------------------------------------------------------------


def test_turn_cap_429(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(monkeypatch, tmp_path, {"DEMO_MAX_TURNS_PER_RUN": "2"})
    created = client.post("/api/v1/runs", json=_run_payload(), headers=_auth_headers())
    assert created.status_code == 201
    run_id = created.json()["id"]

    assert _deny_action(run_id, client).status_code == 201  # type: ignore[union-attr]
    assert _deny_action(run_id, client).status_code == 201  # type: ignore[union-attr]
    capped = _deny_action(run_id, client)
    assert capped.status_code == 429  # type: ignore[union-attr]
    assert capped.json()["error"] == "turn_cap"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Daily run/action budgets (429 budget_exhausted) + rollover
# ---------------------------------------------------------------------------


def test_daily_run_budget_429(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(monkeypatch, tmp_path, {"DEMO_MAX_RUNS_PER_DAY": "1"})
    assert (
        client.post("/api/v1/runs", json=_run_payload("one"), headers=_auth_headers()).status_code
        == 201
    )
    refused = client.post("/api/v1/runs", json=_run_payload("two"), headers=_auth_headers())
    assert refused.status_code == 429
    body = refused.json()
    assert body["error"] == "budget_exhausted"
    assert "message" in body


def test_daily_run_budget_alias_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(monkeypatch, tmp_path, {"DEMO_DAILY_RUN_BUDGET": "1"})
    assert (
        client.post("/api/v1/runs", json=_run_payload("one"), headers=_auth_headers()).status_code
        == 201
    )
    refused = client.post("/api/v1/runs", json=_run_payload("two"), headers=_auth_headers())
    assert refused.status_code == 429
    assert refused.json()["error"] == "budget_exhausted"


def test_daily_action_budget_429(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _make_client(monkeypatch, tmp_path, {"DEMO_MAX_ACTIONS_PER_DAY": "1"})
    created = client.post("/api/v1/runs", json=_run_payload(), headers=_auth_headers())
    assert created.status_code == 201
    run_id = created.json()["id"]

    assert _deny_action(run_id, client).status_code == 201  # type: ignore[union-attr]
    refused = _deny_action(run_id, client)
    assert refused.status_code == 429  # type: ignore[union-attr]
    assert refused.json()["error"] == "budget_exhausted"  # type: ignore[union-attr]


def test_daily_counters_roll_over_at_utc_midnight_unit() -> None:
    # 2026-09-28T23:59:00Z, then past midnight.
    day_one = datetime(2026, 9, 28, 23, 59, tzinfo=timezone.utc).timestamp()
    now = [day_one]
    config = DemoGuardConfig(
        enabled=True,
        demo_token="x",
        max_concurrent_runs=5,
        max_turns_per_run=40,
        rate_limit_runs_per_min_per_ip=100,
        max_runs_per_day=1,
        max_actions_per_day=1,
        valid=True,
        error="",
    )
    policy = DemoGuardPolicy(config, now=lambda: now[0])
    assert policy.try_begin_run("10.0.0.9").allowed is True
    denied = policy.try_begin_run("10.0.0.9")
    assert denied.allowed is False
    assert denied.code == "budget_exhausted"
    assert policy.try_action("run-1").allowed is True
    assert policy.try_action("run-1").allowed is False

    now[0] += 120.0  # cross UTC midnight
    assert policy.try_begin_run("10.0.0.9").allowed is True
    assert policy.try_action("run-1").allowed is True


# ---------------------------------------------------------------------------
# Fail-closed misconfiguration: deny new runs, allow health
# ---------------------------------------------------------------------------


def test_misconfigured_guards_deny_runs_but_allow_health(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _make_client(monkeypatch, tmp_path, {"DEMO_MAX_CONCURRENT_RUNS": "not-a-number"})
    refused = client.post("/api/v1/runs", json=_run_payload(), headers=_auth_headers())
    assert refused.status_code == 503
    assert refused.json()["error"] == "demo_guard_misconfigured"
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/runs").status_code == 200


# ---------------------------------------------------------------------------
# Scripted (mock) scenarios need no demo token when demo mode is off
# ---------------------------------------------------------------------------


def test_scripted_scenario_needs_no_token_when_demo_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Provenance: default local runs (mock profiles) work with no DEMO_TOKEN."""
    import asyncio
    import json

    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    monkeypatch.delenv("SCOPEWATCH_AGENT_PROFILE", raising=False)
    monkeypatch.delenv("SCOPEWATCH_AUDITOR_PROFILE", raising=False)

    from scopewatch.schemas import SubmitActionRequest, TaskScope
    from scopewatch.service import ScopewatchService

    scenario_path = (
        Path(__file__).resolve().parent.parent.parent
        / "demo"
        / "scenarios"
        / "01_safe_audit.json"
    )
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))

    db_file = tmp_path / "scenario.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    (workspace / "invoices" / "approved").mkdir(parents=True)
    (workspace / "invoices" / "approved" / "vendor-a.txt").write_text(
        "synthetic vendor invoice", encoding="utf-8"
    )
    (workspace / "invoices" / "approved" / "vendor-b.txt").write_text(
        "synthetic vendor invoice", encoding="utf-8"
    )
    (workspace / "outputs").mkdir(exist_ok=True)

    service = ScopewatchService(db_path=db_file, workspace_root=workspace)
    scope_fields = dict(scenario["task_scope"])
    scope_fields.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    scope = TaskScope(**scope_fields)
    run, _ = service.create_run(name=scenario["name"], task_scope=scope)
    assert run.id

    for action in scenario["actions"]:
        req = SubmitActionRequest(
            tool=action["tool"],
            operation=action["operation"],
            resource=action["resource"],
            arguments=action.get("arguments", {}),
            requested_by=action.get("requested_by", "synthetic-agent"),
            reasoning_summary=action.get("reasoning_summary"),
        )
        resp = asyncio.run(service.submit_action(run.id, req))
        assert resp.policy_decision.outcome.value == "ALLOW"
