"""SQLite database initialization and connection management."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    task_scope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 1,
    interception_coverage TEXT NOT NULL,
    reasoning_availability TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_requests (
    id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL DEFAULT '1',
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    tool TEXT NOT NULL,
    operation TEXT NOT NULL,
    resource TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    reasoning_summary TEXT,
    exposed_reasoning_trace TEXT,
    reasoning_provenance TEXT NOT NULL,
    turn_id TEXT,
    reasoning_audit_id TEXT
);

CREATE TABLE IF NOT EXISTS reasoning_audits (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    trace_hash TEXT NOT NULL,
    verdict TEXT NOT NULL,
    concern_type TEXT,
    flagged_excerpts_json TEXT NOT NULL,
    explanation TEXT NOT NULL,
    model TEXT NOT NULL,
    profile TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    error_code TEXT,
    audited_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_decisions (
    id TEXT PRIMARY KEY,
    action_request_id TEXT NOT NULL REFERENCES action_requests(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    explanation TEXT NOT NULL,
    matched_rule TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    deterministic INTEGER NOT NULL DEFAULT 1,
    reasoning_audit_id TEXT
);

CREATE TABLE IF NOT EXISTS approval_requests (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    action_request_id TEXT NOT NULL REFERENCES action_requests(id) ON DELETE CASCADE,
    policy_decision_id TEXT NOT NULL REFERENCES policy_decisions(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution_reason TEXT,
    approval_token_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS execution_receipts (
    id TEXT PRIMARY KEY,
    action_request_id TEXT NOT NULL REFERENCES action_requests(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    executor TEXT NOT NULL,
    sanitized_result_json TEXT,
    error_code TEXT,
    resource TEXT NOT NULL,
    operation TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_events (
    sequence INTEGER NOT NULL,
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    actor TEXT NOT NULL,
    summary TEXT NOT NULL,
    action_request_id TEXT,
    policy_decision_id TEXT,
    approval_request_id TEXT,
    execution_receipt_id TEXT,
    details_json TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 1,
    UNIQUE(run_id, sequence)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_active_action
    ON approval_requests(action_request_id)
    WHERE status = 'PENDING';

CREATE INDEX IF NOT EXISTS idx_events_run_seq
    ON evidence_events(run_id, sequence);

CREATE INDEX IF NOT EXISTS idx_approvals_run_status
    ON approval_requests(run_id, status);

CREATE INDEX IF NOT EXISTS idx_approvals_action
    ON approval_requests(action_request_id);

CREATE INDEX IF NOT EXISTS idx_decisions_action
    ON policy_decisions(action_request_id);

CREATE INDEX IF NOT EXISTS idx_receipts_action
    ON execution_receipts(action_request_id);

CREATE INDEX IF NOT EXISTS idx_actions_run
    ON action_requests(run_id);

CREATE INDEX IF NOT EXISTS idx_audit_run_turn_hash
    ON reasoning_audits(run_id, turn_id, trace_hash);
"""


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open an SQLite connection with WAL mode and foreign keys enabled."""
    path = Path(db_path)
    if path != Path(":memory:") and not str(path).startswith("file:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    path_str = str(db_path)
    conn = sqlite3.connect(path_str, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db(db_path: Path | str) -> None:
    """Initialize the SQLite schema idempotently."""
    path = Path(db_path)
    if path != Path(":memory:") and not str(path).startswith("file:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    try:
        conn.executescript(SCHEMA_SQL)
        # Handle migration for existing action_requests table without turn_id/reasoning_audit_id
        cur = conn.execute("PRAGMA table_info(action_requests)")
        cols = {row["name"] for row in cur.fetchall()}
        if cols and "turn_id" not in cols:
            conn.execute("ALTER TABLE action_requests ADD COLUMN turn_id TEXT")
        if cols and "reasoning_audit_id" not in cols:
            conn.execute("ALTER TABLE action_requests ADD COLUMN reasoning_audit_id TEXT")

        cur = conn.execute("PRAGMA table_info(policy_decisions)")
        decision_cols = {row["name"] for row in cur.fetchall()}
        if decision_cols and "reasoning_audit_id" not in decision_cols:
            conn.execute("ALTER TABLE policy_decisions ADD COLUMN reasoning_audit_id TEXT")

        cur = conn.execute("SELECT version FROM schema_version WHERE version = 1")
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (1, datetime('now'))"
            )
    finally:
        conn.close()


@contextmanager
def db_transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    """Provide a transaction block that commits on exit or rolls back on exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
