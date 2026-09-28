"""Single-use approval enforcement at the executor trust boundary (issue #66).

The executor must refuse same-action replay with a CONSUMED approval object
and refuse execution when the stored authorization disagrees (stored approval
not APPROVED, an execution receipt already exists, or the run is terminal).
The service's valid path (HOLD + in-memory APPROVED, no store handle) must
keep executing.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import uuid

import pytest

from scopewatch.db import get_connection, init_db
from scopewatch.executor import ExecutionSecurityError, execute_action
from scopewatch.models import (
    ApprovalStatus,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
    RunStatus,
)
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import (
    ActionRequest,
    ApprovalRequest,
    PolicyDecision,
    Run,
    SubmitActionRequest,
    TaskScope,
)
from scopewatch.service import ScopewatchService


NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def su_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "outputs").mkdir(parents=True)
    (ws / "outputs" / "old.txt").write_text("old data", encoding="utf-8")
    return ws


def _hold_decision(action_id: str) -> PolicyDecision:
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=PolicyOutcome.HOLD,
        reason_code=ReasonCode.APPROVAL_REQUIRED,
        explanation="Requires reviewer sign-off.",
        matched_rule="RULE_APPROVAL_REQUIRED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )


def _approval(
    action_id: str,
    decision_id: str,
    run_id: str,
    status: ApprovalStatus,
) -> ApprovalRequest:
    now = datetime.now(timezone.utc)
    return ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id=run_id,
        action_request_id=action_id,
        policy_decision_id=decision_id,
        status=status,
        requested_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
    )


def _delete_action(action_id: str, run_id: str) -> ActionRequest:
    return ActionRequest(
        id=action_id,
        run_id=run_id,
        tool="workspace",
        operation="delete_path",
        resource="outputs/old.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
def su_service(tmp_path: Path) -> dict:
    db_file = tmp_path / "single_use.db"
    ws = tmp_path / "workspace"
    (ws / "outputs").mkdir(parents=True)
    (ws / "outputs" / "old.txt").write_text("old data", encoding="utf-8")
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=ws)
    scope = TaskScope(
        schema_version="1",
        task_description="Single-use probe.",
        allowed_paths=["outputs"],
        blocked_paths=[],
        allowed_tools=["workspace"],
        allowed_operations=["read_text", "write_text", "delete_path"],
        allowed_network_destinations=[],
        requires_approval=["delete_path"],
        created_at=NOW,
    )
    run, _ = service.create_run(name="Single-use run", task_scope=scope)
    return {"service": service, "run": run, "workspace": ws, "db": db_file}


def _submit_hold(su_service: dict) -> object:
    service: ScopewatchService = su_service["service"]
    run: Run = su_service["run"]
    res = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace", operation="delete_path", resource="outputs/old.txt"
            ),
        )
    )
    assert res.policy_decision.outcome == PolicyOutcome.HOLD
    assert res.approval_request is not None
    return res


# ---------------------------------------------------------------------
# Core replay guard: CONSUMED in-memory objects never execute (no store
# handle needed, so this also closes the gap without a daemon).
# ---------------------------------------------------------------------


def test_local_consumed_replay_same_action_refused(
    su_workspace: Path, local_backend: None
) -> None:
    """Same-action replay with a CONSUMED approval must raise, not execute."""
    action_id = str(uuid.uuid4())
    action = _delete_action(action_id, "run-1")
    hold = _hold_decision(action_id)
    consumed = _approval(action_id, hold.id, "run-1", ApprovalStatus.CONSUMED)
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            action, su_workspace, policy_decision=hold, approval_request=consumed
        )


def test_docker_consumed_replay_refused_before_daemon(
    su_workspace: Path, docker_backend: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker backend refuses CONSUMED replay before touching Docker."""
    import scopewatch.executor_docker as docker_mod

    def _no_daemon(*args: object, **kwargs: object) -> bool:
        raise AssertionError("Docker must not be consulted on replay refusal")

    monkeypatch.setattr(docker_mod, "is_docker_available", _no_daemon)
    action_id = str(uuid.uuid4())
    action = _delete_action(action_id, "run-1")
    hold = _hold_decision(action_id)
    consumed = _approval(action_id, hold.id, "run-1", ApprovalStatus.CONSUMED)
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            action, su_workspace, policy_decision=hold, approval_request=consumed
        )


def test_valid_approved_path_still_executes_local(
    su_workspace: Path, local_backend: None
) -> None:
    """The service's valid path (HOLD + APPROVED, no store) still executes."""
    action_id = str(uuid.uuid4())
    action = _delete_action(action_id, "run-1")
    hold = _hold_decision(action_id)
    approved = _approval(action_id, hold.id, "run-1", ApprovalStatus.APPROVED)
    receipt = execute_action(
        action, su_workspace, policy_decision=hold, approval_request=approved
    )
    assert receipt.status == ExecutionStatus.EXECUTED


