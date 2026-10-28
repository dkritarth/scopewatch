"""Synthetic gateway regressions for optional two-tier auditing (#292)."""
import asyncio
import json
import time

import httpx

import pytest

from scopewatch.db import init_db
from scopewatch.providers.client import ProviderClient
from scopewatch.providers.profile import ProviderProfile
from scopewatch.reasoning_audit import ReasoningAuditor
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService

CLEAN = {"verdict": "NO_CONCERN", "concern_type": None,
         "flagged_excerpts": [], "explanation": "Within scope."}
CONCERN = {"verdict": "CONCERN", "concern_type": "SCOPE_DRIFT",
           "flagged_excerpts": ["unrelated"], "explanation": "Unrelated work."}


def bounded_client(profile, clients):
    client = clients[profile.name]
    client.profile = profile
    return client


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_ROUTING", "two-tier")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_FAST_PROFILE", "fast")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_STRONG_PROFILE", "strong")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "allowed.txt").write_text("Synthetic content")
    db = tmp_path / "gateway.db"
    init_db(db)
    service = ScopewatchService(db_path=db, workspace_root=workspace)
    run, _ = service.create_run(name="Synthetic tier test", task_scope=TaskScope(
        schema_version="1", task_description="Read allowed.txt", allowed_paths=["allowed.txt"],
        blocked_paths=["private.txt"], allowed_tools=["workspace"],
        allowed_operations=["read_text"], created_at="2026-10-27T00:00:00Z"))
    clients = {}

    def configure(fast, strong):
        for name, content in (("fast", fast), ("strong", strong)):
            profile = ProviderProfile(name=name, base_url="mock://test", model=f"synthetic-{name}")
            history = []

            def handler(request, content=content, history=history, model=profile.model):
                history.append(request)
                if isinstance(content, Exception):
                    raise content
                return httpx.Response(200, json={
                    "model": model, "choices": [{"message": {"content": json.dumps(content)}}],
                    "usage": {"total_tokens": 17}})

            client = ProviderClient(profile, transport=httpx.MockTransport(handler))
            client.call_history = history
            clients[name] = client
        monkeypatch.setattr("scopewatch.tiered_audit.get_profile", lambda name: clients[name].profile)
        monkeypatch.setattr("scopewatch.tiered_audit.ProviderClient", lambda profile: bounded_client(profile, clients))

    def submit(resource="allowed.txt", turn="turn-1"):
        return asyncio.run(service.submit_action(run.id, SubmitActionRequest(
            tool="workspace", operation="read_text", resource=resource,
            exposed_reasoning_trace="I will read allowed.txt and do unrelated work.",
            reasoning_provenance="SYNTHETIC_FIXTURE", turn_id=turn)))
    return service, run, clients, configure, submit


@pytest.mark.parametrize("fast,verdict", [(CONCERN, "CONCERN"), ({"verdict": "AMBIGUOUS"}, "FAILED")])
def test_strong_clean_cannot_downgrade_screen(gateway, fast, verdict):
    service, run, clients, configure, submit = gateway
    configure(fast, CLEAN)
    response = submit()
    assert response.policy_decision.outcome.value == "HOLD"
    audit = response.reasoning_audit
    assert audit.verdict == verdict
    assert [tier.tier for tier in audit.tiers] == ["fast_screen", "strong_review"]
    assert [tier.total_tokens for tier in audit.tiers] == [17, 17]
    assert audit.total_tokens == 34
    assert len(clients["strong"].call_history) == 1
    stored = service.get_action(run.id, response.action_request.id).reasoning_audit
    assert stored == audit
    event = next(e for e in response.events if e.event_type.value.startswith("REASONING_AUDIT_"))
    assert event.details["tiers"] == [tier.model_dump(mode="json") for tier in audit.tiers]
    assert response.execution_receipt is None
    assert not any(e.event_type.value.startswith("EXECUTION_") for e in response.events)


def test_clean_screen_skips_strong_and_policy_deny_skips_both(gateway):
    _, _, clients, configure, submit = gateway
    configure(CLEAN, CONCERN)
    response = submit()
    assert response.policy_decision.outcome.value == "ALLOW"
    assert len(response.reasoning_audit.tiers) == 1
    denied = submit("private.txt", "turn-deny")
    assert denied.policy_decision.outcome.value == "DENY"
    assert denied.reasoning_audit is None
    assert len(clients["fast"].call_history) == 1
    assert not clients["strong"].call_history


@pytest.mark.parametrize("strong", [CONCERN, {"verdict": "MAYBE"}, httpx.ReadTimeout("synthetic")])
def test_strong_concern_or_failure_keeps_hold(gateway, strong):
    _, _, clients, configure, submit = gateway
    configure(CONCERN, strong)
    response = submit()
    assert response.policy_decision.outcome.value == "HOLD"
    assert response.reasoning_audit.tiers[1].verdict in ("CONCERN", "FAILED")
    assert len(clients["strong"].call_history) == 1


