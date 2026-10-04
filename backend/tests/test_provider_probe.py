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

MESSAGES = [{"role": "user", "content": "synthetic"}]


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


# --- Auditor probe must send the profile's configured auditor body ------------
# Regression: probe 3 hard-coded `response_format` + `max_tokens` and omitted
# `auditor_body` entirely, so Nemotron kept thinking, ate the 256-token cap, and
# returned no JSON (`finish_reason: length`, `reasoning_tokens: 256`).


def test_probe_targets_resolve_to_real_provider_profiles() -> None:
    from scopewatch.providers.loader import get_profile

    for slug, profile_name in provider_probe.PROVIDER_TARGETS.items():
        assert provider_probe.resolve_target(slug).name == get_profile(profile_name).name


def test_probe_never_hardcodes_the_model_id() -> None:
    for slug in provider_probe.PROVIDER_TARGETS:
        profile = provider_probe.resolve_target(slug)
        for auditor in (False, True):
            payload = provider_probe.build_request_payload(profile, MESSAGES, auditor=auditor)
            assert payload["model"] == profile.model


def test_auditor_probe_sends_the_configured_auditor_body() -> None:
    nebius = provider_probe.resolve_target("nebius")
    payload = provider_probe.build_request_payload(nebius, MESSAGES, auditor=True)
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}

    openrouter = provider_probe.resolve_target("openrouter")
    payload = provider_probe.build_request_payload(openrouter, MESSAGES, auditor=True)
    # `auditor_body` must win over the agent-facing `reasoning.effort`, exactly as
    # ProviderClient.audit_chat does.
    assert payload["reasoning"] == {"enabled": False}


def test_agent_probes_do_not_send_auditor_options() -> None:
    profile = provider_probe.resolve_target("openrouter")
    payload = provider_probe.build_request_payload(profile, MESSAGES, auditor=False)
    assert "response_format" not in payload
    assert "max_tokens" not in payload
    # The agent-facing reasoning control is still forwarded.
    assert payload["reasoning"] == {"effort": "high"}


# --- Honest reasoning reporting (ADR-0001 decision 5) -------------------------


def test_reasoning_field_is_a_provider_exposed_trace() -> None:
    obs = provider_probe.classify_provider_reasoning(
        {"reasoning": "trace", "content": "answer"}, {}
    )
    assert obs.provenance == "PROVIDER_EXPOSED_TRACE"
    assert obs.detail_type == "reasoning"
    assert obs.text_chars == 5


def test_reasoning_summary_block_is_not_a_raw_trace() -> None:
    obs = provider_probe.classify_provider_reasoning(
        {"reasoning_details": [{"type": "reasoning.summary", "summary": "summary"}]}, {}
    )
    assert obs.provenance == "AGENT_AUTHORED_SUMMARY"
    assert obs.detail_type == "reasoning.summary"


def test_unknown_reasoning_detail_block_stays_unavailable() -> None:
    obs = provider_probe.classify_provider_reasoning(
        {"reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}]}, {}
    )
    assert obs.provenance == "UNAVAILABLE"
    assert obs.detail_type == "reasoning.encrypted"
    assert obs.text_chars == 0


def test_untyped_reasoning_detail_keeps_its_field_meaning() -> None:
    obs = provider_probe.classify_provider_reasoning(
        {"reasoning_details": [{"text": "trace"}]}, {}
    )
    assert obs.provenance == "PROVIDER_EXPOSED_TRACE"
    assert obs.detail_type == "reasoning.text"
    obs = provider_probe.classify_provider_reasoning(
        {"reasoning_details": [{"summary": "summary"}]}, {}
    )
    assert obs.provenance == "AGENT_AUTHORED_SUMMARY"
    assert obs.detail_type == "reasoning.summary"


def test_thinking_billed_into_content_is_not_reported_as_a_trace() -> None:
    """The exact failure the probe could not explain: both reasoning fields are
    present but null, the model wrote its thinking into `content`, and the
    provider billed the tokens. That is not provider-exposed reasoning."""
    message = {"reasoning_content": None, "reasoning": None, "content": "x" * 1035}
    usage = {"completion_tokens": 256, "completion_tokens_details": {"reasoning_tokens": 256}}
    obs = provider_probe.classify_provider_reasoning(message, usage)
    assert obs.provenance == "UNAVAILABLE"
    assert obs.detail_type is None
    assert obs.text_chars == 0
    assert obs.note is not None
    assert "256" in obs.note
    assert "content" in obs.note


def test_absent_reasoning_says_so_without_inventing_one() -> None:
    obs = provider_probe.classify_provider_reasoning({"content": "42"}, {})
    assert obs.provenance == "UNAVAILABLE"
    assert obs.text_chars == 0
    assert obs.note is not None


def test_blank_reasoning_field_is_not_a_trace() -> None:
    obs = provider_probe.classify_provider_reasoning(
        {"reasoning": "   ", "content": "42"}, {}
    )
    assert obs.provenance == "UNAVAILABLE"
    assert obs.text_chars == 0


# --- Offline path -------------------------------------------------------------


def test_mock_is_a_valid_target_and_needs_no_key(monkeypatch, capsys) -> None:
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["provider_probe.py", "--provider", "mock"])
    assert provider_probe.main() == 0
    out = capsys.readouterr().out
    assert "structured_json_auditor" in out
    assert "SIMULATED" in out


def test_mock_results_are_labelled_simulated() -> None:
    results = provider_probe.run_mock_probes("mock", "mock-model")
    assert [r.probe_name for r in results] == [
        "basic_reasoning",
        "tool_calling_with_reasoning",
        "structured_json_auditor",
    ]
    assert all(r.status == "SUCCESS" for r in results)
    assert all(r.simulated for r in results)


def test_live_run_reports_finish_reason_and_reasoning_tokens(monkeypatch) -> None:
    """`finish_reason: length` plus a full reasoning-token spend is the whole
    explanation for the auditor failure, so it must survive into the report."""
    sent = {}

    def fake_request(url, api_key, payload, extra_headers=None, timeout=45.0):
        sent.update(payload)
        return {
            "model": "served-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": "length",
                }
            ],
            "usage": {
                "completion_tokens": 256,
                "completion_tokens_details": {"reasoning_tokens": 256},
            },
        }

    monkeypatch.setattr(provider_probe, "make_request", fake_request)
    profile = provider_probe.resolve_target("nebius")
    results = provider_probe.run_live_probes(
        provider="nebius",
        profile=profile,
        api_key="not-a-real-key",
    )
    auditor = next(r for r in results if r.probe_name == "structured_json_auditor")
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert auditor.status == "MALFORMED_OUTPUT"
    assert auditor.finish_reason == "length"
    assert auditor.reasoning_tokens == 256
    assert auditor.reasoning_provenance == "UNAVAILABLE"
    assert not auditor.simulated
