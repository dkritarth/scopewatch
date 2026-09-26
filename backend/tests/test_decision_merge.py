"""Tests for Issue #29: Escalate-only decision merge with per-turn reasoning audit in the gateway."""

import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch
import pytest

from scopewatch.db import get_connection, init_db
from scopewatch.errors import ScopewatchAPIError
from scopewatch.models import (
    ApprovalStatus,
    EventType,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
    ReasoningProvenance,
    RunStatus,
)
from scopewatch.reasoning_audit import (
    AuditConcernType,
    AuditErrorCode,
    MockAuditorProvider,
    ReasoningAuditor,
    ReasoningAuditResult,
    ReasoningAuditVerdict,
)
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import (
    ActionRequest,
    ApprovalRequest,
    EvidenceEvent,
    SubmitActionRequest,
    TaskScope,
)
from scopewatch.service import ScopewatchService


@pytest.fixture
def test_env(tmp_path: Path):
    db_file = tmp_path / "test_gateway.db"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Seed workspace fixtures
    invoices_approved = workspace_dir / "invoices" / "approved"
    invoices_private = workspace_dir / "invoices" / "private"
    outputs_dir = workspace_dir / "outputs"

    invoices_approved.mkdir(parents=True, exist_ok=True)
    invoices_private.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    (invoices_approved / "vendor-a.txt").write_text("Vendor A Invoice: $1000", encoding="utf-8")
    (invoices_private / "salaries.txt").write_text("Executive Salaries: Confidential", encoding="utf-8")
    (outputs_dir / "archive_2025.txt").write_text("Old Archive Data", encoding="utf-8")

    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=workspace_dir)

    # Create active run
    scope = TaskScope(
        schema_version="1",
        task_description="Audit approved invoices in invoices/approved and produce outputs",
        allowed_paths=["invoices/approved", "outputs"],
        blocked_paths=["invoices/private"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "write_text", "delete_path"],
        allowed_network_destinations=[],
        requires_approval=["delete_path"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    run, _ = service.create_run(name="Gateway Decision Merge Test Run", task_scope=scope)

    return {
        "service": service,
        "run": run,
        "db_file": db_file,
        "workspace_dir": workspace_dir,
    }


# =====================================================================
# 1. Table-driven tests covering every row of the rule table
# =====================================================================

@pytest.mark.parametrize(
    "case_id,action_input,expected_outcome,expected_reason_code,audit_expected,expected_verdict",
    [
        # Row 1: Policy DENY -> audit not run -> final decision DENY (policy's reason code)
        (
            "policy_deny_blocked_path",
            {
                "tool": "workspace",
                "operation": "read_text",
                "resource": "invoices/private/salaries.txt",
                "exposed_reasoning_trace": "I want to check executive salaries to audit them.",
            },
            PolicyOutcome.DENY,
            ReasonCode.BLOCKED_PATH,
            False,
            None,
        ),
        (
            "policy_deny_traversal",
            {
                "tool": "workspace",
                "operation": "read_text",
                "resource": "../../etc/passwd",
                "exposed_reasoning_trace": "Attempting path traversal to read system files.",
            },
            PolicyOutcome.DENY,
            ReasonCode.PATH_TRAVERSAL,
            False,
            None,
        ),
        # Row 2: Policy HOLD -> audit run (or attached if available) -> final decision HOLD (policy's reason code)
        (
            "policy_hold_clean_trace",
            {
                "tool": "workspace",
                "operation": "delete_path",
                "resource": "outputs/archive_2025.txt",
                "exposed_reasoning_trace": "Cleaning up obsolete archive file as instructed by user.",
            },
            PolicyOutcome.HOLD,
            ReasonCode.APPROVAL_REQUIRED,
            True,
            ReasoningAuditVerdict.NO_CONCERN.value,
        ),
        # Row 3: Policy ALLOW: not attempted (no trace and no summary) -> final decision ALLOW
        (
            "policy_allow_no_reasoning",
            {
                "tool": "workspace",
                "operation": "read_text",
                "resource": "invoices/approved/vendor-a.txt",
                "exposed_reasoning_trace": None,
                "reasoning_summary": None,
            },
            PolicyOutcome.ALLOW,
            ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
            False,
            None,
        ),
        # Row 4: Policy ALLOW: NO_CONCERN -> final decision ALLOW (policy's code)
        (
            "policy_allow_no_concern",
            {
                "tool": "workspace",
                "operation": "read_text",
                "resource": "invoices/approved/vendor-a.txt",
                "exposed_reasoning_trace": "I need to parse vendor-a invoice total to verify approved status.",
            },
            PolicyOutcome.ALLOW,
            ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
            True,
            ReasoningAuditVerdict.NO_CONCERN.value,
        ),
        # Row 5: Policy ALLOW: CONCERN -> final decision HOLD with reason_code = REASONING_SCOPE_CONCERN
        (
            "policy_allow_concern_scope_drift",
            {
                "tool": "workspace",
                "operation": "read_text",
                "resource": "invoices/approved/vendor-a.txt",
                "exposed_reasoning_trace": "Now expanding my allowed scope and redefining my assigned task to access all records.",
            },
            PolicyOutcome.HOLD,
            ReasonCode.REASONING_SCOPE_CONCERN,
            True,
            ReasoningAuditVerdict.CONCERN.value,
        ),
    ],
)
def test_rule_table_cases(
    test_env: dict[str, Any],
    case_id: str,
    action_input: dict[str, Any],
    expected_outcome: PolicyOutcome,
    expected_reason_code: ReasonCode,
    audit_expected: bool,
    expected_verdict: Optional[str],
):
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    req = SubmitActionRequest(
        tool=action_input["tool"],
        operation=action_input["operation"],
        resource=action_input["resource"],
        exposed_reasoning_trace=action_input.get("exposed_reasoning_trace"),
        reasoning_summary=action_input.get("reasoning_summary"),
        turn_id=f"turn-{case_id}",
    )

    res = asyncio.run(service.submit_action(run.id, req))

    # Verify decision outcome and reason code
    assert res.policy_decision.outcome == expected_outcome
    assert res.policy_decision.reason_code == expected_reason_code

    if audit_expected:
        assert res.reasoning_audit is not None
        assert res.reasoning_audit.verdict == expected_verdict
        assert res.policy_decision.reasoning_audit_id == res.reasoning_audit.id
        assert res.action_request.reasoning_audit_id == res.reasoning_audit.id
    else:
        assert res.reasoning_audit is None
        assert res.policy_decision.reasoning_audit_id is None

    # Verify execution receipt / dispatch behavior
    if expected_outcome == PolicyOutcome.ALLOW:
        assert res.execution_receipt is not None
        assert res.execution_receipt.status == ExecutionStatus.EXECUTED
        assert res.approval_request is None
    elif expected_outcome == PolicyOutcome.HOLD:
        assert res.approval_request is not None
        assert res.approval_request.status == ApprovalStatus.PENDING
        assert res.execution_receipt is None
    elif expected_outcome == PolicyOutcome.DENY:
        assert res.execution_receipt is not None
        assert res.execution_receipt.status == ExecutionStatus.NOT_EXECUTED
        assert res.approval_request is None


# =====================================================================
# Row 6: Policy ALLOW: FAILED audit -> final decision HOLD (REASONING_AUDIT_FAILED)
# =====================================================================

def test_policy_allow_audit_failed_escalates_to_hold(test_env: dict[str, Any]):
    """Test Row 6: Fail closed when auditor returns FAILED (e.g. malformed or ungrounded)."""
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    # Provide an auditor that returns FAILED
    failing_auditor = MagicMock(spec=ReasoningAuditor)
    failing_auditor.audit_turn.return_value = ReasoningAuditResult(
        verdict=ReasoningAuditVerdict.FAILED,
        concern_type=None,
        flagged_excerpts=[],
        explanation="Audit failed closed: simulated auditor parsing failure.",
        model="mock-rules-auditor",
        profile="mock",
        latency_ms=12.5,
        error_code=AuditErrorCode.INVALID_AUDIT_OUTPUT.value,
    )
    service._auditor = failing_auditor

    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        exposed_reasoning_trace="Looking at vendor invoice.",
        turn_id="turn-fail-closed",
    )

    res = asyncio.run(service.submit_action(run.id, req))

    assert res.policy_decision.outcome == PolicyOutcome.HOLD
    assert res.policy_decision.reason_code == ReasonCode.REASONING_AUDIT_FAILED
    assert res.reasoning_audit is not None
    assert res.reasoning_audit.verdict == ReasoningAuditVerdict.FAILED.value
    assert res.approval_request is not None
    assert res.approval_request.status == ApprovalStatus.PENDING
    assert res.execution_receipt is None

    # Check event sequence
    event_types = [e.event_type for e in res.events]
    assert event_types == [
        EventType.ACTION_REQUESTED,
        EventType.REASONING_AUDIT_FAILED,
        EventType.POLICY_HELD,
        EventType.APPROVAL_REQUESTED,
    ]


# =====================================================================
# 2. Test: two actions in one turn cause one audit call (reused from cache)
# =====================================================================

def test_two_actions_one_turn_reused_from_cache(test_env: dict[str, Any]):
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    mock_provider = MagicMock(wraps=MockAuditorProvider(profile="mock", model="mock-rules-auditor"))
    mock_provider.profile = "mock"
    mock_provider.model = "mock-rules-auditor"
    auditor = ReasoningAuditor(provider=mock_provider, profile="mock", model="mock-rules-auditor")
    service._auditor = auditor

    turn_id = "shared-turn-42"
    shared_trace = "Reading vendor invoice a and then vendor invoice b for FY2026 verification."

    # Action 1
    req1 = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        exposed_reasoning_trace=shared_trace,
        turn_id=turn_id,
    )
    res1 = asyncio.run(service.submit_action(run.id, req1))
    assert res1.policy_decision.outcome == PolicyOutcome.ALLOW
    assert res1.reasoning_audit is not None
    audit_id_1 = res1.reasoning_audit.id

    # Action 2 (same turn, same trace)
    req2 = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        exposed_reasoning_trace=shared_trace,
        turn_id=turn_id,
    )
    res2 = asyncio.run(service.submit_action(run.id, req2))
    assert res2.policy_decision.outcome == PolicyOutcome.ALLOW
    assert res2.reasoning_audit is not None
    audit_id_2 = res2.reasoning_audit.id

    # Must reuse the exact same audit record ID
    assert audit_id_1 == audit_id_2

    # Provider should only have been called ONCE
    assert mock_provider.audit_chat.call_count == 1

    # Check database: exactly one reasoning audit record for this turn
    conn = get_connection(test_env["db_file"])
    try:
        cur = conn.execute("SELECT COUNT(*) AS c FROM reasoning_audits WHERE turn_id = ?", (turn_id,))
        assert cur.fetchone()["c"] == 1
    finally:
        conn.close()


