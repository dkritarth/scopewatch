"""Database access repository for runs, actions, decisions, approvals, and events."""

import json
import sqlite3
from typing import Any, Optional
import uuid

from scopewatch.models import (
    ApprovalStatus,
    EventType,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
    ReasoningProvenance,
    RunStatus,
)
from scopewatch.schemas import (
    ActionRequest,
    ApprovalRequest,
    EvidenceEvent,
    ExecutionReceipt,
    PolicyDecision,
    Run,
    TaskScope,
)


class RepositoryConflictError(Exception):
    """Raised on concurrency conflicts, duplicate keys, or invalid state transitions."""


class RepositoryNotFoundError(Exception):
    """Raised when an expected entity does not exist."""


class ScopewatchRepository:
    """Encapsulates SQLite operations with transaction and constraint integrity."""

    # ---------------- Runs ----------------

    @staticmethod
    def create_run(conn: sqlite3.Connection, run: Run) -> Run:
        conn.execute(
            """
            INSERT INTO runs (
                id, name, task_scope_json, status, created_at, updated_at,
                synthetic, interception_coverage, reasoning_availability
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.name,
                run.task_scope.model_dump_json(),
                run.status.value,
                run.created_at,
                run.updated_at,
                1 if run.synthetic else 0,
                run.interception_coverage,
                run.reason_availability if hasattr(run, "reason_availability") else run.reasoning_availability,
            ),
        )
        return run

    @staticmethod
    def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[Run]:
        cur = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        if not row:
            return None
        scope_data = json.loads(row["task_scope_json"])
        return Run(
            id=row["id"],
            name=row["name"],
            task_scope=TaskScope(**scope_data),
            status=RunStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            synthetic=bool(row["synthetic"]),
            interception_coverage=row["interception_coverage"],
            reasoning_availability=row["reasoning_availability"],
        )

    @staticmethod
    def list_runs(conn: sqlite3.Connection) -> list[Run]:
        cur = conn.execute("SELECT * FROM runs ORDER BY created_at DESC")
        runs = []
        for row in cur.fetchall():
            scope_data = json.loads(row["task_scope_json"])
            runs.append(
                Run(
                    id=row["id"],
                    name=row["name"],
                    task_scope=TaskScope(**scope_data),
                    status=RunStatus(row["status"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    synthetic=bool(row["synthetic"]),
                    interception_coverage=row["interception_coverage"],
                    reasoning_availability=row["reasoning_availability"],
                )
            )
        return runs

    @staticmethod
    def update_run_status(
        conn: sqlite3.Connection, run_id: str, new_status: RunStatus, updated_at: str
    ) -> None:
        conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
            (new_status.value, updated_at, run_id),
        )

    # ---------------- Actions ----------------

    @staticmethod
    def create_action_request(conn: sqlite3.Connection, action: ActionRequest) -> ActionRequest:
        conn.execute(
            """
            INSERT INTO action_requests (
                id, run_id, tool, operation, resource, arguments_json,
                requested_by, requested_at, reasoning_summary,
                exposed_reasoning_trace, reasoning_provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action.id,
                action.run_id,
                action.tool,
                action.operation,
                action.resource,
                json.dumps(action.arguments),
                action.requested_by,
                action.requested_at,
                action.reasoning_summary,
                action.exposed_reasoning_trace,
                action.reasoning_provenance.value,
            ),
        )
        return action

    @staticmethod
    def get_action_request(conn: sqlite3.Connection, action_id: str) -> Optional[ActionRequest]:
        cur = conn.execute("SELECT * FROM action_requests WHERE id = ?", (action_id,))
        row = cur.fetchone()
        if not row:
            return None
        return ActionRequest(
            id=row["id"],
            run_id=row["run_id"],
            tool=row["tool"],
            operation=row["operation"],
            resource=row["resource"],
            arguments=json.loads(row["arguments_json"]),
            requested_by=row["requested_by"],
            requested_at=row["requested_at"],
            reasoning_summary=row["reasoning_summary"],
            exposed_reasoning_trace=row["exposed_reasoning_trace"],
            reasoning_provenance=ReasoningProvenance(row["reasoning_provenance"]),
        )

    # ---------------- Policy Decisions ----------------

    @staticmethod
    def create_policy_decision(conn: sqlite3.Connection, decision: PolicyDecision) -> PolicyDecision:
        conn.execute(
            """
            INSERT INTO policy_decisions (
                id, action_request_id, outcome, reason_code, explanation,
                matched_rule, decided_at, deterministic
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                decision.id,
                decision.action_request_id,
                decision.outcome.value,
                decision.reason_code.value,
                decision.explanation,
                decision.matched_rule,
                decision.decided_at,
            ),
        )
        return decision

    @staticmethod
    def get_policy_decision_by_action(conn: sqlite3.Connection, action_id: str) -> Optional[PolicyDecision]:
        cur = conn.execute("SELECT * FROM policy_decisions WHERE action_request_id = ?", (action_id,))
        row = cur.fetchone()
        if not row:
            return None
        return PolicyDecision(
            id=row["id"],
            action_request_id=row["action_request_id"],
            outcome=PolicyOutcome(row["outcome"]),
            reason_code=ReasonCode(row["reason_code"]),
            explanation=row["explanation"],
            matched_rule=row["matched_rule"],
            decided_at=row["decided_at"],
            deterministic=bool(row["deterministic"]),
        )

    # ---------------- Approvals ----------------

    @staticmethod
    def create_approval_request(conn: sqlite3.Connection, approval: ApprovalRequest) -> ApprovalRequest:
        try:
            conn.execute(
                """
                INSERT INTO approval_requests (
                    id, run_id, action_request_id, policy_decision_id,
                    status, requested_at, expires_at, resolved_at, resolved_by,
                    resolution_reason, approval_token_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.id,
                    approval.run_id,
                    approval.action_request_id,
                    approval.policy_decision_id,
                    approval.status.value,
                    approval.requested_at,
                    approval.expires_at,
                    approval.resolved_at,
                    approval.resolved_by,
                    approval.resolution_reason,
                    approval.approval_token_version,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RepositoryConflictError(
                f"An active approval already exists for action {approval.action_request_id}"
            ) from exc
        return approval

    @staticmethod
    def get_approval_request(conn: sqlite3.Connection, approval_id: str) -> Optional[ApprovalRequest]:
        cur = conn.execute(
            """
            SELECT ar.*, a.operation, a.resource, a.tool
            FROM approval_requests ar
            LEFT JOIN action_requests a ON ar.action_request_id = a.id
            WHERE ar.id = ?
            """,
            (approval_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return ApprovalRequest(
            id=row["id"],
            run_id=row["run_id"],
            action_request_id=row["action_request_id"],
            policy_decision_id=row["policy_decision_id"],
            status=ApprovalStatus(row["status"]),
            requested_at=row["requested_at"],
            expires_at=row["expires_at"],
            resolved_at=row["resolved_at"],
            resolved_by=row["resolved_by"],
            resolution_reason=row["resolution_reason"],
            approval_token_version=row["approval_token_version"],
            operation=row["operation"] if "operation" in row.keys() else None,
            resource=row["resource"] if "resource" in row.keys() else None,
            tool=row["tool"] if "tool" in row.keys() else None,
        )

    @staticmethod
    def get_approval_by_action(conn: sqlite3.Connection, action_id: str) -> Optional[ApprovalRequest]:
        cur = conn.execute(
            """
            SELECT ar.*, a.operation, a.resource, a.tool
            FROM approval_requests ar
            LEFT JOIN action_requests a ON ar.action_request_id = a.id
            WHERE ar.action_request_id = ?
            ORDER BY ar.requested_at DESC LIMIT 1
            """,
            (action_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return ApprovalRequest(
            id=row["id"],
            run_id=row["run_id"],
            action_request_id=row["action_request_id"],
            policy_decision_id=row["policy_decision_id"],
            status=ApprovalStatus(row["status"]),
            requested_at=row["requested_at"],
            expires_at=row["expires_at"],
            resolved_at=row["resolved_at"],
            resolved_by=row["resolved_by"],
            resolution_reason=row["resolution_reason"],
            approval_token_version=row["approval_token_version"],
            operation=row["operation"] if "operation" in row.keys() else None,
            resource=row["resource"] if "resource" in row.keys() else None,
            tool=row["tool"] if "tool" in row.keys() else None,
        )

    @staticmethod
    def list_approvals(conn: sqlite3.Connection, status: Optional[ApprovalStatus] = None, run_id: Optional[str] = None) -> list[ApprovalRequest]:
        query = """
            SELECT ar.*, a.operation, a.resource, a.tool
            FROM approval_requests ar
            LEFT JOIN action_requests a ON ar.action_request_id = a.id
            WHERE 1=1
        """
        params: list[Any] = []
        if status:
            query += " AND ar.status = ?"
            params.append(status.value)
        if run_id:
            query += " AND ar.run_id = ?"
            params.append(run_id)
        query += " ORDER BY ar.requested_at DESC"
        cur = conn.execute(query, params)
        approvals = []
        for row in cur.fetchall():
            approvals.append(
                ApprovalRequest(
                    id=row["id"],
                    run_id=row["run_id"],
                    action_request_id=row["action_request_id"],
                    policy_decision_id=row["policy_decision_id"],
                    status=ApprovalStatus(row["status"]),
                    requested_at=row["requested_at"],
                    expires_at=row["expires_at"],
                    resolved_at=row["resolved_at"],
                    resolved_by=row["resolved_by"],
                    resolution_reason=row["resolution_reason"],
                    approval_token_version=row["approval_token_version"],
                    operation=row["operation"] if "operation" in row.keys() else None,
                    resource=row["resource"] if "resource" in row.keys() else None,
                    tool=row["tool"] if "tool" in row.keys() else None,
                )
            )
        return approvals

    @staticmethod
    def resolve_approval(
        conn: sqlite3.Connection,
        approval_id: str,
        new_status: ApprovalStatus,
        resolved_by: str,
        resolved_at: str,
        reason: Optional[str] = None,
    ) -> ApprovalRequest:
        cur = conn.execute("SELECT * FROM approval_requests WHERE id = ?", (approval_id,))
        row = cur.fetchone()
        if not row:
            raise RepositoryNotFoundError(f"Approval request '{approval_id}' not found.")
        current_status = ApprovalStatus(row["status"])
        if current_status != ApprovalStatus.PENDING:
            raise RepositoryConflictError(
                f"Approval '{approval_id}' is already resolved as {current_status.value}."
            )
        conn.execute(
            """
            UPDATE approval_requests
            SET status = ?, resolved_at = ?, resolved_by = ?, resolution_reason = ?
            WHERE id = ? AND status = 'PENDING'
            """,
            (new_status.value, resolved_at, resolved_by, reason, approval_id),
        )
        return ScopewatchRepository.get_approval_request(conn, approval_id)  # type: ignore

    @staticmethod
    def consume_approval(conn: sqlite3.Connection, approval_id: str) -> None:
        """Mark an approved approval request as CONSUMED so it cannot be replayed."""
        conn.execute(
            "UPDATE approval_requests SET status = 'CONSUMED' WHERE id = ? AND status = 'APPROVED'",
            (approval_id,),
        )

    # ---------------- Execution Receipts ----------------

    @staticmethod
    def create_execution_receipt(conn: sqlite3.Connection, receipt: ExecutionReceipt) -> ExecutionReceipt:
        conn.execute(
            """
            INSERT INTO execution_receipts (
                id, action_request_id, status, started_at, completed_at,
                executor, sanitized_result_json, error_code, resource, operation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.id,
                receipt.action_request_id,
                receipt.status.value,
                receipt.started_at,
                receipt.completed_at,
                receipt.executor,
                json.dumps(receipt.sanitized_result) if receipt.sanitized_result is not None else None,
                receipt.error_code,
                receipt.resource,
                receipt.operation,
            ),
        )
        return receipt

    @staticmethod
    def get_execution_receipt_by_action(conn: sqlite3.Connection, action_id: str) -> Optional[ExecutionReceipt]:
        cur = conn.execute("SELECT * FROM execution_receipts WHERE action_request_id = ?", (action_id,))
        row = cur.fetchone()
        if not row:
            return None
        return ExecutionReceipt(
            id=row["id"],
            action_request_id=row["action_request_id"],
            status=ExecutionStatus(row["status"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            executor=row["executor"],
            sanitized_result=json.loads(row["sanitized_result_json"]) if row["sanitized_result_json"] else None,
            error_code=row["error_code"],
            resource=row["resource"],
            operation=row["operation"],
        )

    # ---------------- Evidence Events (Append-only) ----------------

    @staticmethod
    def append_event(
        conn: sqlite3.Connection,
        run_id: str,
        event_type: EventType,
        actor: str,
        summary: str,
        timestamp: str,
        details: dict[str, Any],
        action_request_id: Optional[str] = None,
        policy_decision_id: Optional[str] = None,
        approval_request_id: Optional[str] = None,
        execution_receipt_id: Optional[str] = None,
    ) -> EvidenceEvent:
        # Monotonic sequential ordering per run
        cur = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq FROM evidence_events WHERE run_id = ?",
            (run_id,),
        )
        next_seq = cur.fetchone()["next_seq"]
        event_id = str(uuid.uuid4())

        conn.execute(
            """
            INSERT INTO evidence_events (
                sequence, id, run_id, event_type, timestamp, actor, summary,
                action_request_id, policy_decision_id, approval_request_id,
                execution_receipt_id, details_json, synthetic
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                next_seq,
                event_id,
                run_id,
                event_type.value,
                timestamp,
                actor,
                summary,
                action_request_id,
                policy_decision_id,
                approval_request_id,
                execution_receipt_id,
                json.dumps(details),
            ),
        )

        return EvidenceEvent(
            sequence=next_seq,
            id=event_id,
            run_id=run_id,
            event_type=event_type,
            timestamp=timestamp,
            actor=actor,
            summary=summary,
            action_request_id=action_request_id,
            policy_decision_id=policy_decision_id,
            approval_request_id=approval_request_id,
            execution_receipt_id=execution_receipt_id,
            details=details,
            synthetic=True,
        )

    @staticmethod
    def get_events(
        conn: sqlite3.Connection,
        run_id: str,
        after_sequence: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[EvidenceEvent]:
        query = "SELECT * FROM evidence_events WHERE run_id = ?"
        params: list[Any] = [run_id]
        if after_sequence is not None:
            query += " AND sequence > ?"
            params.append(after_sequence)
        query += " ORDER BY sequence ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        cur = conn.execute(query, params)
        events = []
        for row in cur.fetchall():
            events.append(
                EvidenceEvent(
                    sequence=row["sequence"],
                    id=row["id"],
                    run_id=row["run_id"],
                    event_type=EventType(row["event_type"]),
                    timestamp=row["timestamp"],
                    actor=row["actor"],
                    summary=row["summary"],
                    action_request_id=row["action_request_id"],
                    policy_decision_id=row["policy_decision_id"],
                    approval_request_id=row["approval_request_id"],
                    execution_receipt_id=row["execution_receipt_id"],
                    details=json.loads(row["details_json"]),
                    synthetic=bool(row["synthetic"]),
                )
            )
        return events
