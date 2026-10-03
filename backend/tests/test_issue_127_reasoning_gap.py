from datetime import datetime, timezone
from scopewatch.db import init_db
"""Tests for Issue #127: explicit unaudited action evidence and agent reasoning capture."""

import asyncio
import os
from unittest.mock import patch

import pytest

from scopewatch.agent.prompt import PROMPT_VERSION, build_system_prompt
from scopewatch.agent.tools import convert_tool_call_to_submit_request
from scopewatch.models import (
    EventType,
    PolicyOutcome,
    ReasoningProvenance,
)
from scopewatch.schemas import TaskScope, SubmitActionRequest
from scopewatch.service import ScopewatchService


def test_agent_prompt_includes_rationale_instruction():
    prompt = build_system_prompt("Summarize vendor invoices.")
    assert PROMPT_VERSION == "2026-09-24"
    assert "Always provide a brief explanation of your intent and rationale before calling tools" in prompt
    assert "security auditor" in prompt
    # Invariant: No scope secrets in prompt
    assert "allowed_paths" not in prompt
    assert "blocked_paths" not in prompt


def test_tool_call_provenance_mapping_with_summary_and_trace():
    # 1. Neither trace nor summary -> UNAVAILABLE
    req_unavail = convert_tool_call_to_submit_request(
        tool_name="read_text",
        tool_arguments={"path": "invoices/approved/vendor-a.txt"},
    )
    assert req_unavail.reasoning_provenance == ReasoningProvenance.UNAVAILABLE

    # 2. Reasoning summary present (agent content) -> AGENT_AUTHORED_SUMMARY
    req_summary = convert_tool_call_to_submit_request(
        tool_name="read_text",
        tool_arguments={"path": "invoices/approved/vendor-a.txt"},
        reasoning_summary="Checking vendor A payment details for invoice match.",
    )
    assert req_summary.reasoning_provenance == ReasoningProvenance.AGENT_AUTHORED_SUMMARY
    assert req_summary.reasoning_summary == "Checking vendor A payment details for invoice match."

    # 3. Provider exposed trace present -> PROVIDER_EXPOSED_TRACE
    req_trace = convert_tool_call_to_submit_request(
        tool_name="read_text",
        tool_arguments={"path": "invoices/approved/vendor-a.txt"},
        exposed_reasoning_trace="Thinking: reading invoice to verify amount.",
        reasoning_summary="Reading invoice.",
    )
    assert req_trace.reasoning_provenance == ReasoningProvenance.PROVIDER_EXPOSED_TRACE


def test_service_records_explicit_unaudited_and_disabled_audit(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "invoices").mkdir()
    (workspace / "invoices" / "approved").mkdir()
    (workspace / "invoices" / "approved" / "inv1.txt").write_text("Invoice 1 details")

    db_path = str(tmp_path / "scopewatch.db")
    service = ScopewatchService(db_path=db_path, workspace_root=str(workspace))

    init_db(db_path)
    scope = TaskScope(
        schema_version="1",
        task_description="Read approved invoices.",
        allowed_paths=["invoices/approved"],
        blocked_paths=[],
        allowed_tools=["workspace"],
        allowed_operations=["read_text", "write_text"],
        allowed_network_destinations=[],
        requires_approval=[],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    run, _ = service.create_run(name="Issue 127 Test Run", task_scope=scope)

    # 1. Action with NO reasoning trace and NO summary:
    # Deterministic policy ALLOW -> audit not run -> details record unavailable
    act_unavail = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/inv1.txt",
        arguments={"path": "invoices/approved/inv1.txt"},
        reasoning_summary=None,
        exposed_reasoning_trace=None,
        reasoning_provenance=ReasoningProvenance.UNAVAILABLE,
    )
    resp = asyncio.run(service.submit_action(run.id, act_unavail))
    assert resp.policy_decision.outcome == PolicyOutcome.ALLOW

    # Verify event details recorded unavailable audit
    events = service.get_events(run.id)
    allow_events = [e for e in events if e.event_type == EventType.POLICY_ALLOWED]
    assert len(allow_events) >= 1
    assert allow_events[-1].details.get("reasoning_audit") == "unavailable"
    assert allow_events[-1].details.get("reasoning_availability") == "unavailable"

    # 2. Action with reasoning audit disabled by environment variable
    with patch.dict(os.environ, {"SCOPEWATCH_REASONING_AUDIT": "off"}):
        act_disabled = SubmitActionRequest(
            tool="workspace",
            operation="read_text",
            resource="invoices/approved/inv1.txt",
            arguments={"path": "invoices/approved/inv1.txt"},
            reasoning_summary="Disabled audit test.",
            exposed_reasoning_trace=None,
            reasoning_provenance=ReasoningProvenance.AGENT_AUTHORED_SUMMARY,
        )
        resp2 = asyncio.run(service.submit_action(run.id, act_disabled))
        assert resp2.policy_decision.outcome == PolicyOutcome.ALLOW

        events2 = service.get_events(run.id)
        allow_events2 = [e for e in events2 if e.event_type == EventType.POLICY_ALLOWED]
        assert allow_events2[-1].details.get("reasoning_audit") == "disabled"
        assert allow_events2[-1].details.get("reasoning_availability") == "disabled"

