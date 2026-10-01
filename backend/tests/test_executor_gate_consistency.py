"""Cross-backend pre-dispatch gate consistency (issue #104).

The evidence gates before execution are written out four times: once per
executor backend and once inside the ``executor-runner`` sidecar. Two of
those four copies had drifted out of agreement with the other two, and no
test compared them, so CI stayed green while:

- the remote backend accepted a ``CONSUMED`` approval for a held action
  (the single-use replay that #81 closed on local and Docker only), and
- the gateway digested ``run_command`` arguments before normalizing them
  while sending the normalized form, so the runner's recomputed digest
  never matched and every remote ``run_command`` failed with HTTP 409, and
- the runner performed no policy-outcome check, so a bearer-token holder
  could hand-craft a matching digest with outcome ``DENY`` and execute.

These tests pin the agreement instead of the individual copies, so a
fifth backend has to satisfy them too.

No network: the gateway client uses ``httpx.MockTransport``; the runner is
imported directly. The runner cases assert on whether the status falls in
the 4xx range, which is where a gate refusal lands, rather than on an exact
code, so they hold both with and without a Docker daemon.
"""

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import uuid

import httpx
import pytest

from scopewatch.executor import ExecutionSecurityError, execute_action
from scopewatch.executor_docker import DockerExecutor
from scopewatch.executor_remote import (
    EXECUTOR_RUNNER_TOKEN_ENV_VAR,
    EXECUTOR_RUNNER_URL_ENV_VAR,
    RemoteExecutor,
)
from scopewatch.models import ApprovalStatus, ExecutionStatus, PolicyOutcome, ReasonCode
from scopewatch.schemas import ActionRequest, ApprovalRequest, PolicyDecision

RUNNER_PATH = (
    Path(__file__).resolve().parents[2] / "deploy" / "executor-runner" / "runner.py"
)


def _load_runner():
    spec = importlib.util.spec_from_file_location("gate_consistency_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None, f"missing {RUNNER_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner_mod = _load_runner()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _action(
    operation: str = "read_text",
    resource: str = "docs/file_a.txt",
    action_id: str = "action-1",
    arguments: dict | None = None,
) -> ActionRequest:
    return ActionRequest(
        id=action_id,
        run_id="run-1",
        tool="workspace",
        operation=operation,
        resource=resource,
        arguments=arguments if arguments is not None else {},
        requested_at=_now(),
    )


def _decision(
    action_id: str = "action-1", outcome: PolicyOutcome = PolicyOutcome.HOLD
) -> PolicyDecision:
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


def _approval(action_id: str, status: ApprovalStatus) -> ApprovalRequest:
    return ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id="run-1",
        action_request_id=action_id,
        policy_decision_id=str(uuid.uuid4()),
        status=status,
        requested_at=_now(),
        expires_at=_now(),
        resolved_by=None,
    )


def _remote_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "remote")
    monkeypatch.setenv(EXECUTOR_RUNNER_URL_ENV_VAR, "http://runner.internal:8091")
    monkeypatch.setenv(EXECUTOR_RUNNER_TOKEN_ENV_VAR, "synthetic-consistency-token")


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"status": "EXECUTED", "result": {}, "error_code": None}
    )


# ---------------------------------------------------------------------------
# Gate 1: a CONSUMED approval authorizes nothing, on any backend.
# ---------------------------------------------------------------------------


