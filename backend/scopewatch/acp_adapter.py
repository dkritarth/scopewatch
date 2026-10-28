"""Agent Client Protocol (ACP) adapter: mediates Codex/ACP agent actions via the gateway.

Legacy synthetic dictionary dispatcher from issue #49, following spike #48.
It preserves that local contract for existing callers and tests. Its initialize,
permission, extra filesystem methods, and terminal/run are NOT an ACP v1 wire
implementation. The actual stdio client/session launcher is ``acp_client.py``;
that module advertises only v1 filesystem read/write and no terminal capability.
Shared receipt, HOLD, and relay handling below is reused by that client.

Design rules (following AGENTS.md and ADR-0001 invariants):

* External adapter: The adapter NEVER touches the guarded files, subprocesses,
  or the controlled executor itself. Its sole effect channel is the gateway HTTP API
  (``POST /api/v1/runs/{id}/actions`` plus approval polling). Permission
  preflight reads the gateway's side-effect-free policy dry-run
  (``POST /api/v1/runs/{id}/actions/preview``) and has no effect channel.
* A policy DENY is final and is surfaced to the ACP agent as a JSON-RPC error
  or a denied permission response. The adapter cannot bypass or relax a denial.
* A HOLD suspends the ACP request until human approval resolves. On timeout
  the adapter fails closed: it reports ``HOLD_TIMEOUT`` and never executes.
* Permission preflight (#122) reports the deterministic decision only, is
  explicitly labelled provisional, and never creates an approval, consumes
  budget, records evidence, or executes anything.
* Policy authorization is not execution (#123): a success result requires an
  ``EXECUTED`` receipt. ``FAILED``, ``NOT_EXECUTED``, and missing receipts
  become sanitized JSON-RPC tool errors carrying the real action ID and the
  gateway's failure code.
* Reasoning provenance is strictly ``AGENT_AUTHORED_SUMMARY`` when the agent
  supplies a summary, and ``UNAVAILABLE`` otherwise. Per ADR-0001 and spike #48,
  the adapter never claims or forwards a raw provider trace.
* Coverage: See COVERAGE_STATEMENT. Mediation applies strictly to operations
  submitted through the negotiated ACP capabilities.
* Relay bounds (#154, #169): whatever a receipt relays, the adapter decides
  how much reaches the agent. Command streams and receipt error details are
  clipped with a visible RELAY_TRUNCATION_MARKER (never silently), file
  content is refused above MAX_RELAYED_FILE_CHARS rather than clipped, and
  directory listings are capped by count and name length.
* Residual relay fields (#179): receipt identifiers and decision text are
  type-checked and bounded with visible markers. HTTP error bodies are
  withheld because they can echo raw requests or traces. Listings expose
  only typed name/path/type fields and mark omitted runner metadata.
"""

from __future__ import annotations

import time
from typing import Any, Callable, NoReturn, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scopewatch.models import ReasoningProvenance

GATEWAY_TOOL = "workspace"

# Bounds on executor-supplied data relayed back to the agent (#154, #169).
# The adapter is the boundary that decides what reaches the agent, so the
# bounds live here rather than depending on whichever executor happened to
# cap its own output (docker_job clips streams at 64 KiB, reads return full
# content up to MAX_READ_BYTES since #197, executor_remote relays runner
# results verbatim).
#
# This is a documented per-field set rather than one shared number, because
# the three kinds of data fail differently:
#
# * Streams (stdout/stderr, on the success and the failure path) and receipt
#   error details are clipped to a prefix, because that is what a terminal
#   caller would see, and a cut is only honest if it is marked: every clip
#   ends in RELAY_TRUNCATION_MARKER, so truncation is never silent (#169).
# * File content is never clipped. A short read that looks complete invites
#   the agent to write it back and destroy the tail of the file, so a read
#   whose content exceeds MAX_RELAYED_FILE_CHARS is refused with an error
#   that names the size and the limit instead of being silently shortened.
# * Directory listings are capped by count and per-name length, because a
#   listing is an index, not content (#154).
#
# The stream bound (2000) is reused unchanged from #154 so a command result
# is relayed identically whether it succeeded or failed; the error-detail
# bound (500) is the same value #123 already applied, now named. The file
# bound (64 Ki characters) matches the gateway's MAX_READ_BYTES (64 KiB
# since #197). A file within the byte bound decodes to at most that many
# characters, so a local or docker read the gateway allows is never refused
# here; the check still guards runner results relayed verbatim.
MAX_RELAYED_STREAM_CHARS = 2000
MAX_RELAYED_ERROR_CHARS = 500
MAX_RELAYED_FILE_CHARS = 64 * 1024
MAX_RELAYED_ENTRIES = 500
MAX_RELAYED_NAME_CHARS = 512