# =====================================================================
# 3. Test: audit timeout -> HOLD with REASONING_AUDIT_FAILED, executor not called
# =====================================================================

def test_audit_timeout_fails_closed_and_executor_not_called(test_env: dict[str, Any]):
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    # Auditor that raises a TimeoutError
    timeout_provider = MagicMock()
    timeout_provider.audit_chat.side_effect = TimeoutError("Request timed out after 30s")
    timeout_provider.profile = "mock"
    timeout_provider.model = "mock-rules-auditor"
    timeout_auditor = ReasoningAuditor(provider=timeout_provider, profile="mock", model="mock-rules-auditor")
    service._auditor = timeout_auditor

    with patch("scopewatch.service.execute_action") as mock_executor:
        req = SubmitActionRequest(
            tool="workspace",
            operation="read_text",
            resource="invoices/approved/vendor-a.txt",
            exposed_reasoning_trace="Inspecting vendor a invoice.",
            turn_id="turn-timeout-test",
        )
        res = asyncio.run(service.submit_action(run.id, req))

        # Invariant: Must fail closed to HOLD with REASONING_AUDIT_FAILED
        assert res.policy_decision.outcome == PolicyOutcome.HOLD
        assert res.policy_decision.reason_code == ReasonCode.REASONING_AUDIT_FAILED

        # Executor must NOT be called
        mock_executor.assert_not_called()

        # Approval request generated
        assert res.approval_request is not None
        assert res.approval_request.status == ApprovalStatus.PENDING

        # Events emitted
        event_types = [e.event_type for e in res.events]
        assert event_types == [
            EventType.ACTION_REQUESTED,
            EventType.REASONING_AUDIT_FAILED,
            EventType.POLICY_HELD,
            EventType.APPROVAL_REQUESTED,
        ]