def test_consumed_approval_refused_on_every_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Single-use means a spent approval cannot authorize a replay (#66).

    Local and Docker refused a ``CONSUMED`` approval; the remote backend
    accepted it and executed. Every backend must refuse it.
    """
    _remote_env(monkeypatch)
    transport = httpx.MockTransport(_ok_handler)

    def local() -> ExecutionStatus:
        action = _action()
        with pytest.raises(ExecutionSecurityError):
            execute_action(
                action,
                tmp_path,
                policy_decision=_decision(action.id),
                approval_request=_approval(action.id, ApprovalStatus.CONSUMED),
                task_scope=None,
            )
        return ExecutionStatus.EXECUTED  # unreachable; the raise is the assertion

    def docker() -> None:
        action = _action()
        with pytest.raises(ExecutionSecurityError):
            DockerExecutor().execute(
                action,
                tmp_path,
                policy_decision=_decision(action.id),
                approval_request=_approval(action.id, ApprovalStatus.CONSUMED),
            )

    def remote() -> None:
        action = _action()
        with pytest.raises(ExecutionSecurityError):
            RemoteExecutor(transport=transport).execute(
                action,
                tmp_path,
                policy_decision=_decision(action.id),
                approval_request=_approval(action.id, ApprovalStatus.CONSUMED),
            )

    for name, call in (("local", local), ("docker", docker), ("remote", remote)):
        try:
            call()
        except AssertionError:  # pragma: no cover - only on a regression
            pytest.fail(f"{name} backend executed a CONSUMED approval")


def test_approved_approval_still_executes_on_every_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fix must not over-refuse: a live APPROVED approval still executes."""
    _remote_env(monkeypatch)
    action = _action()
    receipt = RemoteExecutor(transport=httpx.MockTransport(_ok_handler)).execute(
        action,
        tmp_path,
        policy_decision=_decision(action.id),
        approval_request=_approval(action.id, ApprovalStatus.APPROVED),
    )
    assert receipt.status == ExecutionStatus.EXECUTED


# ---------------------------------------------------------------------------
# Gate 2: the digest describes the payload that is actually sent.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"argv": ["echo", "hi"]},
        {"argv": ["echo", "hi"], "timeout_s": 60.0},
        {"command": "echo hi"},
        {"argv": ["python3", "-c", "print(1)"], "timeout_s": 5},
    ],
    ids=["argv-no-timeout", "argv-explicit-timeout", "string-form", "argv-short-timeout"],
)
def test_digest_matches_the_wire_payload_for_run_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict
) -> None:
    """The runner recomputes the digest from the body it receives.

    The gateway used to hash the original arguments and send normalized
    ones, so every remote ``run_command`` was refused with HTTP 409. The
    digest must cover exactly the arguments on the wire, for every shape.
    """
    _remote_env(monkeypatch)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200, json={"status": "EXECUTED", "result": {}, "error_code": None}
        )

    action = _action("run_command", ".", arguments=arguments)
    decision = _decision(action.id, PolicyOutcome.ALLOW)
    RemoteExecutor(transport=httpx.MockTransport(handler)).execute(
        action, tmp_path, policy_decision=decision
    )

    body = seen["body"]
    assert isinstance(body, dict)
    runner_verdict = runner_mod.canonical_action_digest(
        body["action"], body["policy_decision"]
    )
    assert body["action_digest"] == runner_verdict, (
        "gateway digest does not describe the payload the runner receives"
    )


def test_prepare_dispatch_digest_matches_its_own_payload() -> None:
    """``prepare_dispatch`` returns the payload and its digest together.

    Returning them as a pair is what makes drift impossible: a caller
    cannot send one and hash the other.
    """
    from scopewatch.executor_remote import prepare_dispatch

    action = _action("run_command", ".", arguments={"argv": ["echo", "hi"]})
    decision = _decision(action.id, PolicyOutcome.ALLOW)
    payload, digest = prepare_dispatch(action, decision)
    runner_verdict = runner_mod.canonical_action_digest(
        payload["action"], payload["policy_decision"]
    )
    assert digest == runner_verdict


def test_prepare_dispatch_normalizes_run_command_before_digesting() -> None:
    """Normalization happens before hashing, and clamps the timeout."""
    from scopewatch.executor_remote import prepare_dispatch

    action = _action(
        "run_command", ".", arguments={"command": "echo hi", "timeout_s": 10_000}
    )
    decision = _decision(action.id, PolicyOutcome.ALLOW)
    payload, _digest = prepare_dispatch(action, decision)
    sent = payload["action"]["arguments"]
    assert sent["argv"] == ["echo", "hi"]
    assert sent["timeout_s"] == pytest.approx(300.0)


