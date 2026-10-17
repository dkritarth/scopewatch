"""Issue #200: mock replay never labels invented reasoning as provider-exposed.

Missing reasoning must stay visibly missing (AGENTS.md, evidence honesty).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from scopewatch.agent.__main__ import build_scenario_mock_provider
from scopewatch.models import ReasoningProvenance
from scopewatch.providers.client import ChatResult

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCENARIOS = sorted((REPO_ROOT / "demo" / "scenarios").glob("*.json"))
PROVIDER = ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value
UNAVAILABLE = ReasoningProvenance.UNAVAILABLE.value


def _turns(scenario: dict) -> list[ChatResult]:
    mock = build_scenario_mock_provider(scenario, model="test-model")
    return list(mock._queue)  # type: ignore[attr-defined]


def _action(**extra: object) -> dict:
    return {"operation": "read_text", "resource": "invoices/approved/a.txt", **extra}


def test_action_without_reasoning_is_unavailable_with_no_text() -> None:
    turn = _turns({"actions": [_action()]})[0]
    assert turn.reasoning_text is None
    assert turn.reasoning_provenance == UNAVAILABLE


@pytest.mark.parametrize(
    "label",
    [None, PROVIDER, ReasoningProvenance.SYNTHETIC_FIXTURE.value],
)
def test_explicit_label_cannot_dress_up_missing_reasoning(label: object) -> None:
    turn = _turns({"actions": [_action(reasoning_provenance=label)]})[0]
    assert turn.reasoning_text is None
    assert turn.reasoning_provenance == UNAVAILABLE


def test_supplied_trace_and_summary_keep_their_labels() -> None:
    trace_turn, summary_turn, relabelled = _turns({
        "actions": [
            _action(exposed_reasoning_trace="Read the approved invoice."),
            _action(reasoning_summary="Summarised plan."),
            _action(
                exposed_reasoning_trace="Fixture trace.",
                reasoning_provenance=ReasoningProvenance.SYNTHETIC_FIXTURE.value,
            ),
        ]
    })[:3]
    assert (trace_turn.reasoning_text, trace_turn.reasoning_provenance) == (
        "Read the approved invoice.",
        PROVIDER,
    )
    assert summary_turn.reasoning_provenance == ReasoningProvenance.AGENT_AUTHORED_SUMMARY.value
    assert relabelled.reasoning_provenance == ReasoningProvenance.SYNTHETIC_FIXTURE.value


def test_final_completion_turn_is_not_provider_exposed() -> None:
    final = _turns({"actions": [_action()]})[-1]
    assert final.tool_calls == []
    assert final.reasoning_provenance != PROVIDER
    assert "without violations" not in (final.reasoning_text or "")


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.name for p in SCENARIOS])
def test_no_invented_text_is_ever_provider_exposed(path: pathlib.Path) -> None:
    """Every PROVIDER_EXPOSED_TRACE turn carries text the scenario itself supplied."""
    scenario = json.loads(path.read_text(encoding="utf-8"))
    supplied = {
        act.get("exposed_reasoning_trace") for act in scenario.get("actions", [])
    } - {None}
    for turn in _turns(scenario):
        if turn.reasoning_provenance == PROVIDER:
            assert turn.reasoning_text in supplied
