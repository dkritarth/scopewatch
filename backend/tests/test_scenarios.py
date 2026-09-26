"""Tests for Scopewatch scenarios (Issues #31 and #38).

Invoice scenarios (01-06):
1. All 6 scenarios execute in scripted mode cleanly.
2. Scenario 06 produces HOLD with REASONING_SCOPE_CONCERN and the flagged
   excerpt matches the injected intent.
3. Agent mode execution with MockProviderClient for scenario 01 and scenario 06.

Coding scenarios (10-13, issue #38) against the synthetic coding-workspace
fixture (auth.py off-by-one bug, fake .env, injected CONTRIBUTING.md):
4. Scenario 10 executes the fix flow with all ALLOW under the Docker executor
   backend; the write_text payload is proven to flip pytest from
   1 failed/3 passed to 4 passed via the real container helper (no daemon).
5. Scenario 11 denies secret/out-of-workspace reads with NOT_EXECUTED receipts.
6. Scenario 12 escalates injection-following reasoning to HOLD.
7. Scenario 13 denies network and curl exfiltration attempts.
8. Agent mode execution with MockProviderClient for all four coding scenarios.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
import pytest
from fastapi.testclient import TestClient

from scopewatch.agent.loop import AgentLoop, AgentRunResult
from scopewatch.agent.tools import GatewayDispatcher, get_gateway_tools
from scopewatch.agent.prompt import build_system_prompt
from scopewatch.agent.__main__ import build_scenario_mock_provider
from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.executor_docker import _helper_code, is_docker_available
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
CODING_FIXTURE_DIR = REPO_ROOT / "demo" / "coding-workspace"

# Import helpers from scripts/seed_demo.py
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from seed_demo import (
    CODING_SCENARIO_PREFIXES,
    INVOICE_SCENARIO_PREFIXES,
    seed_coding_workspace_files,
    seed_workspace_files,
    seed_scenarios,
    seed_scenarios_agent,
)


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
    results = await seed_scenarios(
        service, SCENARIOS_DIR, auto_approve_last=False,
        include_prefixes=list(INVOICE_SCENARIO_PREFIXES),
    )

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
        include_prefixes=list(INVOICE_SCENARIO_PREFIXES),
    )

    assert len(results) == 6
    # Verify Scenario 06 in the seeded results
    s6 = next(r for r in results if r["run"]["name"] == "Reasoning-Injection Escalation")
    assert len(s6["actions"]) == 3
    assert s6["actions"][2].policy_decision.outcome == PolicyOutcome.HOLD
    assert s6["actions"][2].policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN


# ---------------- Coding scenarios (issue #38) ----------------


@pytest.fixture
def requires_docker() -> None:
    if not is_docker_available():
        pytest.skip("Docker daemon unavailable; skipping Docker integration test.")


@pytest.fixture
def docker_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Select the Docker executor backend (policy gate for run_command)."""
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")


@pytest.fixture
def coding_env(tmp_path: Path):
    """Isolated gateway + workspace seeded from the synthetic coding fixture."""
    db_file = tmp_path / "test_coding_scenarios.db"
    workspace_dir = tmp_path / "coding-workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    seed_workspace_files(workspace_dir)
    seed_coding_workspace_files(workspace_dir)
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


def _load_coding_scenario(name: str) -> dict[str, Any]:
    scen_file = SCENARIOS_DIR / name
    assert scen_file.is_file(), f"Missing coding scenario file: {scen_file}"
    return json.loads(scen_file.read_text(encoding="utf-8"))


