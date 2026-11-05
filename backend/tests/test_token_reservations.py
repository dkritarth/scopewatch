"""Reservations cannot refund another day or another caller's token usage."""

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import threading

import httpx
import pytest

from scopewatch.budget_store import TokenLedger, record_completion
from scopewatch.db import init_db
from scopewatch.providers.client import ProviderClient
from scopewatch.providers.profile import ProviderProfile
from scopewatch.reasoning_audit import ReasoningAuditor
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService


@pytest.mark.parametrize("routing", ["single", "two-tier"])
@pytest.mark.parametrize("rollover", [False, True], ids=["same-day", "midnight"])
def test_audit_reservation_preserves_completion_day_usage(tmp_path, monkeypatch, routing, rollover):
    clock = [datetime(2026, 11, 5, 23, 59, 59, tzinfo=timezone.utc).timestamp()]
    ledger = TokenLedger(now=lambda: clock[0])
    monkeypatch.setattr("scopewatch.budget_store._ledger", ledger)
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_ROUTING", routing)
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_PROFILE", "paid")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_FAST_PROFILE", "paid")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_STRONG_PROFILE", "unused-strong")
    monkeypatch.setenv("SCOPEWATCH_DAILY_TOKEN_BUDGET_PAID", "10000")
    monkeypatch.setenv("SCOPEWATCH_TOKEN_RESERVE_PER_AUDIT", "8000")
    profile = ProviderProfile(name="paid", base_url="https://synthetic.invalid", model="synthetic-paid", max_retries=0)

    def handler(request):
        if rollover:
            clock[0] += 2
        record_completion("paid", {"total_tokens": 4000})
        return httpx.Response(200, json={"model": "synthetic-paid",
            "choices": [{"message": {"content": json.dumps({
                "verdict": "NO_CONCERN", "concern_type": None,
                "flagged_excerpts": [], "explanation": "Within scope.",
            })}}], "usage": {"total_tokens": 5000}})

    with ProviderClient(profile, transport=httpx.MockTransport(handler)) as client:
        def tier_client(configured):
            client.profile = configured
            return client

        monkeypatch.setattr("scopewatch.tiered_audit.get_profile", lambda name: profile)
        monkeypatch.setattr("scopewatch.tiered_audit.ProviderClient", tier_client)
        database = tmp_path / "gateway.db"
        init_db(database)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "input.txt").write_text("synthetic content", encoding="utf-8")
        service = ScopewatchService(db_path=database, workspace_root=workspace,
                                   auditor=ReasoningAuditor(provider=client, profile="paid", model=profile.model))
        run, _ = service.create_run(name="Synthetic token rollover", task_scope=TaskScope(
            schema_version="1", task_description="Read input.txt", allowed_paths=["input.txt"],
            allowed_tools=["workspace"], allowed_operations=["read_text"],
            created_at="2026-11-05T00:00:00Z",
        ))
        response = asyncio.run(service.submit_action(run.id, SubmitActionRequest(
            tool="workspace", operation="read_text", resource="input.txt",
            exposed_reasoning_trace="I will read the permitted input.txt.",
            reasoning_provenance="SYNTHETIC_FIXTURE",
        )))
    assert response.policy_decision.outcome.value == "ALLOW"
    assert response.reasoning_audit.total_tokens == 5000
    assert ledger.used_today("paid") == 9000
    assert not ledger.check("paid", estimate=8000).allowed


def test_reservation_release_is_owned_and_single_use():
    ledger = TokenLedger()
    decision, reservation = ledger.reserve("paid", estimate=8000, budget=10000)
    assert decision.allowed and reservation is not None
    ledger.add("paid", 5000)
    ledger.release(copy.copy(reservation))
    assert ledger.used_today("paid") == 13000
    other = TokenLedger()
    other.add("paid", 7000)
    other.release(reservation)
    assert other.used_today("paid") == 7000
    ledger.release(reservation)
    assert ledger.used_today("paid") == 5000
    ledger.release(reservation)
    assert ledger.used_today("paid") == 5000


def test_reset_invalidates_same_day_reservations():
    ledger = TokenLedger()
    _, old = ledger.reserve("paid", estimate=8000)
    ledger.reset()
    _, current = ledger.reserve("paid", estimate=3000)
    ledger.add("paid", 1000)
    ledger.release(old)
    assert ledger.used_today("paid") == 4000
    ledger.release(current)
    assert ledger.used_today("paid") == 1000


def test_concurrent_reservations_cannot_exceed_one_ledger_budget():
    ledger = TokenLedger()
    ready = threading.Barrier(2)

    def reserve():
        ready.wait(timeout=5)
        return ledger.reserve("paid", estimate=8000, budget=10000)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert sum(decision.allowed for decision, _ in results) == 1
    assert ledger.used_today("paid") == 8000
    assert [handle for decision, handle in results if not decision.allowed] == [None]
    for _, handle in results:
        ledger.release(handle)
    assert ledger.used_today("paid") == 0


def test_persisted_counts_do_not_transfer_reservation_ownership(tmp_path):
    path = tmp_path / "tokens.json"
    clock = [datetime(2026, 11, 5, 23, 59, 59, tzinfo=timezone.utc).timestamp()]
    ledger = TokenLedger(now=lambda: clock[0], store_path=path)
    _, reservation = ledger.reserve("paid", estimate=8000)
    assert set(json.loads(path.read_text())) == {"day", "used"}
    reloaded = TokenLedger(now=lambda: clock[0], store_path=path)
    reloaded.release(reservation)
    assert reloaded.used_today("paid") == 8000
    clock[0] += 2
    reloaded.add("paid", 5000)
    reloaded.release(reservation)
    assert reloaded.used_today("paid") == 5000


@pytest.mark.parametrize("estimate", [0, -10])
def test_zero_reservations_and_mock_bypass(estimate):
    ledger = TokenLedger()
    decision, reservation = ledger.reserve("paid", estimate=estimate, budget=0)
    assert decision.allowed and reservation is not None
    ledger.release(reservation)
    assert ledger.used_today("paid") == 0
    decision, reservation = ledger.reserve("mock", estimate=8000, budget=0)
    assert decision.allowed and reservation is None
