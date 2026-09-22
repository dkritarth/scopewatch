"""Service layer orchestrating domain operations, transactions, and event generation."""

from datetime import datetime, timezone, timedelta
import logging
from pathlib import Path
import sqlite3
from typing import Optional
import uuid

from fastapi import status

from scopewatch.config import DEFAULT_EXPIRY_SECONDS, DEMO_REVIEWER_ID
from scopewatch.db import db_transaction, get_connection
from scopewatch.errors import ScopewatchAPIError
from scopewatch.events import broadcaster
from scopewatch.executor import ExecutionSecurityError, execute_action
from scopewatch.models import (
    ApprovalStatus,
    EventType,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
    ReasoningProvenance,
    RunStatus,
)
from scopewatch.policy import evaluate_policy
from scopewatch.repository import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    ScopewatchRepository,
)
from scopewatch.schemas import (
    ActionRequest,
    ActionResponse,
    ApprovalRequest,
    ApprovalResolutionResponse,
    EvidenceEvent,
    ExecutionReceipt,
    PolicyDecision,
    Run,
    SubmitActionRequest,
    TaskScope,
)

logger = logging.getLogger("scopewatch.service")


class ScopewatchService:
    def __init__(self, db_path: Path | str, workspace_root: Path | str) -> None:
        self.db_path = Path(db_path)
        self.workspace_root = Path(workspace_root)

    def _get_conn(self) -> sqlite3.Connection:
        return get_connection(self.db_path)

    # ---------------- Runs ----------------

    def create_run(self, name: str, task_scope: TaskScope) -> tuple[Run, list[EvidenceEvent]]:
        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        run = Run(
            id=run_id,
            name=name,
            task_scope=task_scope,
            status=RunStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            synthetic=True,
            interception_coverage=(
                "This local baseline mediates only actions submitted through its "
                "synthetic demo gateway. It does not intercept arbitrary host or agent operations."
            ),
            reasoning_availability=(
                "Provider traces unavailable in local baseline. Agent-authored summaries or "
                "synthetic fixtures are labeled explicitly."
            ),
        )

        conn = self._get_conn()
        try:
            with db_transaction(conn):
                ScopewatchRepository.create_run(conn, run)
                event = ScopewatchRepository.append_event(
                    conn,
                    run_id=run.id,
                    event_type=EventType.RUN_CREATED,
                    actor="system",
                    summary=f"Run '{name}' initialized with task scope.",
                    timestamp=now,
                    details={
                        "task_description": task_scope.task_description,
                        "allowed_paths": task_scope.allowed_paths,
                        "blocked_paths": task_scope.blocked_paths,
                        "allowed_tools": task_scope.allowed_tools,
                        "allowed_operations": task_scope.allowed_operations,
                        "requires_approval": task_scope.requires_approval,
                    },
                )
            return run, [event]
        finally:
            conn.close()

    def get_run(self, run_id: str) -> Run:
        conn = self._get_conn()
        try:
            run = ScopewatchRepository.get_run(conn, run_id)
            if not run:
                raise ScopewatchAPIError(
                    code="RUN_NOT_FOUND",
                    message=f"Run '{run_id}' not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            return run
        finally:
            conn.close()

    def list_runs(self) -> list[Run]:
        conn = self._get_conn()
        try:
            return ScopewatchRepository.list_runs(conn)
        finally:
            conn.close()

    def complete_run(self, run_id: str) -> tuple[Run, EvidenceEvent]:
        conn = self._get_conn()
        try:
            run = ScopewatchRepository.get_run(conn, run_id)
            if not run:
                raise ScopewatchAPIError(
                    code="RUN_NOT_FOUND",
                    message=f"Run '{run_id}' not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            now = datetime.now(timezone.utc).isoformat()
            with db_transaction(conn):
                ScopewatchRepository.update_run_status(conn, run_id, RunStatus.COMPLETED, now)
                event = ScopewatchRepository.append_event(
                    conn,
                    run_id=run_id,
                    event_type=EventType.RUN_COMPLETED,
                    actor=DEMO_REVIEWER_ID,
                    summary=f"Run '{run.name}' was marked COMPLETED.",
                    timestamp=now,
                    details={},
                )
            run.status = RunStatus.COMPLETED
            run.updated_at = now
            return run, event
        finally:
            conn.close()

    # ---------------- Actions ----------------

    async def submit_action(self, run_id: str, request: SubmitActionRequest) -> ActionResponse:
        conn = self._get_conn()
        try:
            run = ScopewatchRepository.get_run(conn, run_id)
            if not run:
                raise ScopewatchAPIError(
                    code="RUN_NOT_FOUND",
                    message=f"Run '{run_id}' not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            if run.status != RunStatus.ACTIVE and run.status != RunStatus.WAITING_FOR_APPROVAL:
                raise ScopewatchAPIError(
                    code="RUN_NOT_ACTIVE",
                    message=f"Cannot submit action: run is in status {run.status.value}.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            action_id = str(uuid.uuid4())
            now_iso = datetime.now(timezone.utc).isoformat()

            action = ActionRequest(
                id=action_id,
                run_id=run_id,
                tool=request.tool,
                operation=request.operation,
                resource=request.resource,
                arguments=request.arguments,
                requested_by=request.requested_by or "synthetic-agent",
                requested_at=now_iso,
                reasoning_summary=request.reasoning_summary,
                exposed_reasoning_trace=request.exposed_reasoning_trace,
                reasoning_provenance=request.reasoning_provenance or (
                    ReasoningProvenance.AGENT_AUTHORED_SUMMARY
                    if request.reasoning_summary
                    else ReasoningProvenance.UNAVAILABLE
                ),
            )

            decision = evaluate_policy(action, run, self.workspace_root)
            approval_req: Optional[ApprovalRequest] = None
            receipt: Optional[ExecutionReceipt] = None
            generated_events: list[EvidenceEvent] = []

            with db_transaction(conn):
                ScopewatchRepository.create_action_request(conn, action)
                ev_req = ScopewatchRepository.append_event(
                    conn,
                    run_id=run_id,
                    event_type=EventType.ACTION_REQUESTED,
                    actor=action.requested_by,
                    summary=f"Requested {action.operation} on '{action.resource}'.",
                    timestamp=now_iso,
                    action_request_id=action.id,
                    details={
                        "tool": action.tool,
                        "operation": action.operation,
                        "resource": action.resource,
                        "reasoning_summary": action.reasoning_summary,
                        "reasoning_provenance": action.reasoning_provenance.value,
                    },
                )
                generated_events.append(ev_req)

                ScopewatchRepository.create_policy_decision(conn, decision)

                if decision.outcome == PolicyOutcome.ALLOW:
                    ev_pol = ScopewatchRepository.append_event(
                        conn,
                        run_id=run_id,
                        event_type=EventType.POLICY_ALLOWED,
                        actor="deterministic-policy",
                        summary=f"Allowed {action.operation} on '{action.resource}': {decision.explanation}",
                        timestamp=decision.decided_at,
                        action_request_id=action.id,
                        policy_decision_id=decision.id,
                        details={
                            "reason_code": decision.reason_code.value,
                            "matched_rule": decision.matched_rule,
                        },
                    )
                    generated_events.append(ev_pol)

                    # Execute action
                    exec_started = datetime.now(timezone.utc).isoformat()
                    ev_start = ScopewatchRepository.append_event(
                        conn,
                        run_id=run_id,
                        event_type=EventType.EXECUTION_STARTED,
                        actor="synthetic-workspace-executor",
                        summary=f"Dispatching {action.operation} to synthetic executor.",
                        timestamp=exec_started,
                        action_request_id=action.id,
                        details={"resource": action.resource},
                    )
                    generated_events.append(ev_start)

                    receipt = execute_action(
                        action,
                        self.workspace_root,
                        policy_decision=decision,
                    )
                    ScopewatchRepository.create_execution_receipt(conn, receipt)

                    success = receipt.status == ExecutionStatus.EXECUTED
                    ev_end = ScopewatchRepository.append_event(
                        conn,
                        run_id=run_id,
                        event_type=EventType.EXECUTION_SUCCEEDED if success else EventType.EXECUTION_FAILED,
                        actor="synthetic-workspace-executor",
                        summary=(
                            f"Successfully executed {action.operation} on '{action.resource}'."
                            if success
                            else f"Failed execution of {action.operation}: {receipt.error_code}"
                        ),
                        timestamp=receipt.completed_at,
                        action_request_id=action.id,
                        execution_receipt_id=receipt.id,
                        details={
                            "status": receipt.status.value,
                            "result": receipt.sanitized_result,
                            "error_code": receipt.error_code,
                        },
                    )
                    generated_events.append(ev_end)

                elif decision.outcome == PolicyOutcome.HOLD:
                    ev_pol = ScopewatchRepository.append_event(
                        conn,
                        run_id=run_id,
                        event_type=EventType.POLICY_HELD,
                        actor="deterministic-policy",
                        summary=f"Held {action.operation} on '{action.resource}' for approval: {decision.explanation}",
                        timestamp=decision.decided_at,
                        action_request_id=action.id,
                        policy_decision_id=decision.id,
                        details={
                            "reason_code": decision.reason_code.value,
                            "matched_rule": decision.matched_rule,
                        },
                    )
                    generated_events.append(ev_pol)

                    expires_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_EXPIRY_SECONDS)
                    ).isoformat()
                    approval_req = ApprovalRequest(
                        id=str(uuid.uuid4()),
                        run_id=run_id,
                        action_request_id=action.id,
                        policy_decision_id=decision.id,
                        status=ApprovalStatus.PENDING,
                        requested_at=now_iso,
                        expires_at=expires_at,
                    )
                    ScopewatchRepository.create_approval_request(conn, approval_req)

                    ev_app = ScopewatchRepository.append_event(
                        conn,
                        run_id=run_id,
                        event_type=EventType.APPROVAL_REQUESTED,
                        actor="system",
                        summary=f"Approval requested for {action.operation} on '{action.resource}'.",
                        timestamp=now_iso,
                        action_request_id=action.id,
                        policy_decision_id=decision.id,
                        approval_request_id=approval_req.id,
                        details={
                            "expires_at": expires_at,
                            "reason": decision.explanation,
                        },
                    )
                    generated_events.append(ev_app)
                    ScopewatchRepository.update_run_status(
                        conn, run_id, RunStatus.WAITING_FOR_APPROVAL, now_iso
                    )

                elif decision.outcome == PolicyOutcome.DENY:
                    ev_pol = ScopewatchRepository.append_event(
                        conn,
                        run_id=run_id,
                        event_type=EventType.POLICY_DENIED,
                        actor="deterministic-policy",
                        summary=f"Denied {action.operation} on '{action.resource}': {decision.explanation}",
                        timestamp=decision.decided_at,
                        action_request_id=action.id,
                        policy_decision_id=decision.id,
                        details={
                            "reason_code": decision.reason_code.value,
                            "matched_rule": decision.matched_rule,
                        },
                    )
                    generated_events.append(ev_pol)

                    receipt = ExecutionReceipt(
                        id=str(uuid.uuid4()),
                        action_request_id=action.id,
                        status=ExecutionStatus.NOT_EXECUTED,
                        started_at=decision.decided_at,
                        completed_at=decision.decided_at,
                        executor="synthetic-workspace-executor",
                        sanitized_result={"reason": "Policy outcome was DENY; execution blocked."},
                        error_code=decision.reason_code.value,
                        resource=action.resource,
                        operation=action.operation,
                    )
                    ScopewatchRepository.create_execution_receipt(conn, receipt)

            # Broadcast generated events to live SSE subscribers
            for ev in generated_events:
                await broadcaster.publish(run_id, ev)

            return ActionResponse(
                action_request=action,
                policy_decision=decision,
                approval_request=approval_req,
                execution_receipt=receipt,
                events=generated_events,
            )
        finally:
            conn.close()

    def get_action(self, run_id: str, action_id: str) -> ActionResponse:
        conn = self._get_conn()
        try:
            action = ScopewatchRepository.get_action_request(conn, action_id)
            if not action or action.run_id != run_id:
                raise ScopewatchAPIError(
                    code="ACTION_NOT_FOUND",
                    message=f"Action '{action_id}' not found for run '{run_id}'.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            decision = ScopewatchRepository.get_policy_decision_by_action(conn, action_id)
            approval = ScopewatchRepository.get_approval_by_action(conn, action_id)
            receipt = ScopewatchRepository.get_execution_receipt_by_action(conn, action_id)
            return ActionResponse(
                action_request=action,
                policy_decision=decision,  # type: ignore
                approval_request=approval,
                execution_receipt=receipt,
                events=[],
            )
        finally:
            conn.close()

    # ---------------- Approvals ----------------

    def list_approvals(self, status_filter: Optional[ApprovalStatus] = None, run_id: Optional[str] = None) -> list[ApprovalRequest]:
        conn = self._get_conn()
        try:
            return ScopewatchRepository.list_approvals(conn, status=status_filter, run_id=run_id)
        finally:
            conn.close()

    async def resolve_approval(
        self,
        approval_id: str,
        approve: bool,
        resolved_by: str = DEMO_REVIEWER_ID,
        reason: Optional[str] = None,
    ) -> ApprovalResolutionResponse:
        conn = self._get_conn()
        try:
            approval = ScopewatchRepository.get_approval_request(conn, approval_id)
            if not approval:
                raise ScopewatchAPIError(
                    code="APPROVAL_NOT_FOUND",
                    message=f"Approval request '{approval_id}' not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            if approval.status != ApprovalStatus.PENDING:
                raise ScopewatchAPIError(
                    code="APPROVAL_ALREADY_RESOLVED",
                    message=f"Approval request '{approval_id}' is already {approval.status.value}.",
                    status_code=status.HTTP_409_CONFLICT,
                )

            # Check expiration
            now = datetime.now(timezone.utc)
            now_iso = now.isoformat()
            expires_at = datetime.fromisoformat(approval.expires_at)
            if now > expires_at:
                with db_transaction(conn):
                    ScopewatchRepository.resolve_approval(
                        conn,
                        approval_id=approval_id,
                        new_status=ApprovalStatus.EXPIRED,
                        resolved_by="system",
                        resolved_at=now_iso,
                        reason="Approval request expired.",
                    )
                    ev_exp = ScopewatchRepository.append_event(
                        conn,
                        run_id=approval.run_id,
                        event_type=EventType.APPROVAL_EXPIRED,
                        actor="system",
                        summary=f"Approval request '{approval_id}' expired.",
                        timestamp=now_iso,
                        action_request_id=approval.action_request_id,
                        approval_request_id=approval_id,
                        details={"expired_at": approval.expires_at},
                    )
                await broadcaster.publish(approval.run_id, ev_exp)
                raise ScopewatchAPIError(
                    code="APPROVAL_EXPIRED",
                    message=f"Approval request '{approval_id}' has expired.",
                    status_code=status.HTTP_409_CONFLICT,
                )

            action = ScopewatchRepository.get_action_request(conn, approval.action_request_id)
            decision = ScopewatchRepository.get_policy_decision_by_action(conn, approval.action_request_id)

            if not action or not decision:
                raise ScopewatchAPIError(
                    code="ENTITY_NOT_FOUND",
                    message="Associated action or policy decision not found.",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            # Invariant: Cannot approve if deterministic decision is DENY
            if approve and decision.outcome == PolicyOutcome.DENY:
                raise ScopewatchAPIError(
                    code="DENIED_ACTION_CANNOT_BE_APPROVED",
                    message="Approval cannot override a deterministic DENY outcome.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            receipt: Optional[ExecutionReceipt] = None
            generated_events: list[EvidenceEvent] = []

            with db_transaction(conn):
                if approve:
                    updated_approval = ScopewatchRepository.resolve_approval(
                        conn,
                        approval_id=approval_id,
                        new_status=ApprovalStatus.APPROVED,
                        resolved_by=resolved_by,
                        resolved_at=now_iso,
                        reason=reason,
                    )
                    ev_app = ScopewatchRepository.append_event(
                        conn,
                        run_id=approval.run_id,
                        event_type=EventType.APPROVAL_GRANTED,
                        actor=resolved_by,
                        summary=f"Approved action {action.operation} on '{action.resource}'.",
                        timestamp=now_iso,
                        action_request_id=action.id,
                        approval_request_id=approval_id,
                        details={"reason": reason},
                    )
                    generated_events.append(ev_app)

                    # Execute approved action
                    exec_started = datetime.now(timezone.utc).isoformat()
                    ev_start = ScopewatchRepository.append_event(
                        conn,
                        run_id=approval.run_id,
                        event_type=EventType.EXECUTION_STARTED,
                        actor="synthetic-workspace-executor",
                        summary=f"Executing approved {action.operation} on '{action.resource}'.",
                        timestamp=exec_started,
                        action_request_id=action.id,
                        details={"resource": action.resource},
                    )
                    generated_events.append(ev_start)

                    receipt = execute_action(
                        action,
                        self.workspace_root,
                        policy_decision=decision,
                        approval_request=updated_approval,
                    )
                    ScopewatchRepository.create_execution_receipt(conn, receipt)

                    success = receipt.status == ExecutionStatus.EXECUTED
                    ev_end = ScopewatchRepository.append_event(
                        conn,
                        run_id=approval.run_id,
                        event_type=EventType.EXECUTION_SUCCEEDED if success else EventType.EXECUTION_FAILED,
                        actor="synthetic-workspace-executor",
                        summary=(
                            f"Successfully executed approved {action.operation} on '{action.resource}'."
                            if success
                            else f"Failed execution of approved {action.operation}: {receipt.error_code}"
                        ),
                        timestamp=receipt.completed_at,
                        action_request_id=action.id,
                        execution_receipt_id=receipt.id,
                        details={
                            "status": receipt.status.value,
                            "result": receipt.sanitized_result,
                            "error_code": receipt.error_code,
                        },
                    )
                    generated_events.append(ev_end)

                    # Single-use consumption
                    ScopewatchRepository.consume_approval(conn, approval_id)
                    updated_approval.status = ApprovalStatus.CONSUMED

                else:
                    updated_approval = ScopewatchRepository.resolve_approval(
                        conn,
                        approval_id=approval_id,
                        new_status=ApprovalStatus.DENIED,
                        resolved_by=resolved_by,
                        resolved_at=now_iso,
                        reason=reason,
                    )
                    ev_app = ScopewatchRepository.append_event(
                        conn,
                        run_id=approval.run_id,
                        event_type=EventType.APPROVAL_DENIED,
                        actor=resolved_by,
                        summary=f"Denied approval for {action.operation} on '{action.resource}'.",
                        timestamp=now_iso,
                        action_request_id=action.id,
                        approval_request_id=approval_id,
                        details={"reason": reason},
                    )
                    generated_events.append(ev_app)

                    receipt = ExecutionReceipt(
                        id=str(uuid.uuid4()),
                        action_request_id=action.id,
                        status=ExecutionStatus.NOT_EXECUTED,
                        started_at=now_iso,
                        completed_at=now_iso,
                        executor="synthetic-workspace-executor",
                        sanitized_result={"reason": "Reviewer denied approval."},
                        error_code="APPROVAL_DENIED",
                        resource=action.resource,
                        operation=action.operation,
                    )
                    ScopewatchRepository.create_execution_receipt(conn, receipt)

                # If no more pending approvals for this run, transition back to ACTIVE
                pending = ScopewatchRepository.list_approvals(conn, status=ApprovalStatus.PENDING, run_id=approval.run_id)
                if not pending:
                    ScopewatchRepository.update_run_status(conn, approval.run_id, RunStatus.ACTIVE, now_iso)

            for ev in generated_events:
                await broadcaster.publish(approval.run_id, ev)

            return ApprovalResolutionResponse(
                approval_request=updated_approval,
                execution_receipt=receipt,
                events=generated_events,
            )
        except (RepositoryConflictError, RepositoryNotFoundError) as exc:
            raise ScopewatchAPIError(
                code="APPROVAL_CONFLICT",
                message=str(exc),
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        finally:
            conn.close()

    # ---------------- Events ----------------

    def get_events(
        self,
        run_id: str,
        after_sequence: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[EvidenceEvent]:
        conn = self._get_conn()
        try:
            return ScopewatchRepository.get_events(conn, run_id, after_sequence=after_sequence, limit=limit)
        finally:
            conn.close()