def test_tier_request_caps_override_profile_options(gateway, monkeypatch):
    _, _, clients, configure, submit = gateway
    configure(CONCERN, CLEAN)
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_FAST_MAX_TOKENS", "61")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_STRONG_MAX_TOKENS", "123")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_FAST_TIMEOUT_S", "0.8")
    clients["fast"].profile.auditor_body = {"max_tokens": 99999, "max_completion_tokens": 88888}
    clients["fast"].profile.extra_body = {"max_completion_tokens": 99999}
    response = submit()
    for name, cap in (("fast", 61), ("strong", 123)):
        request = clients[name].call_history[0]
        body = json.loads(request.content)
        assert body["max_tokens"] == cap
        assert "max_completion_tokens" not in body
        assert clients[name].profile.max_retries == 0
    assert clients["fast"].call_history[0].extensions["timeout"]["read"] <= 0.8
    assert response.reasoning_audit.tiers[0].max_tokens == 61
    assert response.reasoning_audit.tiers[0].timeout_s == 0.8


@pytest.mark.parametrize("name,value", [
    ("SCOPEWATCH_AUDITOR_ROUTING", "typo"),
    ("SCOPEWATCH_AUDITOR_FAST_PROFILE", ""),
    ("SCOPEWATCH_AUDITOR_STRONG_TIMEOUT_S", "nan"),
    ("SCOPEWATCH_AUDITOR_FAST_TIMEOUT_S", "inf"),
    ("SCOPEWATCH_AUDITOR_FAST_TIMEOUT_S", "121"),
    ("SCOPEWATCH_AUDITOR_STRONG_MAX_TOKENS", "0"),
    ("SCOPEWATCH_AUDITOR_STRONG_MAX_TOKENS", "16385"),
])
def test_invalid_tier_configuration_fails_closed_before_spend(gateway, monkeypatch, name, value):
    _, _, clients, configure, submit = gateway
    configure(CLEAN, CLEAN)
    monkeypatch.setenv(name, value)
    response = submit()
    assert response.policy_decision.outcome.value == "HOLD"
    assert response.reasoning_audit.error_code == "INVALID_TIER_CONFIG"
    assert not clients["fast"].call_history
    assert not clients["strong"].call_history


def test_screen_timeout_escalates_without_inventing_usage(gateway, monkeypatch):
    _, _, clients, configure, submit = gateway
    configure(CLEAN, CLEAN)
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_FAST_TIMEOUT_S", "0.01")
    original = clients["fast"].audit_chat

    def slow(*args, **kwargs):
        time.sleep(0.04)
        return original(*args, **kwargs)

    clients["fast"].audit_chat = slow
    response = submit()
    assert response.policy_decision.outcome.value == "HOLD"
    audit = response.reasoning_audit
    assert audit.tiers[0].error_code == "AUDIT_TIMEOUT"
    assert audit.tiers[0].attempted is True
    assert audit.tiers[0].total_tokens is None
    assert audit.total_tokens is None
    assert len(clients["strong"].call_history) == 1


def test_budget_refusal_does_not_call_or_downgrade_screen(gateway, monkeypatch):
    _, _, clients, configure, submit = gateway
    configure(CLEAN, CLEAN)
    clients["fast"].profile.base_url = "https://synthetic.invalid"
    monkeypatch.setenv("SCOPEWATCH_DAILY_TOKEN_BUDGET_FAST", "0")
    response = submit()
    audit = response.reasoning_audit
    assert response.policy_decision.outcome.value == "HOLD"
    assert audit.tiers[0].error_code == "TOKEN_BUDGET_EXCEEDED"
    assert audit.tiers[0].attempted is False
    assert audit.tiers[0].total_tokens == 0
    assert not clients["fast"].call_history
    assert len(clients["strong"].call_history) == 1


def test_cache_reuses_evidence_without_new_tier_calls(gateway):
    _, _, clients, configure, submit = gateway
    configure(CONCERN, CLEAN)
    first, second = submit(), submit()
    assert second.reasoning_audit == first.reasoning_audit
    assert all(len(client.call_history) == 1 for client in clients.values())


def test_default_routing_keeps_single_auditor(gateway, monkeypatch):
    service, _, clients, configure, submit = gateway
    configure(CLEAN, CLEAN)
    monkeypatch.delenv("SCOPEWATCH_AUDITOR_ROUTING")
    service._auditor = ReasoningAuditor(profile="mock")
    response = submit()
    assert response.reasoning_audit.verdict == "CONCERN"
    assert response.reasoning_audit.profile == "mock"
    assert response.reasoning_audit.tiers == []
    assert not clients["fast"].call_history
    assert not clients["strong"].call_history


def test_missing_screen_profile_still_reviews_and_fails_closed(gateway, monkeypatch):
    _, _, clients, configure, submit = gateway
    configure(CLEAN, CLEAN)
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_FAST_PROFILE", "missing")
    response = submit()
    audit = response.reasoning_audit
    assert response.policy_decision.outcome.value == "HOLD"
    assert audit.tiers[0].attempted is False
    assert audit.tiers[0].model == "unknown"
    assert audit.tiers[0].profile == "missing"
    assert audit.tiers[0].total_tokens == 0
    assert len(clients["strong"].call_history) == 1


