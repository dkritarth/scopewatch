"""Agent Client Protocol (ACP) adapter: mediates Codex/ACP agent actions via the gateway.

Implementation for issue #49 (following spike #48). This module acts as the
host/client side of the Agent Client Protocol (ACP), accepting JSON-RPC 2.0
requests from an ACP-compliant coding agent (such as Codex via codex-acp) and
mediating every capability request through the Scopewatch gateway HTTP API.

Design rules (following AGENTS.md and ADR-0001 invariants):

* External adapter: The adapter NEVER touches the guarded files, subprocesses,
  or the controlled executor itself. Its sole effect channel is the gateway HTTP API
  (``POST /api/v1/runs/{id}/actions`` plus approval polling).
* A policy DENY is final and is surfaced to the ACP agent as a JSON-RPC error
  or a denied permission response. The adapter cannot bypass or relax a denial.
* A HOLD suspends the ACP request until human approval resolves. On timeout
  the adapter fails closed: it reports ``HOLD_TIMEOUT`` and never executes.
* Reasoning provenance is strictly ``AGENT_AUTHORED_SUMMARY`` when the agent
  supplies a summary, and ``UNAVAILABLE`` otherwise. Per ADR-0001 and spike #48,
  the adapter never claims or forwards a raw provider trace.
* Coverage: See COVERAGE_STATEMENT. Mediation applies strictly to operations
  submitted through the negotiated ACP capabilities.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scopewatch.models import ReasoningProvenance

GATEWAY_TOOL = "workspace"

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
    "(POST /api/v1/runs/{run_id}/actions). If the underlying agent executes "
    "actions outside the negotiated ACP capabilities, such activity is outside "
    "the observation boundary. A missing event never proves an action did not "
    "occur. Gateway evidence represents a tamper-evident audit of mediated "
    "operations only."
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
        action_resp = self._submit_action(
            operation="read_text",
            resource=path,
            arguments={},
            reasoning_summary=params.get("reasoning_summary"),
            turn_id=params.get("turn_id"),
        )
        receipt = action_resp.get("execution_receipt") or {}
        sanitized = receipt.get("sanitized_result") or {}
        content = sanitized.get("content") or sanitized.get("preview", "")
        return {
            "path": path,
            "content": content,
            "action_id": (action_resp.get("action_request") or {}).get("id"),
        }

    def _handle_write_text_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._extract_path(params)
        content = params.get("content")
        if content is None or not isinstance(content, str):
            raise AcpRpcError(code=-32602, message="Missing or invalid required parameter: 'content'")

        action_resp = self._submit_action(
            operation="write_text",
            resource=path,
            arguments={"content": content},
            reasoning_summary=params.get("reasoning_summary"),
            turn_id=params.get("turn_id"),
        )
        receipt = action_resp.get("execution_receipt") or {}
        sanitized = receipt.get("sanitized_result") or {}
        return {
            "path": path,
            "bytes_written": sanitized.get("bytes_written", len(content.encode("utf-8"))),
            "action_id": (action_resp.get("action_request") or {}).get("id"),
            "status": "success",
        }

    def _handle_delete_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._extract_path(params)
        action_resp = self._submit_action(
            operation="delete_path",
            resource=path,
            arguments={},
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

        action_resp = self._submit_action(
            operation="list_directory",
            resource=path,
            arguments={},
            reasoning_summary=params.get("reasoning_summary"),
            turn_id=params.get("turn_id"),
        )
        receipt = action_resp.get("execution_receipt") or {}
        sanitized = receipt.get("sanitized_result") or {}
        return {
            "path": path,
            "entries": sanitized.get("entries", []),
            "action_id": (action_resp.get("action_request") or {}).get("id"),
        }

    def _handle_terminal_run(self, params: dict[str, Any]) -> dict[str, Any]:
        command = params.get("command")
        if not command or not isinstance(command, str):
            raise AcpRpcError(code=-32602, message="Missing or invalid required parameter: 'command'")

        cwd = params.get("cwd", "")
        if not isinstance(cwd, str):
            cwd = ""

        arguments: dict[str, Any] = {"command": command}
        if cwd:
            arguments["cwd"] = cwd
        if "argv" in params and isinstance(params["argv"], list):
            arguments["argv"] = params["argv"]

        action_resp = self._submit_action(
            operation="run_command",
            resource=cwd,
            arguments=arguments,
            reasoning_summary=params.get("reasoning_summary"),
            turn_id=params.get("turn_id"),
        )
        receipt = action_resp.get("execution_receipt") or {}
        sanitized = receipt.get("sanitized_result") or {}
        return {
            "command": command,
            "exit_code": sanitized.get("exit_code", 0),
            "stdout": sanitized.get("stdout", ""),
            "stderr": sanitized.get("stderr", ""),
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
        """Dry-run permission query against the task scope without premature action execution.

        Evaluates whether the requested operation and path conform to the active
        run's TaskScope without submitting a stateful ActionRequest that would
        prematurely execute the command or create phantom single-use approvals.
        """
        operation = params.get("operation")
        path = params.get("path") or params.get("resource") or ""
        if not operation or not isinstance(operation, str):
            raise AcpRpcError(code=-32602, message="Missing required parameter: 'operation'")

        # Fetch active run scope for preflight permission check
        run_resp = self.http.get(f"/api/v1/runs/{self.run_id}")
        if run_resp.status_code != 200:
            return {
                "decision": "deny",
                "reason": f"Run not found or unavailable: HTTP {run_resp.status_code}",
            }

        scope = run_resp.json().get("task_scope") or {}
        allowed_ops = set(scope.get("allowed_operations") or [])
        requires_approval = set(scope.get("requires_approval") or [])
        blocked_paths = scope.get("blocked_paths") or []
        allowed_paths = scope.get("allowed_paths") or []

        if operation not in allowed_ops and operation not in requires_approval:
            return {
                "decision": "deny",
                "reason": f"Operation '{operation}' not permitted in active task scope.",
                "reason_code": "OPERATION_NOT_ALLOWED",
            }

        if path:
            norm_path = str(path).strip().lstrip("/")
            if ".." in norm_path.split("/") or norm_path.startswith("/"):
                return {
                    "decision": "deny",
                    "reason": f"Path '{path}' outside workspace boundary.",
                    "reason_code": "PATH_TRAVERSAL",
                }
            for blocked in blocked_paths:
                if norm_path == blocked or norm_path.startswith(f"{blocked}/"):
                    return {
                        "decision": "deny",
                        "reason": f"Path '{path}' matches blocked directory prefix '{blocked}'.",
                        "reason_code": "BLOCKED_PATH",
                    }
            if allowed_paths and allowed_paths != ["."]:
                in_allowed = any(
                    norm_path == allow or norm_path.startswith(f"{allow}/")
                    for allow in allowed_paths
                )
                if not in_allowed:
                    return {
                        "decision": "deny",
                        "reason": f"Path '{path}' is not within allowed paths.",
                        "reason_code": "PATH_NOT_ALLOWED",
                    }

        if operation in requires_approval:
            return {
                "decision": "requires_approval",
                "reason": f"Operation '{operation}' requires human approval before execution.",
                "reason_code": "APPROVAL_REQUIRED",
            }

        return {
            "decision": "allow",
            "scope_verified": True,
        }

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
            err_data: dict[str, Any] = {}
            try:
                err_data = resp.json()
            except Exception:
                err_data = {"error": "Gateway returned non-JSON HTTP error"}
            raise AcpRpcError(
                code=-32000,
                message=f"Gateway HTTP error {resp.status_code}: {err_data.get('detail', 'Action rejected')}",
                data=err_data,
                status_code=resp.status_code,
            )

        data = resp.json()
        decision_info = data.get("policy_decision") or {}
        outcome = decision_info.get("outcome")
        action_id = (data.get("action_request") or {}).get("id")

        if outcome == "DENY":
            raise AcpRpcError(
                code=-32000,
                message=f"Operation denied by policy: {decision_info.get('reason_code')}",
                data={
                    "reason_code": decision_info.get("reason_code"),
                    "explanation": decision_info.get("explanation"),
                    "action_id": action_id,
                    "matched_rule": decision_info.get("matched_rule"),
                },
            )

        if outcome == "HOLD":
            return self._poll_hold(action_id, initial_response=data)

        if outcome == "ALLOW":
            return data

        raise AcpRpcError(
            code=-32000,
            message=f"Unknown gateway decision outcome: {outcome}",
            data={"outcome": outcome, "action_id": action_id},
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
                receipt = action_data.get("execution_receipt")
                if receipt and receipt.get("status") == "EXECUTED":
                    return action_data
                elif receipt and receipt.get("status") == "FAILED":
                    raise AcpRpcError(
                        code=-32000,
                        message="Held action failed during execution",
                        data={"action_id": action_id, "error_code": receipt.get("error_code")},
                    )

                approval = action_data.get("approval_request")
                if approval and approval.get("status") in ("DENIED", "REJECTED", "EXPIRED"):
                    raise AcpRpcError(
                        code=-32000,
                        message=f"Held action was not approved: {approval.get('status')}",
                        data={"action_id": action_id, "status": approval.get("status")},
                    )

            time.sleep(self.hold.poll_interval_s)

        # Timeout -> fail closed
        raise AcpRpcError(
            code=-32000,
            message=f"Hold timed out after {self.hold.timeout_s}s awaiting human approval",
            data={"reason_code": "HOLD_TIMEOUT", "action_id": action_id},
        )