# Marks every adapter-side clip so a shortened relay is always visible to
# the caller (#169). Reads are refused rather than marked, because a marker
# inside file content would be written back into the file by the agent.
RELAY_TRUNCATION_MARKER = "...[truncated by scopewatch relay bound]"


def _bounded_text(value: Any, limit: int) -> str:
    """Bound relayed executor text to ``limit`` characters, marking any cut.

    The result is never longer than ``limit``, and when a cut happened it
    ends with ``RELAY_TRUNCATION_MARKER`` naming the original size and how
    much was relayed, so the truncation is visible rather than silent.
    A value that already fits is returned byte-for-byte unchanged.
    ``None`` relays as empty output (not the string "None"); any other
    non-text value is coerced to text first, so a malformed receipt cannot
    smuggle an unbounded object past the bound.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    marker = f"\n{RELAY_TRUNCATION_MARKER}: {len(text)} chars total, {limit} relayed\n"
    kept = limit - len(marker)
    if kept <= 0:
        # Pathological limit smaller than the marker itself: the marker is
        # the honest content, so it wins over the head of the text.
        return marker[:limit]
    return text[:kept] + marker


def _bounded_optional(value: Any, limit: int) -> Optional[str]:
    """Relay optional text only; malformed objects must not become payloads."""
    return _bounded_text(value, limit) if isinstance(value, str) else None


def _receipt_int(value: Any) -> Optional[int]:
    """Relay an executor-reported integer, or ``None`` when it is not one.

    A receipt field that is supposed to be a count or an exit status is
    relayed only as an integer: a malformed runner could otherwise hand the
    agent an arbitrarily long string wearing an integer's key (#169).
    ``type(...) is int`` rather than ``isinstance`` on purpose, so a ``bool``
    is excluded too — a flag is not an exit code.
    """
    return value if type(value) is int else None


def _bounded_entries(
    entries: Any, *, action_id: Optional[str] = None
) -> list[str | dict[str, str]]:
    """Relay typed name/path/type fields only; mark omitted runner metadata.

    Unknown keys are deliberately withheld, including nested metadata. No ACP
    consumer needs runner extensions and their size/type has no contract.
    """
    def malformed() -> NoReturn:
        raise AcpRpcError(
            code=-32000,
            message="Gateway returned a malformed directory listing",
            data={"action_id": action_id, "reason_code": "MALFORMED_DIRECTORY_RESULT"},
        )

    if not isinstance(entries, list):
        malformed()
    bounded: list[str | dict[str, str]] = []
    for entry in entries[:MAX_RELAYED_ENTRIES]:
        if isinstance(entry, str):  # Legacy listing format.
            bounded.append(_bounded_text(entry, MAX_RELAYED_NAME_CHARS))
        elif isinstance(entry, dict):
            shown: dict[str, str] = {}
            for key in ("name", "path", "type"):
                if key not in entry:
                    continue
                value = entry[key]
                if not isinstance(value, str):
                    malformed()
                if key == "type" and value not in ("file", "directory"):
                    malformed()
                shown[key] = _bounded_text(value, MAX_RELAYED_NAME_CHARS)
            if any(key not in ("name", "path", "type") for key in entry):
                shown["relay_notice"] = (
                    RELAY_TRUNCATION_MARKER + ": unsupported entry metadata omitted"
                )
            bounded.append(shown)
        else:
            malformed()
    return bounded


MEDIATED_ACP_METHODS = (
    "initialize",
    "fs/read_text_file",
    "fs/write_text_file",
    "fs/delete_file",
    "fs/list_directory",
    "terminal/run",
    "session/request_permission",
)

COVERAGE_STATEMENT = (
    "Scopewatch mediates only file, command, and permission operations submitted "
    "through the Agent Client Protocol (ACP) adapter to the gateway API "
    "(POST /api/v1/runs/{run_id}/actions). Permission preflight "
    "(POST /api/v1/runs/{run_id}/actions/preview) reports the deterministic "
    "policy decision only: it executes nothing, creates no approval, and a "
    "preflight allow is provisional because the submit-time reasoning audit may "
    "still hold the action. If the underlying agent executes actions outside "
    "the negotiated ACP capabilities, such activity is outside the observation "
    "boundary. A missing event never proves an action did not occur. Gateway "
    "evidence represents a tamper-evident audit of mediated operations only."
)


class AcpRpcError(Exception):
    """JSON-RPC 2.0 error representation for ACP communication."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: Optional[dict[str, Any]] = None,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}
        self.status_code = status_code

    def to_rpc_error(self) -> dict[str, Any]:
        err: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.data:
            err["data"] = self.data
        return err


