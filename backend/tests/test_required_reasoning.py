"""Required-reasoning scopes hold unauditable actions before any effect."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from scopewatch.db import init_db
from scopewatch.models import EventType, PolicyOutcome, ReasonCode
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.delenv('DEMO_TOKEN', raising=False)
    monkeypatch.setenv('SCOPEWATCH_AUDITOR_PROFILE', 'mock')
    monkeypatch.setenv('SCOPEWATCH_REASONING_AUDIT', 'on')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'auth.py').write_text('authenticated = True\n')
    db = tmp_path / 'test.db'
    init_db(db)
    return ScopewatchService(db_path=db, workspace_root=workspace)


def new_run(gateway, required=True):
    return gateway.create_run(name='Required reasoning', task_scope=TaskScope(
        task_description='Update auth.py without weakening authentication.',
        allowed_paths=['auth.py'], blocked_paths=['secret.txt'],
        allowed_tools=['workspace'], allowed_operations=['write_text', 'delete_path', 'read_text'],
        require_reasoning_for=['write_text', 'delete_path'] if required else [],
        created_at=datetime.now(timezone.utc).isoformat(),
    ))[0]


def test_missing_required_reasoning_holds_without_execution(gateway):
    run = new_run(gateway)
    result = asyncio.run(gateway.submit_action(run.id, SubmitActionRequest(
        tool='workspace', operation='write_text', resource='auth.py',
        arguments={'content': 'authenticated = False\n'},
    )))
    assert result.policy_decision.outcome == PolicyOutcome.HOLD
    assert result.policy_decision.reason_code == ReasonCode.REASONING_UNAVAILABLE
    assert result.approval_request is not None
    assert result.execution_receipt is None
    events = gateway.get_events(run.id)
    assert not any(event.event_type == EventType.EXECUTION_STARTED for event in events)
    held = next(event for event in events if event.event_type == EventType.POLICY_HELD)
    assert held.details['reasoning_requirement'] == 'missing_text'
    assert held.details['reasoning_provenance'] == 'UNAVAILABLE'


@pytest.mark.parametrize('evidence', [None, '', '   \n '])
def test_blank_reasoning_channels_are_missing(gateway, evidence):
    run = new_run(gateway)
    result = asyncio.run(gateway.submit_action(run.id, SubmitActionRequest(
        tool='workspace', operation='delete_path', resource='auth.py',
        exposed_reasoning_trace=evidence, reasoning_summary=evidence,
    )))
    assert result.policy_decision.reason_code == ReasonCode.REASONING_UNAVAILABLE
    assert result.execution_receipt is None


def test_disabled_auditor_cannot_bypass_required_review(gateway, monkeypatch):
    monkeypatch.setenv('SCOPEWATCH_REASONING_AUDIT', 'off')
    run = new_run(gateway)
    result = asyncio.run(gateway.submit_action(run.id, SubmitActionRequest(
        tool='workspace', operation='write_text', resource='auth.py',
        arguments={'content': 'safe edit'}, reasoning_summary='Correct token expiry.',
    )))
    assert result.policy_decision.reason_code == ReasonCode.REASONING_UNAVAILABLE
    assert result.policy_decision.reasoning_audit_id is None
    event = next(e for e in gateway.get_events(run.id) if e.event_type == EventType.POLICY_HELD)
    assert event.details['reasoning_requirement'] == 'audit_disabled'


def test_default_scope_keeps_legacy_no_reasoning_behavior(gateway):
    run = new_run(gateway, required=False)
    result = asyncio.run(gateway.submit_action(run.id, SubmitActionRequest(
        tool='workspace', operation='write_text', resource='auth.py',
        arguments={'content': 'safe edit'},
    )))
    assert result.policy_decision.outcome == PolicyOutcome.ALLOW
    assert result.execution_receipt.status.value == 'EXECUTED'


def test_policy_deny_is_final_even_if_required_evidence_missing(gateway):
    run = new_run(gateway)
    result = asyncio.run(gateway.submit_action(run.id, SubmitActionRequest(
        tool='workspace', operation='write_text', resource='secret.txt',
        arguments={'content': 'blocked'},
    )))
    assert result.policy_decision.outcome == PolicyOutcome.DENY
    assert result.policy_decision.reason_code == ReasonCode.BLOCKED_PATH
    assert result.approval_request is None


def test_nonrequired_read_does_not_need_reasoning(gateway):
    run = new_run(gateway)
    result = asyncio.run(gateway.submit_action(run.id, SubmitActionRequest(
        tool='workspace', operation='read_text', resource='auth.py',
    )))
    assert result.policy_decision.outcome == PolicyOutcome.ALLOW


def test_caller_summary_is_audited_but_not_promoted_to_provider_trace(gateway):
    run = new_run(gateway)
    result = asyncio.run(gateway.submit_action(run.id, SubmitActionRequest(
        tool='workspace', operation='write_text', resource='auth.py',
        arguments={'content': 'safe edit'},
        reasoning_summary='Fix the token expiry boundary in auth.py.',
        reasoning_provenance='AGENT_AUTHORED_SUMMARY',
    )))
    assert result.policy_decision.reasoning_audit_id is not None
    event = next(e for e in gateway.get_events(run.id) if e.event_type == EventType.ACTION_REQUESTED)
    assert event.details['reasoning_provenance'] == 'CALLER_ASSERTED_SUMMARY'
    assert event.details['reasoning_provenance_verified'] is False


def test_held_missing_reasoning_can_be_denied_without_effect(gateway):
    run = new_run(gateway)
    result = asyncio.run(gateway.submit_action(run.id, SubmitActionRequest(
        tool='workspace', operation='write_text', resource='auth.py',
        arguments={'content': 'bad edit'},
    )))
    resolved = asyncio.run(gateway.resolve_approval(
        result.approval_request.id, approve=False, resolved_by='synthetic-reviewer', reason='No reasoning supplied',
    ))
    assert resolved.approval_request.status.value == 'DENIED'
    assert not any(e.event_type == EventType.EXECUTION_STARTED for e in gateway.get_events(run.id))


def test_reasoning_requirement_rejects_unsupported_operation():
    with pytest.raises(ValidationError):
        TaskScope(task_description='Test', created_at='2026-10-25', require_reasoning_for=['read_text'])


def test_allowed_command_without_required_reasoning_never_dispatches(gateway, monkeypatch):
    monkeypatch.setenv('SCOPEWATCH_EXECUTOR', 'docker')
    run, _ = gateway.create_run(name='Command reasoning', task_scope=TaskScope(
        task_description='Run tests.', allowed_paths=['.'],
        allowed_tools=['workspace'], allowed_operations=['run_command'],
        allowed_commands=[['python', '-m', 'pytest']],
        require_reasoning_for=['run_command'],
        created_at=datetime.now(timezone.utc).isoformat(),
    ))
    result = asyncio.run(gateway.submit_action(run.id, SubmitActionRequest(
        tool='workspace', operation='run_command', resource='.',
        arguments={'argv': ['python', '-m', 'pytest']},
    )))
    assert result.policy_decision.reason_code == ReasonCode.REASONING_UNAVAILABLE
    assert result.approval_request is not None
    assert result.execution_receipt is None
    assert not any(e.event_type == EventType.EXECUTION_STARTED for e in gateway.get_events(run.id))
