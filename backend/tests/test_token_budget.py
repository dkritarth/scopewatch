"""Per-profile daily token metering (#77): budgets, refusal, mock bypass.

The gateway meters model tokens per provider profile per UTC day and refuses
new agent runs (429) once a profile budget is exceeded. Scripted/mock usage
records 0 tokens and is never refused. Only counts are stored and reported;
keys, prompts, and traces are never logged.

All fixtures are synthetic. No network (httpx.MockTransport), no secrets.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from scopewatch.budget_store import (
    TokenLedger,
    daily_token_budget_for,
    get_ledger,
    reset_ledger,
    token_budget_env_name,
)
from scopewatch.db import init_db
from scopewatch.app import create_app

TOKEN = "test-demo-token-12345"


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_ledger()
    yield
    reset_ledger()


def _set_profile_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _make_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra_env: dict[str, str]
) -> TestClient:
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)
    db_file = tmp_path / "tokens_test.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "outputs").mkdir(exist_ok=True)
    app = create_app(db_path=db_file, workspace_root=workspace)
    return TestClient(app, raise_server_exceptions=False)


def _run_payload(name: str = "Token run") -> dict:
    return {
        "name": name,
        "task_scope": {
            "schema_version": "1",
            "task_description": "Synthetic token task",
            "allowed_paths": ["outputs"],
            "blocked_paths": ["invoices/private"],
            "allowed_tools": ["workspace"],
            "allowed_operations": ["list_directory", "read_text", "write_text"],
            "allowed_network_destinations": [],
            "requires_approval": ["delete_path"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def _mock_completion_response(total_tokens: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl-test",
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": "nvidia/llama-3.1-nemotron-70b-instruct",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "synthetic answer"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": total_tokens - 7,
                "completion_tokens": 7,
                "total_tokens": total_tokens,
            },
        },
    )


# ---------------------------------------------------------------------------
# Budget env parsing
# ---------------------------------------------------------------------------


def test_budget_env_name_sanitizes_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    assert token_budget_env_name("nebius-demo") == "SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO"
    assert token_budget_env_name("openrouter-dev") == "SCOPEWATCH_DAILY_TOKEN_BUDGET_OPENROUTER_DEV"
    assert token_budget_env_name("Mock") == "SCOPEWATCH_DAILY_TOKEN_BUDGET_MOCK"


def test_unlimited_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO", raising=False)
    monkeypatch.delenv("SCOPEWATCH_TOKEN_BUDGET_DEFAULT", raising=False)
    assert daily_token_budget_for("nebius-demo") is None


def test_budget_value_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO", "100000")
    assert daily_token_budget_for("nebius-demo") == 100000


def test_malformed_budget_fails_closed_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO", "lots")
    assert daily_token_budget_for("nebius-demo") == 0


def test_default_budget_applies_when_profile_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO", raising=False)
    monkeypatch.setenv("SCOPEWATCH_TOKEN_BUDGET_DEFAULT", "5000")
    assert daily_token_budget_for("nebius-demo") == 5000


# ---------------------------------------------------------------------------
# Provider hook: live usage recorded, mock ignored
# ---------------------------------------------------------------------------


def test_provider_hook_records_live_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    from scopewatch.providers.client import ProviderClient
    from scopewatch.providers.loader import get_profile

    monkeypatch.setenv("NEBIUS_API_KEY", "synthetic-dummy-key")
    profile = get_profile("nebius-demo")
    transport = httpx.MockTransport(lambda request: _mock_completion_response(123))
    client = ProviderClient(profile, transport=transport)
    result = client.complete([{"role": "user", "content": "synthetic prompt"}])
    assert result.usage["total_tokens"] == 123
    assert get_ledger().used_today("nebius-demo") == 123


def test_mock_provider_records_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from scopewatch.providers.client import MockProviderClient

    client = MockProviderClient()
    client.complete([{"role": "user", "content": "synthetic prompt"}])
    assert get_ledger().used_today("mock") == 0


def test_never_logs_keys_or_prompts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from scopewatch.providers.client import ProviderClient
    from scopewatch.providers.loader import get_profile

    secret_key = "sk-syn-t3st-secret-key-xyz"
    secret_prompt = "super secret synthetic prompt payload 84721"
    monkeypatch.setenv("NEBIUS_API_KEY", secret_key)
    profile = get_profile("nebius-demo")
    transport = httpx.MockTransport(lambda request: _mock_completion_response(42))
    client = ProviderClient(profile, transport=transport)
    with caplog.at_level(logging.DEBUG, logger="scopewatch"):
        client.complete([{"role": "user", "content": secret_prompt}])
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_key not in combined
    assert secret_prompt not in combined
    assert "synthetic answer" not in combined


# ---------------------------------------------------------------------------
# Run refusal on exhausted agent-profile budget (429, names profile + rollover)
# ---------------------------------------------------------------------------


def test_run_refused_when_agent_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_profile_env(
        monkeypatch,
        SCOPEWATCH_AGENT_PROFILE="nebius-demo",
        SCOPEWATCH_AUDITOR_PROFILE="mock",
        SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO="100",
    )
    get_ledger().add("nebius-demo", 100)
    client = _make_client(monkeypatch, tmp_path, {"DEMO_TOKEN": TOKEN})
    resp = client.post(
        "/api/v1/runs", json=_run_payload(), headers={"X-Demo-Token": TOKEN}
    )
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "token_budget_exhausted"
    assert "nebius-demo" in body["message"]
    assert "UTC midnight" in body["message"]


def test_run_allowed_under_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_profile_env(
        monkeypatch,
        SCOPEWATCH_AGENT_PROFILE="nebius-demo",
        SCOPEWATCH_AUDITOR_PROFILE="mock",
        SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO="100000",
    )
    get_ledger().add("nebius-demo", 50)
    client = _make_client(monkeypatch, tmp_path, {"DEMO_TOKEN": TOKEN})
    resp = client.post(
        "/api/v1/runs", json=_run_payload(), headers={"X-Demo-Token": TOKEN}
    )
    assert resp.status_code == 201


def test_refusal_names_rollover_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_profile_env(
        monkeypatch,
        SCOPEWATCH_AGENT_PROFILE="openrouter-dev",
        SCOPEWATCH_AUDITOR_PROFILE="mock",
        SCOPEWATCH_DAILY_TOKEN_BUDGET_OPENROUTER_DEV="10",
    )
    get_ledger().add("openrouter-dev", 10)
    client = _make_client(monkeypatch, tmp_path, {"DEMO_TOKEN": TOKEN})
    resp = client.post(
        "/api/v1/runs", json=_run_payload(), headers={"X-Demo-Token": TOKEN}
    )
    assert resp.status_code == 429
    message = resp.json()["message"]
    assert "openrouter-dev" in message
    assert "UTC midnight" in message


def test_refusal_sanitized_no_provider_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_profile_env(
        monkeypatch,
        SCOPEWATCH_AGENT_PROFILE="nebius-demo",
        SCOPEWATCH_AUDITOR_PROFILE="mock",
        SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO="1",
        NEBIUS_API_KEY="synthetic-dummy-key",
    )
    get_ledger().add("nebius-demo", 5)
    client = _make_client(monkeypatch, tmp_path, {"DEMO_TOKEN": TOKEN})
    resp = client.post(
        "/api/v1/runs", json=_run_payload(), headers={"X-Demo-Token": TOKEN}
    )
    assert resp.status_code == 429
    assert "synthetic-dummy-key" not in resp.text
    assert "traceback" not in resp.text.lower()


# ---------------------------------------------------------------------------
# Mock bypass: scripted usage unaffected even with zero budgets
# ---------------------------------------------------------------------------


def test_mock_bypass_despite_zero_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_profile_env(
        monkeypatch,
        SCOPEWATCH_AGENT_PROFILE="mock",
        SCOPEWATCH_AUDITOR_PROFILE="mock",
        SCOPEWATCH_DAILY_TOKEN_BUDGET_MOCK="0",
    )
    client = _make_client(monkeypatch, tmp_path, {"DEMO_TOKEN": TOKEN})
    created = client.post(
        "/api/v1/runs", json=_run_payload(), headers={"X-Demo-Token": TOKEN}
    )
    assert created.status_code == 201
    run_id = created.json()["id"]
    action = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "read_text",
            "resource": "invoices/private/payroll.txt",
            "arguments": {},
            "reasoning_summary": "synthetic summary",
        },
        headers={"X-Demo-Token": TOKEN},
    )
    assert action.status_code == 201
    assert get_ledger().used_today("mock") == 0


def test_mock_tiny_budget_still_allows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_profile_env(
        monkeypatch,
        SCOPEWATCH_AGENT_PROFILE="mock",
        SCOPEWATCH_AUDITOR_PROFILE="mock",
        SCOPEWATCH_DAILY_TOKEN_BUDGET_MOCK="1",
        SCOPEWATCH_TOKEN_BUDGET_DEFAULT="1",
    )
    client = _make_client(monkeypatch, tmp_path, {})
    # No demo token either: fully default local path stays open for mocks.
    resp = client.post("/api/v1/runs", json=_run_payload())
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Check-then-reserve BEFORE spend (service level, stub auditor)
# ---------------------------------------------------------------------------


class _StubAuditor:
    """Live-named auditor stub: returns canned verdict + token count, no network."""

    def __init__(self, total_tokens: int) -> None:
        self.total_tokens = total_tokens
        self.calls = 0
        self.profile = "nebius-demo"
        self.model = "synthetic-stub-model"
        self.timeout_s = 5.0

    def audit_turn(self, **kwargs):  # noqa: ANN001, ANN202
        from scopewatch.reasoning_audit import ReasoningAuditResult, ReasoningAuditVerdict

        self.calls += 1
        return ReasoningAuditResult(
            verdict=ReasoningAuditVerdict.NO_CONCERN,
            concern_type=None,
            flagged_excerpts=[],
            explanation="synthetic stub: no concern",
            model=self.model,
            profile=self.profile,
            latency_ms=1.0,
            total_tokens=self.total_tokens,
        )


def _service_with_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub: _StubAuditor
):
    from scopewatch.schemas import TaskScope
    from scopewatch.service import ScopewatchService

    db_file = tmp_path / "stub.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "outputs").mkdir(exist_ok=True)
    service = ScopewatchService(db_path=db_file, workspace_root=workspace, auditor=stub)
    scope = TaskScope(
        task_description="stub task",
        allowed_paths=["outputs"],
        blocked_paths=[],
        allowed_tools=["workspace"],
        allowed_operations=["read_text"],
        allowed_network_destinations=[],
        requires_approval=[],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return service, scope


def test_auditor_budget_checked_before_spend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Exhausted auditor budget refuses BEFORE the auditor (spend) runs."""
    import asyncio

    from scopewatch.demo_guards import DemoGuardRefusal

    _set_profile_env(
        monkeypatch,
        SCOPEWATCH_AGENT_PROFILE="mock",
        SCOPEWATCH_AUDITOR_PROFILE="nebius-demo",
        SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO="100",
        SCOPEWATCH_TOKEN_RESERVE_PER_AUDIT="8000",
    )
    get_ledger().add("nebius-demo", 100)
    stub = _StubAuditor(total_tokens=50)
    service, scope = _service_with_stub(monkeypatch, tmp_path, stub)
    run, _ = service.create_run(name="stub-run", task_scope=scope)

    from scopewatch.schemas import SubmitActionRequest

    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="outputs/x.txt",
        arguments={},
        reasoning_summary="synthetic",
    )
    with pytest.raises(DemoGuardRefusal) as excinfo:
        asyncio.run(service.submit_action(run.id, req))
    assert excinfo.value.code == "token_budget_exhausted"
    assert stub.calls == 0  # refused before any model spend
    assert get_ledger().used_today("nebius-demo") == 100  # no phantom reservation


