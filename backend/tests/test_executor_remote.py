"""Remote-executor mode (issue #78) + socket-isolation grep test.

Gateway client (``backend/scopewatch/executor_remote.py``) tests use
``httpx.MockTransport`` (no network, no daemon). Runner validation helpers
are exercised by importing ``deploy/executor-runner/runner.py`` directly.
All fixtures are synthetic; secrets in tests are throwaway values.
"""

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import time
import uuid

import httpx
import pytest

from scopewatch.executor import execute_action
from scopewatch.executor_remote import (
    EXECUTOR_RUNNER_SIGNING_KEY_ENV_VAR,
    EXECUTOR_RUNNER_TOKEN_ENV_VAR,
    EXECUTOR_RUNNER_URL_ENV_VAR,
    RemoteExecutor,
    compute_action_digest,
    sign_dispatch_payload,
)
from scopewatch.models import ApprovalStatus, ExecutionStatus, PolicyOutcome, ReasonCode
from scopewatch.schemas import ActionRequest, PolicyDecision

RUNNER_PATH = (
    Path(__file__).resolve().parents[2] / "deploy" / "executor-runner" / "runner.py"
)


def _load_runner():
    spec = importlib.util.spec_from_file_location("executor_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None, f"missing {RUNNER_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner_mod = _load_runner()


def _allow_decision(action_id: str = "action-1") -> PolicyDecision:
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )


def _action(
    operation: str = "read_text",
    resource: str = "docs/file_a.txt",
    action_id: str = "action-1",
    **kwargs: object,
) -> ActionRequest:
    return ActionRequest(
        id=action_id,
        run_id="run-1",
        tool="workspace",
        operation=operation,
        resource=resource,
        arguments=kwargs.get("arguments", {}),  # type: ignore[arg-type]
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


def _runner_success_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("Authorization", "").startswith("Bearer ")
    return httpx.Response(
        200,
        json={
            "status": "EXECUTED",
            "result": {"operation": "read_text", "resource": "docs/file_a.txt"},
            "error_code": None,
        },
    )


def _remote_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "remote")
    monkeypatch.setenv(EXECUTOR_RUNNER_URL_ENV_VAR, "http://runner.internal:8091")
    monkeypatch.setenv(EXECUTOR_RUNNER_TOKEN_ENV_VAR, "synthetic-test-token")
    monkeypatch.setenv(EXECUTOR_RUNNER_SIGNING_KEY_ENV_VAR, "synthetic-independent-signing-key")


# ---------------- Gateway client: mocked-transport dispatch ----------------


def test_remote_success_executes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _remote_env(monkeypatch)
    transport = httpx.MockTransport(_runner_success_handler)
    receipt = RemoteExecutor(transport=transport).execute(
        _action(), tmp_path, policy_decision=_allow_decision()
    )
    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.executor == "remote-executor"


def test_remote_sends_bearer_and_digest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _remote_env(monkeypatch)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"status": "EXECUTED", "result": {}, "error_code": None})

    action = _action()
    decision = _allow_decision()
    transport = httpx.MockTransport(handler)
    RemoteExecutor(transport=transport).execute(action, tmp_path, policy_decision=decision)
    assert seen["auth"] == "Bearer synthetic-test-token"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["dispatch_token"]
    assert body["action_digest"] == compute_action_digest(action, decision)
    assert body["dispatch_signature"] == sign_dispatch_payload(
        body, "synthetic-independent-signing-key"
    )
    assert body["policy_decision"]["action_request_id"] == action.id
    # The secret value never appears anywhere except the header.
    assert "synthetic-test-token" not in json.dumps(body)
    assert "synthetic-independent-signing-key" not in json.dumps(body)


