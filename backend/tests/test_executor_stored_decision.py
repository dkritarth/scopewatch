"""Executor boundary: no stored decision, no execution (defence in depth).

With a store handle, every gateway backend (local, Docker, remote) must
verify the stored decision for the exact action itself: refuse when none is
stored, when it is DENY, when it is HOLD without an APPROVED approval bound
to the action, when the presented decision disagrees with the stored one,
and when an execution receipt already exists. Refusal happens before any
effect: no file read, no ``docker`` subprocess, no HTTP call.

All content is synthetic; subprocess and HTTP are mocked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid

import httpx
import pytest

from scopewatch.db import get_connection, init_db
from scopewatch.executor import ExecutionSecurityError, execute_action
from scopewatch.executor_docker import DockerExecutor
from scopewatch.executor_remote import (
    EXECUTOR_RUNNER_SIGNING_KEY_ENV_VAR,
    EXECUTOR_RUNNER_TOKEN_ENV_VAR,
    EXECUTOR_RUNNER_URL_ENV_VAR,
    RemoteExecutor,
)
from scopewatch.models import (
    ApprovalStatus,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
    RunStatus,
)
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import (
    ActionRequest,
    ApprovalRequest,
    ExecutionReceipt,
    PolicyDecision,
    Run,
    TaskScope,
)

RUN_ID = "run-stored-decision"
BACKENDS = ["local", "docker", "remote"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _action() -> ActionRequest:
    return ActionRequest(
        id="action-stored-decision",
        run_id=RUN_ID,
        tool="workspace",
        operation="read_text",
        resource="docs/file_a.txt",
        arguments={},
        requested_at=_now(),
    )


def _decision(action_id: str, outcome: PolicyOutcome) -> PolicyDecision:
    reason = {
        PolicyOutcome.ALLOW: ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        PolicyOutcome.HOLD: ReasonCode.APPROVAL_REQUIRED,
        PolicyOutcome.DENY: ReasonCode.BLOCKED_PATH,
    }[outcome]
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=outcome,
        reason_code=reason,
        explanation="probe",
        matched_rule="RULE_PROBE",
        decided_at=_now(),
        deterministic=True,
    )


def _approval(action: ActionRequest, decision: PolicyDecision, status: ApprovalStatus) -> ApprovalRequest:
    return ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id=action.run_id,
        action_request_id=action.id,
        policy_decision_id=decision.id,
        status=status,
        requested_at=_now(),
        expires_at=_now(),
        resolved_by=None,
    )


class _Env:
    def __init__(self, tmp_path: Path) -> None:
        self.ws = tmp_path / "ws"
        (self.ws / "docs").mkdir(parents=True)
        (self.ws / "docs" / "file_a.txt").write_text("synthetic")
        self.db = tmp_path / "store.db"
        init_db(self.db)
        self.action = _action()
        conn = get_connection(self.db)
        try:
            ScopewatchRepository.create_run(
                conn,
                Run(
                    id=RUN_ID,
                    name="stored decision",
                    task_scope=TaskScope(
                        task_description="probe",
                        allowed_paths=["."],
                        blocked_paths=[],
                        created_at=_now(),
                    ),
                    status=RunStatus.ACTIVE,
                    created_at=_now(),
                    updated_at=_now(),
                ),
            )
            ScopewatchRepository.create_action_request(conn, self.action)
            conn.commit()
        finally:
            conn.close()

    def store_decision(self, outcome: PolicyOutcome) -> PolicyDecision:
        decision = _decision(self.action.id, outcome)
        conn = get_connection(self.db)
        try:
            ScopewatchRepository.create_policy_decision(conn, decision)
            conn.commit()
        finally:
            conn.close()
        return decision

    def store_approval(self, decision: PolicyDecision, status: ApprovalStatus) -> ApprovalRequest:
        approval = _approval(self.action, decision, status)
        conn = get_connection(self.db)
        try:
            ScopewatchRepository.create_approval_request(conn, approval)
            conn.commit()
        finally:
            conn.close()
        return approval

    def store_receipt(self) -> None:
        conn = get_connection(self.db)
        try:
            ScopewatchRepository.create_execution_receipt(
                conn,
                ExecutionReceipt(
                    id=str(uuid.uuid4()),
                    action_request_id=self.action.id,
                    status=ExecutionStatus.EXECUTED,
                    started_at=_now(),
                    completed_at=_now(),
                    executor="probe",
                    sanitized_result={},
                    resource=self.action.resource,
                    operation=self.action.operation,
                ),
            )
            conn.commit()
        finally:
            conn.close()


class _Calls:
    """Counts effects: docker subprocesses and HTTP requests."""

    def __init__(self) -> None:
        self.docker = 0
        self.http = 0


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    import scopewatch.executor_docker as docker_mod

    c = _Calls()
    monkeypatch.setenv(EXECUTOR_RUNNER_URL_ENV_VAR, "http://runner.internal:8091")
    monkeypatch.setenv(EXECUTOR_RUNNER_TOKEN_ENV_VAR, "synthetic-token")
    monkeypatch.setenv(EXECUTOR_RUNNER_SIGNING_KEY_ENV_VAR, "synthetic-signing-key")

    def no_docker(*args, **kwargs):
        c.docker += 1
        raise AssertionError("docker subprocess must not run on a refusal")

    monkeypatch.setattr(docker_mod.subprocess, "run", no_docker)
    return c


def _run(backend: str, env: _Env, calls: _Calls, decision, approval=None):
    """Invoke one backend with a store handle; returns the receipt."""
    if backend == "local":
        return execute_action(
            env.action, env.ws, policy_decision=decision,
            approval_request=approval, db_path=env.db,
        )
    if backend == "docker":
        return DockerExecutor().execute(
            env.action, env.ws, policy_decision=decision,
            approval_request=approval, db_path=env.db,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.http += 1
        return httpx.Response(200, json={"status": "EXECUTED", "result": {}, "error_code": None})

    return RemoteExecutor(transport=httpx.MockTransport(handler)).execute(
        env.action, env.ws, policy_decision=decision,
        approval_request=approval, db_path=env.db,
    )


def _assert_refused(backend: str, env: _Env, calls: _Calls, decision, code: str, approval=None) -> None:
    with pytest.raises(ExecutionSecurityError) as exc:
        _run(backend, env, calls, decision, approval)
    assert exc.value.code == code
    assert calls.docker == 0
    assert calls.http == 0


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_stored_decision_refused(backend: str, env: _Env, calls: _Calls) -> None:
    presented = _decision(env.action.id, PolicyOutcome.ALLOW)  # never stored
    _assert_refused(backend, env, calls, presented, "NO_STORED_DECISION")


@pytest.mark.parametrize("backend", BACKENDS)
def test_stored_deny_refused_even_if_caller_presents_allow(
    backend: str, env: _Env, calls: _Calls
) -> None:
    env.store_decision(PolicyOutcome.DENY)
    presented = _decision(env.action.id, PolicyOutcome.ALLOW)
    _assert_refused(backend, env, calls, presented, "STORED_DECISION_DENY")


@pytest.mark.parametrize("backend", BACKENDS)
def test_stored_hold_without_approved_approval_refused(
    backend: str, env: _Env, calls: _Calls
) -> None:
    stored = env.store_decision(PolicyOutcome.HOLD)
    # No stored approval at all, caller fabricates an APPROVED one.
    forged = _approval(env.action, stored, ApprovalStatus.APPROVED)
    _assert_refused(backend, env, calls, stored, "APPROVAL_MISMATCH", approval=forged)


@pytest.mark.parametrize("backend", BACKENDS)
def test_stored_hold_with_pending_approval_refused(
    backend: str, env: _Env, calls: _Calls
) -> None:
    stored = env.store_decision(PolicyOutcome.HOLD)
    approval = env.store_approval(stored, ApprovalStatus.PENDING)
    presented = approval.model_copy(update={"status": ApprovalStatus.APPROVED})
    _assert_refused(backend, env, calls, stored, "APPROVAL_NOT_APPROVED", approval=presented)


@pytest.mark.parametrize("backend", BACKENDS)
def test_stored_hold_presented_as_allow_refused(
    backend: str, env: _Env, calls: _Calls
) -> None:
    env.store_decision(PolicyOutcome.HOLD)
    presented = _decision(env.action.id, PolicyOutcome.ALLOW)
    _assert_refused(backend, env, calls, presented, "DECISION_MISMATCH")


@pytest.mark.parametrize("backend", BACKENDS)
def test_replay_with_existing_receipt_refused_for_allow(
    backend: str, env: _Env, calls: _Calls
) -> None:
    stored = env.store_decision(PolicyOutcome.ALLOW)
    env.store_receipt()
    _assert_refused(backend, env, calls, stored, "ALREADY_EXECUTED")


@pytest.mark.parametrize("backend", BACKENDS)
def test_replay_with_existing_receipt_refused_for_approved_hold(
    backend: str, env: _Env, calls: _Calls
) -> None:
    stored = env.store_decision(PolicyOutcome.HOLD)
    approval = env.store_approval(stored, ApprovalStatus.APPROVED)
    env.store_receipt()
    _assert_refused(backend, env, calls, stored, "ALREADY_EXECUTED", approval=approval)


def test_stored_allow_still_executes_local(env: _Env, calls: _Calls) -> None:
    stored = env.store_decision(PolicyOutcome.ALLOW)
    receipt = _run("local", env, calls, stored)
    assert receipt.status == ExecutionStatus.EXECUTED


def test_stored_allow_still_executes_remote(env: _Env, calls: _Calls) -> None:
    stored = env.store_decision(PolicyOutcome.ALLOW)
    receipt = _run("remote", env, calls, stored)
    assert receipt.status == ExecutionStatus.EXECUTED
    assert calls.http == 1


@pytest.mark.parametrize("backend", ["local", "remote"])
def test_stored_hold_with_approved_approval_still_executes(
    backend: str, env: _Env, calls: _Calls
) -> None:
    stored = env.store_decision(PolicyOutcome.HOLD)
    approval = env.store_approval(stored, ApprovalStatus.APPROVED)
    receipt = _run(backend, env, calls, stored, approval)
    assert receipt.status == ExecutionStatus.EXECUTED


# ---- #390 follow-up: decision identity binding and atomic execution claim ----


@pytest.mark.parametrize("backend", BACKENDS)
def test_forged_allow_with_other_decision_id_refused(
    backend: str, env: _Env, calls: _Calls
) -> None:
    stored = env.store_decision(PolicyOutcome.ALLOW)
    forged = stored.model_copy(update={"id": str(uuid.uuid4())})
    _assert_refused(backend, env, calls, forged, "DECISION_MISMATCH")


@pytest.mark.parametrize("backend", BACKENDS)
def test_forged_allow_for_another_action_refused(
    backend: str, env: _Env, calls: _Calls
) -> None:
    env.store_decision(PolicyOutcome.ALLOW)
    forged = _decision("some-other-action", PolicyOutcome.ALLOW)
    _assert_refused(backend, env, calls, forged, "DECISION_MISMATCH")


@pytest.mark.parametrize("backend", BACKENDS)
def test_stored_id_with_other_action_id_refused(
    backend: str, env: _Env, calls: _Calls
) -> None:
    stored = env.store_decision(PolicyOutcome.ALLOW)
    forged = stored.model_copy(update={"action_request_id": "some-other-action"})
    _assert_refused(backend, env, calls, forged, "DECISION_MISMATCH")


def test_concurrent_direct_calls_execute_exactly_once(env: _Env, calls: _Calls) -> None:
    import threading

    stored = env.store_decision(PolicyOutcome.ALLOW)
    n = 8
    barrier = threading.Barrier(n)
    results: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            out: object = execute_action(
                env.action, env.ws, policy_decision=stored,
                approval_request=None, db_path=env.db,
            )
        except ExecutionSecurityError as exc:
            out = exc
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    executed = [r for r in results if isinstance(r, ExecutionReceipt)]
    refused = [r for r in results if isinstance(r, ExecutionSecurityError)]
    assert len(executed) == 1
    assert len(refused) == n - 1
    assert all(r.code == "ALREADY_EXECUTED" for r in refused)


def test_sequential_second_call_refused_by_claim_without_receipt(env: _Env, calls: _Calls) -> None:
    stored = env.store_decision(PolicyOutcome.ALLOW)
    assert _run("local", env, calls, stored).status == ExecutionStatus.EXECUTED
    _assert_refused("local", env, calls, stored, "ALREADY_EXECUTED")


def test_init_db_adds_claims_table_to_existing_database(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    init_db(db)
    conn = get_connection(db)
    try:
        conn.execute("DROP TABLE execution_claims")
    finally:
        conn.close()
    init_db(db)
    init_db(db)  # idempotent
    conn = get_connection(db)
    try:
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "execution_claims" in names
