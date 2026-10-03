"""Tests for Issue #125: Enforce agent wall-clock deadline across provider calls and final responses.

Invariant:
The agent must never report COMPLETED if the provider or execution finishes after
the configured wall-clock deadline. Remaining monotonic budget must be tracked
and enforced across provider calls, final responses, and tool dispatches.
"""

from typing import Any
import time
import httpx
import pytest
from fastapi.testclient import TestClient

from scopewatch.agent.loop import AgentLoop, AgentRunResult
from scopewatch.agent.tools import GatewayDispatcher
from scopewatch.models import (
    ReasoningProvenance,
    RunStatus,
)
from scopewatch.providers.profile import ProviderProfile
from scopewatch.providers.client import ChatResult, ProviderClient
from scopewatch.providers.errors import ProviderError, ProviderErrorCode
from tests.test_agent_loop import create_test_run, test_env


def test_final_response_without_tools_after_deadline_fails(test_env: dict[str, Any]) -> None:
    """A provider returning a final response without tools after the deadline must fail the run."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)

    class ExpiredFinalResponseProvider:
        def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            # Simulate a provider call that took 60ms when the deadline was 20ms
            time.sleep(0.06)
            return ChatResult(
                content="Task finished successfully after timeout.",
                tool_calls=[],
                reasoning_text=None,
                reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
                model="test-model",
                profile="mock",
            )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=ExpiredFinalResponseProvider(),
        dispatcher=dispatcher,
        wall_clock_timeout_s=0.02,
    )

    result: AgentRunResult = loop.run()

    # Invariant: Must NOT be COMPLETED
    assert result.status == "FAILED"
    assert "Wall clock timeout reached" in (result.error or "")

    # Invariant: Gateway run status must be FAILED, not COMPLETED or ACTIVE
    run_resp = client.get(f"/api/v1/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] == RunStatus.FAILED.value


def test_tool_call_after_deadline_fails_before_dispatch(test_env: dict[str, Any]) -> None:
    """If provider returns tool calls after deadline, loop fails immediately without dispatching action."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)

    class ExpiredToolCallProvider:
        def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            time.sleep(0.06)
            return ChatResult(
                content="Let me read the file.",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "read_text",
                            "arguments": '{"path": "invoices/approved/vendor-a.txt"}',
                        },
                    }
                ],
                reasoning_text="Reasoning after expiry.",
                reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
                model="test-model",
                profile="mock",
            )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=ExpiredToolCallProvider(),
        dispatcher=dispatcher,
        wall_clock_timeout_s=0.02,
    )

    result: AgentRunResult = loop.run()

    assert result.status == "FAILED"
    assert "Wall clock timeout reached" in (result.error or "")
    # Invariant: No action was dispatched or recorded
    assert len(result.actions) == 0

    run_resp = client.get(f"/api/v1/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] == RunStatus.FAILED.value


def test_provider_client_respects_request_timeout_parameter() -> None:
    """ProviderClient.complete bounds network requests using request_timeout."""
    profile = ProviderProfile(
        name="test-bound",
        base_url="http://mock-endpoint",
        model="test-model",
        timeout_s=5.0,
        max_retries=0,
    )

    def slow_mock_handler(request: httpx.Request) -> httpx.Response:
        # Check timeout setting on request or simulate slow server
        time.sleep(0.08)
        return httpx.Response(200, json={
            "id": "cmpl-1",
            "choices": [{"message": {"role": "assistant", "content": "Done"}}],
            "usage": {"total_tokens": 10},
        })

    transport = httpx.MockTransport(slow_mock_handler)
    client = ProviderClient(profile=profile, transport=transport)

    # Calling complete with request_timeout=0.02 should timeout
    with pytest.raises(ProviderError) as exc_info:
        client.complete(
            messages=[{"role": "user", "content": "Hello"}],
            request_timeout=0.02,
        )

    assert exc_info.value.code == ProviderErrorCode.PROVIDER_TIMEOUT


def test_provider_retry_aborts_when_delay_exceeds_deadline() -> None:
    """Provider retry loop must abort with PROVIDER_TIMEOUT if delay exceeds remaining deadline."""
    profile = ProviderProfile(
        name="test-retry-deadline",
        base_url="http://mock-endpoint",
        model="test-model",
        timeout_s=5.0,
        max_retries=3,
    )

    def fail_with_503(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "service unavailable"})

    transport = httpx.MockTransport(fail_with_503)
    client = ProviderClient(
        profile=profile,
        transport=transport,
        initial_backoff_s=0.5,
        backoff_factor=2.0,
    )

    # Calling complete with request_timeout=0.05 and initial_backoff_s=0.5
    # The retry delay (0.5s) exceeds deadline (0.05s), so it should abort immediately
    start = time.monotonic()
    with pytest.raises(ProviderError) as exc_info:
        client.complete(
            messages=[{"role": "user", "content": "Hello"}],
            request_timeout=0.05,
        )
    elapsed = time.monotonic() - start

    assert exc_info.value.code == ProviderErrorCode.PROVIDER_TIMEOUT
    # Invariant: Must not have slept for 0.5s
    assert elapsed < 0.25


def test_approval_polling_terminates_on_wall_clock_deadline(test_env: dict[str, Any]) -> None:
    """Approval wait loop must terminate and fail the run when wall-clock deadline expires."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)

    class TriggerHoldProvider:
        def __init__(self) -> None:
            self.turn = 0

        def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            self.turn += 1
            # Delete path generates HOLD requiring approval
            return ChatResult(
                content="Deleting output file.",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "delete_path",
                            "arguments": '{"path": "outputs/archive_2025.txt"}',
                        },
                    }
                ],
                reasoning_text="Deleting archive.",
                reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
                model="test-model",
                profile="mock",
            )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=TriggerHoldProvider(),
        dispatcher=dispatcher,
        wall_clock_timeout_s=0.05,
        approval_timeout_s=10.0,
        poll_interval_s=0.01,
    )

    result: AgentRunResult = loop.run()

    assert result.status == "FAILED"
    assert "Wall clock timeout reached" in (result.error or "")
    assert "while awaiting approval" in (result.error or "")

    run_resp = client.get(f"/api/v1/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] == RunStatus.FAILED.value
