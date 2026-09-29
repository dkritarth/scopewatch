"""Edge-case tests for PR #67 approval guard.

PR #67: ``resolve_approval`` must reject terminal runs with 409 RUN_NOT_ACTIVE.

Covers approve/deny on COMPLETED and FAILED runs, executor silence,
retry-after-reject, ACTIVE/WAITING_FOR_APPROVAL controls, DENY-decision
invariant, expired-on-terminal precedence, consumed-approval behaviour,
and evidence-before-effect (no new events on rejection).

No network, no docker. Uses the svc_env fixture style from test_bypass.py.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scopewatch.db import get_connection, init_db
from scopewatch.errors import ScopewatchAPIError
from scopewatch.models import (
    ApprovalStatus,
    ExecutionStatus,
    PolicyOutcome,
    RunStatus,
)
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import ApprovalRequest, SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService

NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def svc_env(tmp_path: Path):
    db_file = tmp_path / "pr67.db"
    ws = tmp_path / "workspace"
    (ws / "invoices" / "approved").mkdir(parents=True)
    (ws / "invoices" / "private").mkdir(parents=True)
    (ws / "outputs").mkdir(parents=True)
    (ws / "invoices" / "approved" / "vendor-a.txt").write_text(
        "Vendor A Invoice: $1000", encoding="utf-8"
    )
    (ws / "invoices" / "private" / "salaries.txt").write_text(
        "Executive Salaries: Confidential", encoding="utf-8"
    )
    (ws / "outputs" / "old.txt").write_text("old data", encoding="utf-8")
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=ws)
    scope = TaskScope(
        schema_version="1",
        task_description="Audit approved invoices and produce outputs",
        allowed_paths=["invoices/approved", "outputs"],
        blocked_paths=["invoices/private"],
        allowed_tools=["workspace"],
        allowed_operations=["read_text", "write_text", "delete_path"],
        allowed_network_destinations=[],
        requires_approval=["delete_path"],
        created_at=NOW,
    )
    run, _ = service.create_run(name="PR67 Service Run", task_scope=scope)
    return {"service": service, "run": run, "workspace": ws, "db_file": db_file}


def _submit_hold(svc_env: dict, resource: str = "outputs/old.txt"):
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace", operation="delete_path", resource=resource
            ),
        )
    )
    assert res.policy_decision.outcome == PolicyOutcome.HOLD
    assert res.approval_request is not None
    assert res.approval_request.status == ApprovalStatus.PENDING
    assert res.execution_receipt is None
    return res


def _fail_if_executed(*args, **kwargs):
    pytest.fail("Executor was called for a terminal run")


# --- terminal-run rejections -------------------------------------------


def test_pr67_approve_completed_run_rejected_run_not_active(svc_env: dict) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.complete_run(run.id)
    assert service.get_run(run.id).status == RunStatus.COMPLETED
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(
                res.approval_request.id, approve=True, resolved_by="reviewer-1"
            )
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"
    action = service.get_action(run.id, res.action_request.id)
    assert action.approval_request is not None
    assert action.approval_request.status == ApprovalStatus.PENDING
    assert action.approval_request.resolved_at is None
    assert action.execution_receipt is None
    assert service.get_run(run.id).status == RunStatus.COMPLETED


def test_pr67_deny_completed_run_rejected_run_not_active(svc_env: dict) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.complete_run(run.id)
    events_before = service.get_events(run.id)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(
                res.approval_request.id, approve=False, resolved_by="reviewer-1"
            )
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"
    action = service.get_action(run.id, res.action_request.id)
    assert action.approval_request is not None
    assert action.approval_request.status == ApprovalStatus.PENDING
    assert action.execution_receipt is None
    assert service.get_events(run.id) == events_before
    assert service.get_run(run.id).status == RunStatus.COMPLETED


def test_pr67_approve_failed_run_rejected_run_not_active(svc_env: dict) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.fail_run(run.id)
    assert service.get_run(run.id).status == RunStatus.FAILED
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(
                res.approval_request.id, approve=True, resolved_by="reviewer-1"
            )
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"
    action = service.get_action(run.id, res.action_request.id)
    assert action.approval_request is not None
    assert action.approval_request.status == ApprovalStatus.PENDING
    assert action.approval_request.resolved_at is None
    assert action.execution_receipt is None
    assert service.get_run(run.id).status == RunStatus.FAILED


def test_pr67_deny_failed_run_rejected_run_not_active(svc_env: dict) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.fail_run(run.id)
    events_before = service.get_events(run.id)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(
                res.approval_request.id, approve=False, resolved_by="reviewer-1"
            )
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"
    action = service.get_action(run.id, res.action_request.id)
    assert action.approval_request is not None
    assert action.approval_request.status == ApprovalStatus.PENDING
    assert action.execution_receipt is None
    assert service.get_events(run.id) == events_before
    assert service.get_run(run.id).status == RunStatus.FAILED


# --- executor silence --------------------------------------------------


def test_pr67_executor_never_called_on_completed_approve(
    svc_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.complete_run(run.id)
    monkeypatch.setattr("scopewatch.service.execute_action", _fail_if_executed)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(res.approval_request.id, approve=True)
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"


def test_pr67_executor_never_called_on_failed_approve(
    svc_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.fail_run(run.id)
    monkeypatch.setattr("scopewatch.service.execute_action", _fail_if_executed)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(res.approval_request.id, approve=True)
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"


def test_pr67_executor_never_called_on_completed_deny(
    svc_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.complete_run(run.id)
    monkeypatch.setattr("scopewatch.service.execute_action", _fail_if_executed)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(res.approval_request.id, approve=False)
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"
    # Deny path would mint a NOT_EXECUTED receipt on success; on rejection none exists.
    action = service.get_action(run.id, res.action_request.id)
    assert action.execution_receipt is None


# --- retry -------------------------------------------------------------


def test_pr67_retry_after_completed_rejection_still_run_not_active(
    svc_env: dict,
) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.complete_run(run.id)
    for reviewer in ("reviewer-1", "reviewer-2"):
        with pytest.raises(ScopewatchAPIError) as exc:
            asyncio.run(
                service.resolve_approval(
                    res.approval_request.id, approve=True, resolved_by=reviewer
                )
            )
        assert exc.value.code == "RUN_NOT_ACTIVE"
    action = service.get_action(run.id, res.action_request.id)
    assert action.execution_receipt is None
    assert service.get_run(run.id).status == RunStatus.COMPLETED


def test_pr67_retry_after_failed_rejection_still_run_not_active(
    svc_env: dict,
) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.fail_run(run.id)
    for approve in (True, False):
        with pytest.raises(ScopewatchAPIError) as exc:
            asyncio.run(
                service.resolve_approval(res.approval_request.id, approve=approve)
            )
        assert exc.value.code == "RUN_NOT_ACTIVE"
    assert service.get_run(run.id).status == RunStatus.FAILED


# --- controls: non-terminal runs still resolvable ----------------------


def test_pr67_active_run_hold_approve_executes_control(svc_env: dict) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    assert service.get_run(run.id).status == RunStatus.WAITING_FOR_APPROVAL
    resolved = asyncio.run(
        service.resolve_approval(
            res.approval_request.id, approve=True, resolved_by="reviewer-1"
        )
    )
    assert resolved.approval_request.status == ApprovalStatus.CONSUMED
    assert resolved.execution_receipt is not None
    assert resolved.execution_receipt.status == ExecutionStatus.EXECUTED
    assert service.get_run(run.id).status == RunStatus.ACTIVE


def test_pr67_waiting_for_approval_second_hold_still_approvable_control(
    svc_env: dict,
) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    first = _submit_hold(svc_env, resource="outputs/old.txt")
    assert first.approval_request is not None
    (svc_env["workspace"] / "outputs" / "other.txt").write_text(
        "other", encoding="utf-8"
    )
    second = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace",
                operation="delete_path",
                resource="outputs/other.txt",
            ),
        )
    )
    assert second.approval_request is not None
    assert service.get_run(run.id).status == RunStatus.WAITING_FOR_APPROVAL
    resolved_first = asyncio.run(
        service.resolve_approval(
            first.approval_request.id, approve=True, resolved_by="reviewer-1"
        )
    )
    assert resolved_first.execution_receipt is not None
    assert resolved_first.execution_receipt.status == ExecutionStatus.EXECUTED
    # One pending left -> run stays WAITING_FOR_APPROVAL, second still resolvable.
    assert service.get_run(run.id).status == RunStatus.WAITING_FOR_APPROVAL
    resolved_second = asyncio.run(
        service.resolve_approval(
            second.approval_request.id, approve=True, resolved_by="reviewer-1"
        )
    )
    assert resolved_second.execution_receipt is not None
    assert service.get_run(run.id).status == RunStatus.ACTIVE


# --- DENY invariant preserved ------------------------------------------


def test_pr67_denied_action_cannot_be_approved_on_active_run(
    svc_env: dict,
) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace",
                operation="read_text",
                resource="invoices/private/salaries.txt",
            ),
        )
    )
    assert res.policy_decision.outcome == PolicyOutcome.DENY
    conn = get_connection(svc_env["db_file"])
    try:
        ScopewatchRepository.create_approval_request(
            conn,
            ApprovalRequest(
                id="rogue-approval-pr67",
                run_id=run.id,
                action_request_id=res.action_request.id,
                policy_decision_id=res.policy_decision.id,
                status=ApprovalStatus.PENDING,
                requested_at=datetime.now(timezone.utc).isoformat(),
                expires_at=(
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(service.resolve_approval("rogue-approval-pr67", approve=True))
    assert exc.value.code == "DENIED_ACTION_CANNOT_BE_APPROVED"
    assert exc.value.status_code == 400


# --- expired-on-terminal precedence (documents actual behaviour) --------


def test_pr67_expired_approval_on_terminal_run_error_precedence_documented(
    svc_env: dict,
) -> None:
    """Expired approval on a COMPLETED run: RUN_NOT_ACTIVE wins over EXPIRED.

    ``resolve_approval`` checks ``require_resolvable_run()`` before the expiry
    branch (service.py), so a terminal run short-circuits to RUN_NOT_ACTIVE
    and the approval is left PENDING (not transitioned to EXPIRED).
    """
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    conn = get_connection(svc_env["db_file"])
    try:
        conn.execute(
            "UPDATE approval_requests SET expires_at = ? WHERE id = ?",
            (past, res.approval_request.id),
        )
    finally:
        conn.close()
    service.complete_run(run.id)
    events_before = service.get_events(run.id)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(res.approval_request.id, approve=True)
        )
    # Actual behaviour: terminal-run guard fires first.
    assert exc.value.code == "RUN_NOT_ACTIVE"
    assert exc.value.status_code == 409
    action = service.get_action(run.id, res.action_request.id)
    assert action.approval_request is not None
    assert action.approval_request.status == ApprovalStatus.PENDING
    assert action.execution_receipt is None
    assert service.get_events(run.id) == events_before
    assert service.get_run(run.id).status == RunStatus.COMPLETED


# --- consumed approvals unaffected --------------------------------------


def test_pr67_consumed_approval_second_resolve_still_already_resolved(
    svc_env: dict,
) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    first = asyncio.run(
        service.resolve_approval(
            res.approval_request.id, approve=True, resolved_by="reviewer-1"
        )
    )
    assert first.approval_request.status == ApprovalStatus.CONSUMED
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(
                res.approval_request.id, approve=True, resolved_by="reviewer-1"
            )
        )
    assert exc.value.code == "APPROVAL_ALREADY_RESOLVED"
    assert exc.value.status_code == 409
    pending = service.list_approvals(
        status_filter=ApprovalStatus.PENDING, run_id=run.id
    )
    assert [a.id for a in pending] == []
    assert service.get_run(run.id).status == RunStatus.ACTIVE


# --- evidence-before-effect: no new events ------------------------------


def test_pr67_rejected_resolve_adds_no_events_completed(svc_env: dict) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.complete_run(run.id)
    events_before = service.get_events(run.id)
    with pytest.raises(ScopewatchAPIError):
        asyncio.run(
            service.resolve_approval(res.approval_request.id, approve=True)
        )
    assert service.get_events(run.id) == events_before


def test_pr67_rejected_resolve_adds_no_events_failed(svc_env: dict) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.fail_run(run.id)
    events_before = service.get_events(run.id)
    with pytest.raises(ScopewatchAPIError):
        asyncio.run(
            service.resolve_approval(res.approval_request.id, approve=False)
        )
    assert service.get_events(run.id) == events_before


# --- status code + residual state ---------------------------------------


def test_pr67_run_not_active_carries_409_status_code(svc_env: dict) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.complete_run(run.id)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(res.approval_request.id, approve=True)
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"
    assert exc.value.status_code == 409


def test_pr67_approval_stays_pending_unresolved_after_terminal_reject(
    svc_env: dict,
) -> None:
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.fail_run(run.id)
    with pytest.raises(ScopewatchAPIError):
        asyncio.run(
            service.resolve_approval(
                res.approval_request.id, approve=True, resolved_by="reviewer-9"
            )
        )
    action = service.get_action(run.id, res.action_request.id)
    assert action.approval_request is not None
    assert action.approval_request.status == ApprovalStatus.PENDING
    assert action.approval_request.resolved_at is None
    assert action.approval_request.resolved_by is None
    assert action.execution_receipt is None


def test_pr67_no_cancelled_run_status_supported(svc_env: dict) -> None:
    """Documents that no cancel/expire-run path exists: RunStatus has exactly
    ACTIVE, WAITING_FOR_APPROVAL, COMPLETED, FAILED and the service exposes
    no cancel_run method."""
    assert set(RunStatus.__members__) == {
        "ACTIVE",
        "WAITING_FOR_APPROVAL",
        "COMPLETED",
        "FAILED",
    }
    assert not hasattr(ScopewatchService, "cancel_run")
    assert "CANCELLED" not in RunStatus.__members__


def test_pr67_submit_after_terminal_still_rejected_coherence(
    svc_env: dict,
) -> None:
    """Coherence check: submit_action on a COMPLETED run is also RUN_NOT_ACTIVE
    (400 at submit time vs 409 at resolve time) — the gateway never admits new
    work for terminal runs."""
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    _submit_hold(svc_env)
    service.complete_run(run.id)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.submit_action(
                run.id,
                SubmitActionRequest(
                    tool="workspace",
                    operation="delete_path",
                    resource="outputs/old.txt",
                ),
            )
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"


def test_pr67_completed_run_id_roundtrip_unchanged(svc_env: dict) -> None:
    """Resolving on a terminal run with an unknown approval id still yields
    APPROVAL_NOT_FOUND (404) — the terminal guard does not shadow lookup."""
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    _submit_hold(svc_env)
    service.complete_run(run.id)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(
                str(uuid.uuid4()), approve=True, resolved_by="reviewer-1"
            )
        )
    assert exc.value.code == "APPROVAL_NOT_FOUND"
    assert exc.value.status_code == 404
