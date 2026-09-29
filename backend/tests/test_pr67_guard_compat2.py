"""Second-round compatibility/edge tests for PR #67 approval guard.

PR #67: ``resolve_approval`` must reject terminal runs with 409 RUN_NOT_ACTIVE.

Round 1 (test_pr67_approval_guard_edge.py, 21 tests) covered the service-level
terminal-run rejections, executor silence, retry, ACTIVE/WAITING controls, the
DENY invariant on active runs, expired-on-terminal precedence, consumed-approval
behaviour, and evidence-before-effect. This file covers COMPATIBILITY angles
round 1 missed — HTTP surfacing, expiry-on-active control, DENY-vs-terminal
precedence, submit_action coherence, lifecycle idempotency, a threaded race
smoke test, API-level getters after rejection, and resolved_by attribution.

No network, no docker. Service tests use the svc_env fixture style from
test_bypass.py; HTTP tests use TestClient with raise_server_exceptions=False.
"""

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scopewatch.app import create_app
from scopewatch.db import get_connection, init_db
from scopewatch.errors import ScopewatchAPIError
from scopewatch.models import (
    ApprovalStatus,
    ExecutionStatus,
    PolicyOutcome,
    RunStatus,
)
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import ApprovalRequest, SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService

NOW = datetime.now(timezone.utc).isoformat()

TASK_SCOPE_DICT = {
    "schema_version": "1",
    "task_description": "Audit approved invoices and produce outputs",
    "allowed_paths": ["invoices/approved", "outputs"],
    "blocked_paths": ["invoices/private"],
    "allowed_tools": ["workspace"],
    "allowed_operations": ["read_text", "write_text", "delete_path"],
    "allowed_network_destinations": [],
    "requires_approval": ["delete_path"],
    "created_at": NOW,
}


def _make_scope() -> TaskScope:
    return TaskScope(
        schema_version="1",
        task_description="Audit approved invoices and produce outputs",
        allowed_paths=["invoices/approved", "outputs"],
        blocked_paths=["invoices/private"],
        allowed_tools=["workspace"],
        allowed_operations=["read_text", "write_text", "delete_path"],
        allowed_network_destinations=[],
        requires_approval=["delete_path"],
        created_at=NOW,
    )


def _make_workspace(ws: Path) -> None:
    (ws / "invoices" / "approved").mkdir(parents=True, exist_ok=True)
    (ws / "invoices" / "private").mkdir(parents=True, exist_ok=True)
    (ws / "outputs").mkdir(parents=True, exist_ok=True)
    (ws / "invoices" / "approved" / "vendor-a.txt").write_text(
        "Vendor A Invoice: $1000", encoding="utf-8"
    )
    (ws / "invoices" / "private" / "salaries.txt").write_text(
        "Executive Salaries: Confidential", encoding="utf-8"
    )
    (ws / "outputs" / "old.txt").write_text("old data", encoding="utf-8")


@pytest.fixture
def svc_env(tmp_path: Path):
    db_file = tmp_path / "pr67c2.db"
    ws = tmp_path / "workspace"
    _make_workspace(ws)
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=ws)
    run, _ = service.create_run(name="PR67C2 Service Run", task_scope=_make_scope())
    return {"service": service, "run": run, "workspace": ws, "db_file": db_file}


@pytest.fixture
def http_env(tmp_path: Path):
    db_file = tmp_path / "pr67c2api.db"
    ws = tmp_path / "workspace"
    _make_workspace(ws)
    init_db(db_file)
    app = create_app(db_path=db_file, workspace_root=ws)
    client = TestClient(app, raise_server_exceptions=False)
    return {"client": client, "workspace": ws, "db_file": db_file}


def _submit_hold(svc_env: dict, resource: str = "outputs/old.txt"):
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace", operation="delete_path", resource=resource
            ),
        )
    )
    assert res.policy_decision.outcome == PolicyOutcome.HOLD
    assert res.approval_request is not None
    assert res.approval_request.status == ApprovalStatus.PENDING
    assert res.execution_receipt is None
    return res


