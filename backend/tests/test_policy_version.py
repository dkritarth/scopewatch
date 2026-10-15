"""Issue #119: persist the evaluated policy version in action evidence.

The architecture requires every evidence record to name the policy version
used for a decision, and binds approval to the policy version evaluated for
its action. This module covers the five acceptance criteria:

1. every new decision stores a policy identity independent of ``schema_version``;
2. event, API, and export views expose that identity;
3. pre-version rows read back as visibly unknown/legacy, never backfilled;
4. approval evidence retains the identity evaluated for its action, and the
   defined drift semantics are recorded rather than silently reinterpreted;
5. two policy revisions stay distinguishable after a database reload.

Fixtures are synthetic. The mock auditor is the default profile, so nothing
here performs network I/O.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scopewatch import models
from scopewatch.app import create_app
from scopewatch.db import get_connection, init_db
from scopewatch.evidence_chain import EvidenceChain, build_chain_from_events, payload_for_event
from scopewatch.models import (
    LEGACY_POLICY_VERSION_LABEL,
    POLICY_FINGERPRINT_LENGTH,
    POLICY_RULES_REVISION,
    ApprovalStatus,
    EventType,
    PolicyOutcome,
    format_policy_version,
    get_policy_version,
    policy_rules_fingerprint,
)
from scopewatch.policy import evaluate_policy
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import ActionRequest, PolicyDecision, SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchAPIError, ScopewatchService

NOW = datetime.now(timezone.utc).isoformat()

# A pre-#119 policy_decisions table: same columns, no policy_version.
LEGACY_DECISION_TABLE_SQL = """
CREATE TABLE policy_decisions (
    id TEXT PRIMARY KEY,
    action_request_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    explanation TEXT NOT NULL,
    matched_rule TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    deterministic INTEGER NOT NULL DEFAULT 1,
    reasoning_audit_id TEXT
);
"""


def _scope(**overrides: object) -> TaskScope:
    data: dict[str, object] = {
        "schema_version": "1",
        "task_description": "Issue 119 synthetic policy-identity scope",
        "allowed_paths": ["outputs"],
        "blocked_paths": ["blocked"],
        "allowed_tools": ["workspace"],
        "allowed_operations": ["list_directory", "read_text", "write_text", "delete_path"],
        "allowed_network_destinations": [],
        "requires_approval": ["delete_path"],
        "created_at": NOW,
    }
    data.update(overrides)
    return TaskScope(**data)  # type: ignore[arg-type]


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "outputs").mkdir(parents=True, exist_ok=True)
    (ws / "blocked").mkdir(parents=True, exist_ok=True)
    (ws / "outputs" / "data.txt").write_text("synthetic fixture text", encoding="utf-8")
    (ws / "outputs" / "old.txt").write_text("to delete", encoding="utf-8")
    (ws / "blocked" / "secret.txt").write_text("synthetic blocked", encoding="utf-8")
    return ws


@pytest.fixture
def env(tmp_path: Path) -> dict:
    db_file = tmp_path / "issue_119.db"
    ws = _workspace(tmp_path)
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=ws)
    run, _ = service.create_run(name="Issue 119 run", task_scope=_scope())
    return {"service": service, "run": run, "workspace": ws, "db": db_file}


def _submit(service: ScopewatchService, run_id: str, **kwargs: object):
    kwargs.setdefault("tool", "workspace")
    kwargs.setdefault("operation", "read_text")
    kwargs.setdefault("resource", "outputs/data.txt")
    return asyncio.run(service.submit_action(run_id, SubmitActionRequest(**kwargs)))


def _hold(service: ScopewatchService, run: object):
    """Submit the approval-gated delete and return the HOLD response."""
    return _submit(
        service, run.id, operation="delete_path", resource="outputs/old.txt"  # type: ignore[attr-defined]
    )


def _action(run: object, **overrides: object) -> ActionRequest:
    data: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "run_id": run.id,  # type: ignore[attr-defined]
        "tool": "workspace",
        "operation": "read_text",
        "resource": "outputs/data.txt",
        "requested_at": NOW,
    }
    data.update(overrides)
    return ActionRequest(**data)  # type: ignore[arg-type]


def _events_of(response: object, event_type: EventType) -> list:
    return [e for e in response.events if e.event_type == event_type]  # type: ignore[attr-defined]


def _events_in(service: ScopewatchService, run_id: str, event_type: EventType) -> list:
    return [e for e in service.get_events(run_id) if e.event_type == event_type]


# ---------------------------------------------------------------------------
# The identity itself
# ---------------------------------------------------------------------------


def test_identity_is_the_revision_plus_a_derived_fingerprint() -> None:
    version = get_policy_version()
    fingerprint = policy_rules_fingerprint()

    assert version == f"{POLICY_RULES_REVISION}+{fingerprint}"
    assert len(fingerprint) == POLICY_FINGERPRINT_LENGTH
    assert all(char in "0123456789abcdef" for char in fingerprint)
    # Stable within a process, and not derived from import order.
    assert get_policy_version() == version
    assert policy_rules_fingerprint() == fingerprint


def test_identity_is_independent_of_the_data_schema_version(env: dict) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]

    res = _submit(service, run.id)

    assert res.action_request.schema_version == "1"
    assert res.policy_decision.policy_version == get_policy_version()
    assert res.policy_decision.policy_version != res.action_request.schema_version
    # Neither field leaks into the other: the stored data format is untouched.
    assert not get_policy_version().startswith("1")


def test_fingerprint_tracks_the_declared_rule_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The derived half of the identity cannot drift from the rules it covers."""
    before = get_policy_version()

    monkeypatch.setattr(models, "SUPPORTED_TOOLS", {"workspace", "browser"})
    widened = get_policy_version()
    assert widened != before
    assert widened.split("+", 1)[0] == POLICY_RULES_REVISION

    monkeypatch.undo()
    monkeypatch.setattr(models, "RUN_COMMAND_METACHARACTERS", (";", "&"))
    assert get_policy_version() != before

    monkeypatch.undo()
    assert get_policy_version() == before