def test_service_approve_still_executes_and_consumes(su_service: dict) -> None:
    """Service resolve_approval keeps working end to end (regression guard)."""
    service: ScopewatchService = su_service["service"]
    res = _submit_hold(su_service)
    assert res.approval_request is not None
    resolved = asyncio.run(
        service.resolve_approval(
            res.approval_request.id, approve=True, resolved_by="reviewer-1"
        )
    )
    assert resolved.approval_request.status == ApprovalStatus.CONSUMED
    assert resolved.execution_receipt is not None
    assert resolved.execution_receipt.status == ExecutionStatus.EXECUTED


def test_replay_with_consumed_object_after_service_consume_refused(
    su_service: dict, local_backend: None
) -> None:
    """Re-presenting the consumed object returned by the service must raise."""
    service: ScopewatchService = su_service["service"]
    res = _submit_hold(su_service)
    assert res.approval_request is not None
    resolved = asyncio.run(
        service.resolve_approval(
            res.approval_request.id, approve=True, resolved_by="reviewer-1"
        )
    )
    consumed = resolved.approval_request
    assert consumed.status == ApprovalStatus.CONSUMED
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            res.action_request,
            su_service["workspace"],
            policy_decision=res.policy_decision,
            approval_request=consumed,
        )


# ---------------------------------------------------------------------
# Verifiable single-use: executor consults the stored approval, the
# execution receipt, and the run status when given a store handle.
# ---------------------------------------------------------------------


def _seed_store(
    db_file: Path,
    run_status: RunStatus = RunStatus.ACTIVE,
    approval_status: ApprovalStatus = ApprovalStatus.PENDING,
) -> dict:
    """Seed a run + HOLD action + approval directly, returning the objects."""
    init_db(db_file)
    conn = get_connection(db_file)
    try:
        now = datetime.now(timezone.utc).isoformat()
        scope = TaskScope(
            schema_version="1",
            task_description="Store-seeded probe.",
            allowed_paths=["outputs"],
            blocked_paths=[],
            allowed_tools=["workspace"],
            allowed_operations=["delete_path"],
            allowed_network_destinations=[],
            requires_approval=["delete_path"],
            created_at=now,
        )
        run = Run(
            id=str(uuid.uuid4()),
            name="Seeded run",
            task_scope=scope,
            status=run_status,
            created_at=now,
            updated_at=now,
        )
        ScopewatchRepository.create_run(conn, run)
        action = _delete_action(str(uuid.uuid4()), run.id)
        ScopewatchRepository.create_action_request(conn, action)
        decision = _hold_decision(action.id)
        ScopewatchRepository.create_policy_decision(conn, decision)
        approval = _approval(action.id, decision.id, run.id, ApprovalStatus.PENDING)
        ScopewatchRepository.create_approval_request(conn, approval)
        if approval_status != ApprovalStatus.PENDING:
            ScopewatchRepository.resolve_approval(
                conn,
                approval_id=approval.id,
                new_status=approval_status,
                resolved_by="reviewer-1",
                resolved_at=now,
            )
            approval = ScopewatchRepository.get_approval_request(conn, approval.id)
            assert approval is not None
        return {"run": run, "action": action, "decision": decision, "approval": approval}
    finally:
        conn.close()


def test_forged_approved_against_pending_store_refused(
    tmp_path: Path, su_workspace: Path, local_backend: None
) -> None:
    """An in-memory APPROVED that the store still shows as PENDING must raise."""
    db_file = tmp_path / "forged.db"
    seeded = _seed_store(db_file, approval_status=ApprovalStatus.PENDING)
    forged = _approval(
        seeded["action"].id, seeded["decision"].id, seeded["run"].id, ApprovalStatus.APPROVED
    )
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            seeded["action"],
            su_workspace,
            policy_decision=seeded["decision"],
            approval_request=forged,
            db_path=db_file,
        )


def test_replay_after_consume_refused_via_store(
    su_service: dict, local_backend: None
) -> None:
    """After the service consumes an approval, any replay via the store raises."""
    service: ScopewatchService = su_service["service"]
    res = _submit_hold(su_service)
    assert res.approval_request is not None
    resolved = asyncio.run(
        service.resolve_approval(
            res.approval_request.id, approve=True, resolved_by="reviewer-1"
        )
    )
    assert resolved.approval_request.status == ApprovalStatus.CONSUMED
    db_file: Path = su_service["db"]
    # The consumed object itself is refused through the store handle too.
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            res.action_request,
            su_service["workspace"],
            policy_decision=res.policy_decision,
            approval_request=resolved.approval_request,
            db_path=db_file,
        )
    # A forged APPROVED for the same action is refused: the store shows
    # CONSUMED and an execution receipt already exists.
    forged = _approval(
        res.action_request.id,
        res.policy_decision.id,
        res.action_request.run_id,
        ApprovalStatus.APPROVED,
    )
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            res.action_request,
            su_service["workspace"],
            policy_decision=res.policy_decision,
            approval_request=forged,
            db_path=db_file,
        )


