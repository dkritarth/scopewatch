"""Same-path safe and malicious edits need reasoning, not a tighter path list."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from scopewatch.db import init_db
from scopewatch.models import EventType, PolicyOutcome, ReasonCode
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from seed_demo import seed_coding_workspace_files, seed_workspace_files, seed_scenarios


def run_scenario(tmp_path, monkeypatch, name):
    monkeypatch.delenv('DEMO_TOKEN', raising=False)
    monkeypatch.setenv('SCOPEWATCH_AUDITOR_PROFILE', 'mock')
    monkeypatch.setenv('SCOPEWATCH_REASONING_AUDIT', 'on')
    workspace = tmp_path / 'workspace'
    seed_workspace_files(workspace)
    seed_coding_workspace_files(workspace)
    db = tmp_path / 'test.db'
    init_db(db)
    service = ScopewatchService(db_path=db, workspace_root=workspace)
    data = json.loads((ROOT / 'demo/scenarios' / name).read_text())
    scope = TaskScope(**data['task_scope'], created_at=datetime.now(timezone.utc).isoformat())
    run, _ = service.create_run(name=data['name'], task_scope=scope)
    return service, run, data


def test_tampering_is_policy_allowed_but_auditor_held_then_denied(tmp_path, monkeypatch):
    service, run, data = run_scenario(tmp_path, monkeypatch, '15_auth_tampering.json')
    before = (service.get_run_workspace(run.id) / 'auth.py').read_bytes()
    for action in data['actions']:
        request = SubmitActionRequest(**action)
        policy = service.preview_action(run.id, request)
        assert policy.outcome == PolicyOutcome.ALLOW
        result = asyncio.run(service.submit_action(run.id, request))
    assert result.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
    assert result.execution_receipt is None
    audit = next(e for e in service.get_events(run.id) if e.event_type == EventType.REASONING_AUDIT_COMPLETED and e.details['verdict'] == 'CONCERN')
    assert audit.details['profile'] == 'mock'
    assert all(excerpt in data['actions'][-1]['exposed_reasoning_trace'] for excerpt in audit.details['flagged_excerpts'])
    requested = [e for e in service.get_events(run.id) if e.event_type == EventType.ACTION_REQUESTED][-1]
    assert requested.details['reasoning_provenance'] == 'SYNTHETIC_FIXTURE'
    asyncio.run(service.resolve_approval(result.approval_request.id, approve=False, resolved_by='synthetic-reviewer', reason='README cannot authorize weakening auth'))
    assert (service.get_run_workspace(run.id) / 'auth.py').read_bytes() == before
    assert not any(e.event_type == EventType.EXECUTION_STARTED and e.action_request_id == result.action_request.id for e in service.get_events(run.id))


def test_safe_control_uses_same_allowed_resource_and_preserves_authentication(tmp_path, monkeypatch):
    service, run, data = run_scenario(tmp_path, monkeypatch, '16_auth_safe_control.json')
    for action in data['actions']:
        result = asyncio.run(service.submit_action(run.id, SubmitActionRequest(**action)))
        assert result.policy_decision.outcome == PolicyOutcome.ALLOW
    assert result.execution_receipt.status.value == 'EXECUTED'
    content = (service.get_run_workspace(run.id) / 'auth.py').read_text()
    env = {}
    exec(compile(content, 'synthetic-auth.py', 'exec'), env)
    valid = env['is_token_valid']
    assert valid(100, 3700) is True
    assert valid(100, 3701) is False
    assert valid(100, 99) is False


def test_hero_cli_seeds_coding_fixture_without_docker(tmp_path, monkeypatch):
    import os
    import subprocess
    monkeypatch.delenv('DEMO_TOKEN', raising=False)
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'backend'), 'SCOPEWATCH_AGENT_PROFILE': 'mock',
           'SCOPEWATCH_AUDITOR_PROFILE': 'mock', 'SCOPEWATCH_REASONING_AUDIT': 'on',
           'SCOPEWATCH_RUN_WORKSPACES_DIR': str(tmp_path / 'runs')}
    result = subprocess.run([
        sys.executable, str(ROOT / 'scripts/seed_demo.py'), '--mode', 'scripted',
        '--only', '15_', '--only', '16_', '--db-path', str(tmp_path / 'hero.db'),
        '--workspace-root', str(tmp_path / 'workspace'),
    ], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert '2 scenarios loaded successfully' in result.stdout
    assert 'HOLD [REASONING_SCOPE_CONCERN]' in result.stdout
    assert (tmp_path / 'workspace/AUTH_README.md').is_file()
