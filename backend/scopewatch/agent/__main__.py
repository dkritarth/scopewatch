"""CLI entry point for Scopewatch model-driven agent.

Usage:
    python -m scopewatch.agent --scenario demo/scenarios/01_safe_audit.json
    python -m scopewatch.agent --scenario demo/scenarios/01_safe_audit.json --profile openrouter-dev
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Optional
import httpx

from scopewatch.agent.loop import AgentLoop, AgentRunResult
from scopewatch.agent.tools import GatewayDispatcher
from scopewatch.models import ReasoningProvenance
from scopewatch.providers.client import ChatResult, MockProviderClient, ProviderClient
from scopewatch.providers.loader import get_agent_profile, get_profile


def load_scenario(scenario_path: str) -> dict[str, Any]:
    """Load scenario JSON file."""
    path = Path(scenario_path)
    if not path.is_file():
        raise FileNotFoundError(f"Scenario file '{scenario_path}' does not exist.")
    return json.loads(path.read_text(encoding="utf-8"))


def build_scenario_mock_provider(scenario_data: dict[str, Any]) -> MockProviderClient:
    """Build a MockProviderClient scripted from the scenario's planned actions."""
    mock = MockProviderClient()
    actions = scenario_data.get("actions", [])
    for idx, act in enumerate(actions, start=1):
        args_payload: dict[str, Any] = {"path": act["resource"]}
        if act.get("arguments"):
            args_payload.update(act["arguments"])

        mock.enqueue(
            ChatResult(
                content=f"Step {idx}: dispatching {act.get('operation')} on {act.get('resource')}.",
                tool_calls=[
                    {
                        "id": f"call_{idx}",
                        "type": "function",
                        "function": {
                            "name": act["operation"],
                            "arguments": json.dumps(args_payload),
                        },
                    }
                ],
                reasoning_text=(
                    act.get("exposed_reasoning_trace")
                    or act.get("reasoning_summary")
                    or f"Executing planned step {idx} for task completion."
                ),
                reasoning_provenance=(
                    act.get("reasoning_provenance")
                    or (
                        ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value
                        if act.get("exposed_reasoning_trace")
                        else (
                            ReasoningProvenance.AGENT_AUTHORED_SUMMARY.value
                            if act.get("reasoning_summary")
                            else ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value
                        )
                    )
                ),
                model="mock-model",
                profile="mock",
            )
        )

    # Final completion turn with no tool calls
    mock.enqueue(
        ChatResult(
            content=(
                f"Task '{scenario_data.get('name', 'Scenario')}' completed successfully. "
                "All planned actions were executed through the gateway."
            ),
            tool_calls=[],
            reasoning_text="All required operations finished without violations.",
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
            model="mock-model",
            profile="mock",
        )
    )
    return mock


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scopewatch Model-Driven Agent CLI",
        prog="python -m scopewatch.agent",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default="demo/scenarios/01_safe_audit.json",
        help="Path to scenario JSON file (default: demo/scenarios/01_safe_audit.json)",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Provider profile name (defaults to SCOPEWATCH_AGENT_PROFILE or 'mock')",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=os.environ.get("SCOPEWATCH_GATEWAY_URL", "http://localhost:8000"),
        help="Scopewatch gateway API base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--api-key-env",
        type=str,
        default=None,
        help="Environment variable containing the provider API key",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="Maximum conversation turns (default: 20)",
    )

    args = parser.parse_args(argv)

    # 1. Load scenario
    try:
        scenario = load_scenario(args.scenario)
    except Exception as exc:
        print(f"Error loading scenario: {exc}", file=sys.stderr)
        return 1

    # 2. Setup gateway client (connect to running server, or fallback to in-process app)
    base_url = args.base_url.rstrip("/")
    client: httpx.Client
    try:
        probe = httpx.get(f"{base_url}/api/v1/health", timeout=2.0)
        if probe.status_code == 200:
            client = httpx.Client(base_url=base_url, timeout=60.0)
        else:
            raise RuntimeError(f"Gateway returned status {probe.status_code}")
    except Exception:
        # Gateway server not running on network, spin up in-process app
        from fastapi.testclient import TestClient
        from scopewatch.app import create_app
        app = create_app()
        client = TestClient(app, base_url="http://gateway.local")

    # 3. Create run on gateway
    task_scope = dict(scenario.get("task_scope", {}))
    if "created_at" not in task_scope:
        task_scope["created_at"] = datetime.now(timezone.utc).isoformat()
    if "schema_version" not in task_scope:
        task_scope["schema_version"] = "1"

    run_payload = {
        "name": scenario.get("name", "Scopewatch Agent Run"),
        "task_scope": task_scope,
    }

    resp = client.post("/api/v1/runs", json=run_payload)
    if resp.status_code != 201:
        print(f"Failed to create run (HTTP {resp.status_code}): {resp.text}", file=sys.stderr)
        return 1

    run_data = resp.json()
    run_id = run_data["id"]

    # 4. Resolve provider
    if args.profile:
        profile = get_profile(args.profile)
    else:
        profile = get_agent_profile()

    if args.api_key_env:
        profile.api_key_env = args.api_key_env

    is_mock = profile.name == "mock" or profile.base_url.startswith("mock://")
    if is_mock and scenario.get("actions"):
        provider_client: Any = build_scenario_mock_provider(scenario)
    else:
        provider_client = ProviderClient(profile)

    # 5. Execute agent loop
    dispatcher = GatewayDispatcher(base_url=base_url, http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        task_description=task_scope.get("task_description", "Execute task."),
        provider_client=provider_client,
        dispatcher=dispatcher,
        max_turns=args.max_turns,
    )

    print(f"Starting Scopewatch Agent for Run: {run_id}")
    print(f"Task: {task_scope.get('task_description')}")
    print(f"Profile: {profile.name} (model: {profile.model})")
    print("-" * 60)

    result: AgentRunResult = loop.run()

    print("-" * 60)
    print("=== Agent Run Summary ===")
    print(f"Run ID:            {result.run_id}")
    print(f"Final Status:      {result.status}")
    print(f"Turns Completed:   {result.turns}")
    print(f"Total Tool Calls:  {result.total_tool_calls}")
    print(f"Decisions:         {result.decisions}")
    if result.final_response:
        print(f"Final Response:    {result.final_response}")
    if result.error:
        print(f"Error:             {result.error}")
    print(f"Prompt Version:    {result.prompt_version}")

    return 0 if result.status == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(main())