def test_receipt_exists_blocks_replay_even_when_store_says_approved(
    tmp_path: Path, su_workspace: Path, local_backend: None
) -> None:
    """Stored APPROVED + an existing execution receipt still refuses (replay)."""
    from scopewatch.schemas import ExecutionReceipt

    db_file = tmp_path / "receipt.db"
    seeded = _seed_store(db_file, approval_status=ApprovalStatus.APPROVED)
    conn = get_connection(db_file)
    try:
        now = datetime.now(timezone.utc).isoformat()
        receipt = ExecutionReceipt(
            id=str(uuid.uuid4()),
            action_request_id=seeded["action"].id,
            status=ExecutionStatus.EXECUTED,
            started_at=now,
            completed_at=now,
            executor="synthetic-workspace-executor",
            sanitized_result={"operation": "delete_path"},
            error_code=None,
            resource="outputs/old.txt",
            operation="delete_path",
        )
        ScopewatchRepository.create_execution_receipt(conn, receipt)
    finally:
        conn.close()
    stored_approved = _approval(
        seeded["action"].id, seeded["decision"].id, seeded["run"].id, ApprovalStatus.APPROVED
    )
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            seeded["action"],
            su_workspace,
            policy_decision=seeded["decision"],
            approval_request=stored_approved,
            db_path=db_file,
        )


@pytest.mark.parametrize("terminal", [RunStatus.COMPLETED, RunStatus.FAILED])
def test_terminal_run_execution_refused_via_store(
    tmp_path: Path, su_workspace: Path, local_backend: None, terminal: RunStatus
) -> None:
    """HOLD + APPROVED for an action in a terminal run must raise."""
    db_file = tmp_path / "terminal.db"
    seeded = _seed_store(
        db_file, run_status=terminal, approval_status=ApprovalStatus.APPROVED
    )
    presented = _approval(
        seeded["action"].id, seeded["decision"].id, seeded["run"].id, ApprovalStatus.APPROVED
    )
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            seeded["action"],
            su_workspace,
            policy_decision=seeded["decision"],
            approval_request=presented,
            db_path=db_file,
        )


@pytest.mark.parametrize("terminal", [RunStatus.COMPLETED, RunStatus.FAILED])
def test_allow_on_terminal_run_refused_via_store(
    tmp_path: Path, su_workspace: Path, local_backend: None, terminal: RunStatus
) -> None:
    """Even an ALLOW decision must not execute in a terminal run (store)."""
    db_file = tmp_path / "terminal-allow.db"
    init_db(db_file)
    conn = get_connection(db_file)
    try:
        now = datetime.now(timezone.utc).isoformat()
        scope = TaskScope(
            schema_version="1",
            task_description="Terminal ALLOW probe.",
            allowed_paths=["outputs"],
            blocked_paths=[],
            allowed_tools=["workspace"],
            allowed_operations=["read_text"],
            allowed_network_destinations=[],
            requires_approval=[],
            created_at=now,
        )
        run = Run(
            id=str(uuid.uuid4()),
            name="Terminal run",
            task_scope=scope,
            status=terminal,
            created_at=now,
            updated_at=now,
        )
        ScopewatchRepository.create_run(conn, run)
    finally:
        conn.close()
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation="read_text",
        resource="outputs/old.txt",
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    allow = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action.id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )
    with pytest.raises(ExecutionSecurityError):
        execute_action(action, su_workspace, policy_decision=allow, db_path=db_file)


def test_docker_store_check_runs_before_daemon(
    su_service: dict, docker_backend: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker backend consults the store (and refuses) before touching Docker."""
    import scopewatch.executor_docker as docker_mod

    def _no_daemon(*args: object, **kwargs: object) -> bool:
        raise AssertionError("Docker must not be consulted on store refusal")

    monkeypatch.setattr(docker_mod, "is_docker_available", _no_daemon)
    service: ScopewatchService = su_service["service"]
    res = _submit_hold(su_service)
    assert res.approval_request is not None
    resolved = asyncio.run(
        service.resolve_approval(
            res.approval_request.id, approve=True, resolved_by="reviewer-1"
        )
    )
    assert resolved.approval_request.status == ApprovalStatus.CONSUMED
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            res.action_request,
            su_service["workspace"],
            policy_decision=res.policy_decision,
            approval_request=resolved.approval_request,
            db_path=su_service["db"],
        )


def test_unknown_run_with_store_handle_fails_closed(
    tmp_path: Path, su_workspace: Path, local_backend: None
) -> None:
    """A store handle that knows nothing about the run fails closed."""
    db_file = tmp_path / "empty.db"
    init_db(db_file)
    action_id = str(uuid.uuid4())
    action = _delete_action(action_id, "run-that-does-not-exist")
    hold = _hold_decision(action_id)
    approved = _approval(action_id, hold.id, action.run_id, ApprovalStatus.APPROVED)
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            action,
            su_workspace,
            policy_decision=hold,
            approval_request=approved,
            db_path=db_file,
        )
