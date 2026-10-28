"""Synthetic judge recovery through the public gateway boundary (#290)."""
from fastapi.testclient import TestClient

from scopewatch.app import create_app
from scopewatch.db import init_db


def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv('SCOPEWATCH_ALLOW_RESET', 'true')
    monkeypatch.setenv('SCOPEWATCH_REVIEWER_TOKEN', 'synthetic-reviewer-only')
    monkeypatch.setenv('SCOPEWATCH_API_KEY', 'synthetic-agent-only')
    monkeypatch.setenv('SCOPEWATCH_AUDITOR_PROFILE', 'mock')
    monkeypatch.setenv('SCOPEWATCH_RUN_WORKSPACES_ROOT', str(tmp_path / 'runs'))
    db = tmp_path / 'demo.db'
    init_db(db)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    return TestClient(create_app(db_path=db, workspace_root=workspace))


REVIEWER = {'X-Reviewer-Token': 'synthetic-reviewer-only'}


def test_stranger_reseed_deny_reset_and_explicit_gateway_marker(tmp_path, monkeypatch):
    client = gateway(tmp_path, monkeypatch)
    assert 'name="scopewatch-gateway" content="live"' in client.get('/').text
    health = client.get('/api/v1/health').json()
    assert health['demo_reset_enabled'] is True
    assert health['approval_ttl_seconds'] > 0
    assert client.post('/api/v1/demo/reseed').status_code in (401, 403)
    assert client.post('/api/v1/demo/reseed', headers={'X-API-Key': 'synthetic-agent-only'}).status_code == 403
    seeded = client.post('/api/v1/demo/reseed', headers=REVIEWER)
    assert seeded.status_code == 200, seeded.text
    run_id = seeded.json()['run_id']
    approvals = client.get('/api/v1/approvals').json()
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval['status'] == 'PENDING'
    assert client.post('/api/v1/demo/reset', headers=REVIEWER).status_code == 409
    assert len(client.get('/api/v1/runs').json()) == 1
    assert client.post('/api/v1/demo/reseed', headers=REVIEWER).status_code == 409
    denied = client.post(f"/api/v1/approvals/{approval['id']}/deny", headers=REVIEWER, json={'resolution_reason': 'Reject injected invoice instruction'})
    assert denied.status_code == 200, denied.text
    events = client.get(f'/api/v1/runs/{run_id}/events').json()
    assert any(e['event_type'] == 'APPROVAL_DENIED' for e in events)
    assert not any(e['event_type'].startswith('EXECUTION_') for e in events)
    assert not list((tmp_path / 'runs').rglob('partner-payment-details.txt'))
    assert client.post('/api/v1/demo/reset', headers=REVIEWER).status_code == 200
    assert client.get('/api/v1/runs').json() == []
    assert client.post('/api/v1/demo/reseed', headers=REVIEWER).status_code == 200


def test_disabled_reseed_preserves_empty_gateway(tmp_path, monkeypatch):
    client = gateway(tmp_path, monkeypatch)
    monkeypatch.setenv('SCOPEWATCH_ALLOW_RESET', 'false')
    assert client.post('/api/v1/demo/reseed', headers=REVIEWER).status_code == 403
    assert client.get('/api/v1/runs').json() == []


def test_reseed_stays_held_when_auditing_is_disabled(tmp_path, monkeypatch):
    client = gateway(tmp_path, monkeypatch)
    monkeypatch.setenv('SCOPEWATCH_REASONING_AUDIT', 'off')
    response = client.post('/api/v1/demo/reseed', headers=REVIEWER)
    assert response.status_code == 200
    approvals = client.get('/api/v1/approvals').json()
    assert len(approvals) == 1
    assert approvals[0]['status'] == 'PENDING'
    events = client.get(f"/api/v1/runs/{response.json()['run_id']}/events").json()
    assert not any(e['event_type'].startswith('EXECUTION_') for e in events)


def test_reseed_never_uses_the_configured_external_auditor(tmp_path, monkeypatch):
    client = gateway(tmp_path, monkeypatch)
    monkeypatch.setenv('SCOPEWATCH_AUDITOR_PROFILE', 'nebius-demo')
    # Any HTTP provider call would fail this test, even with an ambient key.
    import httpx
    def no_external_calls(*args, **kwargs):
        raise AssertionError('External model call attempted')
    monkeypatch.setattr(httpx.AsyncClient, 'send', no_external_calls)
    response = client.post('/api/v1/demo/reseed', headers=REVIEWER)
    assert response.status_code == 200
    events = client.get(f"/api/v1/runs/{response.json()['run_id']}/events").json()
    audits = [e for e in events if e['event_type'] == 'REASONING_AUDIT_COMPLETED']
    assert len(audits) == 1
    assert audits[0]['details']['profile'] == 'mock'
    assert audits[0]['details']['verdict'] == 'CONCERN'


def test_reset_reseed_retains_runtime_run_budget(tmp_path, monkeypatch):
    monkeypatch.setenv('DEMO_TOKEN', 'synthetic-demo-agent')
    monkeypatch.setenv('DEMO_MAX_RUNS_PER_DAY', '1')
    client = gateway(tmp_path, monkeypatch)
    assert client.post('/api/v1/demo/reseed', headers=REVIEWER).status_code == 200
    approval = client.get('/api/v1/approvals').json()[0]
    denied = client.post(f"/api/v1/approvals/{approval['id']}/deny", headers=REVIEWER, json={})
    assert denied.status_code == 200
    assert client.post('/api/v1/demo/reset', headers=REVIEWER).status_code == 200
    refused = client.post('/api/v1/demo/reseed', headers=REVIEWER)
    assert refused.status_code == 429
    assert client.get('/api/v1/runs').json() == []


def test_reset_reconciles_expiry_without_a_previous_approval_read(tmp_path, monkeypatch):
    monkeypatch.setattr('scopewatch.service.DEFAULT_EXPIRY_SECONDS', -1)
    client = gateway(tmp_path, monkeypatch)
    assert client.post('/api/v1/demo/reseed', headers=REVIEWER).status_code == 200
    assert client.post('/api/v1/demo/reset', headers=REVIEWER).status_code == 200
    assert client.get('/api/v1/runs').json() == []
    assert not list((tmp_path / 'runs').rglob('partner-payment-details.txt'))
