"""Explicit unsuccessful provider audit signals must not authorize execution."""

import asyncio
import json
from pathlib import Path

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

CLEAN = json.dumps({"verdict": "NO_CONCERN", "concern_type": None,
                    "flagged_excerpts": [], "explanation": "Within scope."})
MARKER = "synthetic-private-refusal-marker"
TOOL = [{"id": "synthetic-call", "type": "function",
         "function": {"name": "audit_more", "arguments": "{}"}}]
SIGNALS = [
    ({"finish_reason": "content_filter"}, {}),
    ({"finish_reason": "tool_calls"}, {"tool_calls": TOOL}),
    ({"finish_reason": "tool_calls"}, {}),
    ({"finish_reason": "function_call"}, {}),
    ({"finish_reason": "stop"}, {"tool_calls": TOOL}),
    ({"finish_reason": "stop"}, {"refusal": MARKER}),
    ({}, {"refusal": MARKER}),
    ({}, {"refusal": ""}),
    ({}, {"refusal": False}),
    ({}, {"refusal": 0}),
]


def submit(tmp_path, monkeypatch, routing, finish, message, usage):
    ledger = TokenLedger()
    monkeypatch.setattr(budget_store, "_ledger", ledger)
    for key, value in {
        "SCOPEWATCH_REASONING_AUDIT": "true", "SCOPEWATCH_AUDITOR_ROUTING": routing,
        "SCOPEWATCH_AGENT_PROFILE": "mock", "SCOPEWATCH_AUDITOR_PROFILE": "paid",
        "SCOPEWATCH_AUDITOR_FAST_PROFILE": "paid", "SCOPEWATCH_AUDITOR_STRONG_PROFILE": "paid",
        "SCOPEWATCH_DAILY_TOKEN_BUDGET_PAID": "10000", "SCOPEWATCH_TOKEN_RESERVE_PER_AUDIT": "8000",
        "SCOPEWATCH_EXECUTOR": "local",
    }.items(): monkeypatch.setenv(key, value)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": CLEAN, **message}, **finish}],
                                         "usage": usage})
    profile = ProviderProfile(name="paid", base_url="https://synthetic.invalid", model="synthetic", max_retries=0)
    client = ProviderClient(profile, transport=httpx.MockTransport(handler))
    monkeypatch.setattr("scopewatch.tiered_audit.get_profile", lambda name: profile)
    tier_clients = []
    def tier_client(configured):
        created = ProviderClient(configured, transport=httpx.MockTransport(handler))
        tier_clients.append(created)
        return created
    monkeypatch.setattr("scopewatch.tiered_audit.ProviderClient", tier_client)
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    (workspace / "outputs").mkdir(parents=True)
    service = ScopewatchService(db_path=database, workspace_root=workspace,
                               auditor=ReasoningAuditor(provider=client, profile="paid", model="synthetic"))
    run, _ = service.create_run(name="Synthetic completion metadata", task_scope=TaskScope(
        task_description="Write output", allowed_paths=["outputs"], allowed_tools=["workspace"],
        allowed_operations=["write_text"], created_at="2026-11-06T00:00:00Z"))
    try:
        response = asyncio.run(service.submit_action(run.id, SubmitActionRequest(
            tool="workspace", operation="write_text", resource="outputs/result.txt",
            arguments={"content": "synthetic output"}, exposed_reasoning_trace="I will write the permitted output.",
            reasoning_provenance="SYNTHETIC_FIXTURE")))
    finally:
        client.close()
        for created in tier_clients:
            created.close()
    return response, ledger, Path(run.workspace_path) / "outputs/result.txt", calls


@pytest.mark.parametrize("finish,message", SIGNALS,
                         ids=["filtered", "pending-tools", "tool-status", "legacy-tool-status", "tools-with-stop", "refusal", "refusal-without-status", "empty-refusal", "false-refusal", "zero-refusal"])
@pytest.mark.parametrize("routing", ["single", "two-tier"])
def test_unsuccessful_audit_metadata_holds_with_measured_usage(tmp_path, monkeypatch, routing, finish, message):
    response, ledger, output, calls = submit(tmp_path, monkeypatch, routing, finish, message, {"total_tokens": 150})
    expected_calls = 1 if routing == "single" else 2
    assert response.reasoning_audit.verdict == "FAILED", (response.policy_decision.outcome, output.exists())
    assert response.reasoning_audit.error_code == "INVALID_AUDIT_OUTPUT"
    assert response.reasoning_audit.total_tokens == 150 * expected_calls
    assert ledger.used_today("paid") == 150 * expected_calls
    assert len(calls) == expected_calls
    assert response.policy_decision.outcome == "HOLD"
    assert response.approval_request is not None
    assert response.execution_receipt is None
    assert not output.exists()
    assert MARKER not in response.model_dump_json()


@pytest.mark.parametrize("routing", ["single", "two-tier"])
def test_refusal_without_usage_retains_estimate(tmp_path, monkeypatch, routing):
    response, ledger, output, calls = submit(tmp_path, monkeypatch, routing, {}, {"refusal": MARKER}, None)
    assert response.reasoning_audit.verdict == "FAILED"
    assert response.reasoning_audit.total_tokens is None
    assert ledger.used_today("paid") == 8000
    assert not ledger.check("paid", estimate=8000).allowed
    assert len(calls) == 1
    assert response.execution_receipt is None
    assert not output.exists()


@pytest.mark.parametrize("finish,message", [
    ({"finish_reason": "stop"}, {}), ({}, {}), ({"finish_reason": None}, {}),
    ({"finish_reason": "length"}, {}), ({"finish_reason": "stop"}, {"refusal": None}),
])
def test_supported_complete_audits_remain_accepted(tmp_path, monkeypatch, finish, message):
    response, ledger, output, calls = submit(tmp_path, monkeypatch, "single", finish, message, {"total_tokens": 150})
    assert response.reasoning_audit.verdict == "NO_CONCERN"
    assert response.policy_decision.outcome == "ALLOW"
    assert response.execution_receipt.status == "EXECUTED"
    assert output.read_text() == "synthetic output"
    assert ledger.used_today("paid") == 150
    assert len(calls) == 1


def test_general_provider_tool_responses_stay_supported():
    profile = ProviderProfile(name="synthetic", base_url="https://synthetic.invalid", model="synthetic", max_retries=0)
    def handler(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls",
            "message": {"content": None, "tool_calls": TOOL}}]})
    with ProviderClient(profile, transport=httpx.MockTransport(handler)) as client:
        result = client.complete([])
    assert result.tool_calls == TOOL
    assert result.content is None
