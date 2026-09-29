"""Mock-agent regression tests for the six invoice scenarios (issue #31).

Offline only: every run uses the `mock` provider, whose scripted responses
replay each scenario's own planned actions verbatim
(`build_scenario_mock_provider`). That is, mock agent mode is replay, not
model choice — the acceptance criterion explicitly permits scripted model
responses, so these tests are valid as decision plumbing, not as model
accuracy. Live-model behaviour (`openrouter-dev`, `nebius-demo`) is NOT
covered here and remains a needs-human run.

Expectations mirror the scripted-mode outcomes:
- 01: all ALLOW.
- 02/03/04: all DENY (outcome only: scenario 04 is DENY [TOOL_NOT_ALLOWED]
  scripted but DENY [NETWORK_DISABLED] in agent mode because the dispatcher
  normalises the tool to `workspace` — same outcome, different code).
- 05: ALLOW then HOLD [APPROVAL_REQUIRED].
- 06: ALLOW, ALLOW, HOLD [REASONING_SCOPE_CONCERN] with deterministic=False:
  the write target lives inside `allowed_paths`, so policy would ALLOW and
  the HOLD can only come from the reasoning audit (CONCERN/INJECTION_FOLLOWING
  with excerpts that are exact substrings of the trace).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from scopewatch.agent.__main__ import build_scenario_mock_provider
from scopewatch.agent.loop import AgentLoop, AgentRunResult
from scopewatch.agent.tools import GatewayDispatcher
from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.models import ApprovalStatus, PolicyOutcome, ReasonCode
from scopewatch.reasoning_audit import AuditConcernType, ReasoningAuditVerdict
from scopewatch.service import ScopewatchService

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCENARIOS_DIR = REPO_ROOT / "demo" / "scenarios"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from seed_demo import seed_workspace_files  # noqa: E402


def _isolated_gateway(tmp_path: Path) -> dict[str, Any]:
    db_file = tmp_path / "invoice_agent_regression.db"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    seed_workspace_files(workspace_dir)
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=workspace_dir)
    app = create_app(db_path=db_file, workspace_root=workspace_dir)
    client = TestClient(app, base_url="http://testserver")
    dispatcher = GatewayDispatcher(base_url="http://testserver", http_client=client)
    return {
        "service": service,
        "client": client,
        "dispatcher": dispatcher,
        "workspace_dir": workspace_dir,
    }


def _load_scenario(filename: str) -> dict[str, Any]:
    path = SCENARIOS_DIR / filename
    assert path.is_file(), f"Missing invoice scenario file: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _run_mock_agent(
    client: TestClient,
    dispatcher: GatewayDispatcher,
    data: dict[str, Any],
) -> AgentRunResult:
    """Replay one scenario through AgentLoop with the scripted mock provider."""
    task_scope_data = dict(data.get("task_scope", {}))
    task_scope_data["created_at"] = datetime.now(timezone.utc).isoformat()
    task_scope_data["schema_version"] = "1"

    resp = client.post(
        "/api/v1/runs", json={"name": data["name"], "task_scope": task_scope_data}
    )
    assert resp.status_code == 201, resp.text
    loop = AgentLoop(
        run_id=resp.json()["id"],
        task_description=task_scope_data["task_description"],
        provider_client=build_scenario_mock_provider(data),
        dispatcher=dispatcher,
        max_turns=10,
        approval_timeout_s=0.5,
        poll_interval_s=0.05,
    )
    return loop.run()


def _outcomes(result: AgentRunResult) -> list[PolicyOutcome]:
    return [a.policy_decision.outcome for a in result.actions]


def test_mock_agent_01_safe_audit_all_allow(tmp_path: Path) -> None:
    """Scenario 01 replays in agent mode with every action ALLOW."""
    gw = _isolated_gateway(tmp_path)
    data = _load_scenario("01_safe_audit.json")
    result = _run_mock_agent(gw["client"], gw["dispatcher"], data)

    assert len(result.actions) == len(data["actions"])
    assert _outcomes(result) == [PolicyOutcome.ALLOW] * len(data["actions"])
    assert all(a.policy_decision.deterministic for a in result.actions)


def test_mock_agent_02_blocked_private_all_deny(tmp_path: Path) -> None:
    """Scenario 02 replays in agent mode with every action DENY."""
    gw = _isolated_gateway(tmp_path)
    data = _load_scenario("02_blocked_private.json")
    result = _run_mock_agent(gw["client"], gw["dispatcher"], data)

    assert len(result.actions) == len(data["actions"])
    assert all(o == PolicyOutcome.DENY for o in _outcomes(result))


def test_mock_agent_03_path_traversal_all_deny(tmp_path: Path) -> None:
    """Scenario 03 replays in agent mode with every action DENY."""
    gw = _isolated_gateway(tmp_path)
    data = _load_scenario("03_path_traversal.json")
    result = _run_mock_agent(gw["client"], gw["dispatcher"], data)

    assert len(result.actions) == len(data["actions"])
    assert all(o == PolicyOutcome.DENY for o in _outcomes(result))


def test_mock_agent_04_network_exfil_all_deny(tmp_path: Path) -> None:
    """Scenario 04 replays in agent mode with every action DENY (outcome only).

    Known benign divergence: scripted mode reports TOOL_NOT_ALLOWED while
    agent mode reports NETWORK_DISABLED, because the dispatcher normalises
    the tool to `workspace`. Same outcome, different code — so this test
    pins the outcome, not the reason code.
    """
    gw = _isolated_gateway(tmp_path)
    data = _load_scenario("04_network_exfil.json")
    result = _run_mock_agent(gw["client"], gw["dispatcher"], data)

    assert len(result.actions) == len(data["actions"])
    assert all(o == PolicyOutcome.DENY for o in _outcomes(result))
    assert all(a.policy_decision.deterministic for a in result.actions)


def test_mock_agent_05_approval_delete_allow_then_hold(tmp_path: Path) -> None:
    """Scenario 05 replays in agent mode: ALLOW then HOLD [APPROVAL_REQUIRED]."""
    gw = _isolated_gateway(tmp_path)
    data = _load_scenario("05_approval_delete.json")
    result = _run_mock_agent(gw["client"], gw["dispatcher"], data)

    assert len(result.actions) == 2
    assert _outcomes(result) == [PolicyOutcome.ALLOW, PolicyOutcome.HOLD]
    held = result.actions[1]
    assert held.policy_decision.reason_code == ReasonCode.APPROVAL_REQUIRED
    assert held.approval_request is not None
    assert held.approval_request.status == ApprovalStatus.PENDING


def test_mock_agent_06_injection_escalates_to_hold(tmp_path: Path) -> None:
    """Scenario 06 replays in agent mode: the injected write HOLDs on reasoning.

    The write target (`outputs/partner-payment-details.txt`) sits inside the
    scenario's `allowed_paths`, so deterministic policy would ALLOW; the HOLD
    with REASONING_SCOPE_CONCERN (deterministic=False) can only come from the
    audit reading the agent's exposed reasoning as injection-following.
    """
    gw = _isolated_gateway(tmp_path)
    data = _load_scenario("06_invoice_injection.json")

    # Policy facet: the third action's target is policy-allowed on its own.
    scope = data["task_scope"]
    third = data["actions"][2]
    assert any(
        third["resource"] == p or third["resource"].startswith(p.rstrip("/") + "/")
        for p in scope["allowed_paths"]
    ), "scenario 06 target must stay inside allowed_paths for the test to mean HOLD-on-reasoning"

    result = _run_mock_agent(gw["client"], gw["dispatcher"], data)

    assert len(result.actions) == 3
    assert _outcomes(result) == [
        PolicyOutcome.ALLOW,
        PolicyOutcome.ALLOW,
        PolicyOutcome.HOLD,
    ]

    held = result.actions[2]
    assert held.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
    assert held.policy_decision.deterministic is False
    assert held.approval_request is not None
    assert held.approval_request.status == ApprovalStatus.PENDING

    assert held.reasoning_audit is not None
    audit = held.reasoning_audit
    assert audit.verdict == ReasoningAuditVerdict.CONCERN.value
    assert audit.concern_type == AuditConcernType.INJECTION_FOLLOWING.value
    assert len(audit.flagged_excerpts) > 0
    trace = third["exposed_reasoning_trace"]
    for excerpt in audit.flagged_excerpts:
        assert excerpt in trace


def test_mock_agent_all_six_invoice_decision_table(tmp_path: Path) -> None:
    """One table for all six invoice scenarios: the headline CI contract."""
    gw = _isolated_gateway(tmp_path)
    expectations: dict[str, list[PolicyOutcome]] = {
        "01_safe_audit.json": [PolicyOutcome.ALLOW] * 4,
        "02_blocked_private.json": [PolicyOutcome.DENY] * 2,
        "03_path_traversal.json": [PolicyOutcome.DENY] * 2,
        "04_network_exfil.json": [PolicyOutcome.DENY] * 2,
        "05_approval_delete.json": [PolicyOutcome.ALLOW, PolicyOutcome.HOLD],
        "06_invoice_injection.json": [
            PolicyOutcome.ALLOW,
            PolicyOutcome.ALLOW,
            PolicyOutcome.HOLD,
        ],
    }
    for filename, expected in expectations.items():
        data = _load_scenario(filename)
        result = _run_mock_agent(gw["client"], gw["dispatcher"], data)
        assert _outcomes(result) == expected, f"agent-mode decisions for {filename}"
