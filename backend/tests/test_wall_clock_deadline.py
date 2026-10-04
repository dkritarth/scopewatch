"""Tests for Issue #125: Enforce agent wall-clock deadline across provider calls and final responses.

Invariant:
The agent must never report COMPLETED if the provider or execution finishes after
the configured wall-clock deadline. Remaining monotonic budget must be tracked
and enforced across provider calls, final responses, and tool dispatches.

Since #159 these deadline tests drive a scripted clock instead of sleeping, so
each test asserts which phase expired rather than which phase the machine
happened to reach. Only the two ProviderClient tests below still use real time.
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
from tests.scripted_clock import ScriptedClock
from tests.test_agent_loop import create_test_run, test_env


def test_final_response_without_tools_after_deadline_fails(test_env: dict[str, Any]) -> None:
    """A provider returning a final response without tools after the deadline must fail the run."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)
    clock = ScriptedClock()

    class ExpiredFinalResponseProvider:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            # The call cost 60 ms when the deadline was 20 ms (#159: scripted,
            # so no real sleeping and no dependency on machine speed).
            self.calls += 1
            clock.advance(0.06)
            return ChatResult(
                content="Task finished successfully after timeout.",
                tool_calls=[],
                reasoning_text=None,
                reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
                model="test-model",
                profile="mock",
            )

    provider = ExpiredFinalResponseProvider()
    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=provider,
        dispatcher=dispatcher,
        wall_clock_timeout_s=0.02,
        time_fn=clock,
        sleep_fn=clock.sleep,
    )

    result: AgentRunResult = loop.run()

    # Invariant: Must NOT be COMPLETED
    assert result.status == "FAILED"
    assert "Wall clock timeout reached" in (result.error or "")
    # Phase: the provider ran once and the overrun was caught straight after
    # that call, before completion could be reported.
    assert provider.calls == 1
    assert result.final_response is None

    # Invariant: Gateway run status must be FAILED, not COMPLETED or ACTIVE
    run_resp = client.get(f"/api/v1/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] == RunStatus.FAILED.value


def test_tool_call_after_deadline_fails_before_dispatch(test_env: dict[str, Any]) -> None:
    """If provider returns tool calls after deadline, loop fails immediately without dispatching action."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)
    clock = ScriptedClock()

    class ExpiredToolCallProvider:
        def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            clock.advance(0.06)
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
        time_fn=clock,
        sleep_fn=clock.sleep,
    )

    result: AgentRunResult = loop.run()

    assert result.status == "FAILED"
    assert "Wall clock timeout reached" in (result.error or "")
    # Invariant: No action was dispatched or recorded
    assert len(result.actions) == 0
    # The provider returned after expiry; the post-provider check blocks dispatch.
    assert result.total_tool_calls == 0

    run_resp = client.get(f"/api/v1/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] == RunStatus.FAILED.value


class HookedDispatcher(GatewayDispatcher):
    """GatewayDispatcher that runs scripted clock work at loop checkpoints.

    ``on_record`` fires before the first deadline check (``run()`` reads the
    clock, records the prompt version, then checks the budget), ``on_submit``
    fires inside dispatch, before the next checkpoint. Both give a test a place
    to move the scripted clock without sleeping.
    """

    def __init__(self, *args: Any, on_record: Any = None, on_submit: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_record = on_record
        self._on_submit = on_submit
        self.submits = 0

    def record_prompt_version(self, run_id: str, prompt_version: str) -> Any:
        if self._on_record is not None:
            self._on_record()
        return super().record_prompt_version(run_id, prompt_version)

    def submit_action(self, run_id: str, request: Any) -> Any:
        self.submits += 1
        if self._on_submit is not None:
            self._on_submit(self.submits)
        return super().submit_action(run_id, request)


def test_deadline_already_passed_never_reaches_the_first_provider_call(
    test_env: dict[str, Any],
) -> None:
    """The pre-provider check fires before any model call once the budget is spent (#125/#159).

    Orderings asserted: the clock runs out between the run start and the first
    deadline check, so the provider must not be called at all.
    """
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)
    clock = ScriptedClock()

    class NeverCalledProvider:
        calls = 0

        def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            NeverCalledProvider.calls += 1
            return ChatResult(
                content="Should never run.",
                tool_calls=[],
                reasoning_text=None,
                reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
                model="test-model",
                profile="mock",
            )

    provider = NeverCalledProvider()
    dispatcher = HookedDispatcher(
        base_url="http://testserver",
        http_client=client,
        on_record=lambda: clock.advance(0.02),  # exactly the budget, spent
    )
    loop = AgentLoop(
        run_id=run_id,
        provider_client=provider,
        dispatcher=dispatcher,
        wall_clock_timeout_s=0.02,
        time_fn=clock,
        sleep_fn=clock.sleep,
    )

    result: AgentRunResult = loop.run()

    assert result.status == "FAILED"
    assert "Wall clock timeout reached" in (result.error or "")
    # Phase: expired before the model was asked anything.
    assert provider.calls == 0
    assert len(result.actions) == 0

    run_resp = client.get(f"/api/v1/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] == RunStatus.FAILED.value


def test_exactly_at_the_deadline_is_expired_never_completed(test_env: dict[str, Any]) -> None:
    """Elapsed time exactly equal to the deadline is over the line (#125: ``>=``).

    A provider that lands *exactly* on its budget must not be allowed to
    complete the run; the post-provider check compares with ``>=``.
    """
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)
    clock = ScriptedClock()

    class ExactlyOnBudgetProvider:
        def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            clock.advance(0.02)  # exactly the budget, not a millisecond more
            return ChatResult(
                content="Finished exactly on the deadline.",
                tool_calls=[],
                reasoning_text=None,
                reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
                model="test-model",
                profile="mock",
            )

    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    loop = AgentLoop(
        run_id=run_id,
        provider_client=ExactlyOnBudgetProvider(),
        dispatcher=dispatcher,
        wall_clock_timeout_s=0.02,
        time_fn=clock,
        sleep_fn=clock.sleep,
    )

    result: AgentRunResult = loop.run()

    assert result.status == "FAILED"
    assert "Wall clock timeout reached" in (result.error or "")
    assert result.final_response is None

    run_resp = client.get(f"/api/v1/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] == RunStatus.FAILED.value


def test_tool_call_dispatched_past_the_deadline_is_never_sent(
    test_env: dict[str, Any],
) -> None:
    """The pre-dispatch check stops every later tool call once the budget is spent (#125/#159).

    The first tool call dispatches normally and spends the rest of the budget
    while dispatching; the second call must be blocked *before* dispatch, so
    exactly one action ever reaches the gateway.
    """
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)
    clock = ScriptedClock()

    class TwoToolCallProvider:
        def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
            return ChatResult(
                content="Batch reading two paths.",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "list_directory",
                            "arguments": '{"path": "invoices/approved"}',
                        },
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {
                            "name": "list_directory",
                            "arguments": '{"path": "invoices/approved"}',
                        },
                    },
                ],
                reasoning_text="Listing twice.",
                reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
                model="test-model",
                profile="mock",
            )

    dispatcher = HookedDispatcher(
        base_url="http://testserver",
        http_client=client,
        # The first dispatch spends the whole remaining budget, so the next
        # checkpoint -- the pre-dispatch check for call 2 -- must catch it.
        on_submit=lambda n: clock.advance(0.02) if n == 1 else None,
    )
    loop = AgentLoop(
        run_id=run_id,
        provider_client=TwoToolCallProvider(),
        dispatcher=dispatcher,
        wall_clock_timeout_s=0.02,
        time_fn=clock,
        sleep_fn=clock.sleep,
    )

    result: AgentRunResult = loop.run()

    assert result.status == "FAILED"
    assert "Wall clock timeout reached" in (result.error or "")
    # Phase/ordering: only the pre-deadline dispatch reached the gateway.
    assert dispatcher.submits == 1
    assert len(result.actions) == 1
    assert result.total_tool_calls == 1

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


@pytest.mark.parametrize("expiry_phase", ["dispatch", "poll"])
def test_approval_polling_terminates_on_wall_clock_deadline(
    test_env: dict[str, Any], expiry_phase: str,
) -> None:
    """Approval wait loop must terminate and fail the run when wall-clock deadline expires."""
    client: TestClient = test_env["client"]
    run_id = create_test_run(client)
    clock = ScriptedClock()

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

    # Expire at dispatch return or after four scripted approval polls.
    dispatcher = HookedDispatcher(
        base_url="http://testserver",
        http_client=client,
        on_submit=lambda n: clock.advance(1.0) if expiry_phase == "dispatch" else None,
    )
    loop = AgentLoop(
        run_id=run_id,
        provider_client=TriggerHoldProvider(),
        dispatcher=dispatcher,
        wall_clock_timeout_s=1.0,
        approval_timeout_s=10.0,
        poll_interval_s=0.25,
        time_fn=clock,
        sleep_fn=clock.sleep,
    )

    result: AgentRunResult = loop.run()

    assert result.status == "FAILED"
    assert "Wall clock timeout reached" in (result.error or "")
    # Asserts the run actually reached the approval-poll exit, not the
    # pre-dispatch one; the primary invariant (never COMPLETED) is below.
    assert "while awaiting approval" in (result.error or "")
    # The loop must have actually entered the wait and issued a HOLD, else the
    # assertion above would pass for the wrong reason.
    assert result.total_tool_calls == 1
    assert len(result.decisions) >= 1
    # Prove the intended poll checkpoint expired, including the exact boundary.
    assert clock.sleeps == ([] if expiry_phase == "dispatch" else [0.25] * 4)

    run_resp = client.get(f"/api/v1/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] == RunStatus.FAILED.value

    # Assert wall-clock approval wait timeout transitioned approval to EXPIRED and emitted APPROVAL_EXPIRED (#283)
    approvals_resp = client.get("/api/v1/approvals", params={"run_id": run_id})
    assert approvals_resp.status_code == 200
    approvals = approvals_resp.json()
    assert len(approvals) == 1
    assert approvals[0]["status"] == "EXPIRED"

    events_resp = client.get(f"/api/v1/runs/{run_id}/events")
    assert events_resp.status_code == 200
    kinds = [e["event_type"] for e in events_resp.json()]
    assert "APPROVAL_EXPIRED" in kinds


def test_scripted_clock_can_complete_with_remaining_budget(test_env: dict[str, Any]) -> None:
    """Completion uses the same time domain as the loop, even from epoch zero."""
    from scopewatch.providers.client import MockProviderClient

    clock = ScriptedClock()
    client = test_env["client"]
    run_id = create_test_run(client)
    provider = MockProviderClient()
    provider.enqueue(ChatResult(content="Finished.", model="mock", profile="mock", tool_calls=[]))
    test_env["app"].state.service.time_fn = clock
    loop = AgentLoop(
        run_id=run_id, provider_client=provider,
        dispatcher=GatewayDispatcher(http_client=client, time_fn=clock, sleep_fn=clock.sleep),
        time_fn=clock, sleep_fn=clock.sleep, wall_clock_timeout_s=1,
    )
    result = loop.run()
    assert result.status == "COMPLETED", result.error
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "COMPLETED"