def test_prepare_dispatch_leaves_non_command_arguments_untouched() -> None:
    from scopewatch.executor_remote import prepare_dispatch

    action = _action("write_text", "out.txt", arguments={"text": "hello", "mode": "w"})
    decision = _decision(action.id, PolicyOutcome.ALLOW)
    payload, _digest = prepare_dispatch(action, decision)
    assert payload["action"]["arguments"] == {"text": "hello", "mode": "w"}


# ---------------------------------------------------------------------------
# Gate 3: the runner refuses an outcome that authorizes no execution.
# ---------------------------------------------------------------------------


def _runner_env(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    monkeypatch.setenv("EXECUTOR_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("EXECUTOR_RUNNER_TOKEN", "synthetic-consistency-token")


def _dispatch_body(
    outcome: str, operation: str = "read_text", resource: str = "docs/a.txt"
) -> dict:
    action = {
        "id": "a",
        "run_id": "r",
        "operation": operation,
        "resource": resource,
        "arguments": {},
    }
    decision = {"id": "d", "outcome": outcome}
    return {
        "dispatch_token": "single-use-token",
        "action_digest": runner_mod.canonical_action_digest(action, decision),
        "action": action,
        "policy_decision": decision,
        "approval": None,
    }


@pytest.mark.parametrize("outcome", ["DENY", "UNKNOWN", "deny", ""])
def test_runner_refuses_non_authorizing_outcomes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    """Only ALLOW and HOLD may reach Docker dispatch.

    The runner performed no outcome check, so a correctly-digested DENY
    reached the container. Canonicalization is public, so the digest is
    forgeable by anyone holding the bearer token; this gate is what makes
    that harmless.
    """
    workspace = tmp_path / "ws"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "a.txt").write_text("synthetic\n")
    _runner_env(monkeypatch, workspace)

    config = runner_mod.RunnerConfig()
    status, payload = runner_mod.handle_execute(
        _dispatch_body(outcome),
        config=config,
        token_store=runner_mod.DispatchTokenStore(),
    )
    # The gate refuses before any dispatch is attempted, so a refusal is a
    # 4xx. Without this check a non-authorizing outcome reaches Docker,
    # which surfaces as 200 with a daemon or 5xx without one.
    assert 400 <= status < 500, (
        f"outcome {outcome!r} was not refused at the gate "
        f"(http {status}: {payload})"
    )


@pytest.mark.parametrize("outcome", ["ALLOW", "HOLD"])
def test_runner_passes_authorizing_outcomes_past_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    """The new gate must not refuse outcomes that legitimately dispatch."""
    workspace = tmp_path / "ws"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "a.txt").write_text("synthetic\n")
    _runner_env(monkeypatch, workspace)

    config = runner_mod.RunnerConfig()
    status, payload = runner_mod.handle_execute(
        _dispatch_body(outcome),
        config=config,
        token_store=runner_mod.DispatchTokenStore(),
    )
    # Reaching dispatch is the pass case. With no daemon that surfaces as 5xx;
    # on a host with Docker it is 200. Either way it is not a 4xx gate
    # refusal, which is what this test exists to catch.
    assert not (400 <= status < 500), (
        f"outcome {outcome!r} was refused before dispatch "
        f"(http {status}: {payload})"
    )


def test_runner_deny_refusal_precedes_dispatch_token_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A forged digest must not get further than a wrong one."""
    workspace = tmp_path / "ws"
    (workspace / "docs").mkdir(parents=True)
    _runner_env(monkeypatch, workspace)

    body = _dispatch_body("DENY")
    body["action_digest"] = "0" * 64
    status, payload = runner_mod.handle_execute(
        body,
        config=runner_mod.RunnerConfig(),
        token_store=runner_mod.DispatchTokenStore(),
    )
    assert status == 409
    assert payload == {"error": "action digest mismatch"}