def test_bumping_the_revision_changes_the_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    before = get_policy_version()
    monkeypatch.setattr(models, "POLICY_RULES_REVISION", "2026-11-01.1")
    after = get_policy_version()

    assert after != before
    assert after.startswith("2026-11-01.1+")


def test_identity_cannot_be_overridden_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evidence must not be able to name a revision that never evaluated it.

    An operator-settable identity would let a deployment claim any policy
    revision it liked, which is exactly the ambiguity this field removes.
    """
    monkeypatch.setenv("SCOPEWATCH_POLICY_VERSION", "attacker-chosen-revision")
    monkeypatch.setenv("SCOPEWATCH_POLICY_RULES_REVISION", "attacker-chosen-revision")

    assert get_policy_version() == f"{POLICY_RULES_REVISION}+{policy_rules_fingerprint()}"
    assert "attacker" not in get_policy_version()


def test_format_policy_version_labels_unknown_as_legacy() -> None:
    assert format_policy_version(None) == LEGACY_POLICY_VERSION_LABEL
    assert format_policy_version("") == LEGACY_POLICY_VERSION_LABEL
    assert format_policy_version("   ") == LEGACY_POLICY_VERSION_LABEL
    assert format_policy_version(get_policy_version()) == get_policy_version()


def test_identity_carries_no_provider_or_credential_detail() -> None:
    """The identity names a policy revision, not a model or a secret."""
    version = get_policy_version().lower()
    for forbidden in ("http://", "https://", "nebius", "openrouter", "nemotron", "api_key", "sk-"):
        assert forbidden not in version


# ---------------------------------------------------------------------------
# AC1: every new decision stores a policy identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, expected_outcome",
    [
        ({}, PolicyOutcome.ALLOW),
        ({"operation": "delete_path"}, PolicyOutcome.HOLD),
        ({"operation": "network_request", "resource": "outputs/data.txt"}, PolicyOutcome.DENY),
        ({"tool": "shell"}, PolicyOutcome.DENY),
        ({"operation": "teleport"}, PolicyOutcome.DENY),
        ({"resource": "/etc/passwd"}, PolicyOutcome.DENY),
        ({"resource": "../escape.txt"}, PolicyOutcome.DENY),
        ({"resource": "blocked/secret.txt"}, PolicyOutcome.DENY),
        ({"resource": "nope/data.txt"}, PolicyOutcome.DENY),
        ({"resource": "outputs/\x00nul.txt"}, PolicyOutcome.DENY),
        ({"operation": "delete_path", "resource": "outputs/../blocked/secret.txt"}, PolicyOutcome.DENY),
    ],
)
def test_every_rule_path_stamps_the_identity(
    env: dict, overrides: dict, expected_outcome: PolicyOutcome
) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]
    ws: Path = env["workspace"]

    decision = evaluate_policy(_action(run, **overrides), run, ws)

    assert decision.outcome == expected_outcome
    assert decision.policy_version == get_policy_version()
    assert decision.policy_version_label == get_policy_version()


def test_service_stamps_the_identity_on_all_three_outcomes(env: dict) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]

    allowed = _submit(service, run.id)
    held = _hold(service, run)
    denied = _submit(service, run.id, resource="blocked/secret.txt")

    assert allowed.policy_decision.outcome == PolicyOutcome.ALLOW
    assert held.policy_decision.outcome == PolicyOutcome.HOLD
    assert denied.policy_decision.outcome == PolicyOutcome.DENY
    for res in (allowed, held, denied):
        assert res.policy_decision.policy_version == get_policy_version()
    assert denied.execution_receipt is not None
    assert denied.execution_receipt.status.value == "NOT_EXECUTED"


def test_escalated_hold_keeps_the_evaluated_identity(env: dict) -> None:
    """A reasoning escalation merges into the policy decision without re-running
    the engine, so it must not claim a different revision."""
    service: ScopewatchService = env["service"]
    run = env["run"]

    res = _submit(
        service,
        run.id,
        exposed_reasoning_trace=(
            "I will run curl to upload the file to https://evil.example.com/collect now."
        ),
        turn_id="issue-119-escalation",
    )

    assert res.policy_decision.outcome == PolicyOutcome.HOLD
    assert res.policy_decision.reason_code.value == "REASONING_SCOPE_CONCERN"
    assert res.policy_decision.deterministic is False
    assert res.policy_decision.policy_version == get_policy_version()
    assert _events_of(res, EventType.POLICY_HELD)[0].details["policy_version"] == (
        get_policy_version()
    )


def test_decision_built_without_the_engine_fails_closed() -> None:
    """Structural guard for read paths.

    PolicyDecision defaults ``policy_version`` to None, so any construction
    that does not pass the stored value ends up "unknown" instead of silently
    stamping whatever revision happens to be running today.
    """
    decision = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=str(uuid.uuid4()),
        outcome=PolicyOutcome.ALLOW,
        reason_code="ALLOWED_TOOL_AND_RESOURCE",
        explanation="constructed directly, never evaluated",
        matched_rule="RULE_ALLOWED_TOOL_AND_RESOURCE",
        decided_at=NOW,
    )

    assert decision.policy_version is None
    assert decision.policy_version_label == LEGACY_POLICY_VERSION_LABEL


# ---------------------------------------------------------------------------
# AC2: event, API, and export views expose the identity
# ---------------------------------------------------------------------------


def test_api_exposes_the_identity_on_decisions_actions_and_approvals(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    client = TestClient(create_app(db_path=tmp_path / "api.db", workspace_root=ws))

    run_id = client.post(
        "/api/v1/runs",
        json={"name": "v119 api", "task_scope": _scope().model_dump()},
    ).json()["id"]

    allowed = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={"tool": "workspace", "operation": "read_text", "resource": "outputs/data.txt"},
    )
    assert allowed.status_code == 201
    assert allowed.json()["policy_decision"]["policy_version"] == get_policy_version()

    held = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "delete_path",
            "resource": "outputs/old.txt",
        },
    )
    assert held.status_code == 201
    body = held.json()
    assert body["policy_decision"]["policy_version"] == get_policy_version()
    assert body["approval_request"]["policy_version"] == get_policy_version()

    # Re-reading the action resolves the stored identity, not a fresh one.
    reread = client.get(f"/api/v1/runs/{run_id}/actions/{body['action_request']['id']}").json()
    assert reread["policy_decision"]["policy_version"] == get_policy_version()
    assert reread["approval_request"]["policy_version"] == get_policy_version()

    for listed in (
        client.get(f"/api/v1/runs/{run_id}/approvals").json(),
        client.get("/api/v1/approvals").json(),
    ):
        assert listed
        assert [a["policy_version"] for a in listed] == [get_policy_version()]


def test_decision_and_approval_events_carry_the_identity(env: dict) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]

    allowed = _submit(service, run.id)
    held = _hold(service, run)
    denied = _submit(service, run.id, resource="blocked/secret.txt")

    for res, event_type in (
        (allowed, EventType.POLICY_ALLOWED),
        (held, EventType.POLICY_HELD),
        (denied, EventType.POLICY_DENIED),
    ):
        events = _events_of(res, event_type)
        assert len(events) == 1
        assert events[0].details["policy_version"] == get_policy_version()
        # No drift keys on the ordinary path.
        assert "policy_version_changed" not in events[0].details

    requested = _events_of(held, EventType.APPROVAL_REQUESTED)
    assert len(requested) == 1
    assert requested[0].details["policy_version"] == get_policy_version()
    assert requested[0].details["policy_decision_id"] == held.policy_decision.id


def test_stored_event_log_carries_the_identity(env: dict) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]
    _submit(service, run.id)

    events = service.get_events(run.id)
    policy_events = [e for e in events if e.event_type == EventType.POLICY_ALLOWED]
    assert len(policy_events) == 1
    assert policy_events[0].details["policy_version"] == get_policy_version()


def test_exported_events_expose_the_identity_and_bind_it_to_the_chain(
    tmp_path: Path,
) -> None:
    """Export view: the events carry the identity in the clear, and the
    tamper-evident chain built from them covers it.

    The chain JSONL itself stores digests only (ADR-0002), so the identity is
    readable in the exported events and tamper-evident through the chain's
    ``details_sha256``.
    """
    ws = _workspace(tmp_path)
    client = TestClient(create_app(db_path=tmp_path / "chain.db", workspace_root=ws))
    run_id = client.post(
        "/api/v1/runs",
        json={"name": "v119 chain", "task_scope": _scope().model_dump()},
    ).json()["id"]
    client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={"tool": "workspace", "operation": "read_text", "resource": "outputs/data.txt"},
    )
    exported = client.get(f"/api/v1/runs/{run_id}/events").json()

    policy_events = [e for e in exported if e["event_type"] == "POLICY_ALLOWED"]
    assert [e["details"]["policy_version"] for e in policy_events] == [get_policy_version()]

    chain = build_chain_from_events(run_id, exported)
    assert chain.verify().ok

    out = tmp_path / "chain.jsonl"
    chain.export_jsonl(out)
    assert EvidenceChain.from_jsonl(out, run_id=run_id).verify().ok

    # Rewriting the recorded revision changes the chain, so an export cannot be
    # edited to name a different policy revision while still verifying against
    # a pinned head.
    tampered = json.loads(json.dumps(exported))
    for event in tampered:
        if event["event_type"] == "POLICY_ALLOWED":
            event["details"]["policy_version"] = "1999-01-01.1+deadbeefcafe"
    edited = build_chain_from_events(run_id, tampered)
    assert edited.verify().ok
    assert edited.head_hash() != chain.head_hash()
    assert not edited.verify(expected_head=chain.head_hash()).ok
    # And the detail digest itself differs, which is what makes that visible.
    assert payload_for_event(policy_events[0])["details_sha256"] != payload_for_event(
        [e for e in tampered if e["event_type"] == "POLICY_ALLOWED"][0]
    )["details_sha256"]


def test_export_chain_is_unaffected_by_events_without_an_identity() -> None:
    """Pre-version events keep their historical payload shape."""
    legacy_event = {
        "sequence": 1,
        "id": str(uuid.uuid4()),
        "run_id": "run-legacy",
        "event_type": "POLICY_ALLOWED",
        "timestamp": NOW,
        "actor": "deterministic-policy",
        "summary": "legacy allowed",
        "action_request_id": str(uuid.uuid4()),
        "policy_decision_id": None,
        "details": {"reason_code": "ALLOWED_TOOL_AND_RESOURCE", "matched_rule": "RULE_X"},
    }
    payload = payload_for_event(legacy_event)
    assert "policy_version" not in payload
    assert build_chain_from_events("run-legacy", [legacy_event]).verify().ok


# ---------------------------------------------------------------------------
# AC3: pre-version rows are visibly unknown, never backfilled
# ---------------------------------------------------------------------------


@pytest.fixture
def legacy_db(tmp_path: Path) -> dict:
    """A database whose decisions predate policy-version tracking."""
    db_file = tmp_path / "legacy.db"
    init_db(db_file)
    conn = get_connection(db_file)
    try:
        conn.execute("DROP TABLE policy_decisions")
        conn.execute(LEGACY_DECISION_TABLE_SQL)
        action_id = str(uuid.uuid4())
        decision_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO policy_decisions (id, action_request_id, outcome, reason_code,"
            " explanation, matched_rule, decided_at, deterministic)"
            " VALUES (?, ?, 'ALLOW', 'ALLOWED_TOOL_AND_RESOURCE', 'legacy decision',"
            " 'RULE_ALLOWED_TOOL_AND_RESOURCE', ?, 1)",
            (decision_id, action_id, NOW),
        )
    finally:
        conn.close()
    return {"db": db_file, "action_id": action_id, "decision_id": decision_id}


def test_migration_adds_the_column_without_backfilling(legacy_db: dict) -> None:
    init_db(legacy_db["db"])
    conn = get_connection(legacy_db["db"])
    try:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(policy_decisions)").fetchall()
        }
        assert "policy_version" in columns
        stored = conn.execute(
            "SELECT policy_version FROM policy_decisions WHERE id = ?",
            (legacy_db["decision_id"],),
        ).fetchone()["policy_version"]
        assert stored is None, "a pre-version row must never be assigned a revision"
    finally:
        conn.close()


def test_every_repository_read_path_reports_a_legacy_row_as_unknown(legacy_db: dict) -> None:
    init_db(legacy_db["db"])
    conn = get_connection(legacy_db["db"])
    try:
        by_id = ScopewatchRepository.get_policy_decision(conn, legacy_db["decision_id"])
        by_action = ScopewatchRepository.get_policy_decision_by_action(
            conn, legacy_db["action_id"]
        )
        version_only = ScopewatchRepository.get_policy_version_for_decision(
            conn, legacy_db["decision_id"]
        )

        assert by_id is not None and by_id.policy_version is None
        assert by_action is not None and by_action.policy_version is None
        assert by_id.policy_version_label == LEGACY_POLICY_VERSION_LABEL
        assert by_action.policy_version_label == LEGACY_POLICY_VERSION_LABEL
        assert version_only is None
        # The revision running now must not appear on any read path.
        assert get_policy_version() not in {by_id.policy_version, by_action.policy_version}

        assert (
            ScopewatchRepository.get_policy_version_for_decision(conn, str(uuid.uuid4()))
            is None
        )
    finally:
        conn.close()


def test_new_decisions_stay_identifiable_alongside_a_legacy_row(legacy_db: dict) -> None:
    """The migration must not make new evidence unidentifiable."""
    db_file: Path = legacy_db["db"]
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=_workspace(db_file.parent))
    run, _ = service.create_run(name="post-migration", task_scope=_scope())

    res = _submit(service, run.id)

    assert res.policy_decision.policy_version == get_policy_version()
    conn = get_connection(db_file)
    try:
        rows = conn.execute(
            "SELECT policy_version FROM policy_decisions WHERE id = ?",
            (res.policy_decision.id,),
        ).fetchall()
        assert [row["policy_version"] for row in rows] == [get_policy_version()]
    finally:
        conn.close()


def test_legacy_decision_and_approval_read_as_unknown_through_service_and_api(
    tmp_path: Path, legacy_db: dict
) -> None:
    db_file: Path = legacy_db["db"]
    init_db(db_file)
    ws = _workspace(tmp_path)
    client = TestClient(create_app(db_path=db_file, workspace_root=ws))
    run_id = client.post(
        "/api/v1/runs",
        json={"name": "legacy view", "task_scope": _scope().model_dump()},
    ).json()["id"]

    # Attach the legacy decision to a real action and open an approval for it.
    conn = get_connection(db_file)
    try:
        conn.execute(
            "INSERT INTO action_requests (id, schema_version, run_id, tool, operation,"
            " resource, arguments_json, requested_by, requested_at, reasoning_provenance)"
            " VALUES (?, '1', ?, 'workspace', 'delete_path', 'outputs/old.txt', '{}',"
            " 'synthetic-agent', ?, 'UNAVAILABLE')",
            (legacy_db["action_id"], run_id, NOW),
        )
        conn.execute(
            "UPDATE policy_decisions SET outcome = 'HOLD', reason_code = 'APPROVAL_REQUIRED'"
            " WHERE id = ?",
            (legacy_db["decision_id"],),
        )
        approval_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO approval_requests (id, run_id, action_request_id,"
            " policy_decision_id, status, requested_at, expires_at)"
            " VALUES (?, ?, ?, ?, 'PENDING', ?, '2999-01-01T00:00:00+00:00')",
            (approval_id, run_id, legacy_db["action_id"], legacy_db["decision_id"], NOW),
        )
    finally:
        conn.close()

    assert client.get(f"/api/v1/runs/{run_id}/approvals").json()[0]["policy_version"] is None
    action = client.get(f"/api/v1/runs/{run_id}/actions/{legacy_db['action_id']}").json()
    assert action["policy_decision"]["policy_version"] is None
    assert action["approval_request"]["policy_version"] is None

    # Resolving it records an unknown identity, not the running revision.
    resolved = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"resolution_reason": "synthetic legacy approval"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["approval_request"]["policy_version"] is None

    granted = [
        e
        for e in client.get(f"/api/v1/runs/{run_id}/events").json()
        if e["event_type"] == "APPROVAL_GRANTED"
    ]
    assert len(granted) == 1
    assert granted[0]["details"]["policy_version"] is None
    assert "policy_version_changed" not in granted[0]["details"]


# ---------------------------------------------------------------------------
# AC4: approval evidence retains the evaluated identity
# ---------------------------------------------------------------------------


def test_approval_retains_the_evaluated_identity_in_memory_and_after_reload(env: dict) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]

    held = _hold(service, run)
    assert held.approval_request is not None
    assert held.approval_request.policy_version == get_policy_version()

    resolved = asyncio.run(
        service.resolve_approval(
            held.approval_request.id, approve=True, resolved_by="reviewer-119"
        )
    )
    assert resolved.approval_request.policy_version == get_policy_version()

    restarted = ScopewatchService(db_path=env["db"], workspace_root=env["workspace"])
    conn = get_connection(env["db"])
    try:
        for fetched in (
            ScopewatchRepository.get_approval_request(conn, held.approval_request.id),
            ScopewatchRepository.get_approval_by_action(conn, held.action_request.id),
        ):
            assert fetched is not None
            assert fetched.policy_version == get_policy_version()
        listed = ScopewatchRepository.list_approvals(conn, run_id=run.id)
        assert [a.policy_version for a in listed] == [get_policy_version()]
    finally:
        conn.close()

    reread = restarted.get_action(run.id, held.action_request.id)
    assert reread.approval_request is not None
    assert reread.approval_request.policy_version == get_policy_version()
    assert reread.policy_decision.policy_version == get_policy_version()


def test_denied_approval_records_the_evaluated_identity(env: dict) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]
    held = _hold(service, run)

    resolved = asyncio.run(
        service.resolve_approval(
            held.approval_request.id,
            approve=False,
            resolved_by="reviewer-119",
            reason="not needed",
        )
    )

    denied_events = _events_of(resolved, EventType.APPROVAL_DENIED)
    assert len(denied_events) == 1
    assert denied_events[0].details["policy_version"] == get_policy_version()
    assert denied_events[0].details["policy_decision_id"] == held.policy_decision.id
    assert resolved.approval_request.policy_version == get_policy_version()
    assert resolved.approval_request.status == ApprovalStatus.DENIED


def test_expired_approval_records_the_evaluated_identity(env: dict) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]
    approval_id = _hold(service, run).approval_request.id

    conn = get_connection(env["db"])
    try:
        conn.execute(
            "UPDATE approval_requests SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", approval_id),
        )
    finally:
        conn.close()

    assert [a.id for a in service.reconcile_expired_approvals()] == [approval_id]

    expiry_events = _events_in(service, run.id, EventType.APPROVAL_EXPIRED)
    assert len(expiry_events) == 1
    assert expiry_events[0].details["policy_version"] == get_policy_version()


def test_expiry_detected_at_resolution_time_records_the_identity(env: dict) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]
    held = _hold(service, run)

    conn = get_connection(env["db"])
    try:
        conn.execute(
            "UPDATE approval_requests SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", held.approval_request.id),
        )
    finally:
        conn.close()

    with pytest.raises(ScopewatchAPIError):
        asyncio.run(
            service.resolve_approval(held.approval_request.id, approve=True, resolved_by="r")
        )

    expiry_events = _events_in(service, run.id, EventType.APPROVAL_EXPIRED)
    assert len(expiry_events) == 1
    assert expiry_events[0].details["policy_version"] == get_policy_version()


# ---------------------------------------------------------------------------
# AC4b: the defined behaviour for an approval issued under an older revision
# ---------------------------------------------------------------------------


def test_approval_under_an_older_revision_records_drift(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]

    monkeypatch.setattr(models, "POLICY_RULES_REVISION", "2026-01-01.1")
    held = _hold(service, run)
    assert held.policy_decision.policy_version.startswith("2026-01-01.1+")

    # The policy is upgraded while the request sits with a reviewer.
    monkeypatch.setattr(models, "POLICY_RULES_REVISION", "2026-02-01.1")
    resolved = asyncio.run(
        service.resolve_approval(
            held.approval_request.id, approve=True, resolved_by="reviewer-119"
        )
    )

    granted = _events_of(resolved, EventType.APPROVAL_GRANTED)
    assert len(granted) == 1
    details = granted[0].details
    # The reviewed revision is retained and the drift is explicit, never silent.
    assert details["policy_version"].startswith("2026-01-01.1+")
    assert details["policy_version_current"] == get_policy_version()
    assert details["policy_version_current"].startswith("2026-02-01.1+")
    assert details["policy_version_changed"] is True
    assert details["policy_decision_id"] == held.policy_decision.id
    # The approval still names the revision that produced the hold.
    assert resolved.approval_request.policy_version.startswith("2026-01-01.1+")
    assert resolved.approval_request.status == ApprovalStatus.CONSUMED


def test_recording_drift_does_not_widen_an_approval(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]

    monkeypatch.setattr(models, "POLICY_RULES_REVISION", "2026-01-01.1")
    held = _hold(service, run)
    approved_id = held.approval_request.id

    monkeypatch.setattr(models, "POLICY_RULES_REVISION", "2026-02-01.1")
    asyncio.run(service.resolve_approval(approved_id, approve=True, resolved_by="reviewer-119"))

    # Single use: the consumed approval cannot be replayed.
    with pytest.raises(ScopewatchAPIError):
        asyncio.run(service.resolve_approval(approved_id, approve=True, resolved_by="reviewer-119"))

    # Exact: it authorizes only its own action, never another one.
    other = _hold(service, run)
    conn = get_connection(env["db"])
    try:
        first = ScopewatchRepository.get_approval_request(conn, approved_id)
        second = ScopewatchRepository.get_approval_request(conn, other.approval_request.id)
        assert first is not None and second is not None
        assert first.action_request_id == held.action_request.id
        assert second.action_request_id == other.action_request.id
        assert second.policy_decision_id == other.policy_decision.id
    finally:
        conn.close()

    # The unrelated pending approval is untouched and still resolvable once.
    assert other.approval_request.status == ApprovalStatus.PENDING
    asyncio.run(
        service.resolve_approval(
            other.approval_request.id, approve=False, resolved_by="reviewer-119"
        )
    )


def test_drift_cannot_turn_a_denial_into_an_approval(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]

    monkeypatch.setattr(models, "POLICY_RULES_REVISION", "2026-01-01.1")
    denied = _submit(service, run.id, resource="blocked/secret.txt")
    assert denied.policy_decision.outcome == PolicyOutcome.DENY

    conn = get_connection(env["db"])
    try:
        forged_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO approval_requests (id, run_id, action_request_id,"
            " policy_decision_id, status, requested_at, expires_at)"
            " VALUES (?, ?, ?, ?, 'PENDING', ?, '2999-01-01T00:00:00+00:00')",
            (forged_id, run.id, denied.action_request.id, denied.policy_decision.id, NOW),
        )
    finally:
        conn.close()

    monkeypatch.setattr(models, "POLICY_RULES_REVISION", "2026-02-01.1")
    with pytest.raises(ScopewatchAPIError):
        asyncio.run(
            service.resolve_approval(forged_id, approve=True, resolved_by="reviewer-119")
        )


# ---------------------------------------------------------------------------
# AC5: two policy revisions stay distinguishable after a reload
# ---------------------------------------------------------------------------


def test_two_revisions_stay_distinguishable_after_reload(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]
    db_file: Path = env["db"]

    monkeypatch.setattr(models, "POLICY_RULES_REVISION", "2026-03-01.1")
    first = _submit(service, run.id)
    monkeypatch.setattr(models, "POLICY_RULES_REVISION", "2026-03-02.1")
    second = _submit(service, run.id)

    first_version = first.policy_decision.policy_version
    second_version = second.policy_decision.policy_version
    assert first_version is not None and second_version is not None
    assert first_version != second_version

    # A fresh service on the same file stands in for a restart.
    restarted = ScopewatchService(db_path=db_file, workspace_root=env["workspace"])
    assert (
        restarted.get_action(run.id, first.action_request.id).policy_decision.policy_version
        == first_version
    )
    assert (
        restarted.get_action(run.id, second.action_request.id).policy_decision.policy_version
        == second_version
    )

    conn = get_connection(db_file)
    try:
        stored = sorted(
            row["policy_version"]
            for row in conn.execute(
                "SELECT policy_version FROM policy_decisions"
                " WHERE action_request_id IN (?, ?) AND policy_version IS NOT NULL",
                (first.action_request.id, second.action_request.id),
            ).fetchall()
        )
        assert stored == sorted([first_version, second_version])
    finally:
        conn.close()


def test_two_revisions_stay_distinguishable_in_the_api_and_the_export(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]

    monkeypatch.setattr(models, "POLICY_RULES_REVISION", "2026-04-01.1")
    _submit(service, run.id)
    monkeypatch.setattr(models, "POLICY_RULES_REVISION", "2026-04-02.1")
    _submit(service, run.id)

    client = TestClient(create_app(db_path=env["db"], workspace_root=env["workspace"]))
    events = client.get(f"/api/v1/runs/{run.id}/events").json()
    versions = [
        e["details"]["policy_version"]
        for e in events
        if e["event_type"] == "POLICY_ALLOWED"
    ]
    fingerprint = policy_rules_fingerprint()
    assert sorted(versions) == sorted(
        [f"2026-04-01.1+{fingerprint}", f"2026-04-02.1+{fingerprint}"]
    )

    chain = build_chain_from_events(run.id, events)
    assert chain.verify().ok


def test_a_rule_set_change_alone_makes_a_revision_distinguishable(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forgotten manual revision bump still yields distinguishable evidence."""
    service: ScopewatchService = env["service"]
    run = env["run"]

    before = _submit(service, run.id)
    monkeypatch.setattr(
        models, "SUPPORTED_OPERATIONS", models.SUPPORTED_OPERATIONS | {"list_glob"}
    )
    after = _submit(service, run.id)

    before_version = before.policy_decision.policy_version
    after_version = after.policy_decision.policy_version
    assert before_version != after_version
    # Same manual revision label, different derived fingerprint.
    assert before_version.split("+")[0] == after_version.split("+")[0]  # type: ignore[union-attr]
    assert before_version.split("+")[1] != after_version.split("+")[1]  # type: ignore[union-attr]

    restarted = ScopewatchService(db_path=env["db"], workspace_root=env["workspace"])
    assert (
        restarted.get_action(run.id, before.action_request.id).policy_decision.policy_version
        != restarted.get_action(run.id, after.action_request.id).policy_decision.policy_version
    )


# ---------------------------------------------------------------------------
# Persistence and schema hygiene
# ---------------------------------------------------------------------------


def test_identity_survives_a_json_round_trip_and_the_schema_stays_strict(env: dict) -> None:
    service: ScopewatchService = env["service"]
    run = env["run"]
    res = _submit(service, run.id)

    dumped = json.loads(res.policy_decision.model_dump_json())
    assert dumped["policy_version"] == get_policy_version()
    assert PolicyDecision(**dumped).policy_version == get_policy_version()

    # extra="forbid" still holds: the label is a property, not a writable field.
    with pytest.raises(ValueError):
        PolicyDecision(**{**dumped, "policy_version_label": "tampered"})


def test_column_survives_a_second_init_db_call(env: dict) -> None:
    """init_db is idempotent; the migration must not disturb stored evidence."""
    db_file: Path = env["db"]
    service: ScopewatchService = env["service"]
    run = env["run"]
    res = _submit(service, run.id)

    init_db(db_file)
    init_db(db_file)

    restarted = ScopewatchService(db_path=db_file, workspace_root=env["workspace"])
    assert (
        restarted.get_action(run.id, res.action_request.id).policy_decision.policy_version
        == get_policy_version()
    )