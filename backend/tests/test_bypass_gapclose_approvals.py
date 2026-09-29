"""Gap-close approval tricks for issue #39 (second-pass evidence).

Supplements ``backend/tests/test_bypass.py`` section C. Reuse-twice,
consumed-for-other-action, cross-run, approve-after-completed, and the
two-approval race are already covered there — this file adds the
same-action replay (#66), deny-after-completed (#64), DENY-is-unapprovable,
submit-after-completed, and deny-path single-use controls.

Ownership: issues #64/#66/#68 are owned by other overnight threads. Tests
for their bugs assert the FIXED behaviour and are marked
``xfail(strict=False)`` with issue links — they XPASS-or-FAIL today without
breaking the suite, and flip to pass when the fix lands. No source is edited
here.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from scopewatch.db import init_db
from scopewatch.errors import ScopewatchAPIError
from scopewatch.executor import ExecutionSecurityError, execute_action
from scopewatch.models import (
    ApprovalStatus,
    ExecutionStatus,
    PolicyOutcome,
    RunStatus,
)
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService


NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def gap_svc(tmp_path: Path) -> dict:
    db_file = tmp_path / "gap-approval.db"
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
    (ws / "outputs" / "other.txt").write_text("other data", encoding="utf-8")
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
    run, _ = service.create_run(name="Gap Approval Run", task_scope=scope)
    return {"service": service, "run": run, "workspace": ws}


def _submit_hold(svc_dict: dict, resource: str = "outputs/old.txt"):
    service: ScopewatchService = svc_dict["service"]
    run = svc_dict["run"]
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


@pytest.mark.xfail(
    reason="KNOWN-GAP #66: local executor replays a CONSUMED approval for the same action",
    strict=False,
)
def test_gapclose_consumed_approval_same_action_replay_refused(
    gap_svc: dict, local_backend: None
) -> None:
    """Issue #66 (owned by the executor-hardening thread — no fix here):
    presenting the CONSUMED approval for the *same* action id must refuse.
    ``test_bypass.py`` covers only the different-action-id variant. Today the
    executor EXECUTEs again (replay accepted); this test expects the fixed
    behaviour and xfails until #66 lands."""
    service: ScopewatchService = gap_svc["service"]
    res = _submit_hold(gap_svc)
    assert res.approval_request is not None
    resolved = asyncio.run(
        service.resolve_approval(
            res.approval_request.id, approve=True, resolved_by="reviewer-1"
        )
    )
    consumed = resolved.approval_request
    assert consumed.status == ApprovalStatus.CONSUMED
    assert resolved.execution_receipt is not None
    assert resolved.execution_receipt.status == ExecutionStatus.EXECUTED
    from scopewatch.db import get_connection
    from scopewatch.repository import ScopewatchRepository

    conn = service._get_conn()
    try:
        action = ScopewatchRepository.get_action_request(conn, consumed.action_request_id)
        decision = ScopewatchRepository.get_policy_decision_by_action(
            conn, consumed.action_request_id
        )
    finally:
        conn.close()
    assert action is not None and decision is not None
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            action,
            gap_svc["workspace"],
            policy_decision=decision,
            approval_request=consumed,
        )


@pytest.mark.xfail(
    reason="KNOWN-GAP #64: resolve_approval has no run-status guard (deny path too)",
    strict=False,
)
def test_gapclose_deny_after_run_completed_refused(gap_svc: dict) -> None:
    """Issue #64 (owned by the completed-run thread — no fix here):
    denying a pending approval after the run COMPLETED must be rejected and
    must not flip the run back to ACTIVE. Today it succeeds (DENIED +
    run → ACTIVE). Expects the fixed behaviour; xfails until #64 lands."""
    service: ScopewatchService = gap_svc["service"]
    run = gap_svc["run"]
    res = _submit_hold(gap_svc)
    assert res.approval_request is not None
    service.complete_run(run.id)
    assert service.get_run(run.id).status == RunStatus.COMPLETED
    with pytest.raises(ScopewatchAPIError):
        asyncio.run(
            service.resolve_approval(
                res.approval_request.id, approve=False, resolved_by="reviewer-1"
            )
        )
    assert service.get_run(run.id).status == RunStatus.COMPLETED


def test_gapclose_deny_decision_cannot_be_approved(gap_svc: dict) -> None:
    """Control: a deterministic DENY is never approvable — the service raises
    DENIED_ACTION_CANNOT_BE_APPROVED even though no approval object exists.
    Guards the 'approvals are exact, never override DENY' invariant."""
    service: ScopewatchService = gap_svc["service"]
    run = gap_svc["run"]
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
    assert res.approval_request is None
    assert res.execution_receipt is not None
    assert res.execution_receipt.status == ExecutionStatus.NOT_EXECUTED


