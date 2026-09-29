"""MCP server adapter prototype: every tool call goes through the gateway.

Prototype for issue #51. This module is intentionally dependency-light
(standard library plus ``httpx`` only) so it can run anywhere an MCP server
would run, without importing gateway internals.

Design rules (mirroring the domain invariants in AGENTS.md):

* The adapter NEVER touches the guarded files or the controlled executor
  itself. Its only effect channel is the gateway HTTP API
  (``POST /api/v1/runs/{id}/actions`` plus approval polling). There is no
  direct file, shell, or execution access in this file.
* A policy DENY is final and is surfaced to the MCP client as a tool error.
  The adapter has no path that turns DENY into execution.
* A HOLD suspends the MCP tool call until the approval resolves. On timeout
  the adapter fails closed: it reports ``HOLD_TIMEOUT`` and never claims
  the action executed.
* Reasoning provenance is ``AGENT_AUTHORED_SUMMARY`` when the caller passes
  ``reasoning_summary`` and ``UNAVAILABLE`` otherwise. The adapter has no
  parameter for a provider-exposed trace and never forwards one.

Coverage (see COVERAGE_STATEMENT, following the #61 pattern): the gateway
mediates only actions submitted through this adapter. An MCP client that
uses its own native file or shell tools bypasses Scopewatch entirely: such
activity is not observed, not blocked, and not recorded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

GATEWAY_TOOL = "workspace"

MEDIATED_OPERATIONS = (
    "list_directory",
    "read_text",
    "write_text",
    "delete_path",
    "run_command",
)

MCP_TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "list_directory",
        "description": (
            "List a directory inside the task workspace via the Scopewatch "
            "gateway. Allowed paths only; blocked or escaping paths are denied."
        ),
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
    },
    {
        "name": "read_text",
        "description": "Read a text file inside the task workspace via the gateway.",
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
    },
    {
        "name": "write_text",
        "description": "Write a text file inside the task workspace via the gateway.",
        "input_schema": {
            "type": "object",
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
        },
    },
    {
        "name": "delete_path",
        "description": (
            "Delete a path inside the task workspace via the gateway. "
            "Usually requires human approval (HOLD) before it runs."
        ),
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run an allowlisted command via the gateway. The gateway decides "
            "deterministically; disallowed commands are denied, never executed."
        ),
        "input_schema": {
            "type": "object",
            "required": ["command"],
            "properties": {
                "command": {"type": "string"},
                "argv": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
            },
        },
    },
)

COVERAGE_STATEMENT = (
    "Scopewatch mediates only tool actions submitted through this adapter to "
    "the gateway API (POST /api/v1/runs/{run_id}/actions). An MCP client keeps "
    "its own native file and shell tools; anything the client does with those "
    "directly is voluntary-bypass territory: it is not observed, not blocked, "
    "and not recorded as evidence. A missing event never proves nothing "
    "happened. Reviewers must treat gateway evidence as a record of mediated "
    "actions only."
)


class MCPToolError(Exception):
    """A tool-call failure surfaced to the MCP client as a tool error.

    ``is_error`` mirrors the MCP ``isError`` flag: the tool ran (or was
    refused) through the gateway and the payload describes the outcome.
    """

    is_error = True

    def __init__(
        self,
        code: str,
        message: str,
        *,
        decision: Optional[dict[str, Any]] = None,
        status_code: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.decision = decision
        self.status_code = status_code
        self.details = details or {}

    def to_tool_result(self) -> dict[str, Any]:
        return {
            "isError": True,
            "code": self.code,
            "message": self.message,
            "decision": self.decision,
            "status_code": self.status_code,
            "details": self.details,
        }


@dataclass
class HoldConfig:
    """Polling behaviour for HOLD decisions awaiting human approval."""

    timeout_s: float = 30.0
    poll_interval_s: float = 0.25


class GatewayMCPAdapter:
    """Translate MCP-style tool calls into gateway-mediated actions.

    Parameters
    ----------
    http:
        An ``httpx.Client`` already pointed at the gateway (tests inject a
        client backed by ``httpx.MockTransport``; production would point at
        the real base URL). The adapter performs no network access except
        through this client.
    run_id:
        The gateway run every mediated action is submitted under.
    requested_by:
        Actor label recorded on the gateway side.
    hold:
        Default polling budget for HOLD approvals.
    """

    def __init__(
        self,
        http: httpx.Client,
        run_id: str,
        *,
        requested_by: str = "mcp-adapter-agent",
        hold: Optional[HoldConfig] = None,
    ) -> None:
        self._http = http
        self._run_id = run_id
        self._requested_by = requested_by
        self._hold = hold or HoldConfig()

    # -- discovery --------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the MCP tool definitions this adapter mediates."""
        return [dict(tool) for tool in MCP_TOOL_DEFINITIONS]

    def coverage_statement(self) -> str:
        return COVERAGE_STATEMENT

    # -- tool entry point ---------------------------------------------------

    def call_tool(
        self,
        name: str,
        arguments: Any,
        *,
        reasoning_summary: Optional[str] = None,
        hold_timeout_s: Optional[float] = None,
    ) -> dict[str, Any]:
        """Call one MCP tool through the gateway and return its result.

        Raises :class:`MCPToolError` for every refusal, failure, or timeout.
        """
        if name not in MEDIATED_OPERATIONS:
            raise MCPToolError(
                "UNKNOWN_TOOL",
                f"Tool '{name}' is not offered by this adapter; no gateway call was made.",
                details={"offered": list(MEDIATED_OPERATIONS)},
            )
        if not isinstance(arguments, dict):
            raise MCPToolError(
                "MALFORMED_ARGUMENTS",
                f"Tool '{name}' requires an arguments object; no gateway call was made.",
            )

        payload = self._build_action_payload(name, arguments, reasoning_summary)
        response = self._http.post(
            f"/api/v1/runs/{self._run_id}/actions", json=payload
        )
        if response.status_code == 422:
            raise MCPToolError(
                "GATEWAY_REJECTED",
                "Gateway schema validation refused the action (HTTP 422); nothing executed.",
                status_code=422,
                details={"gateway_error": _safe_json(response)},
            )
        if response.status_code >= 400:
            raise MCPToolError(
                "GATEWAY_REJECTED",
                f"Gateway refused the action (HTTP {response.status_code}); nothing executed.",
                status_code=response.status_code,
                details={"gateway_error": _safe_json(response)},
            )
        body = response.json()
        return self._handle_decision(
            body,
            hold_timeout_s=hold_timeout_s
            if hold_timeout_s is not None
            else self._hold.timeout_s,
        )

    # -- payload mapping ----------------------------------------------------

    def _build_action_payload(
        self,
        name: str,
        arguments: dict[str, Any],
        reasoning_summary: Optional[str],
    ) -> dict[str, Any]:
        """Map one MCP tool call onto a SubmitActionRequest body.

        A missing path/cwd is forwarded as a missing ``resource`` key so that
        gateway schema validation (HTTP 422), not the adapter, rejects it.
        """
        payload: dict[str, Any] = {
            "tool": GATEWAY_TOOL,
            "operation": name,
            "arguments": {},
            "requested_by": self._requested_by,
        }
        if reasoning_summary:
            payload["reasoning_summary"] = reasoning_summary
            payload["reasoning_provenance"] = "AGENT_AUTHORED_SUMMARY"
        else:
            payload["reasoning_provenance"] = "UNAVAILABLE"
        # NOTE: no exposed trace is ever attached; see module docstring.

        if name in ("list_directory", "read_text", "delete_path"):
            path = arguments.get("path")
            if path is not None:
                payload["resource"] = path
            if name == "read_text" and isinstance(arguments.get("content"), str):
                # A stray content field is ignored rather than executed.
                pass
        elif name == "write_text":
            path = arguments.get("path")
            if path is not None:
                payload["resource"] = path
            content = arguments.get("content", "")
            payload["arguments"] = {"content": content}
        elif name == "run_command":
            cwd = arguments.get("cwd")
            if cwd is not None:
                payload["resource"] = cwd
            else:
                payload["resource"] = "."
            cmd_args: dict[str, Any] = {}
            if "command" in arguments:
                cmd_args["command"] = arguments["command"]
            if "argv" in arguments:
                cmd_args["argv"] = arguments["argv"]
            payload["arguments"] = cmd_args
        return payload

    # -- decision handling ----------------------------------------------------

    def _handle_decision(self, body: dict[str, Any], *, hold_timeout_s: float) -> dict[str, Any]:
        decision = body.get("policy_decision") or {}
        outcome = decision.get("outcome")
        receipt = body.get("execution_receipt")
        approval = body.get("approval_request")

        if outcome == "ALLOW":
            if receipt and receipt.get("status") == "EXECUTED":
                return {
                    "isError": False,
                    "outcome": "ALLOW",
                    "decision": decision,
                    "result": receipt.get("sanitized_result"),
                    "action_id": (body.get("action_request") or {}).get("id"),
                }
            raise MCPToolError(
                "EXECUTION_FAILED",
                "Gateway allowed the action but execution did not succeed.",
                decision=decision,
                details={"execution_receipt": receipt},
            )
        if outcome == "DENY":
            raise MCPToolError(
                "POLICY_DENIED",
                decision.get("explanation") or "Gateway policy denied the action.",
                decision=decision,
                details={"reason_code": decision.get("reason_code")},
            )
        if outcome == "HOLD":
            action_id = (body.get("action_request") or {}).get("id")
            approval_id = (approval or {}).get("id")
            if not action_id:
                raise MCPToolError(
                    "HOLD_MALFORMED",
                    "Gateway held the action but returned no action id.",
                    decision=decision,
                )
            return self._await_approval(
                action_id, approval_id, decision, hold_timeout_s=hold_timeout_s
            )
        raise MCPToolError(
            "UNKNOWN_OUTCOME",
            f"Gateway returned an unrecognized outcome: {outcome!r}.",
            decision=decision,
        )

    def _await_approval(
        self,
        action_id: str,
        approval_id: Optional[str],
        decision: dict[str, Any],
        *,
        hold_timeout_s: float,
    ) -> dict[str, Any]:
        """Poll the action until its approval resolves, then report honestly."""
        deadline = time.monotonic() + hold_timeout_s
        interval = max(self._hold.poll_interval_s, 0.01)
        last_body: Optional[dict[str, Any]] = None
        while time.monotonic() < deadline:
            response = self._http.get(
                f"/api/v1/runs/{self._run_id}/actions/{action_id}"
            )
            if response.status_code >= 400:
                raise MCPToolError(
                    "GATEWAY_REJECTED",
                    "Approval poll failed; execution state is unknown, assuming nothing.",
                    status_code=response.status_code,
                    decision=decision,
                )
            last_body = response.json()
            approval = last_body.get("approval_request") or {}
            receipt = last_body.get("execution_receipt")
            status = approval.get("status")
            if receipt and receipt.get("status") == "EXECUTED" and status in (
                "APPROVED",
                "CONSUMED",
            ):
                return {
                    "isError": False,
                    "outcome": "HOLD_THEN_EXECUTED",
                    "decision": decision,
                    "result": receipt.get("sanitized_result"),
                    "action_id": action_id,
                    "approval_id": approval_id,
                }
            if status == "DENIED":
                raise MCPToolError(
                    "APPROVAL_DENIED",
                    "Reviewer denied the held action; nothing executed.",
                    decision=decision,
                    details={"approval_id": approval_id, "execution_receipt": receipt},
                )
            if status == "EXPIRED":
                raise MCPToolError(
                    "APPROVAL_EXPIRED",
                    "Approval request expired before resolution; nothing executed.",
                    decision=decision,
                    details={"approval_id": approval_id},
                )
            if receipt and receipt.get("status") == "EXECUTED":
                # Executed without a recorded approval state: refuse to claim
                # the approval path; surface as an anomaly, fail closed.
                raise MCPToolError(
                    "HOLD_ANOMALY",
                    "Action executed without a recorded approval; refusing to report success.",
                    decision=decision,
                    details={"approval_id": approval_id},
                )
            time.sleep(interval)
        raise MCPToolError(
            "HOLD_TIMEOUT",
            "Approval did not resolve in time. Failing closed: the action may "
            "still be pending reviewer decision; it must NOT be treated as executed. "
            "Re-check gateway evidence before retrying (a retry creates a new action).",
            decision=decision,
            details={"approval_id": approval_id, "action_id": action_id},
        )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw_status": response.status_code}
