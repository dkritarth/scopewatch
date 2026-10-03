"""Remote executor backend for Scopewatch (issue #78).

Dispatches Docker-isolated execution to the dedicated ``executor-runner``
sidecar over an internal-only authenticated HTTP API, so the gateway never
holds the Docker socket. Only the runner mounts the socket (or a rootless
daemon); the gateway, gate, and caddy containers are provably socket-free
(see the socket-grep test in ``backend/tests/test_executor_remote.py``).

Trust model (fail closed everywhere):

- Policy checks run on the gateway *before* any network call, through the one
  shared pre-dispatch gate (``scopewatch.dispatch_gate.authorize_dispatch``,
  issue #105): a stored policy decision is required, ``DENY`` returns
  ``NOT_EXECUTED`` without touching the network, ``HOLD`` requires an
  ``APPROVED`` approval bound to the exact action, and ``network_request`` is
  refused outright. ``CONSUMED`` authorizes nothing, because single-use means a
  spent approval cannot authorize a replay. This backend, the local and Docker
  backends, and the runner sidecar all call that one module and none of them
  restates the rules; ``backend/tests/test_executor_gate_consistency.py``
  asserts the backends agree, since four hand-written copies previously
  drifted.
- Auth is a pre-shared bearer token from the environment only
  (``EXECUTOR_RUNNER_TOKEN``); it is never logged or echoed in errors.
- Each dispatch carries a single-use dispatch token bound to one approved
  action digest (SHA-256 over the canonical action + decision identity).
  The payload and its digest are produced together by
  :func:`prepare_dispatch`, so the digest always describes the normalized
  arguments the runner actually receives. The runner enforces one-shot use
  with a 60s expiry and refuses replays (HTTP 409), digest mismatches
  (HTTP 409), and any policy outcome other than ``ALLOW`` or ``HOLD``
  (HTTP 403, issue #104). The gateway maps any refusal, error, timeout, or
  malformed runner output to ``FAILED``/``EXECUTION_FAILED`` and never falls
  back to local execution.
- ``run_command`` argv normalization and timeout clamping reuse the Docker
  backend limits (no shell, output caps enforced container-side), and happen
  before the digest is computed so the two sides agree.

Environment (all server-side; never accept these from agent input):

- ``SCOPEWATCH_EXECUTOR=remote`` selects this backend.
- ``EXECUTOR_RUNNER_URL`` is the internal runner base URL
  (e.g. ``http://executor-runner:8091``).
- ``EXECUTOR_RUNNER_TOKEN`` is the pre-shared bearer secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from scopewatch.executor_docker import (
    clamp_run_command_timeout,
    extract_run_command_argv,
)
from scopewatch.dispatch_gate import authorize_dispatch
from scopewatch.models import ExecutionStatus
from scopewatch.schemas import (
    ActionRequest,
    ApprovalRequest,
    ExecutionReceipt,
    PolicyDecision,
)

EXECUTOR_NAME = "remote-executor"

EXECUTOR_RUNNER_URL_ENV_VAR = "EXECUTOR_RUNNER_URL"
EXECUTOR_RUNNER_TOKEN_ENV_VAR = "EXECUTOR_RUNNER_TOKEN"
EXECUTOR_RUNNER_SIGNING_KEY_ENV_VAR = "EXECUTOR_RUNNER_SIGNING_KEY"

# Single-use dispatch tokens expire after 60s on the runner (issue #78).
DISPATCH_TOKEN_TTL_S = 60

# Upper bound for one remote dispatch: Docker backend timeout (60s) plus the
# run_command buffer (30s). The runner enforces its own container timeout;
# this is the gateway-side fail-closed cutoff.
REMOTE_TIMEOUT_S = 90.0


def compute_action_digest(
    action: ActionRequest,
    policy_decision: PolicyDecision,
    arguments: Optional[dict[str, Any]] = None,
) -> str:
    """Bind one dispatch to one approved action (hex SHA-256).

    Canonical JSON covers the action identity (id, run, operation, resource,
    arguments) plus the stored decision identity and outcome, so a token
    minted for one action cannot be replayed for another. The runner
    recomputes the same digest from the request body and refuses mismatches.

    ``arguments`` defaults to the action's own arguments. A caller that
    normalizes arguments before sending must pass the normalized form here
    too, so the digest describes the payload the runner receives. Prefer
    :func:`prepare_dispatch`, which cannot be called inconsistently.
    """
    canonical = {
        "action_id": action.id,
        "run_id": action.run_id,
        "operation": action.operation,
        "resource": action.resource,
        "arguments": (action.arguments or {}) if arguments is None else arguments,
        "policy_decision_id": policy_decision.id,
        "policy_outcome": policy_decision.outcome.value,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _new_dispatch_token() -> str:
    """Mint one single-use dispatch token (runner enforces one-shot + TTL)."""
    return secrets.token_urlsafe(32)


def sign_dispatch_payload(payload: dict[str, Any], signing_key: str) -> str:
    """Authenticate the complete dispatch, including approval and one-use token."""
    unsigned = {key: value for key, value in payload.items() if key != "dispatch_signature"}
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(signing_key.encode("utf-8"), encoded, hashlib.sha256).hexdigest()


def _normalize_dispatch_arguments(action: ActionRequest) -> dict[str, Any]:
    """Return the exact argument form the runner will receive.

    ``run_command`` is normalized to the argv-list form with a clamped
    timeout so the runner never has to guess. Every other operation passes
    its arguments through unchanged.
    """
    if action.operation != "run_command":
        return dict(action.arguments or {})
    run_argv = extract_run_command_argv(action)
    if run_argv is None:
        raise ValueError("Malformed run_command arguments.")
    run_timeout_s = clamp_run_command_timeout(
        (action.arguments or {}).get("timeout_s", 60.0)
    )
    return {"argv": run_argv, "timeout_s": run_timeout_s}


def prepare_dispatch(
    action: ActionRequest,
    policy_decision: PolicyDecision,
    dispatch_token: Optional[str] = None,
    approval_request: Optional[ApprovalRequest] = None,
) -> tuple[dict[str, Any], str]:
    """Build the runner request body and the digest that describes it.

    Returns ``(payload, digest)`` as a pair. The digest is computed over
    the normalized arguments that appear in the payload, so the runner's
    recomputation always matches; a caller cannot send one form and hash
    the other. That mistake previously made every remote ``run_command``
    fail with HTTP 409, because the gateway hashed the original arguments
    and sent the normalized ones.
    """
    action_arguments = _normalize_dispatch_arguments(action)
    payload: dict[str, Any] = {
        "issued_at": time.time(),
        "dispatch_token": (
            dispatch_token if dispatch_token is not None else _new_dispatch_token()
        ),
        "action_digest": "",
        "action": {
            "id": action.id,
            "run_id": action.run_id,
            "operation": action.operation,
            "resource": action.resource,
            "arguments": action_arguments,
        },
        "policy_decision": {
            "id": policy_decision.id,
            "action_request_id": policy_decision.action_request_id,
            "outcome": policy_decision.outcome.value,
        },
        "approval": (
            {
                "id": approval_request.id,
                "run_id": approval_request.run_id,
                "action_request_id": approval_request.action_request_id,
                "policy_decision_id": approval_request.policy_decision_id,
                "status": approval_request.status.value,
            }
            if approval_request is not None
            else None
        ),
    }
    digest = compute_action_digest(action, policy_decision, action_arguments)
    payload["action_digest"] = digest
    return payload, digest


def resolve_runner_config(
    runner_url: Optional[str] = None,
    runner_token: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve runner URL/token: explicit args win, else environment only."""
    url = (runner_url if runner_url is not None else os.environ.get(EXECUTOR_RUNNER_URL_ENV_VAR, "")).strip()
    token = runner_token if runner_token is not None else os.environ.get(EXECUTOR_RUNNER_TOKEN_ENV_VAR, "")
    if token is not None:
        token = token.strip()
    return (url or None, token or None)


