#!/usr/bin/env python3
"""Scopewatch executor-runner sidecar (issue #78).

The ONLY process that mounts the Docker socket (or talks to a rootless
daemon). The gateway runs with ``SCOPEWATCH_EXECUTOR=remote`` and calls
``POST /execute`` on the internal network with a pre-shared bearer token
plus a single-use dispatch token bound to one approved action digest.

Stdlib only (no third-party deps): ``http.server`` + ``json`` +
``subprocess``. Import-safe: unit tests import the validation helpers
without starting the server; ``python3 runner.py`` serves.

The pre-dispatch authorization rules (decision required, ``DENY`` final,
``HOLD`` needs an ``APPROVED`` approval bound to the action, no
``network_request``) are **not** written here. They live in one module,
``scopewatch.dispatch_gate``, which the three gateway backends import and the
image COPYs in from its single authored location (issue #105). The rules the
gateways cannot check stay here as signed-protocol checks; this sidecar renders
the shared verdict with its own HTTP vocabulary (``GATE_REFUSALS``).

Request contract (gateway: ``backend/scopewatch/executor_remote.py``)::

    POST /execute
    Authorization: Bearer <EXECUTOR_RUNNER_TOKEN>
    {
      "dispatch_token": "<single-use random token>",
      "action_digest": "<sha256 over canonical action + decision>",
      "action": {"id","run_id","operation","resource","arguments"},
      "policy_decision": {"id","outcome"},
      "approval": {"id","action_request_id","status"} | null
    }

Validation order (fail closed, static messages, no secret in output):

1. Bearer token via ``hmac.compare_digest`` (401 otherwise).
2. Dispatch token present, unseen, unexpired; recorded one-shot with a 60s
   TTL. Replays and expired tokens are refused (409).
3. Action digest recomputed with the same canonicalization as the gateway;
   mismatches refused (409) so a token minted for one action cannot run
   another.
4. Shared pre-dispatch gate (``scopewatch.dispatch_gate``): an authorizing
   outcome, an ``APPROVED`` approval for a ``HOLD``, no ``network_request``.
   Plus this sidecar's own bindings: the decision names this action, and a
   ``HOLD`` approval names this run and decision; ``ALLOW`` carries no
   approval (403). Verdict-to-HTTP mapping is ``GATE_REFUSALS``.
5. Operation allowlisted; resource relative with no null bytes, no absolute
   paths, no ``..`` escape; resolved target must stay inside ``/workspace``
   (symlink escapes refused). ``network_request`` is never executed.
6. Docker dispatch with the same hardening flags as the gateway Docker
   backend (no network, read-only rootfs, nobody user, cap-drop, pids/mem
   caps, workspace-only mount). Any Docker error, timeout, or malformed
   helper output becomes a ``FAILED`` payload (HTTP 200 with
   ``status: FAILED``) or a 502/500 with a static error — never a traceback.

Digest canonicalization MUST match
``backend/scopewatch/executor_remote.py::compute_action_digest`` exactly.

Docker job construction is NOT here (issue #106): the flag list, the in-container
helper source, the image pin, the staged-workspace copy, and the symlink-tolerant
copy-back walk come from ``backend/scopewatch/docker_job.py``, which the gateway
imports too. This module keeps only transport, auth, the dispatch-token store,
and the checks that decide whether to dispatch at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

# Shared Docker job construction (issue #106). The runner is stdlib-only, so
# this module must stay stdlib-only too: it may not import anything from
# ``scopewatch`` except itself, no third-party package, and nothing that
# opens a socket. The flag list, the helper code, the image pin, and the
# copy-back walk are defined there once and imported here, so the gateway and
# the sidecar cannot drift (that drift already cost us one incident: #104).
#
# Deployed as ``/app/scopewatch/docker_job.py`` in the runner image, so the
# plain imports below resolve. When run straight from a checkout without
# ``PYTHONPATH=backend``, add the backend package first.
if not (Path(__file__).resolve().parent / "scopewatch" / "docker_job.py").is_file():
    _REPO_BACKEND = Path(__file__).resolve().parents[2] / "backend"
    if (_REPO_BACKEND / "scopewatch" / "docker_job.py").is_file():
        sys.path.insert(0, str(_REPO_BACKEND))

from scopewatch.docker_job import (  # noqa: E402
    DOCKER_IMAGE as RUNNER_DOCKER_IMAGE,
    DOCKER_TIMEOUT_S,
    RUN_COMMAND_DEFAULT_TIMEOUT_S,
    best_effort_remove,
    build_docker_command,
    clamp_run_command_timeout,
    make_world_accessible,
    prepare_docker_job,
    replace_link,
    stage_workspace_copy,
    sync_copy_back,
)

# Shared pre-dispatch authorization gate (issue #105). The approval-status and
# outcome rules are authored once, in ``backend/scopewatch/dispatch_gate.py``,
# which the three gateway backends import too; the image COPYs it in beside
# ``docker_job.py`` so the plain import resolves. Stdlib-only like that module.
from scopewatch import dispatch_gate  # noqa: E402

# Single-use dispatch tokens expire after 60s (issue #78).
DISPATCH_TOKEN_TTL_S = 60.0
# Cap request bodies (actions are small synthetic ops).
MAX_BODY_BYTES = 256 * 1024

ALLOWED_OPERATIONS = frozenset(
    {"list_directory", "read_text", "write_text", "delete_path", "run_command"}
)

# How this runner renders each shared-gate refusal (issue #105). The gate owns
# the rule and its machine-readable code; this sidecar keeps its own HTTP
# vocabulary and status codes, which are part of the gateway contract.
GATE_REFUSALS: dict[str, tuple[int, dict[str, str]]] = {
    dispatch_gate.NO_DECISION: (403, {"error": "policy decision required"}),
    dispatch_gate.POLICY_DENY: (403, {"error": "policy outcome does not authorize execution"}),
    dispatch_gate.OUTCOME_REFUSED: (403, {"error": "policy outcome does not authorize execution"}),
    dispatch_gate.APPROVAL_REQUIRED: (403, {"error": "valid approval required"}),
    # 400 "unsupported operation" is what this sidecar returned for
    # network_request before the gate existed (it is absent from
    # ALLOWED_OPERATIONS), and the response is part of the runner contract, so
    # the status is preserved even though the rule now comes from the gate.
    dispatch_gate.NETWORK_FORBIDDEN: (400, {"error": "unsupported operation"}),
}


def canonical_action_digest(
    action: dict[str, Any],
    policy_decision: dict[str, Any],
) -> str:
    """Recompute the gateway action digest (must match exactly)."""
    canonical = {
        "action_id": action.get("id"),
        "run_id": action.get("run_id"),
        "operation": action.get("operation"),
        "resource": action.get("resource"),
        "arguments": action.get("arguments") or {},
        "policy_decision_id": policy_decision.get("id"),
        "policy_outcome": policy_decision.get("outcome"),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DispatchTokenStore:
    """One-shot dispatch tokens with TTL (thread-safe, in-memory)."""

    def __init__(self, ttl_s: float = DISPATCH_TOKEN_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[str, float]] = {}

    def _prune_locked(self, now: float) -> None:
        expired = [t for t, (_, exp) in self._seen.items() if exp <= now]
        for token in expired:
            del self._seen[token]

    def consume(self, token: object, digest: object, now: Optional[float] = None) -> str:
        """Record one use; returns "ok", "reused", or "invalid"."""
        if not isinstance(token, str) or not token or len(token) > 256:
            return "invalid"
        if not isinstance(digest, str) or not digest:
            return "invalid"
        at = time.time() if now is None else now
        with self._lock:
            self._prune_locked(at)
            if token in self._seen:
                return "reused"
            self._seen[token] = (digest, at + self.ttl_s)
            return "ok"

    def binding_for(self, token: str) -> Optional[str]:
        """Return the digest a live token was bound to (test helper)."""
        with self._lock:
            entry = self._seen.get(token)
            if entry is None:
                return None
            digest, expires = entry
            if expires <= time.time():
                return None
            return digest


def check_bearer(authorization: Optional[str], expected_token: str) -> bool:
    """Constant-time bearer check; False on missing/malformed/empty secret."""
    if not authorization or not expected_token:
        return False
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return False
    return hmac.compare_digest(credential.strip(), expected_token)


def validate_resource_inside_workspace(
    resource: object,
    workspace_root: Path,
) -> tuple[Optional[Path], Optional[str]]:
    """Resolve ``resource`` inside the workspace (no following escapes).

    Returns (resolved_target, None) on success or (None, static_reason).
    Null bytes, absolute paths, and ``..`` escapes are refused; a resolved
    target escaping the workspace (including via symlink) is refused.
    """
    if not isinstance(resource, str) or not resource:
        return None, "invalid resource"
    if "\x00" in resource:
        return None, "invalid resource"
    if resource.startswith("/") or resource.startswith("\\"):
        return None, "absolute paths are prohibited"
    if len(resource) > 1 and len(resource) > 2 and resource[1] == ":":
        return None, "absolute paths are prohibited"
    parts = Path(resource).parts
    if ".." in parts:
        return None, "path escapes workspace boundary"
    try:
        resolved = (workspace_root / resource).resolve()
        resolved.relative_to(workspace_root.resolve())
    except ValueError:
        return None, "path escapes workspace boundary"
    except Exception:
        return None, "path resolution failed"
    return resolved, None


def normalize_run_command(
    arguments: object,
) -> tuple[Optional[list[str]], float, Optional[str]]:
    """Normalize run_command argv/timeout (no shell); (argv, timeout, error)."""
    if not isinstance(arguments, dict):
        return None, RUN_COMMAND_DEFAULT_TIMEOUT_S, "malformed argv for run_command"
    argv = arguments.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) for item in argv)
    ):
        return None, RUN_COMMAND_DEFAULT_TIMEOUT_S, "malformed argv for run_command"
    if any("\x00" in item for item in argv):
        return None, RUN_COMMAND_DEFAULT_TIMEOUT_S, "null byte in argv"
    timeout = clamp_run_command_timeout(
        arguments.get("timeout_s", RUN_COMMAND_DEFAULT_TIMEOUT_S)
    )
    return list(argv), timeout, None


class RunnerConfig:
    """Runtime config, environment only (never from agent input)."""

    def __init__(self) -> None:
        self.token = os.environ.get("EXECUTOR_RUNNER_TOKEN", "")
        self.signing_key = os.environ.get("EXECUTOR_RUNNER_SIGNING_KEY", "")
        self.host = os.environ.get("EXECUTOR_RUNNER_HOST", "127.0.0.1")
        self.port = int(os.environ.get("EXECUTOR_RUNNER_PORT", "8091"))
        self.workspace = Path(os.environ.get("EXECUTOR_RUNNER_WORKSPACE", "/workspace"))
        self.image = os.environ.get("EXECUTOR_RUNNER_IMAGE", RUNNER_DOCKER_IMAGE)


TOKEN_STORE = DispatchTokenStore()


def handle_execute(
    body: dict[str, Any],
    config: RunnerConfig,
    token_store: DispatchTokenStore = TOKEN_STORE,
    now: Optional[float] = None,
) -> tuple[int, dict[str, Any]]:
    """Pure dispatch validation + Docker execution (testable without HTTP).

    Returns (http_status, payload). Refusals use static messages; Docker and
    helper failures fail closed.
    """
    action = body.get("action")
    decision = body.get("policy_decision")
    if not isinstance(action, dict) or not isinstance(decision, dict):
        return 400, {"error": "malformed dispatch"}
    signature = body.get("dispatch_signature")
    if (
        not config.signing_key
        or config.signing_key == config.token
        or not isinstance(signature, str)
    ):
        return 403, {"error": "dispatch signature required"}
    unsigned = {key: value for key, value in body.items() if key != "dispatch_signature"}
    try:
        encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return 400, {"error": "malformed dispatch"}
    expected_signature = hmac.new(
        config.signing_key.encode("utf-8"), encoded, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return 403, {"error": "invalid dispatch signature"}
    issued_at = body.get("issued_at")
    at = time.time() if now is None else now
    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, (int, float))
        or not 0 <= at - issued_at <= DISPATCH_TOKEN_TTL_S
    ):
        return 403, {"error": "dispatch expired"}
    outcome = token_store.consume(body.get("dispatch_token"), body.get("action_digest"), now=now)
    if outcome == "reused":
        return 409, {"error": "dispatch token already used"}
    if outcome == "invalid":
        return 400, {"error": "malformed dispatch"}
    expected_digest = canonical_action_digest(action, decision)
    if not hmac.compare_digest(str(body.get("action_digest") or ""), expected_digest):
        return 409, {"error": "action digest mismatch"}

    # A signed dispatch still needs an authorizing decision and exact approval.
    # The outcome, approval-status, and network rules come from the one shared
    # gate (issue #105); the run/decision binding below stays here because it
    # is a signed-protocol check the gateways do not receive.
    verdict = dispatch_gate.authorize_dispatch(action, decision, body.get("approval"))
    refused = GATE_REFUSALS.get(verdict.code)
    if refused is not None:
        return refused
    if decision.get("action_request_id") != action.get("id"):
        return 403, {"error": "policy decision does not match action"}
    approval = body.get("approval")
    if verdict.outcome == "HOLD":
        if (
            not isinstance(approval, dict)
            or not approval.get("id")
            or approval.get("run_id") != action.get("run_id")
            or approval.get("policy_decision_id") != decision.get("id")
        ):
            return 403, {"error": "valid approval required"}
    elif approval is not None:
        return 403, {"error": "unexpected approval"}

    operation = action.get("operation")
    resource = action.get("resource")
    run_id = action.get("run_id") or "unknown-run"
    # The network_request ban is the shared gate's (NETWORK_FORBIDDEN above),
    # so it is not repeated here; this allowlist covers the remaining
    # operations, including anything unknown.
    if operation not in ALLOWED_OPERATIONS:
        return 400, {"error": "unsupported operation"}
    if not isinstance(resource, str):
        return 400, {"error": "invalid resource"}

    workspace = config.workspace
    if not workspace.exists():
        return 500, {"error": "workspace unavailable"}
    target, reason = validate_resource_inside_workspace(resource, workspace)
    if target is None:
        code = 403 if "boundary" in (reason or "") else 400
        return code, {"error": reason or "invalid resource"}

    arguments = action.get("arguments") or {}
    if not isinstance(arguments, dict):
        return 400, {"error": "malformed arguments"}
    run_timeout_s: Optional[float] = None
    if operation == "run_command":
        argv, timeout, error = normalize_run_command(arguments)
        if error is not None or argv is None:
            return 400, {"error": error or "malformed argv for run_command"}
        arguments = {"argv": argv, "timeout_s": timeout}
        run_timeout_s = timeout

    staging_root: Optional[Path] = None
    container_name = f"scopewatch-runner-{uuid.uuid4().hex[:12]}"
    try:
        try:
            staging_root, workspace_copy = stage_workspace_copy(workspace)
        except OSError:
            return 500, {"error": "workspace staging failed"}
        # Same job builder as the gateway (issue #106): the JSON the container
        # parses and the argv it receives cannot disagree, and the host wait is
        # derived from the same timeout the container gets.
        job = prepare_docker_job(
            operation=str(operation),
            resource=resource,
            arguments=arguments,
            timeout_s=run_timeout_s,
        )
        cmd = build_docker_command(
            image=config.image,
            workspace_copy=workspace_copy,
            job=job,
            container_name=container_name,
            run_label=str(run_id),
            executor_label="runner",
        )
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=job.host_timeout_s(DOCKER_TIMEOUT_S),
            )
        except subprocess.TimeoutExpired:
            best_effort_remove(container_name)
            return 200, {"status": "FAILED", "result": {"error": "Docker execution timed out."}, "error_code": "EXECUTION_FAILED"}
        except FileNotFoundError:
            return 502, {"error": "Docker daemon unavailable"}
        except Exception:
            best_effort_remove(container_name)
            return 502, {"error": "Docker execution failed"}
        if proc.returncode != 0:
            return 200, {"status": "FAILED", "result": {"error": "Docker execution failed."}, "error_code": "EXECUTION_FAILED"}
        try:
            payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        except Exception:
            return 502, {"error": "Docker helper returned malformed output"}
        status = payload.get("status")
        result = payload.get("result") or {}
        error_code = payload.get("error_code")
        if status not in ("EXECUTED", "FAILED") or not isinstance(result, dict):
            return 502, {"error": "Docker helper returned malformed output"}
        if status == "EXECUTED" and operation in ("write_text", "run_command"):
            try:
                sync_copy_back(workspace_copy, workspace)
            except Exception:
                return 200, {"status": "FAILED", "result": {"error": "Docker execution failed."}, "error_code": "EXECUTION_FAILED"}
        return 200, {"status": status, "result": result, "error_code": error_code}
    finally:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)
        best_effort_remove(container_name)


class _Handler(BaseHTTPRequestHandler):
    config = RunnerConfig()
    token_store = TOKEN_STORE

    def log_message(self, fmt: str, *args: object) -> None:
        # Structured minimal access log without secrets or bodies.
        sys_msg = fmt % args
        if "Authorization" in sys_msg or "Bearer" in sys_msg:
            sys_msg = "[redacted auth log]"
        print(f"executor-runner {self.command} {self.path} -> {sys_msg}", flush=True)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("", "/healthz", "/health"):
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/execute":
            self._send(404, {"error": "not found"})
            return
        if not check_bearer(self.headers.get("Authorization"), self.config.token):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(400, {"error": "malformed dispatch"})
            return
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"error": "malformed dispatch"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "malformed dispatch"})
            return
        status, payload = handle_execute(body, self.config, self.token_store)
        self._send(status, payload)


def serve(config: Optional[RunnerConfig] = None) -> None:
    """Bind the internal-only HTTP API (defaults to loopback)."""
    cfg = config or RunnerConfig()
    if not cfg.token:
        raise SystemExit("EXECUTOR_RUNNER_TOKEN is required (environment only).")
    if not cfg.signing_key:
        raise SystemExit("EXECUTOR_RUNNER_SIGNING_KEY is required (environment only).")
    if cfg.signing_key == cfg.token:
        raise SystemExit("EXECUTOR_RUNNER_SIGNING_KEY must differ from EXECUTOR_RUNNER_TOKEN.")
    _Handler.config = cfg
    server = HTTPServer((cfg.host, cfg.port), _Handler)
    print(f"executor-runner listening on {cfg.host}:{cfg.port} (internal only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve()