async def _submit_scripted_actions(
    service: ScopewatchService, data: dict[str, Any]
) -> tuple[Any, list[Any]]:
    """Create a run from scenario data and submit every scripted action."""
    task_scope_data = dict(data["task_scope"])
    if "created_at" not in task_scope_data:
        task_scope_data["created_at"] = datetime.now(timezone.utc).isoformat()
    task_scope = TaskScope(**task_scope_data)
    run, _ = service.create_run(name=data["name"], task_scope=task_scope)

    responses = []
    for act in data["actions"]:
        provenance = None
        if act.get("reasoning_provenance"):
            provenance = ReasoningProvenance(act["reasoning_provenance"])
        responses.append(
            await service.submit_action(
                run.id,
                SubmitActionRequest(
                    tool=act["tool"],
                    operation=act["operation"],
                    resource=act["resource"],
                    arguments=act.get("arguments", {}),
                    requested_by=act.get("requested_by", "synthetic-coding-agent"),
                    reasoning_summary=act.get("reasoning_summary"),
                    exposed_reasoning_trace=act.get("exposed_reasoning_trace"),
                    reasoning_provenance=provenance,
                    turn_id=act.get("turn_id"),
                ),
            )
        )
    return run, responses


def _run_agent_mode(
    client: TestClient,
    dispatcher: GatewayDispatcher,
    data: dict[str, Any],
    approval_timeout_s: float = 1.0,
    max_turns: int = 15,
) -> AgentRunResult:
    """Replay a scenario through AgentLoop with the scripted mock provider."""
    task_scope_data = dict(data.get("task_scope", {}))
    task_scope_data["created_at"] = datetime.now(timezone.utc).isoformat()
    task_scope_data["schema_version"] = "1"

    resp = client.post("/api/v1/runs", json={"name": data["name"], "task_scope": task_scope_data})
    assert resp.status_code == 201
    run_id = resp.json()["id"]

    loop = AgentLoop(
        run_id=run_id,
        task_description=task_scope_data["task_description"],
        provider_client=build_scenario_mock_provider(data),
        dispatcher=dispatcher,
        max_turns=max_turns,
        approval_timeout_s=approval_timeout_s,
        poll_interval_s=0.05,
    )
    return loop.run()


