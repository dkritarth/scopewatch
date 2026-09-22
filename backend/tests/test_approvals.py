"""Unit tests for approval requests and state transitions."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3
import pytest
import uuid

from scopewatch.db import get_connection, init_db
from scopewatch.models import (
    ApprovalStatus,
    PolicyOutcome,
    ReasonCode,
    RunStatus,
)
from scopewatch.repository import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    ScopewatchRepository,
)
from scopewatch.schemas import (
    ActionRequest,
    ApprovalRequest,
    PolicyDecision,
    Run,
    TaskScope,
)


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_approvals.db"
    init_db(db_file)
    conn = get_connection(db_file)
    yield conn
    conn.close()


@pytest.fixture
def fixtures(db_conn: sqlite3.Connection):
    now = datetime.now(timezone.utc).isoformat()
    scope = TaskScope(
        schema_version="1",
        task_description="Approval test",
        allowed_paths=["docs"],
        blocked_paths=[],
        allowed_tools=["workspace"],
        allowed_operations=["delete_path"],
        allowed_network_destinations=[],
        requires_approval=["delete_path"],
        created_at=now,
    )
    run = Run(
        id=str(uuid.uuid4()),
        name="Approval Run",
        task_scope=scope,
        status=RunStatus.WAITING_FOR_APPROVAL,
        created_at=now,
        updated_at=now,
        synthetic=True,
        interception_coverage="Synthetic test",
        reasoning_availability="Unavailable",
    )
    ScopewatchRepository.create_run(db_conn, run)

    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation="delete_path",
        resource="docs/obsolete.txt",
        requested_at=now,
    )
    ScopewatchRepository.create_action_request(db_conn, action)

    decision = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action.id,
        outcome=PolicyOutcome.HOLD,
        reason_code=ReasonCode.APPROVAL_REQUIRED,
        explanation="Requires reviewer sign-off.",
        matched_rule="RULE_APPROVAL_REQUIRED",
        decided_at=now,
        deterministic=True,
    )
    ScopewatchRepository.create_policy_decision(db_conn, decision)

    return run, action, decision


def test_create_and_resolve_approval(db_conn: sqlite3.Connection, fixtures) -> None:
    run, action, decision = fixtures
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(minutes=5)).isoformat()

    approval = ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        action_request_id=action.id,
        policy_decision_id=decision.id,
        status=ApprovalStatus.PENDING,
        requested_at=now.isoformat(),
        expires_at=expires,
    )
    created = ScopewatchRepository.create_approval_request(db_conn, approval)
    assert created.status == ApprovalStatus.PENDING

    # Resolve as APPROVED
    resolved = ScopewatchRepository.resolve_approval(
        db_conn,
        approval_id=approval.id,
        new_status=ApprovalStatus.APPROVED,
        resolved_by="reviewer-1",
        resolved_at=datetime.now(timezone.utc).isoformat(),
        reason="Approved for testing",
    )
    assert resolved.status == ApprovalStatus.APPROVED
    assert resolved.resolved_by == "reviewer-1"


def test_prevent_duplicate_active_approval(db_conn: sqlite3.Connection, fixtures) -> None:
    run, action, decision = fixtures
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(minutes=5)).isoformat()

    app1 = ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        action_request_id=action.id,
        policy_decision_id=decision.id,
        status=ApprovalStatus.PENDING,
        requested_at=now.isoformat(),
        expires_at=expires,
    )
    ScopewatchRepository.create_approval_request(db_conn, app1)

    app2 = ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        action_request_id=action.id,
        policy_decision_id=decision.id,
        status=ApprovalStatus.PENDING,
        requested_at=now.isoformat(),
        expires_at=expires,
    )
    with pytest.raises(RepositoryConflictError, match="active approval already exists"):
        ScopewatchRepository.create_approval_request(db_conn, app2)


def test_replay_resolution_rejected(db_conn: sqlite3.Connection, fixtures) -> None:
    run, action, decision = fixtures
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(minutes=5)).isoformat()

    approval = ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        action_request_id=action.id,
        policy_decision_id=decision.id,
        status=ApprovalStatus.PENDING,
        requested_at=now.isoformat(),
        expires_at=expires,
    )
    ScopewatchRepository.create_approval_request(db_conn, approval)

    # First resolution: approved
    ScopewatchRepository.resolve_approval(
        db_conn,
        approval_id=approval.id,
        new_status=ApprovalStatus.APPROVED,
        resolved_by="reviewer-1",
        resolved_at=datetime.now(timezone.utc).isoformat(),
    )

    # Second resolution: attempted replay
    with pytest.raises(RepositoryConflictError, match="already resolved"):
        ScopewatchRepository.resolve_approval(
            db_conn,
            approval_id=approval.id,
            new_status=ApprovalStatus.DENIED,
            resolved_by="reviewer-2",
            resolved_at=datetime.now(timezone.utc).isoformat(),
        )


def test_consume_approval_makes_it_consumed(db_conn: sqlite3.Connection, fixtures) -> None:
    run, action, decision = fixtures
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(minutes=5)).isoformat()

    approval = ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        action_request_id=action.id,
        policy_decision_id=decision.id,
        status=ApprovalStatus.PENDING,
        requested_at=now.isoformat(),
        expires_at=expires,
    )
    ScopewatchRepository.create_approval_request(db_conn, approval)
    ScopewatchRepository.resolve_approval(
        db_conn,
        approval_id=approval.id,
        new_status=ApprovalStatus.APPROVED,
        resolved_by="reviewer-1",
        resolved_at=datetime.now(timezone.utc).isoformat(),
    )
    ScopewatchRepository.consume_approval(db_conn, approval.id)

    fetched = ScopewatchRepository.get_approval_request(db_conn, approval.id)
    assert fetched.status == ApprovalStatus.CONSUMED
