"""Gateway-mediated tool definitions, schema conversion, and dispatcher for Scopewatch agent.

INVARIANT:
The agent NEVER touches the executor or filesystem directly!
All operations are mediated strictly through the Scopewatch gateway HTTP API.

NOTE (mock-agent replay honesty):
In agent mode with the ``mock`` provider, the agent does not choose actions.
``build_scenario_mock_provider`` (scopewatch.agent.__main__) replays the
scenario's scripted action list verbatim, one tool call per entry. See
demo/SCENARIOS.md for the scripted vs mock-agent (replay) vs live distinction.
"""

import json
from typing import Any, Optional
import httpx

from scopewatch.models import ReasoningProvenance
from scopewatch.provenance import CAPTURE_TOKEN_HEADER, configured_capture_token
from scopewatch.schemas import (
    ActionResponse,
    ApprovalRequest,
    Run,
    SubmitActionRequest,
)

GATEWAY_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories in a workspace-relative directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path within the workspace (e.g. 'invoices/approved').",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_text",
            "description": "Read text content of a workspace-relative file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path within the workspace (e.g. 'invoices/approved/vendor-a.txt').",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_text",
            "description": "Write text content to a workspace-relative file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path within the workspace (e.g. 'outputs/audit-summary.txt').",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write into the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_path",
            "description": "Delete a file or directory within the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to file or directory to delete.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "network_request",
            "description": (
                "Request an outbound network call to a URL (e.g. POSTing a "
                "result to a reporting endpoint). The request is mediated by "
                "the gateway like every other action: it is evaluated against "
                "the task scope and may be denied. Nothing leaves the "
                "workspace unless the gateway allows it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Destination URL (e.g. 'https://reports.example.invalid/upload').",
                    },
                    "method": {
                        "type": "string",
                        "description": "HTTP method (e.g. 'POST' or 'GET'). Defaults to GET.",
                    },
                    "data": {
                        "type": "string",
                        "description": "Request body payload, if any.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run an allowlisted, shell-free command with a workspace directory "
                "as its working directory ('.' for the workspace root). Only "
                "pre-approved command prefixes run (e.g. 'python -m pytest' when "
                "the scenario scope allowlists it); anything else is denied by "
                "the gateway (COMMAND_NOT_ALLOWED or UNSUPPORTED_OPERATION). "
                "No shell is used, so shell metacharacters are rejected. "
                "Network tools such as curl are never allowlisted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Working directory relative to the workspace root ('.' for the root).",
                    },
                    "command": {
                        "type": "string",
                        "description": "Command string with no shell metacharacters (e.g. 'python -m pytest').",
                    },
                },
                "required": ["path", "command"],
            },
        },
    },
]


def get_gateway_tools() -> list[dict[str, Any]]:
    """Return the gateway tool schemas formatted for OpenAI chat completion function calling."""
    return list(GATEWAY_TOOL_DEFINITIONS)