class HoldConfig(BaseModel):
    """Polling configuration for actions held for human approval."""

    model_config = ConfigDict(extra="forbid")

    timeout_s: float = Field(default=30.0, gt=0.0)
    poll_interval_s: float = Field(default=0.1, gt=0.0)


class AcpClientAdapter:
    """Translate ACP JSON-RPC requests into Scopewatch gateway actions.

    Parameters
    ----------
    http:
        An ``httpx.Client`` pointed at the Scopewatch gateway API.
    run_id:
        The active run ID under which all actions will be recorded.
    requested_by:
        Identifier for the agent actor (e.g. "codex-acp").
    hold:
        Configuration for approval hold polling.
    """

    def __init__(
        self,
        http: httpx.Client,
        run_id: str,
        *,
        requested_by: str = "codex-acp",
        hold: Optional[HoldConfig] = None,
    ) -> None:
        self.http = http
        self.run_id = run_id
        self.requested_by = requested_by
        self.hold = hold or HoldConfig()

        self._dispatch_map: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "initialize": self._handle_initialize,
            "fs/read_text_file": self._handle_read_text_file,
            "fs/write_text_file": self._handle_write_text_file,
            "fs/delete_file": self._handle_delete_file,
            "fs/list_directory": self._handle_list_directory,
            "terminal/run": self._handle_terminal_run,
            "terminal/create": self._handle_terminal_create,
            "session/request_permission": self._handle_request_permission,
        }

    def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a single JSON-RPC 2.0 request and return the JSON-RPC response."""
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if request.get("jsonrpc") != "2.0" or not method:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32600,
                    "message": "Invalid Request: expected jsonrpc 2.0 with a method",
                },
            }

        try:
            result = self.dispatch_method(method, params)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            }
        except AcpRpcError as err:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": err.to_rpc_error(),
            }
        except Exception as exc:  # Fail closed on unexpected errors
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": f"Internal RPC mediation error: {type(exc).__name__}",
                    "data": {"error_type": type(exc).__name__},
                },
            }

    def dispatch_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Route an ACP method call to its gateway-mediated handler."""
        handler = self._dispatch_map.get(method)
        if handler is None:
            raise AcpRpcError(
                code=-32601,
                message=f"Method not found or unsupported by Scopewatch ACP adapter: {method}",
            )
        return handler(params)

    @staticmethod
    def _extract_path(params: dict[str, Any]) -> str:
        """Validate and extract a required path string parameter."""
        path = params.get("path")
        if not path or not isinstance(path, str):
            raise AcpRpcError(code=-32602, message="Missing or invalid required parameter: 'path'")
        return path

    @staticmethod
    def _canonical_action(
        operation: str, params: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Map ACP params to the gateway's canonical (resource, arguments).

        Single source of truth for mediated submission and permission
        preflight (#122): preflight is evaluated against exactly the request a
        real submission would send, so the two cannot drift apart on resource,
        command, or content handling.

        ``run_command`` carries the working directory in the resource field
        (the gateway's ``policy._evaluate_run_command`` reads ``action.resource``
        as cwd), matching ``_handle_terminal_run``. File operations carry the
        requested path as the resource. Only ``write_text`` forwards an
        argument, and the deterministic policy ignores arguments, so preflight
        and submission reach the same decision.
        """
        if operation == "run_command":
            raw_cwd = params.get("cwd", "")
            resource = raw_cwd if isinstance(raw_cwd, str) else ""
            arguments: dict[str, Any] = {}
            if "command" in params:
                arguments["command"] = params["command"]
            if resource:
                arguments["cwd"] = resource
            argv = params.get("argv")
            if isinstance(argv, list):
                arguments["argv"] = argv
            return resource, arguments

        raw_path = params.get("path", params.get("resource", ""))
        resource = raw_path if isinstance(raw_path, str) else ""
        arguments = {}
        if operation == "write_text" and isinstance(params.get("content"), str):
            arguments["content"] = params["content"]
        return resource, arguments

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Negotiate ACP capabilities, advertising mediated client tools."""
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "fs": {
                    "readTextFile": True,
                    "writeTextFile": True,
                    "deleteFile": True,
                    "listDirectory": True,
                },
                "terminal": {
                    "run": True,
                    "create": False,
                },
                "session": {
                    "requestPermission": True,
                },
            },
            "serverInfo": {
                "name": "scopewatch-acp-adapter",
                "version": "1.0.0",
                "coverageStatement": COVERAGE_STATEMENT,
            },
        }

    def _handle_read_text_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._extract_path(params)
        resource, arguments = self._canonical_action("read_text", params)
        action_resp = self._submit_action(
            operation="read_text",
            resource=resource,
            arguments=arguments,
            reasoning_summary=params.get("reasoning_summary"),
            turn_id=params.get("turn_id"),
        )
        receipt = action_resp.get("execution_receipt") or {}
        sanitized = receipt.get("sanitized_result") or {}
        # #169: content is relayed whole or refused, never clipped. Local
        # and docker reads return "content" bounded by MAX_READ_BYTES (#197);
        # the "preview" fallback covers an older runner, and executor_remote
        # passes runner results through verbatim, so the adapter cannot
        # assume the key stays small.
        content = sanitized.get("content")
        if content is None:
            content = sanitized.get("preview")
        if content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)
        if len(content) > MAX_RELAYED_FILE_CHARS:
            # Refuse, not truncate: partial content that looks complete
            # would be written back by an agent about to edit the file.
            # The receipt really did EXECUTED, so the error carries the
            # action ID (evidence join) plus the size and the limit; it
            # never claims the execution failed.
            raise AcpRpcError(
                code=-32000,
                message=(
                    f"File content too large to relay: {len(content)} characters "
                    f"exceeds the {MAX_RELAYED_FILE_CHARS}-character read bound"
                ),
                data={
                    "action_id": (action_resp.get("action_request") or {}).get("id"),
                    "reason_code": "READ_RESULT_TOO_LARGE",
                    "char_count": len(content),
                    "limit": MAX_RELAYED_FILE_CHARS,
                    "path": path,
                },
            )
        return {
            "path": path,
            "content": content,
            # An executor that already cut the file says so; the flag is
            # relayed instead of being dropped, so a preview that is not the
            # whole file never looks complete (#169).
            "truncated": bool(sanitized.get("truncated", False)),
            "action_id": (action_resp.get("action_request") or {}).get("id"),
        }

    def _handle_write_text_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._extract_path(params)
        content = params.get("content")
        if content is None or not isinstance(content, str):
            raise AcpRpcError(code=-32602, message="Missing or invalid required parameter: 'content'")

        resource, arguments = self._canonical_action("write_text", params)
        action_resp = self._submit_action(
            operation="write_text",
            resource=resource,
            arguments=arguments,
            reasoning_summary=params.get("reasoning_summary"),
            turn_id=params.get("turn_id"),
        )
        receipt = action_resp.get("execution_receipt") or {}
        sanitized = receipt.get("sanitized_result") or {}
        # #123: an EXECUTED receipt is guaranteed by _require_executed_receipt,
        # so "success" is earned. The byte count is still reported only when
        # the executor measured it, never inferred from the request, and only
        # when it really is an integer: a malformed receipt would otherwise
        # relay an arbitrarily long string as a byte count (#169).
        bytes_written = _receipt_int(sanitized.get("bytes_written"))
        return {
            "path": path,
            "bytes_written": bytes_written,
            "action_id": (action_resp.get("action_request") or {}).get("id"),
            "status": "success",
        }

    def _handle_delete_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._extract_path(params)
        resource, arguments = self._canonical_action("delete_path", params)
        action_resp = self._submit_action(
            operation="delete_path",
            resource=resource,
            arguments=arguments,
            reasoning_summary=params.get("reasoning_summary"),
            turn_id=params.get("turn_id"),
        )
        return {
            "path": path,
            "action_id": (action_resp.get("action_request") or {}).get("id"),
            "status": "deleted",
        }

    def _handle_list_directory(self, params: dict[str, Any]) -> dict[str, Any]:
        path = params.get("path", "")
        if not isinstance(path, str):
            raise AcpRpcError(code=-32602, message="Invalid parameter: 'path' must be a string")

        resource, arguments = self._canonical_action("list_directory", params)
        action_resp = self._submit_action(
            operation="list_directory",
            resource=resource,
            arguments=arguments,
            reasoning_summary=params.get("reasoning_summary"),
            turn_id=params.get("turn_id"),
        )
        receipt = action_resp.get("execution_receipt") or {}
        sanitized = receipt.get("sanitized_result") or {}
        # Gateway executors report directory items under "items"; accept the
        # legacy "entries" alias so mediated listings stay honest.
        entries = sanitized.get("entries", sanitized.get("items", []))
        return {
            "path": path,
            "entries": _bounded_entries(
                entries, action_id=(action_resp.get("action_request") or {}).get("id")
            ),
            "relay_notice": (
                RELAY_TRUNCATION_MARKER + ": directory entry count capped"
                if isinstance(entries, list) and len(entries) > MAX_RELAYED_ENTRIES else None
            ),
            "action_id": (action_resp.get("action_request") or {}).get("id"),
        }

    def _handle_terminal_run(self, params: dict[str, Any]) -> dict[str, Any]:
        command = params.get("command")
        if not command or not isinstance(command, str):
            raise AcpRpcError(code=-32602, message="Missing or invalid required parameter: 'command'")

        cwd = params.get("cwd", "")
        if not isinstance(cwd, str):
            cwd = ""

        resource, arguments = self._canonical_action("run_command", params)
        action_resp = self._submit_action(
            operation="run_command",
            resource=resource,
            arguments=arguments,
            reasoning_summary=params.get("reasoning_summary"),
            turn_id=params.get("turn_id"),
        )
        receipt = action_resp.get("execution_receipt") or {}
        sanitized = receipt.get("sanitized_result") or {}
        # #123: an EXECUTED receipt is guaranteed here, so exit_code and the
        # output streams come from the executor. A missing exit_code is
        # reported as unknown rather than invented as 0.
        # #169: the success path is bounded exactly like the failure path —
        # same stream bound, same visible marker — so a verbose command
        # cannot relay an arbitrarily large payload to the agent, and an
        # exit_code that is not an integer is malformed receipt data rather
        # than something to relay verbatim.
        exit_code = _receipt_int(sanitized.get("exit_code"))
        return {
            "command": command,
            "exit_code": exit_code,
            "stdout": _bounded_text(sanitized.get("stdout"), MAX_RELAYED_STREAM_CHARS),
            "stderr": _bounded_text(sanitized.get("stderr"), MAX_RELAYED_STREAM_CHARS),
            "action_id": (action_resp.get("action_request") or {}).get("id"),
        }

    def _handle_terminal_create(self, params: dict[str, Any]) -> dict[str, Any]:
        """Reject interactive terminal creation: Scopewatch only mediates isolated commands."""
        raise AcpRpcError(
            code=-32601,
            message=(
                "Interactive terminal sessions ('terminal/create') are not supported by Scopewatch. "
                "Use 'terminal/run' for mediated command execution."
            ),
        )

    def _handle_request_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        """Side-effect-free permission preflight over the real gateway policy.

        #122: preflight used to be a separate partial evaluator reading
        ``TaskScope`` from the run record. It disagreed with the engine that
        actually decides — most visibly it stripped leading slashes before its
        own absolute-path test, so a request the gateway denies
        (``PATH_OUTSIDE_WORKSPACE``) was reported as an allowed, scope-verified
        permission, and it checked no run state, tool, command prefix, or
        approval rule.

        Now preflight builds the exact request submission would send (through
        the same ``_canonical_action`` mapping) and asks the gateway to evaluate
        it with ``POST /api/v1/runs/{id}/actions/preview``. That seam runs the
        real deterministic policy engine against the same run state, workspace
        root, and task scope, so the two agree by construction rather than by
        two implementations happening to match.

        The preview performs no writes: it creates no action, decision,
        approval, or receipt, emits no event, consumes no demo budget, and
        executes nothing.

        A preview ``ALLOW`` is deterministic-only and therefore **provisional**:
        the submit-time reasoning audit may still escalate to HOLD, and
        execution may still fail. It is labelled as such rather than presented
        as a grant. Every transport, schema, or gateway failure fails closed to
        ``deny``.
        """
        operation = params.get("operation")
        if not operation or not isinstance(operation, str):
            raise AcpRpcError(code=-32602, message="Missing required parameter: 'operation'")

        resource, arguments = self._canonical_action(operation, params)
        preview_payload = {
            "tool": GATEWAY_TOOL,
            "operation": operation,
            "resource": resource,
            "arguments": arguments,
            "requested_by": self.requested_by,
        }
        try:
            preview_resp = self.http.post(
                f"/api/v1/runs/{self.run_id}/actions/preview", json=preview_payload
            )
        except Exception:
            return self._preflight_denial(
                "Permission preflight could not reach the gateway; failing closed.",
                "POLICY_ERROR",
            )

        if preview_resp.status_code != 200:
            # Fail closed on every non-decision response (missing run, schema
            # rejection, demo-guard refusal, gateway error). Only the sanitized
            # error code and HTTP status are surfaced: gateway response bodies
            # are never echoed back to the agent.
            return self._preflight_denial(
                "Gateway policy preflight unavailable; failing closed.",
                self._preflight_failure_code(preview_resp),
                preview_status=preview_resp.status_code,
            )

        try:
            decision = preview_resp.json()
        except Exception:
            return self._preflight_denial(
                "Gateway policy preflight returned unreadable output; failing closed.",
                "POLICY_ERROR",
            )

        if not isinstance(decision, dict):
            return self._preflight_denial(
                "Gateway policy preflight returned unreadable output; failing closed.",
                "POLICY_ERROR",
            )

        outcome = decision.get("outcome")
        # Preview is side-effect-free, so its refusal has no stored action ID.
        # All decision text uses the same typed, marked error-detail bound.
        reason_code = (
            _bounded_optional(decision.get("reason_code"), MAX_RELAYED_ERROR_CHARS) or "POLICY_ERROR"
        )
        explanation = _bounded_optional(decision.get("explanation"), MAX_RELAYED_ERROR_CHARS) or ""
        matched_rule = _bounded_optional(decision.get("matched_rule"), MAX_RELAYED_ERROR_CHARS)

        if outcome == "ALLOW":
            # Provisional by construction: the deterministic layer allowed it,
            # but the submit-time reasoning audit may still escalate to HOLD.
            # An ALLOW here is never a promise that execution will happen.
            return {
                "decision": "allow",
                "scope_verified": True,
                "provisional": True,
                "reason_code": reason_code,
                "matched_rule": matched_rule,
                "explanation": explanation,
                "audit_note": (
                    "Deterministic policy ALLOW only; submit-time reasoning "
                    "audit may still escalate to HOLD, and execution may fail."
                ),
            }
        if outcome == "HOLD":
            return {
                "decision": "requires_approval",
                "action_id": None,
                # The template embeds the agent-supplied operation name, which
                # is never length-checked, so the finished reason is bounded
                # even though `explanation` already is (#179).
                "reason": _bounded_text(
                    explanation
                    or f"Operation '{operation}' requires human approval before execution.",
                    MAX_RELAYED_ERROR_CHARS,
                ),
                "reason_code": reason_code,
                "matched_rule": matched_rule,
            }
        if outcome == "DENY":
            return {
                "decision": "deny",
                "action_id": None,
                "reason": _bounded_text(
                    explanation or f"Operation '{operation}' denied by policy.",
                    MAX_RELAYED_ERROR_CHARS,
                ),
                "reason_code": reason_code,
                "matched_rule": matched_rule,
            }
        return self._preflight_denial(
            "Gateway policy preflight returned an unknown outcome; failing closed.",
            "POLICY_ERROR",
        )

    @staticmethod
    def _preflight_denial(
        reason: str, reason_code: str, *, preview_status: Optional[int] = None
    ) -> dict[str, Any]:
        """Build a fail-closed preflight denial without echoing gateway bodies."""
        denial: dict[str, Any] = {
            "decision": "deny",
            "action_id": None,
            "reason": reason,
            "reason_code": reason_code,
            "provisional": False,
        }
        if preview_status is not None:
            denial["preview_status"] = preview_status
        return denial

    @staticmethod
    def _preflight_failure_code(response: Any) -> str:
        """Classify a non-200 preflight response into a stable reason code.

        Derived from the HTTP status only. The gateway's error body is never
        read or echoed: it may carry provider output, prompts, or traces, and a
        permission query has no need to relay it.
        """
        if response.status_code == 404:
            return "RUN_NOT_FOUND"
        if response.status_code == 422:
            return "MALFORMED_REQUEST"
        return "POLICY_ERROR"

    def _submit_action(
        self,
        operation: str,
        resource: str,
        arguments: dict[str, Any],
        reasoning_summary: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Submit an action to the gateway API and handle ALLOW / DENY / HOLD."""
        provenance = (
            ReasoningProvenance.AGENT_AUTHORED_SUMMARY
            if reasoning_summary
            else ReasoningProvenance.UNAVAILABLE
        )

        payload: dict[str, Any] = {
            "tool": GATEWAY_TOOL,
            "operation": operation,
            "resource": resource,
            "arguments": arguments,
            "requested_by": self.requested_by,
            "reasoning_provenance": provenance.value,
        }
        if reasoning_summary:
            payload["reasoning_summary"] = reasoning_summary
        if turn_id:
            payload["turn_id"] = turn_id

        url = f"/api/v1/runs/{self.run_id}/actions"
        resp = self.http.post(url, json=payload)

        if resp.status_code >= 400:
            # Deliberately withhold the entire body, including detail: FastAPI
            # validation can echo requests and upstream errors can contain raw
            # traces. HTTP status is enough to diagnose this refusal. No stored
            # action ID is available on this path; never trust one in the body.
            raise AcpRpcError(
                code=-32000,
                message=f"Gateway HTTP error {resp.status_code}: action rejected",
                data={
                    "action_id": None,
                    "reason_code": "GATEWAY_HTTP_ERROR",
                    "http_status": resp.status_code,
                },
                status_code=resp.status_code,
            )

        data = resp.json()
        decision_info = data.get("policy_decision") or {}
        outcome = decision_info.get("outcome")
        action_id = (data.get("action_request") or {}).get("id")

        if outcome == "DENY":
            # Bound all gateway decision text; malformed/missing codes get a
            # stable local fallback. Preserve action IDs for exact evidence joins.
            reason_code = _bounded_optional(
                decision_info.get("reason_code"), MAX_RELAYED_ERROR_CHARS
            ) or "POLICY_ERROR"
            raise AcpRpcError(
                code=-32000,
                message=f"Operation denied by policy: {reason_code}",
                data={
                    "reason_code": reason_code,
                    "explanation": _bounded_optional(
                        decision_info.get("explanation"), MAX_RELAYED_ERROR_CHARS
                    ),
                    "action_id": action_id,
                    "matched_rule": _bounded_optional(
                        decision_info.get("matched_rule"), MAX_RELAYED_ERROR_CHARS
                    ),
                },
            )

        if outcome == "HOLD":
            return self._poll_hold(action_id, initial_response=data)

        if outcome == "ALLOW":
            return self._require_executed_receipt(data)

        # Same decision-object rule as the DENY branch above (#179): an
        # unrecognised outcome is still relayed, but bounded, and an absent
        # one stays null rather than becoming an empty string.
        shown_outcome = _bounded_optional(outcome, MAX_RELAYED_ERROR_CHARS)
        raise AcpRpcError(
            code=-32000,
            message=f"Unknown gateway decision outcome: {shown_outcome}",
            data={"outcome": shown_outcome, "action_id": action_id, "reason_code": "POLICY_ERROR"},
        )

    def _require_executed_receipt(
        self, data: dict[str, Any], *, context: str = "Action"
    ) -> dict[str, Any]:
        """Require an EXECUTED receipt before reporting a success result (#123).

        Policy authorization is not proof of successful execution. An allowed
        read of a missing file, an oversized write, or a nonzero-exit command
        produces a FAILED receipt, and a NOT_EXECUTED or absent receipt proves
        nothing at all. Either way the agent must see a sanitized JSON-RPC tool
        error rather than empty content, invented byte counts, or exit_code 0.

        Preserves the real action ID and the gateway's failure code so the
        reviewer can join the tool error back to its evidence. The error detail
        comes only from the gateway receipt, which is already sanitized
        server-side; no provider body, trace, or stack trace is echoed.
        """
        receipt = data.get("execution_receipt")
        action_id = (data.get("action_request") or {}).get("id")

        if isinstance(receipt, dict) and receipt.get("status") == "EXECUTED":
            return data

        status = receipt.get("status") if isinstance(receipt, dict) else None
        error_code = receipt.get("error_code") if isinstance(receipt, dict) else None
        # Remote runners may return malformed identifiers. Bound text, use
        # defaults for other types, and derive reason_code from the shown code.
        status = _bounded_optional(status, MAX_RELAYED_ERROR_CHARS) or "MISSING"
        error_code = _bounded_optional(error_code, MAX_RELAYED_ERROR_CHARS) or "EXECUTION_FAILED"
        sanitized = receipt.get("sanitized_result") if isinstance(receipt, dict) else None
        sanitized = sanitized if isinstance(sanitized, dict) else {}

        error_data: dict[str, Any] = {
            "action_id": action_id,
            "status": status,
            "error_code": error_code,
            "reason_code": error_code,
        }

        detail = sanitized.get("error")
        if isinstance(detail, str) and detail:
            # Clipped with a visible marker (#169), never silently shortened.
            error_data["error"] = _bounded_text(detail, MAX_RELAYED_ERROR_CHARS)
        # run_command failures carry the command's own output, which a terminal
        # caller would legitimately see. Bounded and marked by the same helper
        # the success path uses (#169), from the sanitized receipt only.
        for key in ("exit_code", "stdout", "stderr"):
            value = sanitized.get(key)
            if isinstance(value, str):
                error_data[key] = _bounded_text(value, MAX_RELAYED_STREAM_CHARS)
            elif type(value) is int:  # same integer rule as the success path (#169)
                error_data[key] = value

        raise AcpRpcError(
            code=-32000,
            message=(
                f"{context} did not execute successfully "
                f"(status={error_data['status']}): {error_data['error_code']}"
            ),
            data=error_data,
        )

    def _poll_hold(
        self,
        action_id: str,
        initial_response: dict[str, Any],
    ) -> dict[str, Any]:
        """Poll a held action until approved, denied, or timed out."""
        deadline = time.time() + self.hold.timeout_s
        url = f"/api/v1/runs/{self.run_id}/actions/{action_id}"

        while time.time() < deadline:
            resp = self.http.get(url)
            if resp.status_code == 200:
                action_data = resp.json()

                # A hold that was never granted is reported as such first: the
                # gateway records a NOT_EXECUTED receipt when a reviewer denies
                # or an approval expires, and that is an approval outcome, not
                # an execution failure.
                approval = action_data.get("approval_request")
                if isinstance(approval, dict) and approval.get("status") in (
                    "DENIED",
                    "REJECTED",
                    "EXPIRED",
                ):
                    raise AcpRpcError(
                        code=-32000,
                        message=f"Held action was not approved: {approval.get('status')}",
                        data={
                            "action_id": action_id,
                            "status": approval.get("status"),
                            "reason_code": "APPROVAL_NOT_GRANTED",
                        },
                    )

                # An approved hold reports success only on an EXECUTED
                # receipt; FAILED, NOT_EXECUTED, or missing is a tool error.
                if isinstance(action_data.get("execution_receipt"), dict):
                    return self._require_executed_receipt(action_data, context="Held action")

            time.sleep(self.hold.poll_interval_s)

        # Timeout -> fail closed
        raise AcpRpcError(
            code=-32000,
            message=f"Hold timed out after {self.hold.timeout_s}s awaiting human approval",
            data={"reason_code": "HOLD_TIMEOUT", "action_id": action_id},
        )