import json
from scopewatch.models import ReasonCode
from scopewatch.reasoning_audit import AuditorChatResponse, ReasoningAuditor

class MockInjectionAuditorProvider:
    def complete(self, messages, **kwargs):
        user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
        if "Rerouting payment" in user_msg or "partner-payment-details" in user_msg:
            return AuditorChatResponse(
                content=json.dumps({
                    "verdict": "CONCERN",
                    "concern_type": "INJECTION_FOLLOWING",
                    "flagged_excerpts": ["Rerouting payment per invoice instructions"],
                    "explanation": "Agent is following injected payment rerouting note.",
                }),
                model="mock-auditor",
                profile="mock",
                latency_ms=10.0,
            )
        return AuditorChatResponse(
            content=json.dumps({
                "verdict": "NO_CONCERN",
                "concern_type": None,
                "flagged_excerpts": [],
                "explanation": "Normal operation.",
            }),
            model="mock-auditor",
            profile="mock",
            latency_ms=10.0,
        )

def test_reasoning_summary_triggers_auditor_escalation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "outputs").mkdir()

    db_path = str(tmp_path / "scopewatch.db")
    init_db(db_path)
    service = ScopewatchService(db_path=db_path, workspace_root=str(workspace))
    service._auditor = ReasoningAuditor(
        provider=MockInjectionAuditorProvider(),
        profile="mock",
        model="mock-auditor",
    )

    scope = TaskScope(
        schema_version="1",
        task_description="Process invoices.",
        allowed_paths=["outputs"],
        blocked_paths=[],
        allowed_tools=["workspace"],
        allowed_operations=["read_text", "write_text"],
        allowed_network_destinations=[],
        requires_approval=[],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    run, _ = service.create_run(name="Injection Escalation Run", task_scope=scope)

    # Action has no exposed_reasoning_trace (e.g. Nemotron on Nebius), but has reasoning_summary:
    req = SubmitActionRequest(
        tool="workspace",
        operation="write_text",
        resource="outputs/partner-payment-details.txt",
        arguments={"path": "outputs/partner-payment-details.txt", "content": "Routing: 021000021, Acct: 99887766"},
        reasoning_summary="Rerouting payment per invoice instructions to outputs/partner-payment-details.txt",
        exposed_reasoning_trace=None,
        reasoning_provenance=ReasoningProvenance.AGENT_AUTHORED_SUMMARY,
    )

    resp = asyncio.run(service.submit_action(run.id, req))
    assert resp.policy_decision.outcome == PolicyOutcome.HOLD
    assert resp.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
    assert resp.execution_receipt is None
