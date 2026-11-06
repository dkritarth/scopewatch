"""Unknown provider spending must not be refunded as measured zero."""

import asyncio
import json

import httpx
import pytest

from scopewatch import budget_store
from scopewatch.budget_store import TokenLedger
from scopewatch.db import init_db
from scopewatch.providers.client import ProviderClient
from scopewatch.providers.profile import ProviderProfile
from scopewatch.reasoning_audit import ReasoningAuditor
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService


def gateway(tmp_path, monkeypatch, routing, usage, *, metering_fails=False, malformed_content=False, preflight=False, barrier=None):
    ledger = TokenLedger()
    monkeypatch.setattr(budget_store, "_ledger", ledger)
    for key, value in {
        "SCOPEWATCH_AGENT_PROFILE": "mock", "SCOPEWATCH_AUDITOR_PROFILE": "paid",
        "SCOPEWATCH_AUDITOR_ROUTING": routing, "SCOPEWATCH_AUDITOR_FAST_PROFILE": "paid",
        "SCOPEWATCH_AUDITOR_STRONG_PROFILE": "paid", "SCOPEWATCH_REASONING_AUDIT": "true",
        "SCOPEWATCH_DAILY_TOKEN_BUDGET_PAID": "10000", "SCOPEWATCH_TOKEN_RESERVE_PER_AUDIT": "8000",
        "SCOPEWATCH_EXECUTOR": "local",
    }.items(): monkeypatch.setenv(key, value)
    profile = ProviderProfile(name="paid", base_url="https://synthetic.invalid", model="synthetic-paid", max_retries=0)
    calls = []
    def handler(request):
        calls.append(request)
        if barrier is not None:
            entered, release = barrier
            entered.set()
            assert release.wait(5), "late provider was not released"
        content = {"invalid": "synthetic"} if malformed_content else json.dumps({
            "verdict": "NO_CONCERN", "concern_type": None, "flagged_excerpts": [], "explanation": "Within scope."})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": usage})
    client = ProviderClient(profile, transport=httpx.MockTransport(handler))
    monkeypatch.setattr("scopewatch.tiered_audit.get_profile", lambda name: profile)
    monkeypatch.setattr("scopewatch.tiered_audit.ProviderClient", lambda configured: client)
    if metering_fails:
        def fail_recording(*args): raise OSError("Synthetic counter failure")
        monkeypatch.setattr("scopewatch.providers.client.record_completion", fail_recording)
    if preflight:
        monkeypatch.setattr("scopewatch.reasoning_audit.DEFAULT_MAX_TRACE_CHARS", 1)
    auditor = ReasoningAuditor(provider=client, profile="paid", model=profile.model, max_trace_chars=1 if preflight else 16000)
    if preflight:
        real_tier_auditor = ReasoningAuditor
        monkeypatch.setattr("scopewatch.tiered_audit.ReasoningAuditor", lambda **kwargs: real_tier_auditor(**kwargs, max_trace_chars=1))
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("Synthetic content")
    service = ScopewatchService(db_path=database, workspace_root=workspace, auditor=auditor)
    run, _ = service.create_run(name="Synthetic unmeasured audit", task_scope=TaskScope(
        task_description="Read input.txt", allowed_paths=["input.txt"], allowed_tools=["workspace"],
        allowed_operations=["read_text"], created_at="2026-11-05T00:00:00Z"))
    request = SubmitActionRequest(tool="workspace", operation="read_text", resource="input.txt",
                                 exposed_reasoning_trace="I will read input.txt only.", reasoning_provenance="SYNTHETIC_FIXTURE")
    return service, run, request, ledger, client, calls


@pytest.mark.parametrize("routing", ["single", "two-tier"])
@pytest.mark.parametrize("usage,expected_tokens,expected_charge", [
    (None, None, 8000),
    ({"total_tokens": True}, None, 8000),
    ({"prompt_tokens": 5000}, None, 8000),
    ({"total_tokens": 5000}, 5000, 5000),
    ({"total_tokens": 0}, 0, 0),
])
def test_only_confirmed_recorded_usage_releases_reservation(tmp_path, monkeypatch, routing, usage, expected_tokens, expected_charge):
    service, run, request, ledger, client, calls = gateway(tmp_path, monkeypatch, routing, usage)
    try:
        response = asyncio.run(service.submit_action(run.id, request))
    finally: client.close()
    assert response.policy_decision.outcome.value == "ALLOW"
    assert len(calls) == 1
    assert ledger.used_today("paid") == expected_charge
    assert response.reasoning_audit.total_tokens == expected_tokens
    event = next(event for event in response.events if event.event_type.value == "REASONING_AUDIT_COMPLETED")
    assert event.details["tokens_used"] == expected_tokens
    if expected_charge == 8000: assert not ledger.check("paid", estimate=8000).allowed


@pytest.mark.parametrize("routing", ["single", "two-tier"])
def test_recording_failure_retains_estimate_even_with_measured_evidence(tmp_path, monkeypatch, routing):
    service, run, request, ledger, client, calls = gateway(tmp_path, monkeypatch, routing, {"total_tokens": 5000}, metering_fails=True)
    try: response = asyncio.run(service.submit_action(run.id, request))
    finally: client.close()
    assert len(calls) == 1
    assert response.reasoning_audit.total_tokens == 5000
    assert ledger.used_today("paid") == 8000


@pytest.mark.parametrize("routing", ["single", "two-tier"])
def test_local_preflight_refusal_releases_reservation(tmp_path, monkeypatch, routing):
    service, run, request, ledger, client, calls = gateway(tmp_path, monkeypatch, routing, None, preflight=True)
    try: response = asyncio.run(service.submit_action(run.id, request))
    finally: client.close()
    assert not calls
    assert response.reasoning_audit.verdict == "FAILED"
    assert response.reasoning_audit.total_tokens == 0
    assert ledger.used_today("paid") == 0


@pytest.mark.anyio
@pytest.mark.parametrize("routing", ["single", "two-tier"])
async def test_timed_out_worker_cannot_refund_before_late_completion(tmp_path, monkeypatch, routing):
    import threading

    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    service, run, request, ledger, client, calls = gateway(
        tmp_path, monkeypatch, routing, {"total_tokens": 5000}, barrier=(entered, release))
    real_complete = client.complete

    def slow_adapter(messages, **kwargs):
        # Model an adapter that does not implement the requested call timeout.
        # The gateway must still retain its reservation after its own timeout.
        try:
            return real_complete(messages)
        finally:
            completed.set()

    monkeypatch.setattr(client, "audit_chat", slow_adapter)
    service._auditor.timeout_s = 0.05
    client.profile.timeout_s = 0.05
    task = asyncio.create_task(service.submit_action(run.id, request))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        response = await asyncio.wait_for(task, 2)
        assert response.reasoning_audit.verdict == "FAILED"
        assert response.reasoning_audit.total_tokens is None
        assert response.policy_decision.outcome.value == "HOLD"
        assert ledger.used_today("paid") == 8000
        release.set()
        assert await asyncio.to_thread(completed.wait, 5)
        assert len(calls) == 1
        # Conservative overcount is intentional: a late completion cannot
        # retroactively release a reservation already retained at timeout.
        assert ledger.used_today("paid") == 13000
    finally:
        release.set()
        if not task.done():
            await asyncio.wait_for(task, 5)
        await asyncio.to_thread(completed.wait, 5)
        client.close()
