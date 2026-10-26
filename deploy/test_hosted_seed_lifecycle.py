"""Real offline seed regressions for the hosted startup and review boundary."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from scopewatch.app import create_app


def seed_environment(tmp_path):
    env = dict(os.environ)
    env.update(DEMO_TOKEN='synthetic-seed-token', DEMO_MAX_CONCURRENT_RUNS='5',
               DEMO_MAX_RUNS_PER_DAY='200', DEMO_MAX_ACTIONS_PER_DAY='5000',
               DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP='6', DEMO_MAX_TURNS_PER_RUN='40',
               SCOPEWATCH_AGENT_PROFILE='mock', SCOPEWATCH_AUDITOR_PROFILE='mock',
               SCOPEWATCH_EXECUTOR='local', SCOPEWATCH_RUN_WORKSPACES_DIR=str(tmp_path / 'runs'),
               SCOPEWATCH_REVIEWER_TOKEN='synthetic-reviewer-token')
    return env


def test_default_hosted_cap_preserves_two_holds_and_no_injected_output(tmp_path, monkeypatch):
    env = seed_environment(tmp_path)
    db = tmp_path / 'seed.db'
    workspace = tmp_path / 'workspace'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/seed_demo.py'),
        '--db-path', str(db), '--workspace-root', str(workspace), '--mode', 'scripted',
        '--complete-settled'], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT status, count(*) FROM runs GROUP BY status').fetchall() == [('COMPLETED', 4), ('WAITING_FOR_APPROVAL', 2)]
        assert conn.execute("SELECT count(*) FROM approval_requests WHERE status='PENDING'").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM evidence_events WHERE event_type='APPROVAL_GRANTED'").fetchone()[0] == 0
    assert not list((tmp_path / 'runs').rglob('partner-payment-details.txt'))
    for name, value in env.items():
        if name.startswith(('DEMO_', 'SCOPEWATCH_')):
            monkeypatch.setenv(name, value)
    with TestClient(create_app(db_path=db, workspace_root=workspace)) as client:
        # Runtime cap still applies: two seed holds leave exactly three slots.
        payload = {'name': 'Synthetic runtime', 'task_scope': {'task_description':'Synthetic runtime', 'created_at':'2026-10-25T00:00:00Z'}}
        responses = [client.post('/api/v1/runs', headers={'X-Demo-Token':'synthetic-seed-token'}, json=payload) for _ in range(4)]
        assert [r.status_code for r in responses] == [201,201,201,429]
        pending = client.get('/api/v1/approvals?status=PENDING').json()
        assert len(pending) == 2
        injection = next(a for a in pending if a['operation'] == 'write_text')
        denied = client.post(f"/api/v1/approvals/{injection['id']}/deny", json={}, headers={'X-Reviewer-Token':'synthetic-reviewer-token'})
        assert denied.status_code == 200, denied.text
        assert not list((tmp_path / 'runs').rglob('partner-payment-details.txt'))


def test_entrypoint_removes_autoapproval_and_restarts_without_reseeding(tmp_path):
    env = seed_environment(tmp_path)
    source = (ROOT / 'deploy/docker-entrypoint.sh').read_text()
    assert '--auto-approve' not in source
    assert '--complete-settled' in source
    data = tmp_path / 'data'
    data.mkdir()
    # Replace container mount paths only; execute the actual entrypoint twice.
    entrypoint = tmp_path / 'entrypoint.sh'
    source = source.replace('/data/', str(data) + '/')
    source = source.replace('python /app/scripts/seed_demo.py', f'"{sys.executable}" "{ROOT / "scripts/seed_demo.py"}"')
    source = source.replace("if ! python -c", f'if ! "{sys.executable}" -c')
    entrypoint.write_text(source)
    env.update(SCOPEWATCH_DB_PATH=str(data/'seed.db'), SCOPEWATCH_WORKSPACE_ROOT=str(tmp_path/'workspace'))
    for _ in range(2):
        result = subprocess.run(['bash',str(entrypoint),'true'],env=env,capture_output=True,text=True,timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(data/'seed.db') as conn:
        assert conn.execute('SELECT count(*) FROM runs').fetchone()[0] == 6
    assert (data/'.seeded-scripted-v2').exists()


def test_legacy_autoapproved_volume_is_not_silently_reseeded(tmp_path):
    env = seed_environment(tmp_path)
    source = (ROOT/'deploy/docker-entrypoint.sh').read_text().replace('/data/', str(tmp_path)+'/')
    source = source.replace("if ! python -c", f'if ! "{sys.executable}" -c')
    script = tmp_path/'entrypoint.sh'
    script.write_text(source)
    (tmp_path/'.seeded-scripted-v1').touch()
    result = subprocess.run(['bash',str(script),'true'],env=env,capture_output=True,text=True,timeout=10)
    assert result.returncode == 1
    assert 'legacy autoapproved seed detected' in result.stderr
    assert not (tmp_path/'.seeded-scripted-v2').exists()
