"""Regression tests for Issues #109, #110, and #111.

- #109: Persist decisions and execution intent before workspace effects can survive a rollback.
- #110: Reject actions whose run completes during reasoning audit.
- #111: Do not reuse a reasoning verdict for unseen actions in the same turn.
"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from scopewatch.db import get_connection, init_db
from scopewatch.errors import ScopewatchAPIError
from scopewatch.models import (
    EventType,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
    RunStatus,
)
from scopewatch.reasoning_audit import (
    AuditorChatResponse,
    MockAuditorProvider,
    ReasoningAuditor,
    ReasoningAuditResult,
    ReasoningAuditVerdict,
)
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService


@pytest.fixture
def service_env(tmp_path: Path):
    db_file = tmp_path / "test_gateway.db"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    outputs_dir = workspace_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=workspace_dir)

    scope = TaskScope(
        schema_version="1",
        task_description="Process outputs",
        allowed_paths=["outputs"],
        blocked_paths=[],
        allowed_tools=["workspace"],
        allowed_operations=["read_text", "write_text", "delete_path"],
        allowed_network_destinations=[],
        requires_approval=["delete_path"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    run, _ = service.create_run(name="Issues 109 110 111 Test Run", task_scope=scope)

    return {
        "service": service,
        "run": run,
        "db_file": db_file,
        "workspace_dir": workspace_dir,
    }


# =====================================================================
# Issue #110: Reject actions whose run completes during reasoning audit
# =====================================================================

@pytest.mark.anyio
async def test_issue_110_reject_action_when_run_completes_during_audit(service_env):
    service: ScopewatchService = service_env["service"]
    run = service_env["run"]
    workspace_dir: Path = service_env["workspace_dir"]

    audit_started = threading.Event()
    release_auditor = threading.Event()

    class PausingAuditorProvider(MockAuditorProvider):
        def audit_chat(self, *args, **kwargs):
            audit_started.set()
            release_auditor.wait(timeout=5.0)
            return AuditorChatResponse(
                content=json.dumps({
                    "verdict": "NO_CONCERN",
                    "concern_type": None,
                    "flagged_excerpts": [],
                    "explanation": "Audit looks clean.",
                }),
                model="mock-pausing-auditor",
                profile="mock",
                latency_ms=10.0,
            )

    auditor = ReasoningAuditor(
        provider=PausingAuditorProvider(), profile="mock", model="mock-pausing-auditor"
    )
    service._auditor = auditor

    req = SubmitActionRequest(
        tool="workspace",
        operation="write_text",
        resource="outputs/test110.txt",
        arguments={"content": "hello world 110"},
        exposed_reasoning_trace="Reasoning about writing test110.txt",
        turn_id="turn-race-110",
    )

    action_task = asyncio.create_task(service.submit_action(run.id, req))

    # Wait until audit is running in worker thread
    while not audit_started.is_set():
        await asyncio.sleep(0.01)

    # Complete the run while audit is in flight
    service.complete_run(run.id)

    # Now let the auditor finish
    release_auditor.set()

    # submit_action must reject because run is terminal
    with pytest.raises(ScopewatchAPIError) as exc_info:
        await action_task

    assert exc_info.value.code == "RUN_NOT_ACTIVE"

    # Verify no file was created on disk
    target_file = workspace_dir / "outputs" / "test110.txt"
    assert not target_file.exists(), "File should not be created for completed run"

    # Verify no execution events were stored
    events = service.get_events(run.id)
    event_types = [e.event_type for e in events]
    assert EventType.EXECUTION_STARTED not in event_types
    assert EventType.EXECUTION_SUCCEEDED not in event_types


# =====================================================================
# Issue #109: Persist decisions before effects survive a rollback
# =====================================================================

@pytest.mark.anyio
async def test_issue_109_persist_decisions_and_events_before_effect(service_env, monkeypatch):
    service: ScopewatchService = service_env["service"]
    run = service_env["run"]
    workspace_dir: Path = service_env["workspace_dir"]
    db_file: Path = service_env["db_file"]

    # Provide a simple clean auditor
    mock_provider = MockAuditorProvider()
    service._auditor = ReasoningAuditor(
        provider=mock_provider, profile="mock", model="mock-auditor"
    )

    orig_create_receipt = ScopewatchRepository.create_execution_receipt

    # Patch create_execution_receipt to fail simulating a crash or disk failure after effect
    def broken_create_receipt(conn, receipt):
        if receipt.status == ExecutionStatus.EXECUTED:
            raise sqlite3.OperationalError("Simulated post-execution receipt persistence error")
        return orig_create_receipt(conn, receipt)

    monkeypatch.setattr(
        ScopewatchRepository, "create_execution_receipt", broken_create_receipt
    )

    req = SubmitActionRequest(
        tool="workspace",
        operation="write_text",
        resource="outputs/report109.txt",
        arguments={"content": "Report data that survived disk write"},
        exposed_reasoning_trace="Generating safe report.",
        turn_id="turn-109",
    )

    # Attempt submission; receipt insertion will fail
    with pytest.raises(sqlite3.OperationalError, match="Simulated post-execution receipt persistence error"):
        await service.submit_action(run.id, req)

    # Verify the file was written to disk by the executor
    report_file = workspace_dir / "outputs" / "report109.txt"
    assert report_file.exists()
    assert report_file.read_text(encoding="utf-8") == "Report data that survived disk write"

    # Verify the database durably recorded the action, decision, and EXECUTION_STARTED
    # (they did not roll back with the failed receipt!)
    conn = get_connection(db_file)
    try:
        cur = conn.execute("SELECT * FROM action_requests WHERE resource = 'outputs/report109.txt'")
        action_row = cur.fetchone()
        assert action_row is not None, "Action request must survive in database"

        cur = conn.execute("SELECT * FROM policy_decisions WHERE action_request_id = ?", (action_row["id"],))
        dec_row = cur.fetchone()
        assert dec_row is not None, "Policy decision must survive in database"
        assert dec_row["outcome"] == "ALLOW"

        cur = conn.execute("SELECT event_type FROM evidence_events WHERE run_id = ?", (run.id,))
        event_types = [row["event_type"] for row in cur.fetchall()]
        assert "ACTION_REQUESTED" in event_types
        assert "POLICY_ALLOWED" in event_types
        assert "EXECUTION_STARTED" in event_types
    finally:
        conn.close()


# =====================================================================
# Issue #111: Do not reuse audit verdict for unseen actions in same turn
# =====================================================================

@pytest.mark.anyio
async def test_issue_111_do_not_reuse_audit_verdict_for_different_actions_in_turn(service_env):
    service: ScopewatchService = service_env["service"]
    run = service_env["run"]

    call_count = 0

    class ContextualAuditorProvider(MockAuditorProvider):
        def audit_chat(self, messages, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Inspect messages
            user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
            if "outputs/blocked.txt" in user_msg:
                return AuditorChatResponse(
                    content=json.dumps({
                        "verdict": "CONCERN",
                        "concern_type": "POLICY_EVASION",
                        "flagged_excerpts": ["instructed by user prompt"],
                        "explanation": "Auditor detected concern targeting blocked.txt",
                    }),
                    model="mock-contextual-auditor",
                    profile="mock",
                    latency_ms=15.0,
                )
            return AuditorChatResponse(
                content=json.dumps({
                    "verdict": "NO_CONCERN",
                    "concern_type": None,
                    "flagged_excerpts": [],
                    "explanation": "Safe operation on safe.txt",
                }),
                model="mock-contextual-auditor",
                profile="mock",
                latency_ms=10.0,
            )

    auditor = ReasoningAuditor(
        provider=ContextualAuditorProvider(), profile="mock", model="mock-contextual-auditor"
    )
    service._auditor = auditor

    turn_id = "shared-turn-111"
    shared_trace = "Writing outputs as instructed by user prompt."

    # Action 1: safe.txt (allowed and no concern)
    req1 = SubmitActionRequest(
        tool="workspace",
        operation="write_text",
        resource="outputs/safe.txt",
        arguments={"content": "safe output"},
        exposed_reasoning_trace=shared_trace,
        turn_id=turn_id,
    )
    res1 = await service.submit_action(run.id, req1)
    assert res1.policy_decision.outcome == PolicyOutcome.ALLOW
    assert res1.reasoning_audit is not None
    assert res1.reasoning_audit.verdict == "NO_CONCERN"
    assert call_count == 1

    # Action 2: blocked.txt with same turn_id and trace
    # MUST NOT reuse the verdict for safe.txt!
    req2 = SubmitActionRequest(
        tool="workspace",
        operation="write_text",
        resource="outputs/blocked.txt",
        arguments={"content": "secret data exfil"},
        exposed_reasoning_trace=shared_trace,
        turn_id=turn_id,
    )
    res2 = await service.submit_action(run.id, req2)
    assert call_count == 2, "Auditor should be invoked for the different resource"
    assert res2.policy_decision.outcome == PolicyOutcome.HOLD
    assert res2.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
    assert res2.reasoning_audit is not None
    assert res2.reasoning_audit.verdict == "CONCERN"

    # Action 3: identical repeat of Action 1 (same tool, op, resource, arguments, turn, trace)
    # CAN safely reuse the cached verdict
    req3 = SubmitActionRequest(
        tool="workspace",
        operation="write_text",
        resource="outputs/safe.txt",
        arguments={"content": "safe output"},
        exposed_reasoning_trace=shared_trace,
        turn_id=turn_id,
    )
    res3 = await service.submit_action(run.id, req3)
    assert call_count == 2, "Identical action in same turn should reuse cached audit"
    assert res3.reasoning_audit.id == res1.reasoning_audit.id