def _http_create_run(client: TestClient) -> str:
    resp = client.post(
        "/api/v1/runs", json={"name": "PR67C2 HTTP Run", "task_scope": TASK_SCOPE_DICT}
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _http_submit_hold(client: TestClient, run_id: str, resource: str = "outputs/old.txt"):
    resp = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={"tool": "workspace", "operation": "delete_path", "resource": resource},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["policy_decision"]["outcome"] == PolicyOutcome.HOLD.value
    assert data["approval_request"] is not None
    return data


# --- HTTP API level: terminal-run resolve surfacing ----------------------


def test_pr67_c2_http_approve_after_complete_is_409_run_not_active(http_env: dict) -> None:
    """HTTP approve after run completion surfaces 409 + RUN_NOT_ACTIVE code."""
    client: TestClient = http_env["client"]
    run_id = _http_create_run(client)
    data = _http_submit_hold(client, run_id)
    approval_id = data["approval_request"]["id"]

    comp = client.post(f"/api/v1/runs/{run_id}/complete")
    assert comp.status_code == 200

    resp = client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        json={"resolution_reason": "late approve"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "RUN_NOT_ACTIVE"
    assert "request_id" in body["error"]


def test_pr67_c2_http_deny_after_failed_is_409_run_not_active(http_env: dict) -> None:
    """HTTP deny (approve=False) after run failure surfaces 409 RUN_NOT_ACTIVE."""
    client: TestClient = http_env["client"]
    run_id = _http_create_run(client)
    data = _http_submit_hold(client, run_id)
    approval_id = data["approval_request"]["id"]

    failed = client.post(f"/api/v1/runs/{run_id}/fail", params={"reason": "boom"})
    assert failed.status_code == 200

    resp = client.post(
        f"/api/v1/approvals/{approval_id}/deny",
        json={"resolution_reason": "late deny"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "RUN_NOT_ACTIVE"


def test_pr67_c2_http_deny_after_complete_is_409_run_not_active(http_env: dict) -> None:
    """HTTP deny (approve=False) after completion also surfaces 409 RUN_NOT_ACTIVE."""
    client: TestClient = http_env["client"]
    run_id = _http_create_run(client)
    data = _http_submit_hold(client, run_id)
    approval_id = data["approval_request"]["id"]

    comp = client.post(f"/api/v1/runs/{run_id}/complete")
    assert comp.status_code == 200

    resp = client.post(
        f"/api/v1/approvals/{approval_id}/deny",
        json={"resolution_reason": "late deny"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "RUN_NOT_ACTIVE"


# --- expiry precedence pin: control on ACTIVE run -------------------------


def test_pr67_c2_expired_on_active_run_still_expires_control(svc_env: dict) -> None:
    """Control: already-expired approval on a LIVE run yields APPROVAL_EXPIRED.

    Contrasts with round 1's expired-on-terminal case (RUN_NOT_ACTIVE wins):
    when the run is live, the expiry path still works and transitions the
    approval to EXPIRED with an APPROVAL_EXPIRED event.
    """
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    conn = get_connection(svc_env["db_file"])
    try:
        conn.execute(
            "UPDATE approval_requests SET expires_at = ? WHERE id = ?",
            (past, res.approval_request.id),
        )
    finally:
        conn.close()
    assert service.get_run(run.id).status == RunStatus.WAITING_FOR_APPROVAL
    events_before = service.get_events(run.id)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(res.approval_request.id, approve=True)
        )
    assert exc.value.code == "APPROVAL_EXPIRED"
    assert exc.value.status_code == 409
    action = service.get_action(run.id, res.action_request.id)
    assert action.approval_request is not None
    assert action.approval_request.status == ApprovalStatus.EXPIRED
    assert action.approval_request.resolved_by == "system"
    assert action.execution_receipt is None
    events_after = service.get_events(run.id)
    assert len(events_after) == len(events_before) + 1
    assert events_after[-1].event_type.value == "APPROVAL_EXPIRED"


# --- DENY-decision + terminal run precedence pin ---------------------------


def test_pr67_c2_deny_decision_on_terminal_run_deny_check_wins(svc_env: dict) -> None:
    """Precedence pin: DENY decision + COMPLETED run, approve=True.

    The DENY-invariant check runs before the terminal-run guard in
    resolve_approval, so DENIED_ACTION_CANNOT_BE_APPROVED (400) wins over
    RUN_NOT_ACTIVE (409). The approve=False path on the same rogue approval
    skips the DENY check and hits the terminal guard (409 RUN_NOT_ACTIVE).
    """
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    denied = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace",
                operation="read_text",
                resource="invoices/private/salaries.txt",
            ),
        )
    )
    assert denied.policy_decision.outcome == PolicyOutcome.DENY
    conn = get_connection(svc_env["db_file"])
    try:
        ScopewatchRepository.create_approval_request(
            conn,
            ApprovalRequest(
                id="rogue-approval-pr67c2",
                run_id=run.id,
                action_request_id=denied.action_request.id,
                policy_decision_id=denied.policy_decision.id,
                status=ApprovalStatus.PENDING,
                requested_at=datetime.now(timezone.utc).isoformat(),
                expires_at=(
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    service.complete_run(run.id)
    assert service.get_run(run.id).status == RunStatus.COMPLETED

    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(service.resolve_approval("rogue-approval-pr67c2", approve=True))
    assert exc.value.code == "DENIED_ACTION_CANNOT_BE_APPROVED"
    assert exc.value.status_code == 400

    # Same rogue approval, deny path: DENY check is approve-only, so the
    # terminal guard fires instead.
    with pytest.raises(ScopewatchAPIError) as exc2:
        asyncio.run(service.resolve_approval("rogue-approval-pr67c2", approve=False))
    assert exc2.value.code == "RUN_NOT_ACTIVE"
    assert exc2.value.status_code == 409


# --- submit_action coherence ----------------------------------------------


def test_pr67_c2_submit_on_waiting_for_approval_accepted(svc_env: dict) -> None:
    """Submit coherence: WAITING_FOR_APPROVAL runs still accept new actions."""
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    _submit_hold(svc_env, resource="outputs/old.txt")
    assert service.get_run(run.id).status == RunStatus.WAITING_FOR_APPROVAL
    (svc_env["workspace"] / "outputs" / "other.txt").write_text(
        "other", encoding="utf-8"
    )
    second = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace",
                operation="delete_path",
                resource="outputs/other.txt",
            ),
        )
    )
    assert second.policy_decision.outcome == PolicyOutcome.HOLD
    assert second.approval_request is not None
    assert second.approval_request.status == ApprovalStatus.PENDING


def test_pr67_c2_submit_on_completed_rejected_400(svc_env: dict) -> None:
    """Submit coherence: COMPLETED run rejects submit with RUN_NOT_ACTIVE (400)."""
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    _submit_hold(svc_env)
    service.complete_run(run.id)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.submit_action(
                run.id,
                SubmitActionRequest(
                    tool="workspace",
                    operation="delete_path",
                    resource="outputs/old.txt",
                ),
            )
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"
    assert exc.value.status_code == 400


def test_pr67_c2_submit_on_failed_rejected_400(svc_env: dict) -> None:
    """Submit coherence: FAILED run rejects submit with RUN_NOT_ACTIVE (400)."""
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    _submit_hold(svc_env)
    service.fail_run(run.id)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.submit_action(
                run.id,
                SubmitActionRequest(
                    tool="workspace",
                    operation="delete_path",
                    resource="outputs/old.txt",
                ),
            )
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"
    assert exc.value.status_code == 400


# --- complete_run / fail_run lifecycle coherence ---------------------------


def test_pr67_c2_complete_completed_run_no_raise_appends_event(svc_env: dict) -> None:
    """Lifecycle: completing an already-COMPLETED run does not raise.

    Documents actual behaviour: status stays COMPLETED but a second
    RUN_COMPLETED event is appended (no idempotency guard in complete_run).
    """
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    service.complete_run(run.id)
    assert service.get_run(run.id).status == RunStatus.COMPLETED
    events_before = service.get_events(run.id)
    rerun, _ = service.complete_run(run.id)
    assert rerun.status == RunStatus.COMPLETED
    assert service.get_run(run.id).status == RunStatus.COMPLETED
    events_after = service.get_events(run.id)
    assert len(events_after) == len(events_before) + 1
    assert events_after[-1].event_type.value == "RUN_COMPLETED"


def test_pr67_c2_fail_completed_run_flips_to_failed(svc_env: dict) -> None:
    """Lifecycle: failing a COMPLETED run flips it to FAILED (no guard).

    Documents actual behaviour: fail_run unconditionally overwrites the
    status, so a COMPLETED run becomes FAILED with a SYSTEM_ERROR event.
    """
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    service.complete_run(run.id)
    assert service.get_run(run.id).status == RunStatus.COMPLETED
    failed, _ = service.fail_run(run.id, reason="late failure")
    assert failed.status == RunStatus.FAILED
    assert service.get_run(run.id).status == RunStatus.FAILED


# --- threaded race smoke test ----------------------------------------------


def test_pr67_c2_race_complete_vs_resolve_single_winner(svc_env: dict) -> None:
    """Race smoke: complete_run vs resolve_approval from 2 threads.

    Accepts either winner but asserts exactly one wins and data stays
    consistent: a receipt exists iff the approval is CONSUMED, and a
    rejected resolve leaves the approval PENDING on a COMPLETED run.
    Uses threads with a start barrier (WAL + 10s busy timeout serializes
    the writers); documents which outcome occurred via the assertion path.
    """
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    approval_id = res.approval_request.id
    action_id = res.action_request.id

    barrier = threading.Barrier(2)
    outcomes: dict = {}

    def do_complete() -> None:
        barrier.wait(timeout=10)
        try:
            service.complete_run(run.id)
            outcomes["complete"] = "ok"
        except Exception as exc:  # noqa: BLE001 - recorded, asserted below
            outcomes["complete"] = f"error:{type(exc).__name__}:{exc}"

    def do_resolve() -> None:
        barrier.wait(timeout=10)
        try:
            asyncio.run(
                service.resolve_approval(
                    approval_id, approve=True, resolved_by="racer-1"
                )
            )
            outcomes["resolve"] = "ok"
        except ScopewatchAPIError as exc:
            outcomes["resolve"] = f"api:{exc.code}"
        except Exception as exc:  # noqa: BLE001 - recorded, asserted below
            outcomes["resolve"] = f"error:{type(exc).__name__}:{exc}"

    t1 = threading.Thread(target=do_complete)
    t2 = threading.Thread(target=do_resolve)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert not t1.is_alive() and not t2.is_alive(), "race threads hung"
    assert outcomes.get("complete") == "ok", f"complete_run crashed: {outcomes}"

    action = service.get_action(run.id, action_id)
    final_run = service.get_run(run.id)
    resolve_outcome = outcomes.get("resolve")
    assert resolve_outcome in ("ok", "api:RUN_NOT_ACTIVE"), (
        f"unexpected resolve outcome: {outcomes}"
    )
    if resolve_outcome == "ok":
        # Resolve won (possibly before complete overwrote the status):
        # approval consumed and a receipt exists; run is ACTIVE or COMPLETED.
        assert action.approval_request is not None
        assert action.approval_request.status == ApprovalStatus.CONSUMED
        assert action.execution_receipt is not None
        assert action.execution_receipt.status == ExecutionStatus.EXECUTED
        assert final_run.status in (RunStatus.ACTIVE, RunStatus.COMPLETED)
    else:
        # Complete won: approval still PENDING, no receipt, run COMPLETED.
        assert action.approval_request is not None
        assert action.approval_request.status == ApprovalStatus.PENDING
        assert action.execution_receipt is None
        assert final_run.status == RunStatus.COMPLETED
    # Consistency invariant holds in both branches: no receipt without CONSUMED.
    assert (action.execution_receipt is None) == (
        action.approval_request.status != ApprovalStatus.CONSUMED
    )


# --- API-level getters after rejection --------------------------------------


def test_pr67_c2_http_getters_after_terminal_reject(http_env: dict) -> None:
    """Getters via HTTP after a terminal-run rejection: action still shows a
    PENDING approval with no receipt, run stays terminal, events unchanged,
    and the pending-approval listing still contains the approval."""
    client: TestClient = http_env["client"]
    run_id = _http_create_run(client)
    data = _http_submit_hold(client, run_id)
    approval_id = data["approval_request"]["id"]
    action_id = data["action_request"]["id"]

    assert client.post(f"/api/v1/runs/{run_id}/complete").status_code == 200
    events_before = client.get(f"/api/v1/runs/{run_id}/events").json()

    rej = client.post(f"/api/v1/approvals/{approval_id}/approve", json={})
    assert rej.status_code == 409
    assert rej.json()["error"]["code"] == "RUN_NOT_ACTIVE"

    action = client.get(f"/api/v1/runs/{run_id}/actions/{action_id}").json()
    assert action["approval_request"]["status"] == ApprovalStatus.PENDING.value
    assert action["approval_request"]["resolved_at"] is None
    assert action["execution_receipt"] is None

    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == RunStatus.COMPLETED.value

    assert client.get(f"/api/v1/runs/{run_id}/events").json() == events_before

    pending = client.get(
        f"/api/v1/runs/{run_id}/approvals", params={"status": "PENDING"}
    ).json()
    assert [a["id"] for a in pending] == [approval_id]


# --- resolved_by attribution -------------------------------------------------


def test_pr67_c2_resolved_by_recorded_on_success(svc_env: dict) -> None:
    """Control: a successful resolve records resolved_by on the approval."""
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    resolved = asyncio.run(
        service.resolve_approval(
            res.approval_request.id, approve=True, resolved_by="reviewer-7"
        )
    )
    assert resolved.approval_request.status == ApprovalStatus.CONSUMED
    assert resolved.approval_request.resolved_by == "reviewer-7"
    action = service.get_action(run.id, res.action_request.id)
    assert action.approval_request is not None
    assert action.approval_request.resolved_by == "reviewer-7"


def test_pr67_c2_resolved_by_none_after_terminal_reject(svc_env: dict) -> None:
    """Rejected resolve leaves resolved_by None (approval untouched)."""
    service: ScopewatchService = svc_env["service"]
    run = svc_env["run"]
    res = _submit_hold(svc_env)
    assert res.approval_request is not None
    service.complete_run(run.id)
    with pytest.raises(ScopewatchAPIError) as exc:
        asyncio.run(
            service.resolve_approval(
                res.approval_request.id, approve=True, resolved_by="reviewer-7"
            )
        )
    assert exc.value.code == "RUN_NOT_ACTIVE"
    action = service.get_action(run.id, res.action_request.id)
    assert action.approval_request is not None
    assert action.approval_request.status == ApprovalStatus.PENDING
    assert action.approval_request.resolved_by is None
    assert action.approval_request.resolved_at is None


def test_pr67_c2_no_cancel_or_expire_run_helpers() -> None:
    """Pin: the service exposes no cancel_run/expire_run lifecycle helpers."""
    assert not hasattr(ScopewatchService, "cancel_run")
    assert not hasattr(ScopewatchService, "expire_run")
    assert not hasattr(ScopewatchService, "cancel_approval")
    assert set(RunStatus.__members__) == {
        "ACTIVE",
        "WAITING_FOR_APPROVAL",
        "COMPLETED",
        "FAILED",
    }
