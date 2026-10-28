"""Real SQLite locks coordinate retention with concurrent operator/gateway work."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest

from scopewatch import retention
from scopewatch.db import db_transaction, get_connection
from scopewatch.models import ApprovalStatus, EventType, RunStatus
from scopewatch.repository import ScopewatchRepository as Repository
from scopewatch.service import ScopewatchService
from scopewatch.workspaces import WorkspacePruneTarget

from test_workspace_retention import NOW, OLD, add_approval, events, seed


def test_pending_approval_committed_after_attempt_is_rechecked_before_effect(tmp_path, monkeypatch):
    db, manager, workspace = seed(tmp_path)
    attempt_committed = threading.Event()
    resume = threading.Event()
    first = True

    @contextmanager
    def pause_after_commit(conn):
        nonlocal first
        with db_transaction(conn):
            yield conn
        if first:
            first = False
            attempt_committed.set()
            assert resume.wait(5), "test did not resume operator"

    monkeypatch.setattr(retention, "db_transaction", pause_after_commit)
    with ThreadPoolExecutor() as pool:
        reaper = pool.submit(retention.reap_workspaces, db, manager, now=NOW, apply=True)
        try:
            assert attempt_committed.wait(5)
            conn = get_connection(db)
            try:
                with db_transaction(conn):
                    add_approval(conn, ApprovalStatus.PENDING)
            finally:
                conn.close()
        finally:
            resume.set()
        report = reaper.result(timeout=5)
    assert report["reaped_count"] == 0
    assert report["failed_count"] == 1
    assert workspace.is_dir()
    assert [e.event_type.value for e in events(db)] == [
        "WORKSPACE_PRUNE_ATTEMPTED", "WORKSPACE_PRUNE_FAILED",
    ]
    assert events(db)[-1].details["reason"] == "pending_approval"


@pytest.mark.parametrize("status,updated_at,reason", [
    (RunStatus.ACTIVE, OLD, "nonterminal"),
    (RunStatus.COMPLETED, NOW.isoformat(), "retention_window"),
])
def test_terminal_state_and_age_are_rechecked_after_attempt(tmp_path, monkeypatch, status, updated_at, reason):
    db, manager, workspace = seed(tmp_path)
    first = True

    @contextmanager
    def change_after_commit(conn):
        nonlocal first
        with db_transaction(conn):
            yield conn
        if first:
            first = False
            other = get_connection(db)
            try:
                with db_transaction(other):
                    Repository.update_run_status(other, "synthetic-run", status, updated_at)
            finally:
                other.close()

    monkeypatch.setattr(retention, "db_transaction", change_after_commit)
    assert retention.reap_workspaces(db, manager, now=NOW, apply=True)["reaped_count"] == 0
    assert workspace.is_dir()
    assert events(db)[-1].details["reason"] == reason


def test_delete_holds_same_write_lock_as_gateway_closure(tmp_path, monkeypatch):
    db, manager, workspace = seed(tmp_path)
    deleting = threading.Event()
    release = threading.Event()
    closing = threading.Event()
    real_delete = WorkspacePruneTarget.delete
    service = ScopewatchService(db_path=db, workspace_root=manager.source_root,
                                run_workspaces_root=manager.root)

    def pause_delete(target):
        deleting.set()
        assert release.wait(5), "test did not release deletion"
        real_delete(target)

    def close_run():
        closing.set()
        return service.complete_run("synthetic-run")

    monkeypatch.setattr(WorkspacePruneTarget, "delete", pause_delete)
    with ThreadPoolExecutor() as pool:
        reaper = pool.submit(retention.reap_workspaces, db, manager, now=NOW, apply=True)
        try:
            assert deleting.wait(5)
            closer = pool.submit(close_run)
            assert closing.wait(5)
            # A separate connection cannot get the dispatch/closure write lock.
            probe = get_connection(db)
            try:
                probe.execute("PRAGMA busy_timeout = 0")
                import sqlite3
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    probe.execute("BEGIN IMMEDIATE")
            finally:
                probe.close()
            assert not closer.done()
        finally:
            release.set()
        assert reaper.result(timeout=5)["reaped_count"] == 1
        assert closer.result(timeout=5)[0].status == RunStatus.COMPLETED
    assert not workspace.exists()
    assert [e.event_type for e in events(db)] == [
        EventType.WORKSPACE_PRUNE_ATTEMPTED, EventType.WORKSPACE_PRUNED, EventType.RUN_COMPLETED,
    ]


def test_concurrent_reapers_cannot_both_report_success(tmp_path):
    db, manager, workspace = seed(tmp_path)
    start = threading.Barrier(2)

    def reap():
        start.wait(timeout=5)
        return retention.reap_workspaces(db, manager, now=NOW, apply=True)

    with ThreadPoolExecutor() as pool:
        first, second = pool.submit(reap), pool.submit(reap)
        reports = [first.result(timeout=10), second.result(timeout=10)]
    assert sum(report["reaped_count"] for report in reports) == 1
    assert not workspace.exists()
    history = events(db)
    assert sum(event.event_type == EventType.WORKSPACE_PRUNED for event in history) == 1
    attempts = {e.details["attempt_id"] for e in history if e.event_type == EventType.WORKSPACE_PRUNE_ATTEMPTED}
    results = {e.details["attempt_id"] for e in history if e.event_type != EventType.WORKSPACE_PRUNE_ATTEMPTED}
    assert attempts == results
