"""Contract tests for the synthetic live-provider probe."""

import importlib.util
import sys
from pathlib import Path


PROBE_PATH = Path(__file__).parents[2] / "scripts" / "spikes" / "provider_probe.py"
SPEC = importlib.util.spec_from_file_location("provider_probe", PROBE_PATH)
assert SPEC and SPEC.loader
provider_probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = provider_probe
SPEC.loader.exec_module(provider_probe)


def test_reasoning_requires_a_provider_response_field() -> None:
    assert provider_probe.extract_provider_reasoning(
        {"reasoning_content": "provider trace", "content": "answer"}
    ) == (True, "reasoning_content")
    assert provider_probe.extract_provider_reasoning(
        {"reasoning": "provider trace", "content": "answer"}
    ) == (True, "reasoning")
    assert provider_probe.extract_provider_reasoning(
        {"reasoning_details": [{"type": "reasoning.text", "text": "trace"}]}
    ) == (True, "reasoning_details")
    assert provider_probe.extract_provider_reasoning(
        {"content": "<think>agent-authored text</think>answer"}
    ) == (False, None)
