"""Configured aliases must use the same token counter as provider completions."""

import asyncio
import json

import httpx
import pytest

from scopewatch.budget_store import TokenLedger
from scopewatch.db import init_db
from scopewatch.demo_guards import DemoGuardRefusal
from scopewatch.providers.client import ProviderClient
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService


CANONICAL = '''[profiles.canonical]
base_url = "https://synthetic.invalid"
model = "synthetic-canonical"
'''


@pytest.fixture(params=["alias", "canonical"])
def identity_gateway(tmp_path, monkeypatch, request):
    configuration = tmp_path / "providers.toml"
    configuration.write_text('''[profiles.alias]
name = "canonical"
base_url = "https://synthetic.invalid"
model = "synthetic-canonical"
''' + CANONICAL + '''[profiles.mock]
base_url = "mock://test"
model = "synthetic-mock"
''', encoding="utf-8")
    monkeypatch.setenv("SCOPEWATCH_PROVIDERS_CONFIG", str(configuration))
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "mock")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_PROFILE", request.param)
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_ROUTING", "single")
    monkeypatch.setenv("SCOPEWATCH_DAILY_TOKEN_BUDGET_CANONICAL", "100")
    monkeypatch.setenv("SCOPEWATCH_TOKEN_RESERVE_PER_AUDIT", "8")
    ledger = TokenLedger()
    monkeypatch.setattr("scopewatch.budget_store._ledger", ledger)
    calls = []
    clients = []

    def handler(request):
        calls.append((ledger.used_today("canonical"), ledger.used_today("alias")))
        return httpx.Response(200, json={"model": "synthetic-canonical",
            "choices": [{"message": {"content": json.dumps({
                "verdict": "NO_CONCERN", "concern_type": None,
                "flagged_excerpts": [], "explanation": "Within scope.",
            })}}], "usage": {"total_tokens": 7}})

    def client_factory(profile):
        client = ProviderClient(profile, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr("scopewatch.providers.client.ProviderClient", client_factory)
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("synthetic content", encoding="utf-8")
    service = ScopewatchService(db_path=database, workspace_root=workspace)
    scope = TaskScope(schema_version="1", task_description="Read input.txt",
        allowed_paths=["input.txt"], allowed_tools=["workspace"],
        allowed_operations=["read_text"], created_at="2026-11-05T00:00:00Z")

    def submit(run):
        return asyncio.run(service.submit_action(run.id, SubmitActionRequest(
            tool="workspace", operation="read_text", resource="input.txt",
            exposed_reasoning_trace="I will read the permitted input.txt.",
            reasoning_provenance="SYNTHETIC_FIXTURE",
        )))

    yield service, scope, ledger, calls, submit, configuration, request.param
    for client in clients:
        client.close()


def test_audit_refuses_exhausted_resolved_profile_before_spend(identity_gateway):
    service, scope, ledger, calls, submit, _, _ = identity_gateway
    ledger.add("canonical", 100)
    run, _ = service.create_run(name="Synthetic exhausted auditor", task_scope=scope)
    with pytest.raises(DemoGuardRefusal) as error:
        submit(run)
    assert error.value.code == "token_budget_exhausted"
    assert calls == []
    assert ledger.used_today("canonical") == 100
    assert ledger.used_today("alias") == 0


def test_audit_reservation_and_actuals_share_resolved_profile(identity_gateway):
    service, scope, ledger, calls, submit, _, _ = identity_gateway
    ledger.add("canonical", 10)
    run, _ = service.create_run(name="Synthetic allowed auditor", task_scope=scope)
    response = submit(run)
    assert response.reasoning_audit.profile == "canonical"
    assert calls == [(18, 0)]
    assert ledger.used_today("canonical") == 17
    assert ledger.used_today("alias") == 0


def test_agent_creation_checks_resolved_budget(identity_gateway, monkeypatch):
    service, scope, ledger, calls, _, _, configured = identity_gateway
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", configured)
    ledger.add("canonical", 100)
    with pytest.raises(DemoGuardRefusal) as error:
        service.create_run(name="Synthetic exhausted agent", task_scope=scope)
    assert error.value.code == "token_budget_exhausted"
    assert calls == []
    assert service.list_runs() == []


def test_agent_snapshot_names_requested_profile_and_budget_counter(identity_gateway, monkeypatch):
    service, scope, ledger, _, _, _, configured = identity_gateway
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", configured)
    ledger.add("canonical", 10)
    _, events = service.create_run(name="Synthetic agent snapshot", task_scope=scope)
    snapshot = events[0].details["token_budget"]
    assert snapshot["agent_profile"] == configured
    assert snapshot["budget_profile"] == "canonical"
    assert (snapshot["used_today"], snapshot["budget"]) == (10, 100)


def test_missing_canonical_configuration_still_fails_closed(identity_gateway):
    service, scope, ledger, calls, submit, configuration, _ = identity_gateway
    configuration.write_text(configuration.read_text().replace(CANONICAL, ""), encoding="utf-8")
    run, _ = service.create_run(name="Synthetic unresolved client", task_scope=scope)
    response = submit(run)
    assert response.policy_decision.outcome.value == "HOLD"
    assert response.reasoning_audit.error_code == "PROFILE_NOT_FOUND"
    assert ledger.used_today("canonical") == 0
    assert calls == []


def test_mock_url_does_not_spend_or_refuse_canonical_budget(identity_gateway):
    service, scope, ledger, calls, submit, configuration, _ = identity_gateway
    configuration.write_text(configuration.read_text().replace("https://synthetic.invalid", "mock://synthetic"), encoding="utf-8")
    ledger.add("canonical", 100)
    run, _ = service.create_run(name="Synthetic scripted auditor", task_scope=scope)
    response = submit(run)
    assert response.policy_decision.outcome.value == "ALLOW"
    assert len(calls) == 1
    assert ledger.used_today("canonical") == 100
    assert ledger.used_today("alias") == 0


@pytest.mark.parametrize("canonical_mock", [False, True])
def test_auditor_budget_uses_actual_client_url(identity_gateway, canonical_mock):
    service, scope, ledger, calls, submit, configuration, _ = identity_gateway
    text = configuration.read_text()
    if canonical_mock:
        text = text.replace(CANONICAL, CANONICAL.replace("https://synthetic.invalid", "mock://synthetic"))
    else:
        text = text.replace("https://synthetic.invalid", "mock://synthetic", 1)
    configuration.write_text(text, encoding="utf-8")
    ledger.add("canonical", 100)
    run, _ = service.create_run(name="Synthetic divergent URL", task_scope=scope)
    if canonical_mock:
        assert submit(run).policy_decision.outcome.value == "ALLOW"
        assert len(calls) == 1
    else:
        with pytest.raises(DemoGuardRefusal):
            submit(run)
        assert calls == []
    assert ledger.used_today("canonical") == 100


def test_unexpected_profile_loader_failure_remains_failed_hold(identity_gateway):
    service, scope, ledger, calls, submit, configuration, _ = identity_gateway
    run, _ = service.create_run(name="Synthetic malformed profile table", task_scope=scope)
    configuration.write_text("profiles = []", encoding="utf-8")
    response = submit(run)
    assert response.policy_decision.outcome.value == "HOLD"
    assert response.reasoning_audit.verdict == "FAILED"
    assert calls == []
    assert ledger.used_today("canonical") == ledger.used_today("alias") == 0


@pytest.mark.parametrize("identity_gateway", ["mock"], indirect=True)
def test_mock_named_alias_cannot_hide_live_canonical_usage(identity_gateway):
    service, scope, ledger, calls, submit, configuration, _ = identity_gateway
    run, _ = service.create_run(name="Synthetic mock-named alias", task_scope=scope)
    configuration.write_text(configuration.read_text().replace(
        '[profiles.mock]\nbase_url = "mock://test"',
        '[profiles.mock]\nname = "canonical"\nbase_url = "https://synthetic.invalid"',
    ), encoding="utf-8")
    ledger.add("canonical", 100)
    with pytest.raises(DemoGuardRefusal):
        submit(run)
    assert calls == []
    assert ledger.used_today("canonical") == 100


@pytest.mark.parametrize("identity_gateway", ["mock"], indirect=True)
def test_injected_client_uses_its_actual_profile_budget(identity_gateway):
    from scopewatch.providers.client import ProviderClient as configured_client
    from scopewatch.providers.loader import get_profile
    from scopewatch.reasoning_audit import ReasoningAuditor

    service, scope, ledger, calls, submit, _, _ = identity_gateway
    run, _ = service.create_run(name="Synthetic injected client", task_scope=scope)
    client = configured_client(get_profile("canonical"))
    service._auditor = ReasoningAuditor(provider=client, profile="canonical", model=client.profile.model)
    ledger.add("canonical", 100)
    with pytest.raises(DemoGuardRefusal):
        submit(run)
    assert calls == []
    assert ledger.used_today("canonical") == 100
