"""Measured provider counts must agree with budget charges and audit evidence."""

import json

import httpx
import pytest

from scopewatch import budget_store
from scopewatch.budget_store import TokenLedger, total_tokens_from_usage
from scopewatch.providers.client import ProviderClient
from scopewatch.providers.profile import ProviderProfile
from scopewatch.reasoning_audit import ReasoningAuditor
from scopewatch.schemas import TaskScope


CASES = [
    ({"total_tokens": "10000"}, 10000),
    ({"total_tokens": " 10000 "}, 10000),
    ({"total_tokens": True, "prompt_tokens": 6000, "completion_tokens": 4000}, 10000),
    ({"total_tokens": -1, "prompt_tokens": "6000", "completion_tokens": "4000"}, 10000),
    ({"prompt_tokens": 6000, "completion_tokens": 4000}, 10000),
    ({"total_tokens": 10000.0}, 10000),
    ({"total_tokens": 0, "prompt_tokens": 6000, "completion_tokens": 4000}, 0),
    ({"total_tokens": "0"}, 0),
    ({"total_tokens": True}, None),
    ({"total_tokens": -1}, None),
    ({"total_tokens": 1.9}, None),
    ({"total_tokens": float("inf")}, None),
    ({"total_tokens": float("nan")}, None),
    ({"total_tokens": "1.5"}, None),
    ({"total_tokens": "+10000"}, None),
    ({"total_tokens": "1_000"}, None),
    ({"total_tokens": "1e4"}, None),
    ({"total_tokens": "١٠٠٠٠"}, None),
    ({"prompt_tokens": -2, "completion_tokens": 10}, None),
    ({"prompt_tokens": 2.9, "completion_tokens": 10}, None),
    ({"prompt_tokens": True, "completion_tokens": 10}, None),
    ({"prompt_tokens": 6000}, None),
    ({"completion_tokens": 4000}, None),
    ({}, None),
    (None, None),
]


@pytest.mark.parametrize("usage,expected", CASES)
def test_usage_metering_normalizes_without_truncation_or_boolean_counts(usage, expected):
    assert total_tokens_from_usage(usage) == (expected if expected is not None else 0)


@pytest.mark.parametrize("usage,expected", CASES)
def test_real_provider_audit_evidence_agrees_with_metering(monkeypatch, usage, expected):
    ledger = TokenLedger()
    monkeypatch.setattr(budget_store, "_ledger", ledger)
    profile = ProviderProfile(name="synthetic-usage", base_url="https://provider.example.test/v1", model="synthetic-model", max_retries=0)
    content = json.dumps({"verdict": "NO_CONCERN", "concern_type": None,
                          "flagged_excerpts": [], "explanation": "Synthetic clean result"})
    # Encode manually so nonfinite JSON values reach the real client's parser.
    body = json.dumps({"choices": [{"message": {"content": content}}], "usage": usage})
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body, headers={"content-type": "application/json"}))
    with ProviderClient(profile, transport=transport) as client:
        auditor = ReasoningAuditor(provider=client, profile=profile.name, model=profile.model)
        result = auditor.audit_turn(task_scope=TaskScope(
            task_description="Read a synthetic output", allowed_paths=["outputs"],
            allowed_tools=["workspace"], allowed_operations=["read_text"],
            created_at="2026-11-05T00:00:00Z"), turn_id="synthetic-turn",
            reasoning_text="I will read the permitted output only.", reasoning_provenance="SYNTHETIC_FIXTURE")
    assert result.verdict == "NO_CONCERN"
    assert result.total_tokens == expected
    assert ledger.used_today(profile.name) == (expected if expected is not None else 0)
    if expected and expected >= 8000:
        assert not ledger.reserve(profile.name, estimate=8000, budget=8000)[0].allowed
