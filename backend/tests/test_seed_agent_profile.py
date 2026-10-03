"""seed_demo --profile defaults to SCOPEWATCH_AGENT_PROFILE (#131)."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from seed_demo import build_parser, default_agent_profile  # noqa: E402


def test_default_agent_profile_reads_the_documented_variable() -> None:
    assert default_agent_profile({"SCOPEWATCH_AGENT_PROFILE": "nebius-demo"}) == "nebius-demo"
    assert default_agent_profile({"SCOPEWATCH_AGENT_PROFILE": "  nebius-demo  "}) == "nebius-demo"


def test_default_agent_profile_is_none_when_unset_or_blank() -> None:
    assert default_agent_profile({}) is None
    assert default_agent_profile({"SCOPEWATCH_AGENT_PROFILE": ""}) is None
    assert default_agent_profile({"SCOPEWATCH_AGENT_PROFILE": "   "}) is None


def test_parser_uses_environment_when_flag_absent(monkeypatch) -> None:
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "nebius-demo")
    assert build_parser().parse_args([]).profile == "nebius-demo"


def test_explicit_flag_overrides_environment(monkeypatch) -> None:
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "nebius-demo")
    assert build_parser().parse_args(["--profile", "openrouter-dev"]).profile == "openrouter-dev"


def test_parser_defaults_to_mock_replay_without_environment(monkeypatch) -> None:
    monkeypatch.delenv("SCOPEWATCH_AGENT_PROFILE", raising=False)
    assert build_parser().parse_args([]).profile is None
