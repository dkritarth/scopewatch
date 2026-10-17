"""Issue #192: a typo in SCOPEWATCH_REASONING_AUDIT must not disable the audit.

Two fail-open gaps are covered:

1. Only ``on`` / ``true`` / ``1`` enabled the audit; ``yes``, ``enabled``, or a
   trailing space turned it off silently. Now only an explicit off value
   disables it; everything else stays enabled, and unrecognised values warn.
2. A cached audit row with a verdict outside the three known values left
   ``ALLOW`` in place. Now every unrecognised verdict fails closed to
   ``HOLD / REASONING_AUDIT_FAILED``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scopewatch.db import get_connection, init_db
from scopewatch.models import EventType, PolicyOutcome, ReasonCode
from scopewatch.reasoning_audit import MockAuditorProvider, ReasoningAuditor
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import (
    ActionRequest,
    ReasoningAuditRecord,
    SubmitActionRequest,
    TaskScope,
)
from scopewatch.service import (
    ScopewatchService,
    reasoning_audit_enabled_from_env,
)

UNKNOWN_VERDICT = "MYSTERY_VERDICT"
TRACE = "Reading the approved vendor invoice for the audit summary."


@pytest.fixture
def test_env(tmp_path: Path):
    db_file = tmp_path / "test_gateway.db"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "invoices").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "invoices" / "approved").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "invoices" / "approved" / "vendor-a.txt").write_text(
        "Vendor A Invoice: $1000", encoding="utf-8"
    )

    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=workspace_dir)

    scope = TaskScope(
        schema_version="1",
        task_description="Audit approved invoices.",
        allowed_paths=["invoices/approved"],
        blocked_paths=["invoices/private"],
        allowed_tools=["workspace"],
        allowed_operations=["read_text"],
        allowed_network_destinations=[],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    run, _ = service.create_run(name="Issue 192 Test Run", task_scope=scope)

    return {
        "service": service,
        "run": run,
        "db_file": db_file,
        "workspace_dir": workspace_dir,
    }


# ---------------------------------------------------------------------------
# 1. Environment switch parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, True),
        ("on", True),
        ("on ", True),
        ("  ON  ", True),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("enabled", True),
        ("off", False),
        ("OFF", False),
        (" off ", False),
        ("false", False),
        ("0", False),
        ("", True),
    ],
)
def test_reasoning_audit_env_switch(raw: Any, expected: bool) -> None:
    assert reasoning_audit_enabled_from_env(raw) is expected


def test_unrecognised_value_enables_audit_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="scopewatch.service"):
        assert reasoning_audit_enabled_from_env("tru") is True

    assert any(
        "SCOPEWATCH_REASONING_AUDIT" in record.message for record in caplog.records
    )


def test_recognised_values_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="scopewatch.service"):
        reasoning_audit_enabled_from_env("yes")
        reasoning_audit_enabled_from_env("off")

    assert [r for r in caplog.records if "SCOPEWATCH_REASONING_AUDIT" in r.message] == []


@pytest.mark.parametrize("raw", ["yes", "on "])
def test_affirmative_values_still_run_the_audit(
    test_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Acceptance: "yes" and "on " keep the audit running (#192)."""
    monkeypatch.setenv("SCOPEWATCH_REASONING_AUDIT", raw)
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]

    service._auditor = ReasoningAuditor(
        provider=MockAuditorProvider(), profile="mock", model="mock-auditor"
    )

    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        exposed_reasoning_trace=TRACE,
    )
    res = asyncio.run(service.submit_action(run.id, req))

    assert res.reasoning_audit is not None


# ---------------------------------------------------------------------------
# 2. Cached unknown verdict fails closed
# ---------------------------------------------------------------------------

def _seed_cached_audit_with_unknown_verdict(db_file: Path, run_id: str) -> str:
    """Insert a cached audit row with an unknown verdict plus its joined action."""
    now = datetime.now(timezone.utc).isoformat()
    trace_hash = hashlib.sha256(TRACE.encode("utf-8")).hexdigest()
    audit_id = "cached-mystery-audit"

    conn = get_connection(db_file)
    try:
        ScopewatchRepository.create_reasoning_audit(
            conn,
            ReasoningAuditRecord(
                id=audit_id,
                run_id=run_id,
                turn_id="turn-mystery",
                trace_hash=trace_hash,
                verdict=UNKNOWN_VERDICT,
                concern_type=None,
                flagged_excerpts=[],
                explanation="Synthetic tampered row for issue #192.",
                model="mock-auditor",
                profile="mock",
                latency_ms=1.0,
                error_code=None,
                audited_at=now,
            ),
        )
        # The cache lookup joins the audit to the action it examined (#111), so
        # the tampered row needs a matching action to be found.
        ScopewatchRepository.create_action_request(
            conn,
            ActionRequest(
                id="cached-mystery-action",
                run_id=run_id,
                tool="workspace",
                operation="read_text",
                resource="invoices/approved/vendor-a.txt",
                arguments={},
                requested_by="synthetic-agent",
                requested_at=now,
                turn_id="turn-mystery",
                reasoning_audit_id=audit_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return audit_id


def test_cached_unknown_verdict_fails_closed_to_hold(test_env: dict[str, Any]) -> None:
    """Acceptance: a cached unknown verdict yields HOLD, not ALLOW (#192)."""
    service: ScopewatchService = test_env["service"]
    run = test_env["run"]
    audit_id = _seed_cached_audit_with_unknown_verdict(test_env["db_file"], run.id)

    # The audit must come from the cache, not a fresh call.
    auditor_mock = MagicMock(spec=ReasoningAuditor)
    service._auditor = auditor_mock

    req = SubmitActionRequest(
        tool="workspace",
        operation="read_text",
        resource="invoices/approved/vendor-a.txt",
        exposed_reasoning_trace=TRACE,
        turn_id="turn-mystery",
    )
    res = asyncio.run(service.submit_action(run.id, req))

    auditor_mock.audit_turn.assert_not_called()
    assert res.policy_decision.outcome == PolicyOutcome.HOLD
    assert res.policy_decision.reason_code == ReasonCode.REASONING_AUDIT_FAILED
    assert res.reasoning_audit is not None
    assert res.reasoning_audit.id == audit_id
    assert res.reasoning_audit.verdict == UNKNOWN_VERDICT

    # The unrecognised verdict is reported as a failed audit event, and the raw
    # (attacker-controlled) verdict string is not echoed into the explanation.
    failed_events = [
        e for e in res.events if e.event_type == EventType.REASONING_AUDIT_FAILED
    ]
    assert failed_events
    assert UNKNOWN_VERDICT not in (res.policy_decision.explanation or "")