def test_strong_budget_is_checked_under_strong_profile(gateway, monkeypatch):
    _, _, clients, configure, submit = gateway
    configure(CONCERN, CLEAN)
    clients["strong"].profile.base_url = "https://synthetic.invalid"
    monkeypatch.setenv("SCOPEWATCH_DAILY_TOKEN_BUDGET_STRONG", "0")
    response = submit()
    assert response.policy_decision.outcome.value == "HOLD"
    assert response.reasoning_audit.tiers[1].error_code == "TOKEN_BUDGET_EXCEEDED"
    assert not clients["strong"].call_history


def test_existing_audit_rows_survive_idempotent_migration(gateway, tmp_path):
    from scopewatch.db import get_connection
    from scopewatch.repository import ScopewatchRepository

    _, run, _, _, _ = gateway
    db = tmp_path / "gateway.db"
    with get_connection(db) as conn:
        conn.execute("ALTER TABLE reasoning_audits DROP COLUMN tier_evidence_json")
        conn.execute("ALTER TABLE reasoning_audits DROP COLUMN total_tokens")
        conn.execute("""INSERT INTO reasoning_audits
            (id, run_id, turn_id, trace_hash, verdict, flagged_excerpts_json,
             explanation, model, profile, latency_ms, audited_at)
            VALUES (?, ?, 'old-turn', 'hash', 'NO_CONCERN', '[]', 'Synthetic legacy row',
                    'synthetic-legacy', 'mock', 1, '2026-10-27T00:00:00Z')""", ("legacy-audit", run.id))
    init_db(db)
    init_db(db)
    with get_connection(db) as conn:
        audit = ScopewatchRepository.get_reasoning_audit(conn, "legacy-audit")
    assert audit.verdict == "NO_CONCERN"
    assert audit.tiers == []
    assert audit.total_tokens is None


def test_reported_model_and_configured_model_remain_distinct(gateway):
    _, _, clients, configure, submit = gateway
    configure(CONCERN, CLEAN)
    # The synthetic response still reports synthetic-fast.
    clients["fast"].profile.model = "synthetic-configured-alias"
    response = submit()
    tier = response.reasoning_audit.tiers[0]
    assert tier.configured_model == "synthetic-configured-alias"
    assert tier.model == "synthetic-fast"
    assert tier.profile == "fast"
    assert tier.latency_ms > 0


@pytest.mark.parametrize("option_field", ["auditor_body", "extra_body"])
def test_tier_requests_only_one_completion(gateway, option_field):
    _, _, clients, configure, submit = gateway
    configure(CLEAN, CLEAN)
    setattr(clients["fast"].profile, option_field, {"n": 4, "best_of": 4})
    response = submit()
    assert response.policy_decision.outcome.value == "ALLOW"
    body = json.loads(clients["fast"].call_history[0].content)
    assert body["n"] == 1
    assert "best_of" not in body


@pytest.mark.parametrize("construction_fails", [False, True])
def test_aliased_profile_reservation_and_evidence(tmp_path, monkeypatch, construction_fails):
    from scopewatch.budget_store import TokenLedger
    from scopewatch.reasoning_audit import ReasoningAuditResult, ReasoningAuditVerdict
    from scopewatch.tiered_audit import run_tiered_audit

    config = tmp_path / "providers.toml"
    config.write_text('''[profiles.fast]
name = "canonical"
base_url = "https://synthetic.invalid"
model = "synthetic-fast"
[profiles.strong]
base_url = "mock://strong"
model = "synthetic-strong"
''')
    monkeypatch.setenv("SCOPEWATCH_PROVIDERS_CONFIG", str(config))
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_ROUTING", "two-tier")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_FAST_PROFILE", "fast")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_STRONG_PROFILE", "strong")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_FAST_MAX_TOKENS", "8")
    monkeypatch.setenv("SCOPEWATCH_TOKEN_RESERVE_PER_AUDIT", "8")
    ledger = TokenLedger()

    class Client:
        def __init__(self, profile):
            if profile.name == "canonical":
                assert ledger.used_today("canonical") > 0
                if construction_fails:
                    raise RuntimeError("Synthetic construction failure")

        def close(self):
            pass

    monkeypatch.setattr("scopewatch.tiered_audit.ProviderClient", Client)

    async def run(auditor):
        if auditor.profile == "canonical":
            ledger.add("canonical", 1)
        return ReasoningAuditResult(verdict=ReasoningAuditVerdict.NO_CONCERN,
                                   profile=auditor.profile, model=auditor.model,
                                   explanation="Synthetic clean response.", total_tokens=1)

    result = asyncio.run(run_tiered_audit(run, ledger))
    assert ledger.used_today("canonical") == (0 if construction_fails else 1)
    assert ledger.used_today("fast") == 0
    assert result.tiers[0].profile == "fast"
    assert result.tiers[0].resolved_profile == "canonical"
    if construction_fails:
        assert result.verdict == ReasoningAuditVerdict.FAILED
        assert len(result.tiers) == 2
    else:
        assert result.verdict == ReasoningAuditVerdict.NO_CONCERN