# =====================================================================
# 4. Test: policy DENY never calls the auditor
# =====================================================================

def test_policy_deny_never_calls_auditor(test_env: dict[str, Any]):
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    auditor_mock = MagicMock(spec=ReasoningAuditor)
    service._auditor = auditor_mock

    # Action targeting blocked path
    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/private/salaries.txt",
        exposed_reasoning_trace="Looking at private salaries with possible malicious intent.",
        turn_id="turn-deny-test",
    )
    res = asyncio.run(service.submit_action(run.id, req))

    assert res.policy_decision.outcome == PolicyOutcome.DENY
    assert res.policy_decision.reason_code == ReasonCode.BLOCKED_PATH

    # Auditor was completely skipped (never called)
    auditor_mock.audit_turn.assert_not_called()

    # DB contains no reasoning audit records
    conn = get_connection(test_env["db_file"])
    try:
        cur = conn.execute("SELECT COUNT(*) AS c FROM reasoning_audits WHERE turn_id = 'turn-deny-test'")
        assert cur.fetchone()["c"] == 0
    finally:
        conn.close()

    # Evidence events do not include any audit events
    event_types = [e.event_type for e in res.events]
    assert EventType.REASONING_AUDIT_COMPLETED not in event_types
    assert EventType.REASONING_AUDIT_FAILED not in event_types
    assert event_types == [EventType.ACTION_REQUESTED, EventType.POLICY_DENIED]


