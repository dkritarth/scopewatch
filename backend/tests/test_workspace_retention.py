"""Operator retention uses only synthetic, pytest-owned workspace trees."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import pytest

from scopewatch.db import db_transaction, get_connection, init_db
from scopewatch.models import ApprovalStatus, EventType, PolicyOutcome, ReasonCode, RunStatus
from scopewatch.repository import ScopewatchRepository as Repository
from scopewatch.retention import reap_workspaces
from scopewatch.schemas import ActionRequest, ApprovalRequest, PolicyDecision, Run, TaskScope
from scopewatch.workspaces import RunWorkspaceManager, WORKSPACE_MARKER_NAME

NOW = datetime(2026, 10, 28, 4, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=8)).isoformat()


def seed(tmp_path: Path, *, status=RunStatus.COMPLETED, updated_at=OLD):
    source = tmp_path / "fixture"
    source.mkdir(exist_ok=True)
    (source / "synthetic.txt").write_text("synthetic retention fixture")
    manager = RunWorkspaceManager(source, tmp_path / "runs")
    workspace = manager.initialize("synthetic-run")
    marker_path = workspace / WORKSPACE_MARKER_NAME
    marker = json.loads(marker_path.read_text())
    marker["created_at"] = OLD
    marker_path.write_text(json.dumps(marker))
    db = tmp_path / "evidence.db"
    init_db(db)
    conn = get_connection(db)
    try:
        with db_transaction(conn):
            Repository.create_run(conn, Run(
                id="synthetic-run", name="Synthetic retention run",
                task_scope=TaskScope(task_description="Synthetic only", created_at=OLD),
                status=status, created_at=OLD, updated_at=updated_at,
                workspace_path=str(workspace),
            ))
    finally:
        conn.close()
    return db, manager, workspace


def events(db):
    conn = get_connection(db)
    try:
        return Repository.get_events(conn, "synthetic-run")
    finally:
        conn.close()


def test_default_dry_run_then_explicit_apply_preserves_run_and_evidence(tmp_path):
    db, manager, workspace = seed(tmp_path)
    expected_bytes = sum(p.stat().st_size for p in workspace.rglob("*") if p.is_file())
    report = reap_workspaces(db, manager, now=NOW)
    assert report["eligible_count"] == 1
    assert report["eligible_bytes"] == expected_bytes
    assert report["reaped_count"] == 0
    assert workspace.is_dir()
    assert events(db) == []

    report = reap_workspaces(db, manager, now=NOW, apply=True)
    assert report["reaped_count"] == 1
    assert not workspace.exists()
    assert (manager.source_root / "synthetic.txt").is_file()
    history = events(db)
    assert [e.event_type.value for e in history] == [
        "WORKSPACE_PRUNE_ATTEMPTED", "WORKSPACE_PRUNED",
    ]
    assert history[0].details["attempt_id"] == history[1].details["attempt_id"]
    assert str(tmp_path) not in "".join(e.model_dump_json() for e in history)
    conn = get_connection(db)
    try:
        assert Repository.get_run(conn, "synthetic-run").status == RunStatus.COMPLETED
    finally:
        conn.close()
    assert reap_workspaces(db, manager, now=NOW, apply=True)["reaped_count"] == 0
    assert len(events(db)) == 2


@pytest.mark.parametrize("status", [RunStatus.ACTIVE, RunStatus.WAITING_FOR_APPROVAL])
def test_nonterminal_runs_never_reaped(tmp_path, status):
    db, manager, workspace = seed(tmp_path, status=status)
    report = reap_workspaces(db, manager, now=NOW, apply=True)
    assert report["results"][0]["reason"] == "nonterminal"
    assert workspace.is_dir()
    assert events(db) == []


def add_approval(conn, approval_status):
    Repository.create_action_request(conn, ActionRequest(
        id="action", run_id="synthetic-run", tool="workspace", operation="write_text",
        resource="synthetic.txt", requested_at=OLD,
    ))
    Repository.create_policy_decision(conn, PolicyDecision(
        id="decision", action_request_id="action", outcome=PolicyOutcome.HOLD,
        reason_code=ReasonCode.APPROVAL_REQUIRED, explanation="Synthetic approval",
        matched_rule="synthetic", decided_at=OLD,
    ))
    Repository.create_approval_request(conn, ApprovalRequest(
        id="approval", run_id="synthetic-run", action_request_id="action",
        policy_decision_id="decision", status=approval_status,
        requested_at=OLD, expires_at=OLD,
    ))


@pytest.mark.parametrize("approval_status", [ApprovalStatus.PENDING, ApprovalStatus.APPROVED])
def test_even_expired_but_unresolved_approvals_block_terminal_reap(tmp_path, approval_status):
    db, manager, workspace = seed(tmp_path)
    conn = get_connection(db)
    try:
        with db_transaction(conn):
            add_approval(conn, approval_status)
    finally:
        conn.close()
    report = reap_workspaces(db, manager, now=NOW, apply=True)
    assert report["eligible_count"] == 0
    assert workspace.is_dir()
    assert events(db) == []


@pytest.mark.parametrize("status", [ApprovalStatus.DENIED, ApprovalStatus.EXPIRED, ApprovalStatus.CONSUMED])
def test_resolved_approvals_do_not_block_and_decisions_are_preserved(tmp_path, status):
    db, manager, workspace = seed(tmp_path, status=RunStatus.FAILED)
    conn = get_connection(db)
    try:
        with db_transaction(conn):
            add_approval(conn, status)
        decision_before = Repository.get_policy_decision_by_action(conn, "action")
        assert reap_workspaces(db, manager, now=NOW, apply=True)["reaped_count"] == 1
        assert not workspace.exists()
        assert Repository.get_policy_decision_by_action(conn, "action") == decision_before
        assert Repository.get_approval_request(conn, "approval").status == status
    finally:
        conn.close()


@pytest.mark.parametrize("terminal_event", [None, EventType.EXECUTION_SUCCEEDED, EventType.EXECUTION_FAILED])
def test_direct_allow_execution_must_have_a_terminal_event(tmp_path, terminal_event):
    db, manager, workspace = seed(tmp_path)
    conn = get_connection(db)
    try:
        with db_transaction(conn):
            Repository.create_action_request(conn, ActionRequest(
                id="direct-action", run_id="synthetic-run", tool="workspace",
                operation="read_text", resource="synthetic.txt", requested_at=OLD,
            ))
            for event_type in [EventType.EXECUTION_STARTED, *([terminal_event] if terminal_event else [])]:
                Repository.append_event(conn, "synthetic-run", event_type, "synthetic-executor",
                                        "Synthetic execution", OLD, {}, action_request_id="direct-action")
    finally:
        conn.close()
    report = reap_workspaces(db, manager, now=NOW, apply=True)
    assert report["reaped_count"] == (1 if terminal_event else 0)
    assert workspace.exists() == (terminal_event is None)


@pytest.mark.parametrize("field", ["updated_at", "created_at"])
@pytest.mark.parametrize("offset,eligible", [(-1, True), (0, True), (1, False)])
def test_age_boundary_uses_later_workspace_or_run_time(tmp_path, field, offset, eligible):
    boundary = (NOW - timedelta(days=7) + timedelta(microseconds=offset)).isoformat()
    db, manager, workspace = seed(tmp_path, updated_at=boundary if field == "updated_at" else OLD)
    if field == "created_at":
        marker_path = workspace / WORKSPACE_MARKER_NAME
        marker = json.loads(marker_path.read_text())
        marker["created_at"] = boundary
        marker_path.write_text(json.dumps(marker))
    report = reap_workspaces(db, manager, now=NOW, apply=True)
    assert report["reaped_count"] == int(eligible)
    assert workspace.exists() == (not eligible)


@pytest.mark.parametrize("bad_time", ["not-a-time", "2026-01-01T00:00:00", "99999-01-01"])
def test_invalid_or_naive_time_fails_closed(tmp_path, bad_time):
    db, manager, workspace = seed(tmp_path, updated_at=bad_time)
    assert reap_workspaces(db, manager, now=NOW, apply=True)["eligible_count"] == 0
    assert workspace.is_dir()


def test_removal_failure_is_recorded_without_host_path_or_false_success(tmp_path, monkeypatch):
    from scopewatch.workspaces import WorkspacePruneTarget
    db, manager, workspace = seed(tmp_path)

    def fail_delete(target):
        (workspace / "synthetic.txt").unlink()  # A real partial removal in scratch only.
        raise PermissionError(f"private host path {tmp_path}")

    monkeypatch.setattr(WorkspacePruneTarget, "delete", fail_delete)
    report = reap_workspaces(db, manager, now=NOW, apply=True)
    assert report["failed_count"] == 1
    assert report["reaped_count"] == report["reaped_bytes"] == 0
    assert workspace.is_dir()
    assert [e.event_type.value for e in events(db)] == [
        "WORKSPACE_PRUNE_ATTEMPTED", "WORKSPACE_PRUNE_FAILED",
    ]
    assert str(tmp_path) not in json.dumps(report)
    assert str(tmp_path) not in "".join(e.model_dump_json() for e in events(db))


@pytest.mark.parametrize("mutation", [
    "directory_link", "file_link", "marker_link", "root_link", "ancestor_link",
    "missing_marker", "wrong_owner", "wrong_version", "invalid_marker_time",
    "missing_identity", "sibling_identity", "source_root", "fifo",
])
def test_unknown_symlink_and_unsafe_workspaces_fail_closed(tmp_path, mutation):
    db, manager, workspace = seed(tmp_path)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("Synthetic unrelated directory must survive")
    marker_path = workspace / WORKSPACE_MARKER_NAME
    conn = get_connection(db)
    try:
        if mutation == "directory_link":
            shutil.move(workspace, outside / "moved")
            workspace.symlink_to(outside / "moved", target_is_directory=True)
        elif mutation == "file_link":
            (workspace / "linked").symlink_to(sentinel)
        elif mutation == "marker_link":
            marker_path.rename(outside / "marker")
            marker_path.symlink_to(outside / "marker")
        elif mutation in ("root_link", "ancestor_link"):
            if mutation == "root_link":
                alias = tmp_path / "alias"
                alias.symlink_to(manager.root, target_is_directory=True)
                manager = RunWorkspaceManager(manager.source_root, alias)
            else:
                alias = tmp_path / "parent-alias"
                alias.symlink_to(tmp_path, target_is_directory=True)
                manager = RunWorkspaceManager(manager.source_root, alias / "runs")
            with db_transaction(conn):
                Repository.update_run_workspace_path(conn, "synthetic-run", str(manager.path_for("synthetic-run")), OLD)
        elif mutation == "missing_marker":
            marker_path.unlink()
        elif mutation in ("wrong_owner", "wrong_version", "invalid_marker_time"):
            marker = json.loads(marker_path.read_text())
            field, value = {"wrong_owner": ("run_id", "another-run"),
                            "wrong_version": ("version", 99),
                            "invalid_marker_time": ("created_at", "bad-time")}[mutation]
            marker[field] = value
            marker_path.write_text(json.dumps(marker))
        elif mutation in ("missing_identity", "sibling_identity"):
            with db_transaction(conn):
                Repository.update_run_workspace_path(
                    conn, "synthetic-run", "" if mutation == "missing_identity" else str(outside), OLD,
                )
        elif mutation == "source_root":
            manager = RunWorkspaceManager(manager.root, manager.root)
        elif mutation == "fifo":
            os.mkfifo(workspace / "pipe")
    finally:
        conn.close()
    report = reap_workspaces(db, manager, now=NOW, apply=True)
    assert report["reaped_count"] == report["eligible_count"] == 0
    assert sentinel.read_text() == "Synthetic unrelated directory must survive"
    assert (outside / "moved" if mutation == "directory_link" else workspace).is_dir()
    assert events(db) == []


def test_unsafe_root_containing_database_refuses_entire_operation(tmp_path):
    db, manager, workspace = seed(tmp_path)
    unsafe = RunWorkspaceManager(manager.source_root, manager.source_root / "..")
    with pytest.raises(ValueError, match="outside the managed root"):
        reap_workspaces(db, unsafe, now=NOW, apply=True)
    assert workspace.is_dir()
    assert db.is_file()


def test_reap_only_touches_registered_eligible_run(tmp_path):
    db, manager, workspace = seed(tmp_path)
    orphan = manager.initialize("orphan-not-in-database")
    unknown = manager.root / "unknown-directory"
    unknown.mkdir()
    (unknown / "keep").write_text("synthetic unknown content")
    report = reap_workspaces(db, manager, now=NOW, apply=True)
    assert report["reaped_count"] == 1
    assert not workspace.exists()
    assert orphan.is_dir()
    assert (unknown / "keep").read_text() == "synthetic unknown content"


def test_attempt_commits_before_effect_and_result_commit_failure_does_not_claim_success(tmp_path, monkeypatch):
    import sqlite3
    from scopewatch.workspaces import WorkspacePruneTarget
    db, manager, workspace = seed(tmp_path)
    real_delete = WorkspacePruneTarget.delete
    real_append = Repository.append_event

    def observe_delete(target):
        assert [e.event_type.value for e in events(db)] == ["WORKSPACE_PRUNE_ATTEMPTED"]
        real_delete(target)

    def fail_result(*args, **kwargs):
        if args[2] == EventType.WORKSPACE_PRUNED:
            raise sqlite3.OperationalError("Synthetic result persistence failure")
        return real_append(*args, **kwargs)

    monkeypatch.setattr(WorkspacePruneTarget, "delete", observe_delete)
    monkeypatch.setattr(Repository, "append_event", fail_result)
    with pytest.raises(sqlite3.OperationalError):
        reap_workspaces(db, manager, now=NOW, apply=True)
    assert not workspace.exists()
    assert [e.event_type.value for e in events(db)] == ["WORKSPACE_PRUNE_ATTEMPTED"]


def test_cli_defaults_to_dry_run_then_apply_on_scratch_only(tmp_path):
    db, manager, workspace = seed(tmp_path)
    # A historical synthetic timestamp makes the real-clock subprocess stable.
    conn = get_connection(db)
    try:
        with db_transaction(conn):
            Repository.update_run_status(conn, "synthetic-run", RunStatus.COMPLETED, "2000-01-01T00:00:00+00:00")
    finally:
        conn.close()
    marker_path = workspace / WORKSPACE_MARKER_NAME
    marker = json.loads(marker_path.read_text())
    marker["created_at"] = "2000-01-01T00:00:00+00:00"
    marker_path.write_text(json.dumps(marker))
    command = [sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/reap_workspaces.py"),
               "--db-path", str(db), "--workspace-root", str(manager.source_root),
               "--run-workspaces-root", str(manager.root)]
    dry = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(dry.stdout)["eligible_count"] == 1
    assert workspace.is_dir()
    assert events(db) == []
    applied = subprocess.run([*command, "--apply"], capture_output=True, text=True, check=True)
    assert json.loads(applied.stdout)["reaped_count"] == 1
    assert not workspace.exists()
    assert str(tmp_path) not in dry.stdout + applied.stdout
    invalid = subprocess.run([*command, "--retention-days", "0"], capture_output=True, text=True)
    assert invalid.returncode == 2
    assert str(tmp_path) not in invalid.stderr


def test_retention_evidence_exports_after_terminal_run_and_cannot_reopen_actions(tmp_path):
    from scopewatch.evidence_chain import build_chain_from_events, EvidenceChain
    db, manager, _ = seed(tmp_path)
    conn = get_connection(db)
    try:
        with db_transaction(conn):
            Repository.append_event(conn, "synthetic-run", EventType.RUN_COMPLETED,
                                    "synthetic-reviewer", "Synthetic completion", OLD, {})
    finally:
        conn.close()
    reap_workspaces(db, manager, now=NOW, apply=True)
    chain = build_chain_from_events("synthetic-run", events(db))
    assert chain.verify().ok
    chain.append("POLICY_ALLOWED", action_id="late-action")
    assert chain.verify().code == "ORDERING_VIOLATION"
    for event_type in ("WORKSPACE_PRUNE_ATTEMPTED", "WORKSPACE_PRUNED", "WORKSPACE_PRUNE_FAILED"):
        for terminal, action_id in ((False, None), (True, "action")):
            invalid = EvidenceChain("synthetic-run")
            if terminal:
                invalid.append("RUN_COMPLETED")
            invalid.append(event_type, action_id=action_id)
            assert invalid.verify().code == "ORDERING_VIOLATION"


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_workspace_replacement_immediately_before_delete_is_refused(tmp_path, monkeypatch, replacement):
    from scopewatch.workspaces import WorkspacePruneTarget
    db, manager, workspace = seed(tmp_path)
    moved = manager.root / "displaced-original"
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "keep").write_text("synthetic unrelated content")
    real_delete = WorkspacePruneTarget.delete

    def replace_then_delete(target):
        workspace.rename(moved)
        if replacement == "directory":
            workspace.mkdir()
            (workspace / "keep").write_text("synthetic replacement")
        else:
            workspace.symlink_to(outside, target_is_directory=True)
        real_delete(target)

    monkeypatch.setattr(WorkspacePruneTarget, "delete", replace_then_delete)
    report = reap_workspaces(db, manager, now=NOW, apply=True)
    assert report["reaped_count"] == 0
    assert report["failed_count"] == 1
    assert (moved / "synthetic.txt").is_file()
    assert (outside / "keep").read_text() == "synthetic unrelated content"
    assert workspace.is_dir()
    assert events(db)[-1].event_type == EventType.WORKSPACE_PRUNE_FAILED
