"""Unit tests for append-only evidence event store."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import pytest
import uuid

from scopewatch.db import db_transaction, get_connection, init_db
from scopewatch.models import EventType, RunStatus
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import Run, TaskScope


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    db_file = tmp_path / "test_events.db"
    init_db(db_file)
    conn = get_connection(db_file)
    yield conn
    conn.close()


@pytest.fixture
def test_run(db_conn: sqlite3.Connection) -> Run:
    scope = TaskScope(
        schema_version="1",
        task_description="Test scope",
        allowed_paths=["outputs"],
        blocked_paths=[],
        allowed_tools=["workspace"],
        allowed_operations=["read_text"],
        allowed_network_destinations=[],
        requires_approval=[],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    run = Run(
        id=str(uuid.uuid4()),
        name="Test Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        synthetic=True,
        interception_coverage="Synthetic test",
        reasoning_availability="Unavailable",
    )
    ScopewatchRepository.create_run(db_conn, run)
    return run


def test_monotonic_ascending_sequence(db_conn: sqlite3.Connection, test_run: Run) -> None:
    now = datetime.now(timezone.utc).isoformat()
    ev1 = ScopewatchRepository.append_event(
        db_conn,
        run_id=test_run.id,
        event_type=EventType.RUN_CREATED,
        actor="system",
        summary="Run initialized",
        timestamp=now,
        details={"synthetic": True},
    )
    ev2 = ScopewatchRepository.append_event(
        db_conn,
        run_id=test_run.id,
        event_type=EventType.ACTION_REQUESTED,
        actor="agent",
        summary="Action submitted",
        timestamp=now,
        details={"op": "read_text"},
    )
    ev3 = ScopewatchRepository.append_event(
        db_conn,
        run_id=test_run.id,
        event_type=EventType.POLICY_ALLOWED,
        actor="policy-engine",
        summary="Policy allowed action",
        timestamp=now,
        details={"rule": "RULE_ALLOWED"},
    )

    assert ev1.sequence == 1
    assert ev2.sequence == 2
    assert ev3.sequence == 3

    events = ScopewatchRepository.get_events(db_conn, test_run.id)
    assert [e.sequence for e in events] == [1, 2, 3]


def test_pagination_after_sequence(db_conn: sqlite3.Connection, test_run: Run) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for i in range(5):
        ScopewatchRepository.append_event(
            db_conn,
            run_id=test_run.id,
            event_type=EventType.ACTION_REQUESTED,
            actor="agent",
            summary=f"Event {i+1}",
            timestamp=now,
            details={"step": i + 1},
        )

    paged = ScopewatchRepository.get_events(db_conn, test_run.id, after_sequence=2, limit=2)
    assert len(paged) == 2
    assert [e.sequence for e in paged] == [3, 4]


def test_state_and_event_commit_in_transaction(db_conn: sqlite3.Connection, test_run: Run) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db_transaction(db_conn):
        ScopewatchRepository.update_run_status(db_conn, test_run.id, RunStatus.COMPLETED, now)
        ScopewatchRepository.append_event(
            db_conn,
            run_id=test_run.id,
            event_type=EventType.RUN_COMPLETED,
            actor="reviewer",
            summary="Run completed by reviewer",
            timestamp=now,
            details={},
        )

    updated_run = ScopewatchRepository.get_run(db_conn, test_run.id)
    assert updated_run.status == RunStatus.COMPLETED

    events = ScopewatchRepository.get_events(db_conn, test_run.id)
    assert len(events) == 1
    assert events[0].event_type == EventType.RUN_COMPLETED


def test_transaction_rollback_preserves_integrity(db_conn: sqlite3.Connection, test_run: Run) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        with db_transaction(db_conn):
            ScopewatchRepository.append_event(
                db_conn,
                run_id=test_run.id,
                event_type=EventType.SYSTEM_ERROR,
                actor="system",
                summary="Should rollback",
                timestamp=now,
                details={},
            )
            raise RuntimeError("Simulated crash")
    except RuntimeError:
        pass

    events = ScopewatchRepository.get_events(db_conn, test_run.id)
    assert len(events) == 0
