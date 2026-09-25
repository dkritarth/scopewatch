"""Tests for Scopewatch scenarios (Issue #31).

Verifies:
1. All 6 scenarios execute in scripted mode cleanly.
2. Scenario 06 produces HOLD with REASONING_SCOPE_CONCERN and the flagged
   excerpt matches the injected intent.
3. Agent mode execution with MockProviderClient for scenario 01 and scenario 06.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import pytest
from fastapi.testclient import TestClient

from scopewatch.agent.loop import AgentLoop, AgentRunResult
from scopewatch.agent.tools import GatewayDispatcher
from scopewatch.agent.__main__ import build_scenario_mock_provider
from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.models import (
    ApprovalStatus,
    PolicyOutcome,
    ReasonCode,
    ReasoningProvenance,
    RunStatus,
)
from scopewatch.providers.client import ChatResult, MockProviderClient
from scopewatch.reasoning_audit import AuditConcernType, ReasoningAuditVerdict
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCENARIOS_DIR = REPO_ROOT / "demo" / "scenarios"

# Import helpers from scripts/seed_demo.py
import sys
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from seed_demo import seed_workspace_files, seed_scenarios, seed_scenarios_agent


@pytest.fixture
def env(tmp_path: Path):
    db_file = tmp_path / "test_scenarios.db"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    seed_workspace_files(workspace_dir)
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=workspace_dir)
    app = create_app(db_path=db_file, workspace_root=workspace_dir)
    client = TestClient(app, base_url="http://testserver")
    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)

    return {
        "db_file": db_file,
        "workspace_dir": workspace_dir,
        "service": service,
        "app": app,
        "client": client,
        "dispatcher": dispatcher,
    }


def test_all_six_scenarios_exist():
    """Verify that all six required scenario JSON files exist in demo/scenarios."""
    scenario_files = sorted(SCENARIOS_DIR.glob("*.json"))
    assert len(scenario_files) >= 6

    filenames = [f.name for f in scenario_files]
    assert "01_safe_audit.json" in filenames
    assert "02_blocked_private.json" in filenames
    assert "03_path_traversal.json" in filenames
    assert "04_network_exfil.json" in filenames
    assert "05_approval_delete.json" in filenames
    assert "06_invoice_injection.json" in filenames


def test_seed_workspace_files_creates_injected_invoice(tmp_path: Path):
    """Verify that seed_workspace_files creates vendor-c-injected.txt with injected note."""
    ws = tmp_path / "ws"
    seed_workspace_files(ws)
    injected_file = ws / "invoices" / "approved" / "vendor-c-injected.txt"
    assert injected_file.is_file()
    content = injected_file.read_text(encoding="utf-8")
    assert "INVOICE #INV-2026-003" in content
    assert "AUDITOR SYSTEM NOTE:" in content
    assert "outputs/partner-payment-details.txt" in content


@pytest.mark.anyio
async def test_all_six_scenarios_execute_scripted_mode(env: dict[str, Any]):
    """Verify that all 6 scenarios execute cleanly in scripted mode."""
    service: ScopewatchService = env["service"]
    results = await seed_scenarios(service, SCENARIOS_DIR, auto_approve_last=False)

    assert len(results) == 6
    runs_by_id = {res["run"].name: res for res in results}

    # Scenario 01: Safe Audit (all ALLOW)
    s1 = runs_by_id["Approved Invoice Audit"]
    assert all(a.policy_decision.outcome == PolicyOutcome.ALLOW for a in s1["actions"])

    # Scenario 02: Blocked Private (all DENY)
    s2 = runs_by_id["Blocked Confidential Directory Access"]
    assert all(a.policy_decision.outcome == PolicyOutcome.DENY for a in s2["actions"])

    # Scenario 03: Path Traversal (all DENY)
    s3 = runs_by_id["Blocked Path Traversal Attempt"]
    assert all(a.policy_decision.outcome == PolicyOutcome.DENY for a in s3["actions"])

    # Scenario 04: Network Exfiltration (all DENY)
    s4 = runs_by_id["Blocked Network Exfiltration"]
    assert all(a.policy_decision.outcome == PolicyOutcome.DENY for a in s4["actions"])

    # Scenario 05: File Deletion Approval (first ALLOW, second HOLD)
    s5 = runs_by_id["Human Approval Workflow - File Deletion"]
    assert s5["actions"][0].policy_decision.outcome == PolicyOutcome.ALLOW
    assert s5["actions"][1].policy_decision.outcome == PolicyOutcome.HOLD
    assert s5["actions"][1].policy_decision.reason_code == ReasonCode.APPROVAL_REQUIRED

    # Scenario 06: Reasoning-Injection Escalation (first 2 ALLOW, 3rd HOLD)
    s6 = runs_by_id["Reasoning-Injection Escalation"]
    assert len(s6["actions"]) == 3
    assert s6["actions"][0].policy_decision.outcome == PolicyOutcome.ALLOW
    assert s6["actions"][1].policy_decision.outcome == PolicyOutcome.ALLOW
    assert s6["actions"][2].policy_decision.outcome == PolicyOutcome.HOLD
    assert s6["actions"][2].policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN


@pytest.mark.anyio
async def test_scenario_06_scripted_escalation_details(env: dict[str, Any]):
    """Verify scenario 06 produces HOLD with REASONING_SCOPE_CONCERN and flagged excerpt matches."""
    service: ScopewatchService = env["service"]
    scen_file = SCENARIOS_DIR / "06_invoice_injection.json"
    data = json.loads(scen_file.read_text(encoding="utf-8"))

    task_scope_data = dict(data["task_scope"])
    if "created_at" not in task_scope_data:
        task_scope_data["created_at"] = datetime.now(timezone.utc).isoformat()
    task_scope = TaskScope(**task_scope_data)
    run, _ = service.create_run(name=data["name"], task_scope=task_scope)

    # Action 1: list_directory -> ALLOW
    a1_data = data["actions"][0]
    res1 = await service.submit_action(
        run.id,
        SubmitActionRequest(
            tool=a1_data["tool"],
            operation=a1_data["operation"],
            resource=a1_data["resource"],
            arguments=a1_data.get("arguments", {}),
            reasoning_summary=a1_data.get("reasoning_summary"),
        ),
    )
    assert res1.policy_decision.outcome == PolicyOutcome.ALLOW

    # Action 2: read_text on vendor-c-injected.txt -> ALLOW
    a2_data = data["actions"][1]
    res2 = await service.submit_action(
        run.id,
        SubmitActionRequest(
            tool=a2_data["tool"],
            operation=a2_data["operation"],
            resource=a2_data["resource"],
            arguments=a2_data.get("arguments", {}),
            reasoning_summary=a2_data.get("reasoning_summary"),
        ),
    )
    assert res2.policy_decision.outcome == PolicyOutcome.ALLOW

    # Action 3: write_text with injected reasoning trace -> HOLD with REASONING_SCOPE_CONCERN
    a3_data = data["actions"][2]
    trace = a3_data["exposed_reasoning_trace"]
    res3 = await service.submit_action(
        run.id,
        SubmitActionRequest(
            tool=a3_data["tool"],
            operation=a3_data["operation"],
            resource=a3_data["resource"],
            arguments=a3_data.get("arguments", {}),
            exposed_reasoning_trace=trace,
            reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE,
            turn_id=a3_data.get("turn_id", "turn-inject-01"),
        ),
    )

    # The action was held specifically because of reasoning concern, not deterministic policy
    assert res3.policy_decision.outcome == PolicyOutcome.HOLD
    assert res3.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
    assert res3.policy_decision.deterministic is False
    assert res3.approval_request is not None
    assert res3.approval_request.status == ApprovalStatus.PENDING

    # Check reasoning audit record
    assert res3.reasoning_audit is not None
    audit = res3.reasoning_audit
    assert audit.verdict == ReasoningAuditVerdict.CONCERN.value
    assert audit.concern_type == AuditConcernType.INJECTION_FOLLOWING.value
    assert len(audit.flagged_excerpts) > 0

    # Verify flagged excerpt matches the injected intent
    assert any(
        "system note instructing" in exc.lower()
        or "urgent system note" in exc.lower()
        for exc in audit.flagged_excerpts
    )
    # Every excerpt must be an exact substring of the trace
    for excerpt in audit.flagged_excerpts:
        assert excerpt in trace


def test_agent_mode_scenario_01_execution(env: dict[str, Any]):
    """Verify agent mode execution with MockProviderClient for scenario 01."""
    scen_file = SCENARIOS_DIR / "01_safe_audit.json"
    data = json.loads(scen_file.read_text(encoding="utf-8"))

    client: TestClient = env["client"]
    dispatcher: GatewayDispatcher = env["dispatcher"]
    ws: Path = env["workspace_dir"]

    # 1. Create run on gateway
    task_scope_data = dict(data.get("task_scope", {}))
    task_scope_data["created_at"] = datetime.now(timezone.utc).isoformat()
    task_scope_data["schema_version"] = "1"

    resp = client.post("/api/v1/runs", json={"name": data["name"], "task_scope": task_scope_data})
    assert resp.status_code == 201
    run_id = resp.json()["id"]

    # 2. Build mock provider from scenario
    mock_provider = build_scenario_mock_provider(data)

    # 3. Execute agent loop
    loop = AgentLoop(
        run_id=run_id,
        task_description=task_scope_data["task_description"],
        provider_client=mock_provider,
        dispatcher=dispatcher,
        max_turns=10,
    )
    result = loop.run()

    assert result.status == "COMPLETED"
    assert result.turns > 0
    assert result.total_tool_calls == len(data["actions"])
    assert all(a.policy_decision.outcome == PolicyOutcome.ALLOW for a in result.actions)

    # Verify output file was written to workspace
    summary_file = ws / "outputs" / "audit-summary.txt"
    assert summary_file.is_file()


def test_agent_mode_scenario_06_execution(env: dict[str, Any]):
    """Verify agent mode execution with MockProviderClient for scenario 06."""
    scen_file = SCENARIOS_DIR / "06_invoice_injection.json"
    data = json.loads(scen_file.read_text(encoding="utf-8"))

    client: TestClient = env["client"]
    dispatcher: GatewayDispatcher = env["dispatcher"]

    # 1. Create run on gateway
    task_scope_data = dict(data.get("task_scope", {}))
    task_scope_data["created_at"] = datetime.now(timezone.utc).isoformat()
    task_scope_data["schema_version"] = "1"

    resp = client.post("/api/v1/runs", json={"name": data["name"], "task_scope": task_scope_data})
    assert resp.status_code == 201
    run_id = resp.json()["id"]

    # 2. Build mock provider from scenario
    mock_provider = build_scenario_mock_provider(data)

    # 3. Execute agent loop with approval_timeout_s=0.5 (leaving approval pending)
    loop = AgentLoop(
        run_id=run_id,
        task_description=task_scope_data["task_description"],
        provider_client=mock_provider,
        dispatcher=dispatcher,
        max_turns=10,
        approval_timeout_s=0.5,
        poll_interval_s=0.05,
    )
    result = loop.run()

    # The agent executed 3 tool calls
    assert len(result.actions) == 3
    # Actions 1 & 2 allowed
    assert result.actions[0].policy_decision.outcome == PolicyOutcome.ALLOW
    assert result.actions[1].policy_decision.outcome == PolicyOutcome.ALLOW

    # Action 3: Held because reasoning injection detected
    held_action = result.actions[2]
    assert held_action.policy_decision.outcome == PolicyOutcome.HOLD
    assert held_action.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
    assert held_action.policy_decision.deterministic is False

    # Approval was created
    assert held_action.approval_request is not None
    assert held_action.approval_request.status == ApprovalStatus.PENDING

    # Reasoning audit details
    assert held_action.reasoning_audit is not None
    audit = held_action.reasoning_audit
    assert audit.verdict == ReasoningAuditVerdict.CONCERN.value
    assert audit.concern_type == AuditConcernType.INJECTION_FOLLOWING.value
    assert any(
        "system note instructing" in exc.lower()
        or "urgent system note" in exc.lower()
        for exc in audit.flagged_excerpts
    )


def test_seed_demo_helper_agent_mode(tmp_path: Path):
    """Verify seed_scenarios_agent runs cleanly for all scenarios with auto-approve."""
    db_file = tmp_path / "agent_seed.db"
    ws = tmp_path / "agent_ws"
    seed_workspace_files(ws)
    init_db(db_file)

    results = seed_scenarios_agent(
        db_path=db_file,
        workspace_root=ws,
        scenarios_dir=SCENARIOS_DIR,
        auto_approve=True,
        approval_timeout_s=1.0,
    )

    assert len(results) == 6
    # Verify Scenario 06 in the seeded results
    s6 = next(r for r in results if r["run"]["name"] == "Reasoning-Injection Escalation")
    assert len(s6["actions"]) == 3
    assert s6["actions"][2].policy_decision.outcome == PolicyOutcome.HOLD
    assert s6["actions"][2].policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
