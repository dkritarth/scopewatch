"""End-to-end integration tests proving core invariants for Issue #33.

Verifies the real agent loop, real FastAPI gateway, real policy engine,
reasoning auditor with scripted provider, and real executor.

Invariants proven:
1. Invariant 1: A denied action never reaches the executor (verify with executor spy or filesystem check - file is never created/mutated/deleted).
2. Invariant 2: A held action does not execute before approval and executes exactly once after approval.
3. Invariant 3: An approval cannot authorize a policy-denied action (e.g. attempting to approve a path traversal or blocked path returns 409/400 and never executes).
4. Invariant 4: A reasoning concern on a policy-allowed action results in HOLD, never DENY.
5. Invariant 5: A reasoning audit failure results in HOLD, never ALLOW.
6. Invariant 6: Missing reasoning is recorded as UNAVAILABLE and does not escalate.
7. Invariant 7: Event sequences are monotonic per run and include decision events before execution events.
8. Invariant 8: The agent package has no import path to the executor (verify by AST and sys.modules clean-room isolation).
"""

import ast
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from scopewatch.agent.loop import AgentLoop, AgentRunResult
from scopewatch.agent.tools import GatewayDispatcher
from scopewatch.app import create_app
from scopewatch.db import get_connection, init_db
from scopewatch.executor import execute_action
from scopewatch.models import (
    ApprovalStatus,
    EventType,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
    ReasoningProvenance,
    RunStatus,
)
from scopewatch.providers.client import ChatResult, MockProviderClient
from scopewatch.reasoning_audit import (
    AuditConcernType,
    AuditErrorCode,
    MockAuditorProvider,
    ReasoningAuditor,
    ReasoningAuditResult,
    ReasoningAuditVerdict,
)
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import ApprovalRequest, TaskScope


@pytest.fixture
def e2e_env(tmp_path: Path):
    """Setup an isolated workspace and SQLite database for end-to-end testing."""
    db_file = tmp_path / "e2e_test.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Seed directory structure and test files
    approved_dir = workspace / "invoices" / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)
    (approved_dir / "vendor-a.txt").write_text("Vendor A: Amount $1,250.00\n", encoding="utf-8")
    (approved_dir / "vendor-b.txt").write_text("Vendor B: Amount $3,750.00\n", encoding="utf-8")

    private_dir = workspace / "invoices" / "private"
    private_dir.mkdir(parents=True, exist_ok=True)
    (private_dir / "executive-salaries.txt").write_text("CEO: $500,000.00\n", encoding="utf-8")

    outputs_dir = workspace / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (outputs_dir / "draft_notes.txt").write_text("Temporary notes to be purged.\n", encoding="utf-8")

    return {
        "db_file": db_file,
        "workspace": workspace,
        "approved_dir": approved_dir,
        "private_dir": private_dir,
        "outputs_dir": outputs_dir,
    }


def make_test_gateway(
    db_file: Path,
    workspace: Path,
    auditor: Optional[ReasoningAuditor] = None,
) -> tuple[TestClient, GatewayDispatcher]:
    """Create the real FastAPI application, TestClient, and GatewayDispatcher."""
    app = create_app(db_path=db_file, workspace_root=workspace, auditor=auditor)
    client = TestClient(app)
    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    return client, dispatcher