def test_audit_settles_estimate_and_reports_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Allowed audit releases its estimate; events carry counts only."""
    import asyncio

    _set_profile_env(
        monkeypatch,
        SCOPEWATCH_AGENT_PROFILE="mock",
        SCOPEWATCH_AUDITOR_PROFILE="nebius-demo",
        SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO="100000",
        SCOPEWATCH_TOKEN_RESERVE_PER_AUDIT="8000",
    )
    stub = _StubAuditor(total_tokens=5000)
    service, scope = _service_with_stub(monkeypatch, tmp_path, stub)
    run, _ = service.create_run(name="stub-run", task_scope=scope)

    from scopewatch.schemas import SubmitActionRequest

    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="outputs/x.txt",
        arguments={},
        reasoning_summary="synthetic",
    )
    resp = asyncio.run(service.submit_action(run.id, req))
    assert stub.calls == 1
    # Estimate released (stub makes no provider call, so net ledger delta is 0).
    assert get_ledger().used_today("nebius-demo") == 0

    audit_events = [
        event
        for event in resp.events
        if event.event_type.value == "REASONING_AUDIT_COMPLETED"
    ]
    assert len(audit_events) == 1
    details = audit_events[0].details
    assert details["tokens_used"] == 5000
    assert details["profile"] == "nebius-demo"
    for forbidden in ("prompt", "trace", "content", "messages", "api_key"):
        assert forbidden not in details


def test_run_created_event_reports_token_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_profile_env(
        monkeypatch,
        SCOPEWATCH_AGENT_PROFILE="nebius-demo",
        SCOPEWATCH_AUDITOR_PROFILE="mock",
        SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO="100000",
    )
    get_ledger().add("nebius-demo", 250)
    client = _make_client(monkeypatch, tmp_path, {"DEMO_TOKEN": TOKEN})
    resp = client.post(
        "/api/v1/runs", json=_run_payload(), headers={"X-Demo-Token": TOKEN}
    )
    assert resp.status_code == 201
    run_id = resp.json()["id"]
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    created = next(e for e in events if e["event_type"] == "RUN_CREATED")
    budget = created["details"]["token_budget"]
    assert budget["agent_profile"] == "nebius-demo"
    assert budget["used_today"] == 250
    assert budget["budget"] == 100000


# ---------------------------------------------------------------------------
# Ledger rollover + per-profile isolation
# ---------------------------------------------------------------------------


def test_ledger_rolls_over_at_utc_midnight() -> None:
    day_one = datetime(2026, 9, 28, 23, 59, tzinfo=timezone.utc).timestamp()
    now = [day_one]
    ledger = TokenLedger(now=lambda: now[0])
    ledger.add("nebius-demo", 100)
    assert ledger.used_today("nebius-demo") == 100
    assert ledger.check("nebius-demo", estimate=1, budget=100).allowed is False
    now[0] += 120.0
    assert ledger.used_today("nebius-demo") == 0
    assert ledger.check("nebius-demo", estimate=1, budget=100).allowed is True


def test_ledger_is_per_profile() -> None:
    ledger = TokenLedger()
    ledger.add("nebius-demo", 100)
    assert ledger.used_today("nebius-demo") == 100
    assert ledger.used_today("openrouter-dev") == 0
    assert ledger.check("openrouter-dev", estimate=50, budget=100).allowed is True


def test_reset_in_names_midnight_rollover() -> None:
    ledger = TokenLedger()
    reset_in = ledger.reset_in_seconds()
    assert 0 < reset_in <= 24 * 3600