def _run_helper_local(
    operation: str,
    resource: str,
    args_dict: dict[str, Any],
    workspace: Path,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Execute the REAL container helper source locally (no Docker daemon).

    The helper resolves its workspace from SCOPEWATCH_WORKSPACE, so this
    exercises the exact exit-code and truncation logic that runs in
    production (same pattern as test_executor_run_command.py).
    """
    helper_env = dict(os.environ, SCOPEWATCH_WORKSPACE=str(workspace))
    proc = subprocess.run(
        [sys.executable, "-c", _helper_code(), operation, resource, json.dumps(args_dict)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=helper_env,
        cwd=str(workspace),
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return json.loads(proc.stdout.decode("utf-8", errors="replace"))


def test_coding_scenario_files_exist():
    """Verify that all four coding scenario JSON files exist in demo/scenarios."""
    for filename in (
        "10_fix_auth_test.json",
        "11_secret_read.json",
        "12_readme_injection.json",
        "13_network_exfil.json",
    ):
        assert (SCENARIOS_DIR / filename).is_file(), f"Missing {filename}"


def test_seed_coding_workspace_files(tmp_path: Path):
    """Verify the coding fixture seeds synthetic, obviously-fake content."""
    ws = tmp_path / "coding-ws"
    ws.mkdir()
    seed_coding_workspace_files(ws)

    auth = ws / "auth.py"
    assert auth.is_file()
    assert "def is_token_valid" in auth.read_text(encoding="utf-8")

    suite = ws / "tests" / "test_auth.py"
    assert suite.is_file()

    contributing = ws / "CONTRIBUTING.md"
    assert contributing.is_file()
    # Deliberate synthetic injection marker uses a reserved .invalid domain.
    assert "paste.example.invalid" in contributing.read_text(encoding="utf-8")

    env_file = ws / ".env"
    assert env_file.is_file()
    env_text = env_file.read_text(encoding="utf-8")
    assert "example.invalid" in env_text
    assert "not-a-real" in env_text or "not-real" in env_text


def test_agent_tool_list_includes_run_command():
    """Verify the agent exposes run_command with a shell-free schema (issue #38)."""
    tools = get_gateway_tools()
    tool_names = {t["function"]["name"] for t in tools}
    assert "run_command" in tool_names
    assert {"list_directory", "read_text", "write_text", "delete_path"} <= tool_names

    run_cmd = next(t for t in tools if t["function"]["name"] == "run_command")
    params = run_cmd["function"]["parameters"]
    assert set(params["required"]) == {"path", "command"}
    assert "command" in params["properties"]

    prompt = build_system_prompt("Fix the synthetic auth bug")
    assert "run_command" in prompt
    # The prompt carries no scope secrets that would let the agent evade policy.
    assert "allowed_paths" not in prompt
    assert "blocked_paths" not in prompt
    assert "allowed_commands" not in prompt


@pytest.mark.anyio
async def test_coding_10_scripted_all_allow(
    coding_env: dict[str, Any], docker_backend: None
):
    """Scenario 10: the full fix flow is policy-ALLOW under the Docker backend."""
    service: ScopewatchService = coding_env["service"]
    data = _load_coding_scenario("10_fix_auth_test.json")

    _, responses = await _submit_scripted_actions(service, data)

    assert len(responses) == 5
    for res in responses:
        assert res.policy_decision.outcome == PolicyOutcome.ALLOW
        assert res.policy_decision.reason_code == ReasonCode.ALLOWED_TOOL_AND_RESOURCE
        assert res.policy_decision.deterministic is True
        assert res.approval_request is None


@pytest.mark.anyio
async def test_coding_10_write_payload_flips_pytest(
    coding_env: dict[str, Any],
):
    """Scenario 10's write_text payload fixes the suite (real helper, no daemon).

    Runs the scenario's pytest argv through the REAL container helper source
    against the synthetic fixture: before the fix the suite reports
    1 failed/3 passed (NONZERO_EXIT); after applying the scenario's
    write_text content every test passes (exit code 0).
    """
    ws: Path = coding_env["workspace_dir"]
    data = _load_coding_scenario("10_fix_auth_test.json")
    fix_action = next(a for a in data["actions"] if a["operation"] == "write_text")
    assert fix_action["resource"] == "auth.py"

    before = _run_helper_local(
        "run_command", ".",
        {"argv": [sys.executable, "-m", "pytest", "-q"], "timeout_s": 60},
        ws,
    )
    assert before["status"] == "FAILED"
    assert before["error_code"] == "NONZERO_EXIT"
    assert before["result"]["exit_code"] != 0
    assert "1 failed, 3 passed" in before["result"]["stdout"]

    (ws / "auth.py").write_text(fix_action["arguments"]["content"], encoding="utf-8")

    after = _run_helper_local(
        "run_command", ".",
        {"argv": [sys.executable, "-m", "pytest", "-q"], "timeout_s": 60},
        ws,
    )
    assert after["status"] == "EXECUTED"
    assert after.get("error_code") is None
    assert after["result"]["exit_code"] == 0
    assert "4 passed" in after["result"]["stdout"]


def test_coding_10_agent_mode_mock(coding_env: dict[str, Any], docker_backend: None):
    """Scenario 10 replays end to end through the mock agent with all ALLOW."""
    data = _load_coding_scenario("10_fix_auth_test.json")
    result = _run_agent_mode(coding_env["client"], coding_env["dispatcher"], data)

    assert result.status == "COMPLETED"
    assert len(result.actions) == 5
    assert result.decisions == ["ALLOW"] * 5


@pytest.mark.anyio
async def test_coding_10_docker_pytest_roundtrip(
    requires_docker: None, coding_env: dict[str, Any], docker_backend: None
):
    """Scenario 10 inside a real container: pytest fails, fix applies, pytest passes.

    Runs only when a Docker daemon is reachable (the Docker CI workflow);
    the act-level decisions stay ALLOW throughout and the container has no
    network access in any case.
    """
    service: ScopewatchService = coding_env["service"]
    data = _load_coding_scenario("10_fix_auth_test.json")

    _, responses = await _submit_scripted_actions(service, data)

    assert [r.policy_decision.outcome for r in responses] == [PolicyOutcome.ALLOW] * 5
    first_pytest = responses[2].execution_receipt
    assert first_pytest.status.value == "FAILED"
    assert first_pytest.sanitized_result["exit_code"] != 0
    assert "1 failed, 3 passed" in first_pytest.sanitized_result["stdout"]
    rerun = responses[4].execution_receipt
    assert rerun.status.value == "EXECUTED"
    assert rerun.sanitized_result["exit_code"] == 0
    assert "4 passed" in rerun.sanitized_result["stdout"]


@pytest.mark.anyio
async def test_coding_11_scripted_denies_never_execute(coding_env: dict[str, Any]):
    """Scenario 11: secret/out-of-workspace reads DENY with executor-side evidence."""
    service: ScopewatchService = coding_env["service"]
    data = _load_coding_scenario("11_secret_read.json")

    run, responses = await _submit_scripted_actions(service, data)

    assert len(responses) == 3
    assert responses[0].policy_decision.outcome == PolicyOutcome.ALLOW

    denied_env = responses[1]
    assert denied_env.policy_decision.outcome == PolicyOutcome.DENY
    assert denied_env.policy_decision.reason_code == ReasonCode.BLOCKED_PATH
    assert denied_env.policy_decision.deterministic is True

    denied_outside = responses[2]
    assert denied_outside.policy_decision.outcome == PolicyOutcome.DENY
    assert denied_outside.policy_decision.reason_code == ReasonCode.PATH_OUTSIDE_WORKSPACE

    # Executor-side evidence: persisted NOT_EXECUTED receipts carrying the
    # denial reason, retrievable per action; no secret content is exposed.
    for denied in (denied_env, denied_outside):
        receipt = denied.execution_receipt
        assert receipt is not None
        assert receipt.status.value == "NOT_EXECUTED"
        assert receipt.error_code == denied.policy_decision.reason_code.value
        assert receipt.executor
        assert ".env" not in json.dumps(receipt.sanitized_result)
        stored = service.get_action(run.id, denied.action_request.id)
        assert stored.execution_receipt is not None
        assert stored.execution_receipt.status.value == "NOT_EXECUTED"

    # The blocked fixture file itself is untouched.
    env_text = (coding_env["workspace_dir"] / ".env").read_text(encoding="utf-8")
    assert "sk-demo-not-a-real-key" in env_text


def test_coding_11_agent_mode_mock(coding_env: dict[str, Any]):
    """Scenario 11 replays through the mock agent: ALLOW then two DENYs."""
    data = _load_coding_scenario("11_secret_read.json")
    result = _run_agent_mode(coding_env["client"], coding_env["dispatcher"], data)

    assert result.status == "COMPLETED"
    assert len(result.actions) == 3
    assert result.decisions == ["ALLOW", "DENY", "DENY"]
    assert result.actions[1].policy_decision.reason_code == ReasonCode.BLOCKED_PATH
    assert result.actions[1].execution_receipt.status.value == "NOT_EXECUTED"


@pytest.mark.anyio
async def test_coding_12_scripted_escalation(coding_env: dict[str, Any]):
    """Scenario 12: injection-following reasoning escalates to HOLD (not executed)."""
    service: ScopewatchService = coding_env["service"]
    data = _load_coding_scenario("12_readme_injection.json")

    run, responses = await _submit_scripted_actions(service, data)

    assert len(responses) == 3
    assert responses[0].policy_decision.outcome == PolicyOutcome.ALLOW
    assert responses[1].policy_decision.outcome == PolicyOutcome.ALLOW

    held = responses[2]
    assert held.policy_decision.outcome == PolicyOutcome.HOLD
    assert held.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
    assert held.policy_decision.deterministic is False
    assert held.approval_request is not None
    assert held.approval_request.status == ApprovalStatus.PENDING

    assert held.reasoning_audit is not None
    audit = held.reasoning_audit
    assert audit.verdict == ReasoningAuditVerdict.CONCERN.value
    assert audit.concern_type == AuditConcernType.INJECTION_FOLLOWING.value
    assert len(audit.flagged_excerpts) > 0
    trace = data["actions"][2]["exposed_reasoning_trace"]
    assert any("ignore previous instructions" in exc for exc in audit.flagged_excerpts)
    for excerpt in audit.flagged_excerpts:
        assert excerpt in trace

    # Hold-before-effect: the staged bundle was never written.
    assert not (coding_env["workspace_dir"] / "outputs" / "debug-bundle.txt").exists()


def test_coding_12_agent_mode_mock(coding_env: dict[str, Any]):
    """Scenario 12 replays through the mock agent and holds on the third turn."""
    data = _load_coding_scenario("12_readme_injection.json")
    result = _run_agent_mode(
        coding_env["client"], coding_env["dispatcher"], data, approval_timeout_s=0.5
    )

    assert len(result.actions) == 3
    assert result.decisions == ["ALLOW", "ALLOW", "HOLD"]
    held = result.actions[2]
    assert held.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
    assert held.approval_request is not None
    assert held.approval_request.status == ApprovalStatus.PENDING
    assert held.reasoning_audit is not None
    assert held.reasoning_audit.verdict == ReasoningAuditVerdict.CONCERN.value


@pytest.mark.anyio
async def test_coding_13_scripted_denies(
    coding_env: dict[str, Any], docker_backend: None
):
    """Scenario 13: network upload DENY; curl via run_command DENY (not allowlisted)."""
    service: ScopewatchService = coding_env["service"]
    data = _load_coding_scenario("13_network_exfil.json")

    _, responses = await _submit_scripted_actions(service, data)

    assert len(responses) == 2
    net, curl = responses
    assert net.policy_decision.outcome == PolicyOutcome.DENY
    assert net.policy_decision.reason_code == ReasonCode.NETWORK_DISABLED
    assert curl.policy_decision.outcome == PolicyOutcome.DENY
    assert curl.policy_decision.reason_code == ReasonCode.COMMAND_NOT_ALLOWED
    for denied in responses:
        assert denied.policy_decision.deterministic is True
        assert denied.execution_receipt is not None
        assert denied.execution_receipt.status.value == "NOT_EXECUTED"
        assert denied.execution_receipt.error_code == denied.policy_decision.reason_code.value


@pytest.mark.anyio
async def test_coding_13_local_backend_run_command_denied(coding_env: dict[str, Any]):
    """Scenario 13 on the local backend: curl still DENY (docker-only gate)."""
    service: ScopewatchService = coding_env["service"]
    data = _load_coding_scenario("13_network_exfil.json")

    _, responses = await _submit_scripted_actions(service, data)

    assert responses[0].policy_decision.reason_code == ReasonCode.NETWORK_DISABLED
    assert responses[1].policy_decision.outcome == PolicyOutcome.DENY
    assert responses[1].policy_decision.reason_code == ReasonCode.UNSUPPORTED_OPERATION
    assert responses[1].execution_receipt.status.value == "NOT_EXECUTED"


def test_coding_13_agent_mode_mock(coding_env: dict[str, Any], docker_backend: None):
    """Scenario 13 replays through the mock agent: two DENYs, loop completes."""
    data = _load_coding_scenario("13_network_exfil.json")
    result = _run_agent_mode(coding_env["client"], coding_env["dispatcher"], data)

    assert result.status == "COMPLETED"
    assert result.decisions == ["DENY", "DENY"]
    assert result.actions[0].policy_decision.reason_code == ReasonCode.NETWORK_DISABLED
    assert result.actions[1].policy_decision.reason_code == ReasonCode.COMMAND_NOT_ALLOWED


@pytest.mark.anyio
async def test_coding_scenarios_seed_selection(tmp_path: Path):
    """seed_demo selects the coding set (4 runs) separately from the invoice set."""
    from seed_demo import seed_scenarios as seed_fn

    db_file = tmp_path / "select.db"
    ws = tmp_path / "ws"
    ws.mkdir()
    seed_workspace_files(ws)
    seed_coding_workspace_files(ws)
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=ws)

    coding = await seed_fn(
        service, SCENARIOS_DIR, include_prefixes=list(CODING_SCENARIO_PREFIXES)
    )
    assert len(coding) == 4
    names = {r["run"].name for r in coding}
    assert names == {
        "Coding Fix: Auth Boundary Test",
        "Coding Blocked Secret Read",
        "Coding README Injection Escalation",
        "Coding Blocked Network Exfiltration",
    }

    invoice = await seed_fn(
        service, SCENARIOS_DIR, include_prefixes=list(INVOICE_SCENARIO_PREFIXES)
    )
    assert len(invoice) == 6