# =====================================================================
# 5. Test: approving an escalated HOLD executes exactly once; second approval fails
# =====================================================================

def test_approving_escalated_hold_executes_once_second_fails(test_env: dict[str, Any]):
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    # Submit action with trace that flags CONCERN
    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        exposed_reasoning_trace="I am expanding my allowed scope beyond authorized bounds.",
        turn_id="turn-escalated-approval",
    )
    res = asyncio.run(service.submit_action(run.id, req))

    assert res.policy_decision.outcome == PolicyOutcome.HOLD
    assert res.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
    assert res.approval_request is not None
    approval_id = res.approval_request.id

    # First approval: succeeds and executes action
    resolution_1 = asyncio.run(service.resolve_approval(
        approval_id,
        approve=True,
        resolved_by="compliance-officer@synthetic.local",
        reason="Reviewed intent and manually authorized single read.",
    ))
    assert resolution_1.approval_request.status == ApprovalStatus.CONSUMED
    assert resolution_1.execution_receipt is not None
    assert resolution_1.execution_receipt.status == ExecutionStatus.EXECUTED

    # Second approval attempt: MUST fail with 409 Conflict (APPROVAL_ALREADY_RESOLVED)
    with pytest.raises(ScopewatchAPIError) as exc_info:
        asyncio.run(service.resolve_approval(
            approval_id,
            approve=True,
            resolved_by="another-reviewer@synthetic.local",
            reason="Replay approval attempt.",
        ))
    assert exc_info.value.code == "APPROVAL_ALREADY_RESOLVED"
    assert exc_info.value.status_code == 409


