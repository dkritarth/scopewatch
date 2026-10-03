"""Scripted scenario files must not present hand-written reasoning as provider output (#130)."""

import json
from pathlib import Path

import pytest

SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "demo" / "scenarios"
SCENARIO_FILES = sorted(SCENARIOS_DIR.glob("*.json"))


def test_scenarios_directory_is_not_empty() -> None:
    assert SCENARIO_FILES, f"no scenario files found in {SCENARIOS_DIR}"


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda p: p.name)
def test_hand_written_traces_are_labelled_synthetic_fixture(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for action in data.get("actions", []):
        if not action.get("exposed_reasoning_trace"):
            continue
        assert action.get("reasoning_provenance") == "SYNTHETIC_FIXTURE", (
            f"{path.name}: a scenario-authored exposed_reasoning_trace must be labelled "
            "SYNTHETIC_FIXTURE, not PROVIDER_EXPOSED_TRACE"
        )