def test_remote_token_reuse_refused_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Runner 409 on a replayed dispatch token maps to FAILED (no retry)."""
    _remote_env(monkeypatch)
    monkeypatch.setattr(
        "scopewatch.executor_remote.secrets.token_urlsafe", lambda n=32: "fixed-token"
    )
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_tokens.append(body["dispatch_token"])
        if seen_tokens.count("fixed-token") > 1:
            return httpx.Response(409, json={"error": "dispatch token already used"})
        return httpx.Response(200, json={"status": "EXECUTED", "result": {}, "error_code": None})

    transport = httpx.MockTransport(handler)
    first = RemoteExecutor(transport=transport).execute(
        _action(), tmp_path, policy_decision=_allow_decision()
    )
    assert first.status == ExecutionStatus.EXECUTED
    # A second dispatch reusing the same token value is refused fail-closed.
    replay = httpx.MockTransport(lambda req: httpx.Response(409, json={"error": "reused"}))
    second = RemoteExecutor(transport=replay).execute(
        _action(), tmp_path, policy_decision=_allow_decision()
    )
    assert second.status == ExecutionStatus.FAILED
    assert second.error_code == "EXECUTION_FAILED"
    assert second.executor == "remote-executor"


def test_remote_digest_mismatch_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _remote_env(monkeypatch)
    transport = httpx.MockTransport(
        lambda req: httpx.Response(409, json={"error": "action digest mismatch"})
    )
    receipt = RemoteExecutor(transport=transport).execute(
        _action(), tmp_path, policy_decision=_allow_decision()
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "EXECUTION_FAILED"
    assert receipt.sanitized_result == {"error": "Remote dispatch refused."}


def test_remote_timeout_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _remote_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("synthetic timeout")

    receipt = RemoteExecutor(transport=httpx.MockTransport(handler)).execute(
        _action(), tmp_path, policy_decision=_allow_decision()
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.sanitized_result == {"error": "Remote execution timed out."}


def test_remote_malformed_output_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _remote_env(monkeypatch)
    for bad in (
        httpx.Response(200, content=b"not-json{{"),
        httpx.Response(200, json={"status": "MAYBE", "result": {}}),
        httpx.Response(200, json={"unexpected": "shape"}),
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(401, json={"error": "unauthorized"}),
    ):
        transport = httpx.MockTransport(lambda req, r=bad: r)
        receipt = RemoteExecutor(transport=transport).execute(
            _action(), tmp_path, policy_decision=_allow_decision()
        )
        assert receipt.status == ExecutionStatus.FAILED
        assert receipt.error_code == "EXECUTION_FAILED"
        assert receipt.executor == "remote-executor"


def test_remote_missing_config_fail_closed_no_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "remote")
    monkeypatch.delenv(EXECUTOR_RUNNER_URL_ENV_VAR, raising=False)
    monkeypatch.delenv(EXECUTOR_RUNNER_TOKEN_ENV_VAR, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no network call expected without runner config")

    receipt = RemoteExecutor(transport=httpx.MockTransport(handler)).execute(
        _action(), tmp_path, policy_decision=_allow_decision()
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "EXECUTION_FAILED"


def test_remote_deny_never_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _remote_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("DENY must not dispatch")

    deny = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id="action-1",
        outcome=PolicyOutcome.DENY,
        reason_code=ReasonCode.BLOCKED_PATH,
        explanation="Blocked",
        matched_rule="RULE_BLOCKED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )
    receipt = RemoteExecutor(transport=httpx.MockTransport(handler)).execute(
        _action(), tmp_path, policy_decision=deny
    )
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert receipt.error_code == ReasonCode.BLOCKED_PATH.value


def test_remote_hold_without_approval_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scopewatch.executor import ExecutionSecurityError

    _remote_env(monkeypatch)
    hold = PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id="action-1",
        outcome=PolicyOutcome.HOLD,
        reason_code=ReasonCode.APPROVAL_REQUIRED,
        explanation="Hold",
        matched_rule="RULE_HOLD",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )
    with pytest.raises(ExecutionSecurityError, match="Held action requires valid approved"):
        RemoteExecutor().execute(_action(), tmp_path, policy_decision=hold)


def test_remote_network_request_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scopewatch.executor import ExecutionSecurityError

    _remote_env(monkeypatch)
    with pytest.raises(ExecutionSecurityError, match="Network requests are forbidden"):
        RemoteExecutor().execute(
            _action("network_request", "https://example.com"),
            tmp_path,
            policy_decision=_allow_decision(),
        )


def test_remote_requires_stored_decision(tmp_path: Path) -> None:
    from scopewatch.executor import ExecutionSecurityError

    with pytest.raises(ExecutionSecurityError, match="Direct execution without policy evidence"):
        RemoteExecutor(
            runner_url="http://runner.internal:8091", runner_token="synthetic"
        ).execute(_action(), tmp_path, policy_decision=None)


# ---------------- Runner validation (imported sidecar helpers) ----------------


def test_runner_digest_matches_gateway() -> None:
    action = _action()
    decision = _allow_decision()
    gateway_digest = compute_action_digest(action, decision)
    runner_digest = runner_mod.canonical_action_digest(
        {
            "id": action.id,
            "run_id": action.run_id,
            "operation": action.operation,
            "resource": action.resource,
            "arguments": dict(action.arguments or {}),
        },
        {"id": decision.id, "outcome": decision.outcome.value},
    )
    assert runner_digest == gateway_digest


def test_runner_token_store_one_shot_with_ttl() -> None:
    store = runner_mod.DispatchTokenStore(ttl_s=60.0)
    assert store.consume("tok-1", "digest-a", now=1000.0) == "ok"
    assert store.consume("tok-1", "digest-a", now=1001.0) == "reused"
    assert store.consume("tok-1", "digest-other", now=1001.0) == "reused"
    assert store.consume("", "digest-a", now=1001.0) == "invalid"
    # After expiry the token slot is pruned (a *new* dispatch may mint the
    # same string only if the generator collides; reuse inside TTL refuses).
    assert store.consume("tok-2", "digest-a", now=2000.0) == "ok"


def test_runner_bearer_check() -> None:
    assert runner_mod.check_bearer("Bearer secret-value", "secret-value") is True
    assert runner_mod.check_bearer("bearer secret-value", "secret-value") is True
    assert runner_mod.check_bearer("Bearer wrong", "secret-value") is False
    assert runner_mod.check_bearer(None, "secret-value") is False
    assert runner_mod.check_bearer("Bearer secret-value", "") is False
    assert runner_mod.check_bearer("Token secret-value", "secret-value") is False


def test_runner_resource_validation(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ok, reason = runner_mod.validate_resource_inside_workspace("docs/a.txt", ws)
    assert reason is None and ok is not None
    for bad in ("/etc/passwd", "../escape.txt", "a\x00b", "", "sub/../../x"):
        target, _reason = runner_mod.validate_resource_inside_workspace(bad, ws)
        assert target is None, bad


def test_runner_handle_execute_refuses_reused_token(tmp_path: Path) -> None:
    """End-to-end refusal at the runner layer without a Docker daemon.

    Only the pre-Docker refusal paths run here (token reuse, digest
    mismatch); Docker dispatch itself is covered by gateway mocks.
    """
    config = runner_mod.RunnerConfig()
    config.token = "synthetic"
    config.signing_key = "synthetic-independent-signing-key"
    config.workspace = tmp_path / "ws"
    config.workspace.mkdir()
    store = runner_mod.DispatchTokenStore()
    action = {"id": "a1", "run_id": "r1", "operation": "read_text",
              "resource": "docs/a.txt", "arguments": {}}
    decision = {"id": "d1", "action_request_id": "a1", "outcome": "ALLOW"}
    digest = runner_mod.canonical_action_digest(action, decision)
    body = {"issued_at": time.time(), "dispatch_token": "once-only", "action_digest": digest,
            "action": action, "policy_decision": decision, "approval": None}
    body["dispatch_signature"] = sign_dispatch_payload(body, config.signing_key)
    # First consume reserves the token; the digest check then passes and the
    # runner proceeds to Docker (no daemon here -> 502, which still proves
    # the token/digest gates passed rather than refusing 409/400).
    status, _ = runner_mod.handle_execute(dict(body), config, store)
    assert status != 409
    # Replaying the same dispatch token is refused even though Docker state
    # is unchanged.
    status2, payload2 = runner_mod.handle_execute(dict(body), config, store)
    assert status2 == 409
    assert payload2 == {"error": "dispatch token already used"}


def test_runner_handle_execute_refuses_digest_mismatch(tmp_path: Path) -> None:
    config = runner_mod.RunnerConfig()
    config.token = "synthetic"
    config.signing_key = "synthetic-independent-signing-key"
    config.workspace = tmp_path / "ws"
    config.workspace.mkdir()
    store = runner_mod.DispatchTokenStore()
    body = {
        "issued_at": time.time(),
        "dispatch_token": "fresh-token",
        "action_digest": "0" * 64,
        "action": {"id": "a1", "run_id": "r1", "operation": "read_text",
                   "resource": "docs/a.txt", "arguments": {}},
        "policy_decision": {"id": "d1", "action_request_id": "a1", "outcome": "ALLOW"},
        "approval": None,
    }
    body["dispatch_signature"] = sign_dispatch_payload(body, config.signing_key)
    status, payload = runner_mod.handle_execute(body, config, store)
    assert status == 409
    assert payload == {"error": "action digest mismatch"}


# ---------------- Socket isolation + backend matrix ----------------

# Files outside ``deploy/executor-runner/`` that are allowed to *name* the
# Docker socket, because they verify it is NOT mounted and NOT reachable.
# An entry is still scanned for mount/dial patterns below, and every one of
# its ``docker.sock`` mentions must sit on an absence-asserting line (see
# NEGATIVE_CONTEXT_MARKERS).
SOCKET_VERIFIER_ALLOWLIST = frozenset(
    {
        # Static M2 verifier: asserts "docker.sock" not in the executor
        # source so the image never gains a socket mount.
        "scripts/check_docker_acceptance.py",
        # Docs-submission checker: asserts absent "docker.sock" mounts in
        # deploy/ read-only output, so the submission bundle cannot gain one.
        "scripts/check_docs_links.py",
    }
)

# Patterns that would actually obtain the socket: spell out the host path,
# bind-mount it into a container, or dial it. Applied to every scanned file,
# allowlisted verifiers included, so a verifier may only *inspect source for*
# the socket string, never reach for the socket itself.
DANGEROUS_SOCKET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/(?:var/)?run[^\"'\s]*docker\.sock"), "host socket path"),
    (re.compile(r"docker\.sock\s*:\s*/"), "bind-mount of the socket"),
    (re.compile(r"unix://[^\"'\s]*docker\.sock"), "DOCKER_HOST dial"),
    (re.compile(r"docker\.from_env"), "docker SDK daemon handle"),
    (re.compile(r"\bAF_UNIX\b|\buds\s*="), "raw unix-socket dial"),
)

# Markers that keep an allowlisted verifier's ``docker.sock`` mention inside
# an absence-asserting context (compared against inspected source that must
# not contain it, or described as unreachable).
NEGATIVE_CONTEXT_MARKERS = (
    "not in",
    "!=",
    "absent",
    "absence",
    "never",
    "must not",
    "unreachable",
    "no-",
)


def test_no_docker_socket_outside_runner() -> None:
    """Gateway/gate/caddy/proxy code must never reference the Docker socket.

    Scope is deployable runtime code (``backend/scopewatch``, ``frontend``,
    ``scripts``, ``deploy``) excluding the socket-holding sidecar directory
    ``deploy/executor-runner/``. Test assertions (``backend/tests``) and
    prose docs intentionally mention ``docker.sock`` to assert its absence;
    they are not deployable runtime and are out of scope here.

    Exception: ``SOCKET_VERIFIER_ALLOWLIST`` holds scripts whose job is to
    prove the socket stays unmounted/unreachable. Those still must not
    contain any mount/dial pattern, and each of their ``docker.sock``
    mentions must read as an absence assertion.
    """
    repo_root = Path(__file__).resolve().parents[2]
    scanned = [
        repo_root / "backend" / "scopewatch",
        repo_root / "frontend",
        repo_root / "scripts",
        repo_root / "deploy",
    ]
    allowed_dir = repo_root / "deploy" / "executor-runner"
    offenders: list[str] = []
    weak_verifier_mentions: list[str] = []
    for root in scanned:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if allowed_dir in path.parents or path == allowed_dir:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="strict")
            except Exception:
                continue
            rel = str(path.relative_to(repo_root))
            # Nothing outside the runner may mount or dial the socket, not
            # even an allowlisted verifier.
            for pattern, label in DANGEROUS_SOCKET_PATTERNS:
                if pattern.search(text):
                    offenders.append(f"{rel} ({label})")
            if "docker.sock" not in text:
                continue
            if rel not in SOCKET_VERIFIER_ALLOWLIST:
                offenders.append(rel)
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "docker.sock" in line and not any(
                    marker in line for marker in NEGATIVE_CONTEXT_MARKERS
                ):
                    weak_verifier_mentions.append(f"{rel}:{lineno}")
    assert weak_verifier_mentions == [], (
        "verifier allowlist entries must mention the socket only to assert "
        f"its absence: {weak_verifier_mentions}"
    )
    assert offenders == [], f"socket references outside deploy/executor-runner/: {offenders}"


def test_executor_backend_matrix_local_docker_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "hello.txt").write_text("synthetic", encoding="utf-8")

    # local: reads through the in-process backend.
    monkeypatch.delenv("SCOPEWATCH_EXECUTOR", raising=False)
    local_receipt = execute_action(
        _action("read_text", "hello.txt"), ws, policy_decision=_allow_decision()
    )
    assert local_receipt.executor == "synthetic-workspace-executor"
    assert local_receipt.status == ExecutionStatus.EXECUTED

    # docker: unavailable daemon fails closed with the docker executor name.
    import scopewatch.executor_docker as docker_mod

    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: False)
    docker_receipt = execute_action(
        _action("read_text", "hello.txt"), ws, policy_decision=_allow_decision()
    )
    assert docker_receipt.executor == "docker-executor"
    assert docker_receipt.status == ExecutionStatus.FAILED

    # remote: mocked runner transport succeeds without a daemon or socket.
    _remote_env(monkeypatch)
    import scopewatch.executor_remote as remote_mod

    real_client = remote_mod.httpx.Client

    def fake_client(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(_runner_success_handler),
                           timeout=kwargs.get("timeout", 5.0))

    monkeypatch.setattr(remote_mod.httpx, "Client", fake_client)
    remote_receipt = execute_action(
        _action("read_text", "hello.txt"), ws, policy_decision=_allow_decision()
    )
    assert remote_receipt.executor == "remote-executor"
    assert remote_receipt.status == ExecutionStatus.EXECUTED


def test_run_command_local_still_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Local backend still refuses run_command (remote/docker only)."""
    from scopewatch.executor import ExecutionSecurityError

    monkeypatch.delenv("SCOPEWATCH_EXECUTOR", raising=False)
    with pytest.raises(ExecutionSecurityError, match="requires the Docker executor"):
        execute_action(
            _action("run_command", ".", arguments={"command": "true"}),
            tmp_path,
            policy_decision=_allow_decision(),
        )
