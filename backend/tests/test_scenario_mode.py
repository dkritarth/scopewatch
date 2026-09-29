"""Issue #73: per-scenario mode key removed; mock-agent mode is replay.

Decision (b): scenario files carry no ``mode`` key. The global ``--mode``
(scripted vs agent) in run_demo.sh / seed_demo.py is the sole control.
Mock-agent mode with the default ``mock`` provider is deterministic replay,
not model choice: build_scenario_mock_provider enqueues one tool call per
scripted action, verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

from scopewatch.agent.__main__ import build_scenario_mock_provider

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCENARIOS_DIR = REPO_ROOT / "demo" / "scenarios"


def _all_scenario_files() -> list[Path]:
    files = sorted(SCENARIOS_DIR.glob("*.json"))
    assert len(files) >= 10, "expected invoice (01-06) + coding (10-13) sets"
    return files


def test_no_scenario_file_carries_mode_key():
    """No scenario JSON may carry a top-level mode key (dead key removed)."""
    offenders = []
    for scen_file in _all_scenario_files():
        data = json.loads(scen_file.read_text(encoding="utf-8"))
        if "mode" in data:
            offenders.append(scen_file.name)
    assert offenders == [], f"scenario files must not carry a mode key: {offenders}"


def test_scenario_files_have_no_per_scenario_mode_semantics():
    """Spot-check: task_scope carries no mode either; global --mode decides."""
    for scen_file in _all_scenario_files():
        data = json.loads(scen_file.read_text(encoding="utf-8"))
        assert "mode" not in data.get("task_scope", {}), scen_file.name


def test_mock_provider_replays_actions_verbatim():
    """Mock provider enqueues one verbatim tool call per scripted action."""
    scen_file = SCENARIOS_DIR / "11_secret_read.json"
    data = json.loads(scen_file.read_text(encoding="utf-8"))
    actions = data["actions"]

    mock = build_scenario_mock_provider(data)
    # One queued response per action plus a final completion turn.
    assert len(mock._queue) == len(actions) + 1

    for idx, act in enumerate(actions):
        queued = mock._queue[idx]
        assert len(queued.tool_calls) == 1
        fn = queued.tool_calls[0]["function"]
        assert fn["name"] == act["operation"]
        args = json.loads(fn["arguments"])
        # Resource travels as path; extra arguments are merged verbatim.
        assert args["path"] == act["resource"]
        for key, value in (act.get("arguments") or {}).items():
            assert args[key] == value
        # Reasoning is replayed verbatim, never invented.
        expected_reasoning = (
            act.get("exposed_reasoning_trace")
            or act.get("reasoning_summary")
            or f"Executing planned step {idx + 1} for task completion."
        )
        assert queued.reasoning_text == expected_reasoning


def test_mock_provider_final_turn_completes_without_tools():
    """The trailing queued turn has no tool calls (loop termination)."""
    scen_file = SCENARIOS_DIR / "10_fix_auth_test.json"
    data = json.loads(scen_file.read_text(encoding="utf-8"))
    mock = build_scenario_mock_provider(data)
    final = mock._queue[-1]
    assert final.tool_calls == []
    assert "completed" in (final.content or "").lower()


def test_seed_mode_help_documents_replay_and_sole_control(tmp_path: Path):
    """seed_demo --mode help states replay semantics and sole control."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "seed_demo.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    help_text = proc.stdout.lower()
    assert "replay" in help_text
    assert "sole control" in help_text


def test_run_demo_help_documents_modes():
    """run_demo.sh --help explains scripted vs mock-agent (replay) vs live."""
    import subprocess

    proc = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "run_demo.sh"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    help_text = proc.stdout.lower()
    assert "deterministic replay" in help_text
    assert "model choice" in help_text
    assert "per-scenario mode" in help_text
