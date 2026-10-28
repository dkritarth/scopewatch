"""Bounded, offline scenario-06 review seed. Never invokes a configured provider."""
from datetime import datetime, timezone
import json
from pathlib import Path

from scopewatch.reasoning_audit import ReasoningAuditor
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService


async def seed_judge_review(service: ScopewatchService) -> str:
    """Create one fresh HOLD, retaining normal run/action guards and evidence.

    Only the final proposed write is submitted. No fixture read or write needs
    execution to explain the injection to a judge. Required reasoning also
    keeps this action held when auditing is explicitly disabled.
    """
    scenario_path = Path(__file__).resolve().parents[2] / 'demo/scenarios/06_invoice_injection.json'
    data = json.loads(scenario_path.read_text(encoding='utf-8'))
    scope = TaskScope(**data['task_scope'], require_reasoning_for=['write_text'],
                      created_at=datetime.now(timezone.utc).isoformat())
    run, _ = service.create_run(name='06 Invoice injection (synthetic mock replay)', task_scope=scope)
    offline = ScopewatchService(
        db_path=service.db_path, workspace_root=service.workspace_root,
        run_workspaces_root=service.run_workspaces.root,
        auditor=ReasoningAuditor(profile='mock'), guards=service.guards,
    )
    await offline.submit_action(run.id, SubmitActionRequest(**data['actions'][-1]))
    return run.id
