"""A DEMO_TOKEN with surrounding whitespace (e.g. a trailing newline from a
secret file) must still authenticate the exact token. Synthetic values only."""

from __future__ import annotations

from scopewatch.demo_guards import DemoGuardConfig, DemoGuardPolicy


def test_demo_token_with_trailing_newline_accepts_stripped_header() -> None:
    cfg = DemoGuardConfig.from_env({"DEMO_TOKEN": "synthetic-token\n"})
    assert cfg.enabled is True
    assert cfg.demo_token == "synthetic-token"
    policy = DemoGuardPolicy(cfg)
    assert policy.check_token("synthetic-token").allowed is True
    assert policy.check_token("wrong-token").allowed is False


def test_whitespace_only_demo_token_stays_disabled() -> None:
    assert DemoGuardConfig.from_env({"DEMO_TOKEN": "  \n"}).enabled is False