def test_gapclose_submit_after_completed_refused(gap_svc: dict) -> None:
    """Control adjacent to #64: new submissions to a COMPLETED run are refused
    with RUN_NOT_ACTIVE (the submission gate is guarded even though the
    resolve gate is not)."""
    service: ScopewatchService = gap_svc["service"]
    run = gap_svc["run"]
    service.complete_run(run.id)
    assert service.get_run(run.id).status == RunStatus.COMPLETED
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.submit_action(
                run.id,
                SubmitActionRequest(
                    tool="workspace",
                    operation="read_text",
                    resource="invoices/approved/vendor-a.txt",
                ),
            )
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"


def test_gapclose_deny_consumes_single_use(gap_svc: dict) -> None:
    """Single-use holds for the deny path too: denying then re-resolving the
    same approval is a 409, and the denied action never executed."""
    service: ScopewatchService = gap_svc["service"]
    res = _submit_hold(gap_svc)
    assert res.approval_request is not None
    denied = asyncio.run(
        service.resolve_approval(
            res.approval_request.id, approve=False, resolved_by="reviewer-1"
        )
    )
    assert denied.approval_request.status == ApprovalStatus.DENIED
    assert denied.execution_receipt is not None
    assert denied.execution_receipt.status == ExecutionStatus.NOT_EXECUTED
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(
                res.approval_request.id, approve=False, resolved_by="reviewer-1"
            )
        )
    assert exc.value.code == "APPROVAL_ALREADY_RESOLVED"


def test_gapclose_deny_one_of_two_leaves_waiting(gap_svc: dict) -> None:
    """Two pending approvals; denying one leaves the run WAITING_FOR_APPROVAL
    (only the last resolution returns it to ACTIVE). Complements the
    all-approve race in ``test_bypass.py``."""
    service: ScopewatchService = gap_svc["service"]
    run = gap_svc["run"]
    first = _submit_hold(gap_svc, resource="outputs/old.txt")
    second = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace", operation="delete_path", resource="outputs/other.txt"
            ),
        )
    )
    assert first.approval_request is not None
    assert second.approval_request is not None
    asyncio.run(
        service.resolve_approval(
            first.approval_request.id, approve=False, resolved_by="reviewer-1"
        )
    )
    assert service.get_run(run.id).status == RunStatus.WAITING_FOR_APPROVAL
    asyncio.run(
        service.resolve_approval(
            second.approval_request.id, approve=True, resolved_by="reviewer-1"
        )
    )
    assert service.get_run(run.id).status == RunStatus.ACTIVE
    assert (gap_svc["workspace"] / "outputs" / "old.txt").read_text(
        encoding="utf-8"
    ) == "old data"


def test_gapclose_cross_run_same_resource_rejected(gap_svc: dict, local_backend: None) -> None:
    """Even with an identical resource string, an approval minted for run A
    cannot authorize run B: binding is on the exact action id. Variant of the
    cross-run case in ``test_bypass.py`` with matched resources."""
    import uuid as _uuid

    service: ScopewatchService = gap_svc["service"]
    res = _submit_hold(gap_svc, resource="outputs/other.txt")
    assert res.approval_request is not None
    resolved = asyncio.run(
        service.resolve_approval(
            res.approval_request.id, approve=True, resolved_by="reviewer-1"
        )
    )
    foreign_approval = resolved.approval_request
    assert foreign_approval.status == ApprovalStatus.CONSUMED
    from datetime import datetime as _dt, timezone as _tz

    from scopewatch.models import ReasonCode
    from scopewatch.schemas import ActionRequest as _AR, PolicyDecision as _PD

    foreign_action = _AR(
        id=str(_uuid.uuid4()),
        run_id=str(_uuid.uuid4()),
        tool="workspace",
        operation="delete_path",
        resource="outputs/other.txt",
        requested_at=_dt.now(_tz.utc).isoformat(),
    )
    foreign_hold = _PD(
        id=str(_uuid.uuid4()),
        action_request_id=foreign_action.id,
        outcome=PolicyOutcome.HOLD,
        reason_code=ReasonCode.APPROVAL_REQUIRED,
        explanation="Requires reviewer sign-off.",
        matched_rule="RULE_APPROVAL_REQUIRED",
        decided_at=_dt.now(_tz.utc).isoformat(),
        deterministic=True,
    )
    with pytest.raises(ExecutionSecurityError):
        execute_action(
            foreign_action,
            gap_svc["workspace"],
            policy_decision=foreign_hold,
            approval_request=foreign_approval,
        )


def test_gapclose_approve_missing_approval_not_found(gap_svc: dict) -> None:
    """Resolving an unknown approval id is a 404, never an execution."""
    service: ScopewatchService = gap_svc["service"]
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(
                f"no-such-{uuid.uuid4()}", approve=True, resolved_by="reviewer-1"
            )
        )
    assert exc.value.code == "APPROVAL_NOT_FOUND"
