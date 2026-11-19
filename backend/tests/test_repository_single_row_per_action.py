"""One decision per action: the database must refuse a second.

Readers pick a single decision per action, so a stored duplicate would make
the evidence ambiguous (which decision authorized execution?).
"""

import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from scopewatch.db import get_connection, init_db
from scopewatch.models import (
    PolicyOutcome,
    ReasonCode,
    RunStatus,
)
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import PolicyDecision

NOW = datetime.now(timezone.utc).isoformat()


def _seed(conn) -> str:
    run_id, action_id = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute(
        "INSERT INTO runs (id, name, task_scope_json, status, created_at, updated_at,"
        " interception_coverage, reasoning_availability) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, "r", "{}", RunStatus.ACTIVE.value, NOW, NOW, "x", "y"),
    )
    conn.execute(
        "INSERT INTO action_requests (id, run_id, tool, operation, resource, arguments_json,"
        " requested_by, requested_at, reasoning_provenance) VALUES (?,?,?,?,?,?,?,?,?)",
        (action_id, run_id, "workspace", "read_text", "a.txt", "{}", "agent", NOW, "NONE"),
    )
    return action_id


def _decision(action_id: str, outcome: PolicyOutcome) -> PolicyDecision:
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=outcome,
        reason_code=next(iter(ReasonCode)),
        explanation="e",
        matched_rule="r",
        decided_at=NOW,
    )


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    c = get_connection(db)
    yield c
    c.close()


def test_second_decision_for_same_action_is_rejected(conn):
    action_id = _seed(conn)
    ScopewatchRepository.create_policy_decision(conn, _decision(action_id, PolicyOutcome.HOLD))
    with pytest.raises(sqlite3.IntegrityError):
        ScopewatchRepository.create_policy_decision(conn, _decision(action_id, PolicyOutcome.ALLOW))
    stored = ScopewatchRepository.get_policy_decision_by_action(conn, action_id)
    assert stored.outcome == PolicyOutcome.HOLD



def test_legacy_duplicates_keep_working_and_are_logged(tmp_path, caplog):
    """A store that already holds duplicates is not rewritten; init_db warns."""
    db = tmp_path / "legacy.db"
    init_db(db)
    c = get_connection(db)
    try:
        c.execute("DROP INDEX uq_decisions_action")
        action_id = _seed(c)
        ScopewatchRepository.create_policy_decision(c, _decision(action_id, PolicyOutcome.HOLD))
        ScopewatchRepository.create_policy_decision(c, _decision(action_id, PolicyOutcome.ALLOW))
        c.commit()
    finally:
        c.close()
    with caplog.at_level("WARNING", logger="scopewatch.db"):
        init_db(db)
    assert "uniqueness guard was not added" in caplog.text
    c = get_connection(db)
    try:
        count = c.execute(
            "SELECT COUNT(*) FROM policy_decisions WHERE action_request_id = ?", (action_id,)
        ).fetchone()[0]
    finally:
        c.close()
    assert count == 2
