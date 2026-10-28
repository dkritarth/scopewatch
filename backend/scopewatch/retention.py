"""Explicit operator retention. No startup hook, scheduler, or HTTP route."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from scopewatch.db import db_transaction, get_connection
from scopewatch.models import ApprovalStatus, EventType, RunStatus
from scopewatch.repository import ScopewatchRepository as Repository
from scopewatch.schemas import Run
from scopewatch.service import ScopewatchService
from scopewatch.workspaces import RunWorkspaceError, RunWorkspaceManager

DEFAULT_RETENTION_DAYS = 7


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Retention timestamps must include a timezone.")
    return parsed


def _guard(conn: sqlite3.Connection, run: Run, cutoff: datetime) -> str | None:
    if run.status not in (RunStatus.COMPLETED, RunStatus.FAILED):
        return "nonterminal"
    if Repository.list_approvals(conn, status_filter=ApprovalStatus.PENDING, run_id=run.id):
        return "pending_approval"
    if Repository.list_approvals(conn, status_filter=ApprovalStatus.APPROVED, run_id=run.id):
        return "approved_action_in_flight"
    if ScopewatchService.in_flight_action_ids(conn, run_id=run.id):
        return "execution_in_flight"
    try:
        if _timestamp(run.updated_at) > cutoff:
            return "retention_window"
    except ValueError:
        return "invalid_timestamp"
    return None


def reap_workspaces(
    db_path: Path | str,
    manager: RunWorkspaceManager,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Report eligible workspaces, deleting only on explicit apply.

    Age is the later of workspace creation and the terminal run's last update.
    The inclusive cutoff is computed once. Every candidate is re-read under
    BEGIN IMMEDIATE immediately before deletion, which holds the same write
    lock as dispatch/approval until the result event commits. Attempt evidence
    commits separately before any effect. A crash may leave only an attempt;
    it must never imply success. Database and evidence rows are never removed.
    """
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 1:
        raise ValueError("Retention days must be a positive integer.")
    instant = now or datetime.now(timezone.utc)
    _timestamp(instant.isoformat())
    try:
        cutoff = instant - timedelta(days=retention_days)
    except OverflowError as exc:
        raise ValueError("Retention window exceeds the supported date range.") from exc
    database = Path(db_path)
    if not database.is_file() or database.resolve().is_relative_to(manager.root.resolve()):
        raise ValueError("Retention requires an existing database outside the managed root.")
    report: dict[str, Any] = {
        "dry_run": not apply, "retention_days": retention_days,
        "cutoff": cutoff.isoformat(), "eligible_count": 0, "eligible_bytes": 0,
        "reaped_count": 0, "reaped_bytes": 0, "failed_count": 0, "skipped_count": 0,
        "results": [],
    }
    conn = get_connection(database)
    try:
        run_ids = [run.id for run in Repository.list_runs(conn)]
        for run_id in run_ids:
            details: dict[str, Any] = {}
            reason = None
            with db_transaction(conn):
                run = Repository.get_run(conn, run_id)
                reason = "missing_run" if run is None else _guard(conn, run, cutoff)
                if reason is None:
                    try:
                        with manager.retention_target(run_id, run.workspace_path) as target:
                            if _timestamp(target.created_at) > cutoff:
                                reason = "retention_window"
                            else:
                                details = {"workspace_key": manager.workspace_key(run_id),
                                           "bytes": target.size_bytes, "retention_days": retention_days,
                                           "cutoff": cutoff.isoformat()}
                    except (RunWorkspaceError, ValueError):
                        reason = "unsafe_or_unavailable_workspace"
                if reason is None and apply:
                    details["attempt_id"] = str(uuid.uuid4())
                    Repository.append_event(
                        conn, run_id, EventType.WORKSPACE_PRUNE_ATTEMPTED,
                        "workspace-retention-operator", "Workspace pruning attempted.",
                        datetime.now(timezone.utc).isoformat(), details,
                    )
            if reason is not None:
                report["skipped_count"] += 1
                report["results"].append({"run_id": run_id, "outcome": "skipped", "reason": reason})
                continue
            report["eligible_count"] += 1
            report["eligible_bytes"] += details["bytes"]
            if not apply:
                report["results"].append({"run_id": run_id, "outcome": "eligible", "bytes": details["bytes"]})
                continue
            # The first transaction committed the attempt before effect. Re-read
            # both DB eligibility and workspace ownership under the dispatch lock.
            with db_transaction(conn):
                run = Repository.get_run(conn, run_id)
                reason = "missing_run" if run is None else _guard(conn, run, cutoff)
                try:
                    if reason is None:
                        with manager.retention_target(run_id, run.workspace_path) as target:
                            if _timestamp(target.created_at) > cutoff:
                                reason = "retention_window"
                            else:
                                details["bytes"] = target.size_bytes
                                target.delete()
                except (RunWorkspaceError, ValueError):
                    reason = "workspace_removal_failed"
                success = reason is None
                result_details = {**details, "outcome": "pruned" if success else "failed"}
                if reason is not None:
                    result_details["reason"] = reason
                Repository.append_event(
                    conn, run_id, EventType.WORKSPACE_PRUNED if success else EventType.WORKSPACE_PRUNE_FAILED,
                    "workspace-retention-operator",
                    "Workspace pruned." if success else "Workspace pruning failed; removal may be partial.",
                    datetime.now(timezone.utc).isoformat(), result_details,
                )
            report["reaped_count" if success else "failed_count"] += 1
            if success:
                report["reaped_bytes"] += details["bytes"]
            report["results"].append({"run_id": run_id, **result_details})
    finally:
        conn.close()
    return report