# =====================================================================
# 6. Test: event sequence for escalated action is
#    ACTION_REQUESTED -> REASONING_AUDIT_COMPLETED -> POLICY_HELD -> APPROVAL_REQUESTED
# =====================================================================

def test_event_sequence_for_escalated_action(test_env: dict[str, Any]):
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        exposed_reasoning_trace="redefining my assigned task to read other documents.",
        turn_id="turn-event-sequence-test",
    )
    res = asyncio.run(service.submit_action(run.id, req))

    assert res.policy_decision.outcome == PolicyOutcome.HOLD
    assert res.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN

    expected_sequence = [
        EventType.ACTION_REQUESTED,
        EventType.REASONING_AUDIT_COMPLETED,
        EventType.POLICY_HELD,
        EventType.APPROVAL_REQUESTED,
    ]
    actual_sequence = [e.event_type for e in res.events]
    assert actual_sequence == expected_sequence

    # Verify monotonic ascending sequence numbers
    sequences = [e.sequence for e in res.events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)

    # Verify event links
    audit_ev = res.events[1]
    assert audit_ev.details.get("verdict") == ReasoningAuditVerdict.CONCERN.value
    assert audit_ev.details.get("audit_id") == res.reasoning_audit.id


# =====================================================================
# 7. Test: approval cannot override a deterministic DENY outcome
# =====================================================================

def test_approval_cannot_override_policy_deny(test_env: dict[str, Any]):
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/private/salaries.txt",
        turn_id="turn-deny-override-test",
    )
    res = asyncio.run(service.submit_action(run.id, req))
    assert res.policy_decision.outcome == PolicyOutcome.DENY

    # Verify manually that trying to resolve an approval for this denied action is rejected
    conn = get_connection(test_env["db_file"])
    try:
        # Create a synthetic rogue approval pointing to the denied action
        ScopewatchRepository.create_approval_request(
            conn,
            ApprovalRequest(
                id="rogue-approval-1",
                run_id=run.id,
                action_request_id=res.action_request.id,
                policy_decision_id=res.policy_decision.id,
                status=ApprovalStatus.PENDING,
                requested_at=datetime.now(timezone.utc).isoformat(),
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            ),
        )
    finally:
        conn.close()

    with pytest.raises(ScopewatchAPIError) as exc_info:
        asyncio.run(service.resolve_approval("rogue-approval-1", approve=True))
    assert exc_info.value.code == "DENIED_ACTION_CANNOT_BE_APPROVED"


# =====================================================================
# 8. Test: feature flag SCOPEWATCH_REASONING_AUDIT=off bypasses audit
# =====================================================================

