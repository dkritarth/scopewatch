"""Tests for issue #149: the agent wall-clock budget must fit live model turns.

The deadline bounds one whole agent run, so its right value depends on what a
model turn costs. Scripted replay (``mock``) answers instantly and keeps the
historical 120 s; a live tool-calling turn costs seconds, so a live profile gets
a budget sized for it.

The invariant from #125 is unchanged and asserted here too: reaching the budget
fails the run, it never reports COMPLETED.
"""

from __future__ import annotations

import pytest

from scopewatch.agent.loop import MIN_TURNS_AT_MEASURED_P95
from scopewatch.config import (
    AGENT_WALL_CLOCK_ENV_VAR,
    LIVE_AGENT_WALL_CLOCK_S,
    REPLAY_AGENT_WALL_CLOCK_S,
    agent_wall_clock_timeout_s,
)
from scopewatch.providers.profile import ProviderProfile


def _profile(name: str, base_url: str, **kw) -> ProviderProfile:
    return ProviderProfile(name=name, base_url=base_url, model="m", **kw)


class TestBudgetResolution:
    """The budget follows the agent model, not a single hard-coded number."""

    def test_scripted_replay_keeps_the_short_historical_budget(self) -> None:
        assert (
            agent_wall_clock_timeout_s("mock", "mock://localhost", env={})
            == REPLAY_AGENT_WALL_CLOCK_S
        )

    def test_a_live_profile_gets_the_live_budget(self) -> None:
        assert (
            agent_wall_clock_timeout_s("nebius-demo", "https://api.example/v1", env={})
            == LIVE_AGENT_WALL_CLOCK_S
        )
        assert (
            agent_wall_clock_timeout_s(
                "openrouter-dev", "https://openrouter.ai/api/v1", env={}
            )
            == LIVE_AGENT_WALL_CLOCK_S
        )

    def test_an_unknown_profile_is_treated_as_scripted_replay(self) -> None:
        # A test double with no profile must not silently get a live budget.
        assert agent_wall_clock_timeout_s("", "", env={}) == REPLAY_AGENT_WALL_CLOCK_S

    def test_live_budget_is_larger_than_replay(self) -> None:
        # The whole point of #149: 120 s truncated live runs mid-scenario.
        assert LIVE_AGENT_WALL_CLOCK_S > REPLAY_AGENT_WALL_CLOCK_S

    def test_env_override_wins_over_the_profile_derived_budget(self) -> None:
        env = {AGENT_WALL_CLOCK_ENV_VAR: "42"}
        assert agent_wall_clock_timeout_s("nebius-demo", "https://x/v1", env=env) == 42.0
        assert agent_wall_clock_timeout_s("mock", "mock://localhost", env=env) == 42.0

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "not-a-number", "0", "-5"],
    )
    def test_unusable_env_values_fall_back_to_the_profile_budget(
        self, raw: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A typo in the env var must not disable the deadline entirely."""
        env = {AGENT_WALL_CLOCK_ENV_VAR: raw}
        assert (
            agent_wall_clock_timeout_s("nebius-demo", "https://x/v1", env=env)
            == LIVE_AGENT_WALL_CLOCK_S
        )
        if raw.strip():
            assert any("Ignoring" in r.getMessage() for r in caplog.records)


class TestBudgetSizeWarning:
    """A mis-sized budget reads like a policy failure, so warn before the run."""

    def test_warns_when_the_budget_cannot_buy_enough_measured_turns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from scopewatch.agent.loop import AgentLoop

        profile = _profile(
            "live-test", "https://example.invalid/v1", agent_turn_plan_s=14.4
        )

        class _Client:
            def __init__(self) -> None:
                self.profile = profile

        # 120 s buys ~8 turns at 14.4 s, which is fine.
        AgentLoop(
            run_id="r",
            provider_client=_Client(),
            task_description="t",
            wall_clock_timeout_s=120.0,
        )
        assert not any("buys only" in r.getMessage() for r in caplog.records)

        caplog.clear()
        # 20 s buys ~1.4 turns: almost certainly truncated.
        AgentLoop(
            run_id="r",
            provider_client=_Client(),
            task_description="t",
            wall_clock_timeout_s=20.0,
        )
        assert any("buys only" in r.getMessage() for r in caplog.records)

    def test_unmeasured_profile_is_not_warned_about(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No measurement means no claim to make -- stay quiet."""
        from scopewatch.agent.loop import AgentLoop

        class _Client:
            class profile:  # no agent_turn_plan_s attribute
                name = "unmeasured"
                base_url = "https://example.invalid/v1"

        AgentLoop(
            run_id="r",
            provider_client=_Client(),
            task_description="t",
            wall_clock_timeout_s=1.0,
        )
        assert not any("buys only" in r.getMessage() for r in caplog.records)

    def test_minimum_turns_constant_is_sane(self) -> None:
        assert MIN_TURNS_AT_MEASURED_P95 >= 2


class TestDeadlineInvariantUnchanged:
    """#125 must still hold: the budget fails a run, it never completes one."""

    def test_explicit_budget_is_respected_verbatim(self) -> None:
        from scopewatch.agent.loop import AgentLoop

        profile = _profile("live-test", "https://example.invalid/v1")

        class _Client:
            def __init__(self) -> None:
                self.profile = profile

        loop = AgentLoop(
            run_id="r",
            provider_client=_Client(),
            task_description="t",
            wall_clock_timeout_s=7.5,
        )
        assert loop.wall_clock_timeout_s == 7.5

    def test_a_run_past_its_budget_reports_failed(self) -> None:
        """A live-profile run that exceeds the budget must fail, not complete."""
        import time

        from scopewatch.agent.loop import AgentLoop
        from scopewatch.models import ReasoningProvenance
        from scopewatch.providers.client import ChatResult

        class SlowProvider:
            """Returns a final response only after the budget has passed."""

            def complete(self, messages, **kwargs) -> ChatResult:
                time.sleep(0.08)
                return ChatResult(
                    content="done",
                    tool_calls=[],
                    reasoning_text=None,
                    reasoning_provenance=ReasoningProvenance.UNAVAILABLE.value,
                    model="test-model",
                    profile="mock",
                )

        class _NoopDispatcher:
            def fail_run(self, run_id, reason=""):
                return None

            def record_prompt_version(self, run_id, version):
                return None

        loop = AgentLoop(
            run_id="r",
            task_description="t",
            provider_client=SlowProvider(),
            dispatcher=_NoopDispatcher(),
            wall_clock_timeout_s=0.02,
        )
        result = loop.run()

        assert result.status == "FAILED"
        assert "Wall clock timeout reached" in (result.error or "")
        assert result.status != "COMPLETED"