class RemoteExecutor:
    """Gateway-side client for the executor-runner sidecar."""

    def __init__(
        self,
        runner_url: Optional[str] = None,
        runner_token: Optional[str] = None,
        timeout_s: float = REMOTE_TIMEOUT_S,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        url, token = resolve_runner_config(runner_url, runner_token)
        self.runner_url = url.rstrip("/") if url else None
        # Held in memory only; never logged or included in error output.
        self._runner_token = token
        self._signing_key = os.environ.get(EXECUTOR_RUNNER_SIGNING_KEY_ENV_VAR, "").strip()
        self.timeout_s = timeout_s
        self._transport = transport

    def execute(
        self,
        action: ActionRequest,
        workspace_root: Path,
        policy_decision: Optional[PolicyDecision] = None,
        approval_request: Optional[ApprovalRequest] = None,
    ) -> ExecutionReceipt:
        receipt_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()

        def _failed(error_code: str, message: str) -> ExecutionReceipt:
            completed_at = datetime.now(timezone.utc).isoformat()
            return ExecutionReceipt(
                id=receipt_id,
                action_request_id=action.id,
                status=ExecutionStatus.FAILED,
                started_at=started_at,
                completed_at=completed_at,
                executor=EXECUTOR_NAME,
                sanitized_result={"error": message},
                error_code=error_code,
                resource=action.resource,
                operation=action.operation,
            )

        # Pre-dispatch gate (issue #105): the one shared authorization
        # decision runs before any network call, so "no decision, no
        # dispatch", "DENY never dispatches", "HOLD needs an APPROVED
        # approval bound to this action", and the network_request ban are
        # written once for the whole executor family. Digest construction
        # and the request/response handling below stay backend-specific.
        verdict = authorize_dispatch(action, policy_decision, approval_request)
        if verdict.kind == "deny":
            completed_at = datetime.now(timezone.utc).isoformat()
            assert policy_decision is not None  # DENY implies a decision
            return ExecutionReceipt(
                id=receipt_id,
                action_request_id=action.id,
                status=ExecutionStatus.NOT_EXECUTED,
                started_at=started_at,
                completed_at=completed_at,
                executor=EXECUTOR_NAME,
                sanitized_result={"reason": "Policy outcome was DENY; execution blocked."},
                error_code=policy_decision.reason_code.value,
                resource=action.resource,
                operation=action.operation,
            )
        if not verdict.proceed:
            from scopewatch.executor import ExecutionSecurityError

            raise ExecutionSecurityError(verdict.reason)

        if (
            not self.runner_url
            or not self._runner_token
            or not self._signing_key
            or self._runner_token == self._signing_key
        ):
            return _failed("EXECUTION_FAILED", "Remote executor unavailable; failing closed.")

        # Build the body and its digest together so the digest always
        # describes the arguments the runner receives (#104).
        dispatch_token = _new_dispatch_token()
        try:
            payload, _digest = prepare_dispatch(
                action, policy_decision, dispatch_token, approval_request
            )
            payload["dispatch_signature"] = sign_dispatch_payload(payload, self._signing_key)
        except ValueError:
            return _failed("EXECUTION_FAILED", "Malformed run_command arguments.")

        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout_s) as client:
                response = client.post(
                    f"{self.runner_url}/execute",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._runner_token}"},
                )
        except httpx.TimeoutException:
            return _failed("EXECUTION_FAILED", "Remote execution timed out.")
        except Exception:
            return _failed("EXECUTION_FAILED", "Remote execution failed.")
        # Refusals (401 auth, 409 token-reuse/digest-mismatch, 4xx validation,
        # 5xx runner errors) all fail closed with a static message: never echo
        # the runner body (it may contain paths or diagnostics).
        if response.status_code != 200:
            if response.status_code == 409:
                return _failed("EXECUTION_FAILED", "Remote dispatch refused.")
            return _failed("EXECUTION_FAILED", "Remote execution failed.")
        try:
            body: dict[str, Any] = response.json()
        except Exception:
            return _failed("EXECUTION_FAILED", "Remote runner returned malformed output.")
        status = body.get("status")
        result = body.get("result") or {}
        error_code = body.get("error_code")
        if status not in ("EXECUTED", "FAILED") or not isinstance(result, dict):
            return _failed("EXECUTION_FAILED", "Remote runner returned malformed output.")
        completed_at = datetime.now(timezone.utc).isoformat()
        return ExecutionReceipt(
            id=receipt_id,
            action_request_id=action.id,
            status=ExecutionStatus.EXECUTED if status == "EXECUTED" else ExecutionStatus.FAILED,
            started_at=started_at,
            completed_at=completed_at,
            executor=EXECUTOR_NAME,
            sanitized_result=result,
            error_code=error_code,
            resource=action.resource,
            operation=action.operation,
        )


def execute_action_remote(
    action: ActionRequest,
    workspace_root: Path,
    policy_decision: Optional[PolicyDecision] = None,
    approval_request: Optional[ApprovalRequest] = None,
    runner_url: Optional[str] = None,
    runner_token: Optional[str] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> ExecutionReceipt:
    """Convenience wrapper used by the gateway entry point and tests."""
    return RemoteExecutor(
        runner_url=runner_url,
        runner_token=runner_token,
        transport=transport,
    ).execute(
        action,
        workspace_root,
        policy_decision=policy_decision,
        approval_request=approval_request,
    )