def test_policy_decision_reasoning_audit_id_round_trip(test_env: dict[str, Any]):
    """Defect 21: PolicyDecision.reasoning_audit_id must survive a DB round trip.

    Previously the column did not exist, so the field always read back None.
    """
    import uuid as uuid_mod

    db_file = test_env["db_file"]
    run = test_env["run"]
    now = datetime.now(timezone.utc).isoformat()

    action = ActionRequest(
        id=str(uuid_mod.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        arguments={},
        requested_by="synthetic-agent",
        requested_at=now,
    )
    conn = get_connection(db_file)
    try:
        ScopewatchRepository.create_action_request(conn, action)

        from scopewatch.schemas import PolicyDecision

        decision = PolicyDecision(
            id=str(uuid_mod.uuid4()),
            action_request_id=action.id,
            outcome=PolicyOutcome.ALLOW,
            reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
            explanation="permitted",
            matched_rule="RULE_ALLOWED_TOOL_AND_RESOURCE",
            decided_at=now,
            deterministic=True,
            reasoning_audit_id="audit-record-1",
        )
        ScopewatchRepository.create_policy_decision(conn, decision)
        reloaded = ScopewatchRepository.get_policy_decision_by_action(conn, action.id)
        assert reloaded is not None
        assert reloaded.reasoning_audit_id == "audit-record-1"

        action2 = ActionRequest(
            id=str(uuid_mod.uuid4()),
            run_id=run.id,
            tool="workspace",
            operation="read_text",
            resource="invoices/approved/vendor-a.txt",
            arguments={},
            requested_by="synthetic-agent",
            requested_at=now,
        )
        ScopewatchRepository.create_action_request(conn, action2)
        decision2 = PolicyDecision(
            id=str(uuid_mod.uuid4()),
            action_request_id=action2.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.BLOCKED_PATH,
            explanation="blocked",
            matched_rule="RULE_BLOCKED_PATH_MATCHED",
            decided_at=now,
            deterministic=True,
            reasoning_audit_id=None,
        )
        ScopewatchRepository.create_policy_decision(conn, decision2)
        reloaded2 = ScopewatchRepository.get_policy_decision_by_action(conn, action2.id)
        assert reloaded2 is not None
        assert reloaded2.reasoning_audit_id is None
    finally:
        conn.close()


def test_effective_turn_id_persisted_on_action_row(test_env: dict[str, Any]):
    """Defect 15: the generated effective turn_id must persist on the action row.

    Previously the audit record carried the turn but the action row stored the
    raw (often None) request turn_id, leaving seeded timelines turn-less.
    """
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        exposed_reasoning_trace="Reading vendor invoice for verification.",
    )
    assert req.turn_id is None
    res = asyncio.run(service.submit_action(run.id, req))

    assert res.action_request.turn_id, "effective turn_id must be generated"
    assert res.reasoning_audit is not None
    assert res.reasoning_audit.turn_id == res.action_request.turn_id

    conn = get_connection(test_env["db_file"])
    try:
        row = conn.execute(
            "SELECT turn_id, reasoning_audit_id FROM action_requests WHERE id = ?",
            (res.action_request.id,),
        ).fetchone()
        assert row is not None
        assert row["turn_id"] == res.action_request.turn_id
        assert row["turn_id"] is not None
    finally:
        conn.close()


def test_feature_flag_disabled_bypasses_audit(test_env: dict[str, Any], monkeypatch):
    monkeypatch.setenv("SCOPEWATCH_REASONING_AUDIT", "off")

    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    auditor_mock = MagicMock(spec=ReasoningAuditor)
    service._auditor = auditor_mock

    # Suspicious trace that would flag CONCERN if audited
    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        exposed_reasoning_trace="expanding my allowed scope to everything.",
        turn_id="turn-flag-off",
    )
    res = asyncio.run(service.submit_action(run.id, req))

    # When feature flag is off, audit does not run, deterministic policy ALLOW prevails
    assert res.policy_decision.outcome == PolicyOutcome.ALLOW
    assert res.policy_decision.reason_code == ReasonCode.ALLOWED_TOOL_AND_RESOURCE
    auditor_mock.audit_turn.assert_not_called()
    assert res.reasoning_audit is None

    # Defect 17: flag-off evidence must say the audit was disabled, not "unavailable".
    allow_events = [e for e in res.events if e.event_type == EventType.POLICY_ALLOWED]
    assert len(allow_events) == 1
    assert allow_events[0].details.get("reasoning_audit") == "disabled"


def test_no_reasoning_wording_stays_unavailable(test_env: dict[str, Any]):
    """Defect 17: 'no reasoning supplied' keeps the distinct 'unavailable' label."""
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
    )
    res = asyncio.run(service.submit_action(run.id, req))

    assert res.policy_decision.outcome == PolicyOutcome.ALLOW
    assert res.reasoning_audit is None
    allow_events = [e for e in res.events if e.event_type == EventType.POLICY_ALLOWED]
    assert len(allow_events) == 1
    assert allow_events[0].details.get("reasoning_audit") == "unavailable"
