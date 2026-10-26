"""Bounded Claude Code PreToolUse preflight; never executes a native tool.

Gateway ALLOW is provisional: this hook returns ASK, never native permission
ALLOW. The native tool still executes outside the gateway if a human accepts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

MAX_INPUT_BYTES = 64 * 1024


def decision(value: str, reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
            "permissionDecision": value, "permissionDecisionReason": reason}}


def map_tool(payload: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Map only exact supported native tools; never translate shell text."""
    if payload.get("hook_event_name") != "PreToolUse":
        raise ValueError("Unsupported event")
    name, args = payload.get("tool_name"), payload.get("tool_input")
    if name not in ("Read", "Write") or not isinstance(args, dict):
        raise ValueError("Unsupported native tool")
    path = args.get("file_path")
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError("Invalid path")
    root = workspace.resolve(strict=True)
    native = Path(path)
    if not native.is_absolute():
        native = root / native
    resource = native.resolve(strict=False).relative_to(root).as_posix()
    arguments: dict[str, Any] = {"path": resource}
    if name == "Write":
        if not isinstance(args.get("content"), str):
            raise ValueError("Invalid write content")
        arguments["content"] = args["content"]
    return {"tool": "workspace", "operation": "read_text" if name == "Read" else "write_text",
            "resource": resource, "arguments": arguments,
            "requested_by": "claude-code-preflight", "reasoning_provenance": "UNAVAILABLE"}


def preflight(payload: dict[str, Any], client: Any, run_id: str, workspace: Path,
              token: str | None = None) -> dict[str, Any]:
    try:
        request = map_tool(payload, workspace)
        headers = {"X-Demo-Token": token} if token else {}
        response = client.post(f"/api/v1/runs/{run_id}/actions/preview", json=request, headers=headers)
        if response.status_code != 200:
            return decision("deny", "Scopewatch preflight unavailable; native action blocked.")
        outcome = response.json().get("outcome")
        if outcome == "DENY":
            return decision("deny", "Scopewatch deterministic scope denies this native action.")
        if outcome not in ("ALLOW", "HOLD"):
            raise ValueError("Invalid preview decision")
        return decision("ask", f"Scopewatch preview {outcome}: preflight only, not execution authorization. "
                        "Native execution is outside gateway evidence; no reasoning audit ran.")
    except Exception:
        # Tool arguments, transcript paths, credentials and provider bodies are
        # deliberately absent from hook diagnostics.
        return decision("deny", "Scopewatch preflight failed or tool/path unsupported; native action blocked.")


def main(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    result = decision("deny", "Scopewatch hook configuration or input invalid; native action blocked.")
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("Hook input too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Invalid hook input")
        url = env.get("SCOPEWATCH_HOOK_GATEWAY", "http://127.0.0.1:8000")
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Only local gateway supported")
        run_id = env["SCOPEWATCH_HOOK_RUN_ID"]
        if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id):
            raise ValueError("Invalid run id")
        with httpx.Client(base_url=url, timeout=3.0, follow_redirects=False, trust_env=False) as client:
            result = preflight(payload, client, run_id, Path(env["SCOPEWATCH_HOOK_WORKSPACE"]), env.get("DEMO_TOKEN"))
    except Exception:
        pass
    print(json.dumps(result))
    return 0  # Valid deny JSON, rather than a non-blocking hook exception.


if __name__ == "__main__":
    raise SystemExit(main())