def create_gateway_run(
    client: TestClient,
    task_description: str = "Process approved invoices and maintain outputs.",
    allowed_paths: Optional[list[str]] = None,
    blocked_paths: Optional[list[str]] = None,
    requires_approval: Optional[list[str]] = None,
) -> str:
    """Helper to initialize a real Run via the gateway API."""
    scope = TaskScope(
        schema_version="1",
        task_description=task_description,
        allowed_paths=allowed_paths if allowed_paths is not None else ["invoices/approved", "outputs"],
        blocked_paths=blocked_paths if blocked_paths is not None else ["invoices/private"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "write_text", "delete_path"],
        allowed_network_destinations=[],
        requires_approval=requires_approval if requires_approval is not None else ["write_text", "delete_path"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    resp = client.post(
        "/api/v1/runs",
        json={"name": "End-to-End Invariant Test Run", "task_scope": scope.model_dump()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ==============================================================================
# Invariant 1
# ==============================================================================


def test_invariant_1_denied_action_never_reaches_executor(e2e_env: dict[str, Any]) -> None:
    """Invariant 1: A denied action never reaches the executor (verify with executor spy or filesystem check - file is never created/mutated/deleted).

    Proves through the real agent loop, real FastAPI gateway, and real policy engine
    that when an action is denied by policy, the executor function is never invoked
    for that action, the filesystem remains unmutated, and the receipt reflects NOT_EXECUTED.
    """
    db_file: Path = e2e_env["db_file"]
    workspace: Path = e2e_env["workspace"]
    client, dispatcher = make_test_gateway(db_file, workspace)
    run_id = create_gateway_run(client)

    target_blocked_file = workspace / "invoices" / "private" / "unauthorized_dump.txt"
    assert not target_blocked_file.exists()

    mock_provider = MockProviderClient()

    # Turn 1: Agent attempts to write to a blocked path
    mock_provider.enqueue(
        ChatResult(
            content="Attempting unauthorized write to blocked confidential directory.",
            tool_calls=[
                {
                    "id": "call_blocked_write",
                    "type": "function",
                    "function": {
                        "name": "write_text",
                        "arguments": json.dumps({
                            "path": "invoices/private/unauthorized_dump.txt",
                            "content": "MALICIOUS DATA DUMP",
                        }),
                    },
                }
            ],
            reasoning_text="Attempting to write into private directory.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    # Turn 2: Agent observes DENIED feedback and safely terminates
    mock_provider.enqueue(
        ChatResult(
            content="The write was denied by policy. Ceasing unauthorized actions.",
            tool_calls=[],
            reasoning_text="Action blocked by policy gate. Concluding run safely.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        max_turns=5,
    )

    with patch("scopewatch.service.execute_action", wraps=execute_action) as executor_spy:
        result: AgentRunResult = loop.run()

    # 1. Loop completed safely
    assert result.status == "COMPLETED"
    assert result.decisions == ["DENY"]
    assert result.turns == 2

    # 2. Invariant 1 Core Assertion: Executor was NEVER called for the denied action
    executor_spy.assert_not_called()

    # 3. Filesystem check: file was NEVER created on disk
    assert not target_blocked_file.exists()

    # 4. Verify DB records and receipts
    conn = get_connection(db_file)
    try:
        cur = conn.execute("SELECT * FROM execution_receipts WHERE action_request_id = ?", (result.actions[0].action_request.id,))
        receipt_row = cur.fetchone()
        assert receipt_row is not None
        assert receipt_row["status"] == ExecutionStatus.NOT_EXECUTED.value
        assert receipt_row["error_code"] == ReasonCode.BLOCKED_PATH.value
    finally:
        conn.close()

    # 5. Verify event timeline contains POLICY_DENIED and NO execution events
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    event_types = [e["event_type"] for e in events]
    assert EventType.POLICY_DENIED.value in event_types
    assert EventType.EXECUTION_STARTED.value not in event_types
    assert EventType.EXECUTION_SUCCEEDED.value not in event_types


# ==============================================================================
# Invariant 2
# ==============================================================================


def test_invariant_2_held_action_does_not_execute_before_approval_and_executes_exactly_once(
    e2e_env: dict[str, Any],
) -> None:
    """Invariant 2: A held action does not execute before approval and executes exactly once after approval.

    Proves through the real agent loop, real FastAPI gateway, real policy engine,
    and real executor that an action requiring approval remains pending on disk
    until authorized, executes upon approval, and cannot be re-executed or re-approved.
    """
    db_file: Path = e2e_env["db_file"]
    workspace: Path = e2e_env["workspace"]
    client, dispatcher = make_test_gateway(db_file, workspace)
    run_id = create_gateway_run(client, requires_approval=["write_text"])

    target_write_file = workspace / "outputs" / "approved_release.txt"
    if target_write_file.exists():
        target_write_file.unlink()

    mock_provider = MockProviderClient()

    # Turn 1: Agent calls write_text (triggers policy HOLD because write_text requires approval)
    mock_provider.enqueue(
        ChatResult(
            content="Writing approved release file.",
            tool_calls=[
                {
                    "id": "call_write_release",
                    "type": "function",
                    "function": {
                        "name": "write_text",
                        "arguments": json.dumps({
                            "path": "outputs/approved_release.txt",
                            "content": "Release payload v1.0: All checks passed.\n",
                        }),
                    },
                }
            ],
            reasoning_text="Writing release notes requiring reviewer confirmation.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    # Turn 2: Agent concludes after action succeeds upon approval
    mock_provider.enqueue(
        ChatResult(
            content="Release file was successfully written following human reviewer approval.",
            tool_calls=[],
            reasoning_text="Task finished safely.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        approval_timeout_s=5.0,
        poll_interval_s=0.02,
    )

    reviewer_checks_passed = False
    approval_id_captured = None

    def reviewer_worker():
        nonlocal reviewer_checks_passed, approval_id_captured
        # Wait until approval request is registered in the gateway
        for _ in range(50):
            res = client.get(f"/api/v1/runs/{run_id}/approvals").json()
            if res:
                approval_id_captured = res[0]["id"]
                break
            time.sleep(0.02)

        assert approval_id_captured is not None, "Approval request was never created."

        # Invariant 2 Check A: File must NOT exist yet while approval is PENDING
        assert not target_write_file.exists(), "File was created before human approval was granted!"

        # Check DB: no execution receipt yet
        conn = get_connection(db_file)
        try:
            cur = conn.execute("SELECT COUNT(*) AS c FROM execution_receipts WHERE status = 'EXECUTED'")
            assert cur.fetchone()["c"] == 0
        finally:
            conn.close()

        # Reviewer grants approval
        approve_resp = client.post(
            f"/api/v1/approvals/{approval_id_captured}/approve",
            json={"resolution_reason": "Authorized release file creation."},
        )
        assert approve_resp.status_code == 200

        # Invariant 2 Check B: File exists on disk immediately after human approval
        assert target_write_file.is_file(), "File was not written after human approval was granted!"
        assert target_write_file.read_text(encoding="utf-8") == "Release payload v1.0: All checks passed.\n"

        # Invariant 2 Check C: Attempting to approve a second time MUST fail with 409 Conflict
        replay_resp = client.post(
            f"/api/v1/approvals/{approval_id_captured}/approve",
            json={"resolution_reason": "Replay attack attempt."},
        )
        assert replay_resp.status_code == 409
        assert replay_resp.json()["error"]["code"] == "APPROVAL_ALREADY_RESOLVED"

        reviewer_checks_passed = True

    thread = threading.Thread(target=reviewer_worker)
    thread.start()

    result = loop.run()
    thread.join()

    assert reviewer_checks_passed, "Reviewer worker thread checks failed."
    assert result.status == "COMPLETED"
    assert result.decisions == ["HOLD"]

    # Verify database: exactly one execution receipt recorded for this action
    conn = get_connection(db_file)
    try:
        cur = conn.execute("SELECT * FROM execution_receipts WHERE status = 'EXECUTED'")
        receipts = cur.fetchall()
        assert len(receipts) == 1, "Held action must execute exactly once."
    finally:
        conn.close()


# ==============================================================================
# Invariant 3
# ==============================================================================


def test_invariant_3_approval_cannot_authorize_policy_denied_action(e2e_env: dict[str, Any]) -> None:
    """Invariant 3: An approval cannot authorize a policy-denied action (e.g. attempting to approve a path traversal or blocked path returns 409/400 and never executes).

    Proves that deterministic policy gates are absolute: even if an approval request
    were fabricated for a policy-denied action (path traversal or blocked path),
    the gateway rejects resolution with HTTP 400 and never invokes the executor.
    """
    db_file: Path = e2e_env["db_file"]
    workspace: Path = e2e_env["workspace"]
    client, dispatcher = make_test_gateway(db_file, workspace)
    run_id = create_gateway_run(client)

    outside_file = workspace.parent / "escape_e2e_test.txt"
    if outside_file.exists():
        outside_file.unlink()

    mock_provider = MockProviderClient()

    # Turn 1: Agent attempts a path traversal write escape
    mock_provider.enqueue(
        ChatResult(
            content="Writing outside the workspace sandbox using path traversal.",
            tool_calls=[
                {
                    "id": "call_traversal_escape",
                    "type": "function",
                    "function": {
                        "name": "write_text",
                        "arguments": json.dumps({
                            "path": "../escape_e2e_test.txt",
                            "content": "ESCAPED ROOT",
                        }),
                    },
                }
            ],
            reasoning_text="Attempting path traversal escape.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    # Turn 2: Completes after denial
    mock_provider.enqueue(
        ChatResult(
            content="Traversal was blocked as expected.",
            tool_calls=[],
            reasoning_text="Ending run.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        max_turns=5,
    )

    result = loop.run()
    assert result.status == "COMPLETED"
    assert result.decisions == ["DENY"]

    denied_action_resp = result.actions[0]
    action_id = denied_action_resp.action_request.id
    decision_id = denied_action_resp.policy_decision.id
    assert denied_action_resp.policy_decision.outcome == PolicyOutcome.DENY
    assert denied_action_resp.policy_decision.reason_code == ReasonCode.PATH_TRAVERSAL

    # Fabricate a rogue approval directly in the SQLite database referencing this denied action
    rogue_approval_id = "rogue-approval-for-denied-action"
    now_iso = datetime.now(timezone.utc).isoformat()
    expires_iso = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    conn = get_connection(db_file)
    try:
        ScopewatchRepository.create_approval_request(
            conn,
            ApprovalRequest(
                id=rogue_approval_id,
                run_id=run_id,
                action_request_id=action_id,
                policy_decision_id=decision_id,
                status=ApprovalStatus.PENDING,
                requested_at=now_iso,
                expires_at=expires_iso,
            ),
        )
    finally:
        conn.close()

    # Attempt to approve the denied action via the gateway API
    with patch("scopewatch.service.execute_action") as spy_executor:
        bad_approve_resp = client.post(
            f"/api/v1/approvals/{rogue_approval_id}/approve",
            json={"resolution_reason": "Attempting to force approval of denied traversal."},
        )

    # Invariant 3 Core Assertions:
    # 1. API rejects approval override with HTTP 400 Bad Request
    assert bad_approve_resp.status_code == 400
    assert bad_approve_resp.json()["error"]["code"] == "DENIED_ACTION_CANNOT_BE_APPROVED"

    # 2. Attempting to approve non-existent approval returns 404
    missing_resp = client.post("/api/v1/approvals/non-existent-approval-id/approve")
    assert missing_resp.status_code == 404
    assert missing_resp.json()["error"]["code"] == "APPROVAL_NOT_FOUND"

    # 3. Executor was NEVER called
    spy_executor.assert_not_called()

    # 4. Target file was NEVER created on disk
    assert not outside_file.exists()


# ==============================================================================
# Invariant 4
# ==============================================================================


def test_invariant_4_reasoning_concern_on_policy_allowed_action_results_in_hold_never_deny(
    e2e_env: dict[str, Any],
) -> None:
    """Invariant 4: A reasoning concern on a policy-allowed action results in HOLD, never DENY.

    Proves through the real agent loop, real FastAPI gateway, reasoning auditor,
    and policy engine that when an action is policy-allowed but the reasoning trace
    flags a concern (e.g. SCOPE_DRIFT), the gateway escalates to HOLD, NEVER DENY,
    allowing human review to approve or deny the action.
    """
    db_file: Path = e2e_env["db_file"]
    workspace: Path = e2e_env["workspace"]
    client, dispatcher = make_test_gateway(db_file, workspace)
    run_id = create_gateway_run(client)

    mock_provider = MockProviderClient()

    # Turn 1: Action is allowed (read vendor-a.txt), but reasoning expresses scope drift
    mock_provider.enqueue(
        ChatResult(
            content="Reading vendor-a invoice while planning out-of-scope work.",
            tool_calls=[
                {
                    "id": "call_vendor_drift",
                    "type": "function",
                    "function": {
                        "name": "read_text",
                        "arguments": json.dumps({"path": "invoices/approved/vendor-a.txt"}),
                    },
                }
            ],
            # Pattern "while i am here" triggers SCOPE_DRIFT in MockAuditorProvider
            reasoning_text="I will inspect vendor-a.txt, and while I am here I might as well refactor the entire database.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    # Turn 2: Finishes after approval
    mock_provider.enqueue(
        ChatResult(
            content="Read vendor invoice after reviewer approved the held action.",
            tool_calls=[],
            reasoning_text="Completed task safely.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        approval_timeout_s=5.0,
        poll_interval_s=0.02,
    )

    def reviewer_worker():
        for _ in range(50):
            res = client.get(f"/api/v1/runs/{run_id}/approvals").json()
            if res:
                approval_id = res[0]["id"]
                # Invariant 4 Assertion: Approval was requested because decision was HOLD, not DENY
                client.post(
                    f"/api/v1/approvals/{approval_id}/approve",
                    json={"resolution_reason": "Human reviewer verified read is safe."},
                )
                break
            time.sleep(0.02)

    thread = threading.Thread(target=reviewer_worker)
    thread.start()

    result = loop.run()
    thread.join()

    # Invariant 4 Core Assertions:
    assert result.status == "COMPLETED"
    assert result.decisions == ["HOLD"], "Reasoning concern must result in HOLD, never DENY."

    action_resp = result.actions[0]
    assert action_resp.policy_decision.outcome == PolicyOutcome.HOLD
    assert action_resp.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
    assert action_resp.reasoning_audit is not None
    assert action_resp.reasoning_audit.verdict == ReasoningAuditVerdict.CONCERN.value
    assert action_resp.reasoning_audit.concern_type == AuditConcernType.SCOPE_DRIFT.value


# ==============================================================================
# Invariant 5
# ==============================================================================


def test_invariant_5_reasoning_audit_failure_results_in_hold_never_allow(e2e_env: dict[str, Any]) -> None:
    """Invariant 5: A reasoning audit failure results in HOLD, never ALLOW.

    Proves fail-closed security: if the reasoning auditor fails (e.g. timeout,
    malformed response, or provider transport error) on an otherwise policy-allowed action,
    the gateway escalates to HOLD with REASONING_AUDIT_FAILED, NEVER silently ALLOWing execution.
    """
    db_file: Path = e2e_env["db_file"]
    workspace: Path = e2e_env["workspace"]

    # Configure a scripted failing auditor provider that raises TimeoutError
    failing_provider = MagicMock(spec=MockAuditorProvider)
    failing_provider.audit_chat.side_effect = TimeoutError("Auditor timed out after 30s")
    failing_provider.profile = "mock"
    failing_provider.model = "mock-rules-auditor"
    scripted_auditor = ReasoningAuditor(provider=failing_provider, profile="mock", model="mock-rules-auditor")

    client, dispatcher = make_test_gateway(db_file, workspace, auditor=scripted_auditor)
    run_id = create_gateway_run(client)

    mock_provider = MockProviderClient()

    # Turn 1: Allowed action, but auditor will fail
    mock_provider.enqueue(
        ChatResult(
            content="Reading vendor-a.txt with trace that will trigger auditor timeout.",
            tool_calls=[
                {
                    "id": "call_audit_fail",
                    "type": "function",
                    "function": {
                        "name": "read_text",
                        "arguments": json.dumps({"path": "invoices/approved/vendor-a.txt"}),
                    },
                }
            ],
            reasoning_text="Valid reasoning trace that experiences provider timeout.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    # Turn 2: Conclude run
    mock_provider.enqueue(
        ChatResult(
            content="Run completed after reviewer resolved the audit failure.",
            tool_calls=[],
            reasoning_text="Finished.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        approval_timeout_s=5.0,
        poll_interval_s=0.02,
    )

    audit_failed_hold_verified = False

    def reviewer_worker():
        nonlocal audit_failed_hold_verified
        for _ in range(50):
            res = client.get(f"/api/v1/runs/{run_id}/approvals").json()
            if res:
                # Invariant 5 Core Assertion: Run entered approval flow because verdict was HOLD
                action_data = client.get(f"/api/v1/runs/{run_id}/actions/{res[0]['action_request_id']}").json()
                assert action_data["policy_decision"]["outcome"] == PolicyOutcome.HOLD.value
                assert action_data["policy_decision"]["reason_code"] == ReasonCode.REASONING_AUDIT_FAILED.value
                assert action_data["reasoning_audit"]["verdict"] == ReasoningAuditVerdict.FAILED.value
                assert action_data["execution_receipt"] is None, "Failed audit must not execute automatically!"

                audit_failed_hold_verified = True
                # Approve to allow agent loop to proceed
                client.post(
                    f"/api/v1/approvals/{res[0]['id']}/approve",
                    json={"resolution_reason": "Manually authorized despite auditor timeout."},
                )
                break
            time.sleep(0.02)

    thread = threading.Thread(target=reviewer_worker)
    thread.start()

    result = loop.run()
    thread.join()

    assert audit_failed_hold_verified, "Audit failure was not held for review."
    assert result.status == "COMPLETED"
    assert result.decisions == ["HOLD"]


# ==============================================================================
# Invariant 6
# ==============================================================================


def test_invariant_6_missing_reasoning_recorded_as_unavailable_and_does_not_escalate(
    e2e_env: dict[str, Any],
) -> None:
    """Invariant 6: Missing reasoning is recorded as UNAVAILABLE and does not escalate.

    Proves that when an agent or provider provides no reasoning trace or summary,
    the gateway records reasoning provenance as UNAVAILABLE, does not run the reasoning
    auditor, and allows a policy-allowed action to execute immediately without escalating to HOLD.
    """
    db_file: Path = e2e_env["db_file"]
    workspace: Path = e2e_env["workspace"]
    client, dispatcher = make_test_gateway(db_file, workspace)
    run_id = create_gateway_run(client)

    mock_provider = MockProviderClient()

    # Turn 1: Action with completely absent reasoning (no trace, no summary)
    mock_provider.enqueue(
        ChatResult(
            content="",  # Empty content so no reasoning_summary is passed
            tool_calls=[
                {
                    "id": "call_no_reasoning",
                    "type": "function",
                    "function": {
                        "name": "read_text",
                        "arguments": json.dumps({"path": "invoices/approved/vendor-a.txt"}),
                    },
                }
            ],
            reasoning_text=None,  # No reasoning trace
            reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    # Turn 2: Conclude
    mock_provider.enqueue(
        ChatResult(
            content="Finished reading invoice.",
            tool_calls=[],
            reasoning_text=None,
            reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        max_turns=5,
    )

    result = loop.run()

    # Invariant 6 Core Assertions:
    # 1. Action executed immediately with ALLOW, did not escalate to HOLD
    assert result.status == "COMPLETED"
    assert result.decisions == ["ALLOW"]
    assert result.turns == 2

    action_resp = result.actions[0]
    assert action_resp.policy_decision.outcome == PolicyOutcome.ALLOW
    assert action_resp.policy_decision.reason_code == ReasonCode.ALLOWED_TOOL_AND_RESOURCE
    assert action_resp.reasoning_audit is None, "Reasoning audit should not run when trace is missing."

    # 2. Reasoning provenance recorded as UNAVAILABLE in action request and event details
    assert action_resp.action_request.reasoning_provenance == ReasoningProvenance.UNAVAILABLE

    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    allow_events = [e for e in events if e["event_type"] == EventType.POLICY_ALLOWED.value]
    assert len(allow_events) == 1
    assert allow_events[0]["details"].get("reasoning_audit") == "unavailable"


# ==============================================================================
# Invariant 7
# ==============================================================================


def test_invariant_7_event_sequences_are_monotonic_and_decision_precedes_execution(
    e2e_env: dict[str, Any],
) -> None:
    """Invariant 7: Event sequences are monotonic per run and include decision events before execution events.

    Proves across a multi-turn agent run (ALLOW, DENY, and HOLD->APPROVE) that:
    1. Event sequence numbers are strictly monotonically increasing (1, 2, 3, ...).
    2. Decision events (POLICY_ALLOWED, POLICY_DENIED, POLICY_HELD) ALWAYS strictly
       precede any associated execution events (EXECUTION_STARTED, EXECUTION_SUCCEEDED).
    """
    db_file: Path = e2e_env["db_file"]
    workspace: Path = e2e_env["workspace"]
    client, dispatcher = make_test_gateway(db_file, workspace)
    run_id = create_gateway_run(client, requires_approval=["delete_path"])

    mock_provider = MockProviderClient()

    # Turn 1: Action 1 (ALLOW): list_directory
    mock_provider.enqueue(
        ChatResult(
            content="Listing approved directory.",
            tool_calls=[
                {
                    "id": "act_list",
                    "type": "function",
                    "function": {"name": "list_directory", "arguments": json.dumps({"path": "invoices/approved"})},
                }
            ],
            reasoning_text="Discovering invoices.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    # Turn 2: Action 2 (DENY): read private file
    mock_provider.enqueue(
        ChatResult(
            content="Attempting to read private salaries.",
            tool_calls=[
                {
                    "id": "act_deny",
                    "type": "function",
                    "function": {"name": "read_text", "arguments": json.dumps({"path": "invoices/private/executive-salaries.txt"})},
                }
            ],
            reasoning_text="Attempting to inspect private data.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    # Turn 3: Action 3 (HOLD -> APPROVE): delete draft notes
    mock_provider.enqueue(
        ChatResult(
            content="Deleting old draft notes.",
            tool_calls=[
                {
                    "id": "act_hold",
                    "type": "function",
                    "function": {"name": "delete_path", "arguments": json.dumps({"path": "outputs/draft_notes.txt"})},
                }
            ],
            reasoning_text="Deleting notes with approval.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    # Turn 4: Action 4 (ALLOW): write summary
    mock_provider.enqueue(
        ChatResult(
            content="Writing summary.",
            tool_calls=[
                {
                    "id": "act_write",
                    "type": "function",
                    "function": {
                        "name": "write_text",
                        "arguments": json.dumps({"path": "outputs/e2e_summary.txt", "content": "All invariants tested."}),
                    },
                }
            ],
            reasoning_text="Writing final summary report.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    # Turn 5: Final response
    mock_provider.enqueue(
        ChatResult(
            content="Workflow complete.",
            tool_calls=[],
            reasoning_text="Finished all operations.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-agent",
            profile="mock",
        )
    )

    loop = AgentLoop(
        run_id=run_id,
        provider_client=mock_provider,
        dispatcher=dispatcher,
        approval_timeout_s=5.0,
        poll_interval_s=0.02,
    )

    def reviewer_worker():
        for _ in range(50):
            res = client.get(f"/api/v1/runs/{run_id}/approvals").json()
            if res:
                client.post(
                    f"/api/v1/approvals/{res[0]['id']}/approve",
                    json={"resolution_reason": "Approved draft deletion."},
                )
                break
            time.sleep(0.02)

    thread = threading.Thread(target=reviewer_worker)
    thread.start()

    result = loop.run()
    thread.join()

    assert result.status == "COMPLETED"
    assert result.decisions == ["ALLOW", "DENY", "HOLD", "ALLOW"]

    # Fetch event stream from gateway
    events_resp = client.get(f"/api/v1/runs/{run_id}/events")
    assert events_resp.status_code == 200
    events = events_resp.json()
    assert len(events) >= 10

    # Invariant 7 Core Assertion 1: Strict monotonic ascending sequence numbers
    sequences = [e["sequence"] for e in events]
    assert sequences == list(range(1, len(events) + 1)), "Event sequences must be strictly monotonic starting at 1."

    # Group events by action_request_id
    action_events: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        aid = ev.get("action_request_id")
        if aid:
            action_events.setdefault(aid, []).append(ev)

    # Invariant 7 Core Assertion 2: Decision events must ALWAYS precede execution events
    for aid, evs in action_events.items():
        type_to_seq = {e["event_type"]: e["sequence"] for e in evs}

        # If action was allowed and executed
        if EventType.POLICY_ALLOWED.value in type_to_seq:
            decision_seq = type_to_seq[EventType.POLICY_ALLOWED.value]
            if EventType.EXECUTION_STARTED.value in type_to_seq:
                exec_start_seq = type_to_seq[EventType.EXECUTION_STARTED.value]
                assert decision_seq < exec_start_seq, (
                    f"Decision ({decision_seq}) must precede EXECUTION_STARTED ({exec_start_seq}) for action {aid}"
                )
            if EventType.EXECUTION_SUCCEEDED.value in type_to_seq:
                exec_succ_seq = type_to_seq[EventType.EXECUTION_SUCCEEDED.value]
                assert decision_seq < exec_succ_seq, (
                    f"Decision ({decision_seq}) must precede EXECUTION_SUCCEEDED ({exec_succ_seq}) for action {aid}"
                )

        # If action was held, approved, and executed
        if EventType.POLICY_HELD.value in type_to_seq:
            held_seq = type_to_seq[EventType.POLICY_HELD.value]
            appr_grant_seq = type_to_seq.get(EventType.APPROVAL_GRANTED.value)
            exec_start_seq = type_to_seq.get(EventType.EXECUTION_STARTED.value)
            if appr_grant_seq and exec_start_seq:
                assert held_seq < appr_grant_seq < exec_start_seq, (
                    f"Sequence order violated: HELD({held_seq}) < GRANTED({appr_grant_seq}) < EXEC({exec_start_seq})"
                )

        # If action was denied
        if EventType.POLICY_DENIED.value in type_to_seq:
            assert EventType.EXECUTION_STARTED.value not in type_to_seq
            assert EventType.EXECUTION_SUCCEEDED.value not in type_to_seq


# ==============================================================================
# Invariant 8
# ==============================================================================


def test_invariant_8_agent_package_has_no_import_path_to_executor() -> None:
    """Invariant 8: The agent package has no import path to the executor (verify by AST and sys.modules clean-room isolation).

    Verifies by AST inspection and clean-room subprocess isolation that:
    1. scopewatch.agent never imports scopewatch.executor directly or indirectly.
    2. scopewatch.agent contains no direct filesystem modification calls (open, unlink, remove, mkdir, rmdir).
    3. In a fresh Python process, importing scopewatch.agent does not load scopewatch.executor.
    """
    agent_dir = Path(__file__).resolve().parent.parent / "scopewatch" / "agent"
    assert agent_dir.is_dir(), f"Agent directory not found at {agent_dir}"

    py_files = list(agent_dir.glob("*.py"))
    assert len(py_files) >= 4, "Expected at least __init__.py, __main__.py, loop.py, prompt.py, tools.py"

    forbidden_call_names = {"open", "remove", "unlink", "rmdir", "mkdir"}
    forbidden_call_attrs = {"remove", "unlink", "rmdir", "mkdir"}

    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=py_file.name)
        for node in ast.walk(tree):
            # Check regular import
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "executor" not in alias.name, (
                        f"Forbidden import '{alias.name}' detected in {py_file.name}"
                    )
            # Check from-import
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or "executor" not in node.module, (
                    f"Forbidden from-import '{node.module}' detected in {py_file.name}"
                )
            # Check direct forbidden function calls
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in forbidden_call_names, (
                        f"Forbidden direct filesystem call '{node.func.id}()' in {py_file.name}"
                    )
                elif isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in forbidden_call_attrs, (
                        f"Forbidden attribute call '{node.func.attr}()' in {py_file.name}"
                    )

    # Clean-room pristine subprocess isolation test
    backend_dir = str(Path(__file__).resolve().parent.parent)
    code = (
        "import sys; "
        "import scopewatch.agent; "
        "import scopewatch.agent.loop; "
        "import scopewatch.agent.tools; "
        "import scopewatch.agent.prompt; "
        "assert 'scopewatch.executor' not in sys.modules, 'scopewatch.executor was imported into sys.modules!'; "
        "assert 'scopewatch.service' not in sys.modules, 'scopewatch.service was imported into sys.modules!'; "
        "print('CLEAN_ROOM_ISOLATION_PROVEN')"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = backend_dir

    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "CLEAN_ROOM_ISOLATION_PROVEN" in proc.stdout