def parse_tool_call_arguments(args: Any) -> dict[str, Any]:
    """Parse tool call arguments if stringified JSON, or return dict."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def convert_tool_call_to_submit_request(
    tool_name: str,
    tool_arguments: dict[str, Any] | str,
    turn_id: Optional[str] = None,
    exposed_reasoning_trace: Optional[str] = None,
    reasoning_provenance: Optional[ReasoningProvenance | str] = None,
    reasoning_summary: Optional[str] = None,
    requested_by: str = "scopewatch-agent",
) -> SubmitActionRequest:
    """Convert an LLM tool call into a standardized SubmitActionRequest for the gateway."""
    args = parse_tool_call_arguments(tool_arguments)

    provenance: Optional[ReasoningProvenance] = None
    if reasoning_provenance is not None:
        if isinstance(reasoning_provenance, str):
            try:
                provenance = ReasoningProvenance(reasoning_provenance)
            except ValueError:
                provenance = ReasoningProvenance.UNAVAILABLE
        else:
            provenance = reasoning_provenance
    elif exposed_reasoning_trace:
        provenance = ReasoningProvenance.PROVIDER_EXPOSED_TRACE
    elif reasoning_summary:
        provenance = ReasoningProvenance.AGENT_AUTHORED_SUMMARY
    else:
        provenance = ReasoningProvenance.UNAVAILABLE

    if tool_name == "workspace":
        operation = str(args.get("operation") or "")
        resource = str(args.get("resource") or args.get("path") or "")
        action_args = dict(args.get("arguments") or {})
        if "content" in args and "content" not in action_args:
            action_args["content"] = args["content"]
    else:
        operation = tool_name
        # ``url`` is the destination field of the network_request tool; the
        # other tools address a workspace path through ``path``.
        resource = str(args.get("path") or args.get("resource") or args.get("url") or "")
        action_args = {
            k: v for k, v in args.items() if k not in ("path", "resource", "url")
        }

    return SubmitActionRequest(
        tool="workspace",
        operation=operation,
        resource=resource,
        arguments=action_args,
        requested_by=requested_by,
        reasoning_summary=reasoning_summary,
        exposed_reasoning_trace=exposed_reasoning_trace,
        reasoning_provenance=provenance,
        turn_id=turn_id,
    )


class GatewayDispatcher:
    """Submits agent actions to the Scopewatch gateway REST API.

    Ensures that all operations pass through the gateway and never directly touch the filesystem.

    When a capture credential is available (``SCOPEWATCH_CAPTURE_TOKEN``, or an
    explicit ``capture_token``) the submission is marked as a trusted
    in-process provider capture, so the gateway may store the reasoning
    provenance label the provider client derived (#116). Without it the
    gateway stores the label as an unverified caller assertion.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        http_client: Optional[httpx.Client] = None,
        transport: Optional[httpx.BaseTransport] = None,
        timeout_s: float = 30.0,
        capture_token: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.capture_token = (
            capture_token if capture_token is not None else configured_capture_token()
        )
        self._external_client = http_client is not None
        self._client = http_client or httpx.Client(
            base_url=self.base_url,
            transport=transport,
            timeout=timeout_s,
        )

    def close(self) -> None:
        """Close the underlying HTTP client if owned."""
        if not self._external_client:
            self._client.close()

    def __enter__(self) -> "GatewayDispatcher":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def submit_action(
        self,
        run_id: str,
        action: SubmitActionRequest,
    ) -> ActionResponse:
        """Submit an action request to POST /api/v1/runs/{run_id}/actions."""
        url = f"/api/v1/runs/{run_id}/actions"
        headers = {CAPTURE_TOKEN_HEADER: self.capture_token} if self.capture_token else {}
        resp = self._client.post(url, json=action.model_dump(), headers=headers)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Gateway rejected action submission (HTTP {resp.status_code}): {resp.text}"
            )
        return ActionResponse.model_validate(resp.json())

    def get_action(self, run_id: str, action_id: str) -> ActionResponse:
        """Fetch action details from GET /api/v1/runs/{run_id}/actions/{action_id}."""
        url = f"/api/v1/runs/{run_id}/actions/{action_id}"
        resp = self._client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to get action '{action_id}' (HTTP {resp.status_code}): {resp.text}"
            )
        return ActionResponse.model_validate(resp.json())

    def list_approvals(self, run_id: str) -> list[ApprovalRequest]:
        """Fetch approvals for run, trying /runs/{run_id}/approvals or /approvals?run_id=."""
        url = f"/api/v1/runs/{run_id}/approvals"
        resp = self._client.get(url)
        if resp.status_code == 404:
            resp = self._client.get("/api/v1/approvals", params={"run_id": run_id})
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to list approvals (HTTP {resp.status_code}): {resp.text}"
            )
        return [ApprovalRequest.model_validate(item) for item in resp.json()]

    def complete_run(self, run_id: str) -> Run:
        """Mark run as COMPLETED via POST /api/v1/runs/{run_id}/complete."""
        url = f"/api/v1/runs/{run_id}/complete"
        resp = self._client.post(url)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to complete run '{run_id}' (HTTP {resp.status_code}): {resp.text}"
            )
        return Run.model_validate(resp.json())

    def fail_run(self, run_id: str, reason: str = "") -> Optional[Run]:
        """Mark run as FAILED via POST /api/v1/runs/{run_id}/fail if supported."""
        url = f"/api/v1/runs/{run_id}/fail"
        resp = self._client.post(url, params={"reason": reason})
        if resp.status_code == 200:
            return Run.model_validate(resp.json())
        return None

    def record_prompt_version(self, run_id: str, prompt_version: str) -> Optional[Run]:
        """Record the prompt version for the run via PATCH /api/v1/runs/{run_id}."""
        url = f"/api/v1/runs/{run_id}"
        resp = self._client.patch(url, json={"prompt_version": prompt_version})
        if resp.status_code == 200:
            return Run.model_validate(resp.json())
        return None
