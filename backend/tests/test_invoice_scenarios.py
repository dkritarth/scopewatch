"""M1 invoice-scenario gap tests (issue #31, with #32 mock-agent support).

Supplements backend/tests/test_scenarios.py (scripted outcomes via the seed
helper, 06 scripted details, agent 01/06, seed-agent helper) and deliberately
avoids duplicating the still-open PR #74 file
backend/tests/test_invoice_agent_regression.py (direct AgentLoop replay of all
six with a decision table). Coverage here is the part neither has:

- Isolated scripted tests per invoice scenario (01-05) with reason codes,
  deterministic flags, receipts, and audit attachment — not just outcomes.
- 06 scripted hold-before-effect, approval single-use, and DB event order.
- Synthetic-content markers (invented vendors / routing / account / sink).
- Scripted-mode green via the seed helper across all six.
- Mock-agent decisions for all six via seed_scenarios_agent (a different
  entry point from PR #74's direct AgentLoop), documenting replay-not-choice
  and the benign 04 code divergence.

Offline only: mock provider, synthetic workspace, no network. Live
openrouter-dev / nebius-demo runs are NOT attempted here (no keys) and are
recorded as PENDING in the PR body.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scopewatch.db import get_connection, init_db
from scopewatch.models import (
    ApprovalStatus,
    EventType,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
)
from scopewatch.reasoning_audit import AuditConcernType, ReasoningAuditVerdict
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCENARIOS_DIR = REPO_ROOT / "demo" / "scenarios"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from seed_demo import (  # noqa: E402
    INVOICE_SCENARIO_PREFIXES,
    seed_scenarios,
    seed_scenarios_agent,
    seed_workspace_files,
)


@pytest.fixture
def invoice_env(tmp_path: Path) -> dict[str, Any]:
    db_file = tmp_path / "m1_invoice.db"
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    seed_workspace_files(ws)
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=ws)
    return {"service": service, "db_file": db_file, "workspace": ws}


def _load(name: str) -> dict[str, Any]:
    path = SCENARIOS_DIR / name
    assert path.is_file(), f"Missing invoice scenario: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


async def _submit_scripted(
    service: ScopewatchService, data: dict[str, Any]
) -> tuple[Any, list[Any]]:
    task_scope_data = dict(data["task_scope"])
    if "created_at" not in task_scope_data:
        task_scope_data["created_at"] = datetime.now(timezone.utc).isoformat()
    run, _ = service.create_run(name=data["name"], task_scope=TaskScope(**task_scope_data))
    responses = []
    for act in data["actions"]:
        responses.append(
            await service.submit_action(
                run.id,
                SubmitActionRequest(
                    tool=act["tool"],
                    operation=act["operation"],
                    resource=act["resource"],
                    arguments=act.get("arguments", {}),
                    requested_by=act.get("requested_by", "synthetic-agent"),
                    reasoning_summary=act.get("reasoning_summary"),
                    exposed_reasoning_trace=act.get("exposed_reasoning_trace"),
                    turn_id=act.get("turn_id"),
                ),
            )
        )
    return run, responses


def _run(coro):
    return asyncio.run(coro)


def test_m1_invoice_01_scripted_all_allow(invoice_env: dict[str, Any]):
    service: ScopewatchService = invoice_env["service"]
    data = _load("01_safe_audit.json")
    run, responses = _run(_submit_scripted(service, data))

    assert len(responses) == 4
    for res in responses:
        assert res.policy_decision.outcome == PolicyOutcome.ALLOW
        assert res.policy_decision.reason_code == ReasonCode.ALLOWED_TOOL_AND_RESOURCE
        assert res.policy_decision.deterministic is True
        assert res.execution_receipt is not None
        assert res.execution_receipt.status == ExecutionStatus.EXECUTED
        assert res.approval_request is None
    # The report lands in the RUN's workspace copy (#117); the shared fixture
    # stays read-only so later runs start from the same baseline.
    run_workspace = service.get_run_workspace(run.id)
    out = run_workspace / "outputs" / "audit-summary.txt"
    assert out.is_file()
    assert "AUDIT REPORT" in out.read_text(encoding="utf-8")
    assert not (invoice_env["workspace"] / "outputs" / "audit-summary.txt").exists()
    _ = run


def test_m1_invoice_02_scripted_all_deny(invoice_env: dict[str, Any]):
    service: ScopewatchService = invoice_env["service"]
    data = _load("02_blocked_private.json")
    run, responses = _run(_submit_scripted(service, data))

    assert len(responses) == 2
    for res in responses:
        assert res.policy_decision.outcome == PolicyOutcome.DENY
        assert res.policy_decision.reason_code == ReasonCode.BLOCKED_PATH
        assert res.policy_decision.deterministic is True
        assert res.reasoning_audit is None  # DENY short-circuits even with summaries
        assert res.execution_receipt is not None
        assert res.execution_receipt.status == ExecutionStatus.NOT_EXECUTED
        assert res.approval_request is None
        # No secret content leaks into receipts.
        assert "350,000" not in json.dumps(res.execution_receipt.sanitized_result)
    conn = get_connection(invoice_env["db_file"])
    try:
        assert conn.execute("SELECT COUNT(*) c FROM reasoning_audits").fetchone()["c"] == 0
    finally:
        conn.close()
    _ = run


def test_m1_invoice_03_scripted_all_deny(invoice_env: dict[str, Any]):
    service: ScopewatchService = invoice_env["service"]
    data = _load("03_path_traversal.json")
    _, responses = _run(_submit_scripted(service, data))

    assert len(responses) == 2
    for res in responses:
        assert res.policy_decision.outcome == PolicyOutcome.DENY
        assert res.policy_decision.reason_code == ReasonCode.PATH_TRAVERSAL
        assert res.policy_decision.deterministic is True
        assert res.execution_receipt is not None
        assert res.execution_receipt.status == ExecutionStatus.NOT_EXECUTED


def test_m1_invoice_04_scripted_all_deny(invoice_env: dict[str, Any]):
    """Scripted 04 pins codes; agent mode normalises the tool so only the outcome is pinned there."""
    service: ScopewatchService = invoice_env["service"]
    data = _load("04_network_exfil.json")
    _, responses = _run(_submit_scripted(service, data))

    assert len(responses) == 2
    assert responses[0].policy_decision.outcome == PolicyOutcome.DENY
    assert responses[0].policy_decision.reason_code == ReasonCode.NETWORK_DISABLED
    assert responses[1].policy_decision.outcome == PolicyOutcome.DENY
    assert responses[1].policy_decision.reason_code == ReasonCode.TOOL_NOT_ALLOWED
    for res in responses:
        assert res.policy_decision.deterministic is True
        assert res.execution_receipt is not None
        assert res.execution_receipt.status == ExecutionStatus.NOT_EXECUTED


def test_m1_invoice_05_scripted_allow_then_hold(invoice_env: dict[str, Any]):
    service: ScopewatchService = invoice_env["service"]
    data = _load("05_approval_delete.json")
    _, responses = _run(_submit_scripted(service, data))

    assert len(responses) == 2
    first, second = responses
    assert first.policy_decision.outcome == PolicyOutcome.ALLOW
    assert first.execution_receipt is not None
    assert first.execution_receipt.status == ExecutionStatus.EXECUTED
    assert second.policy_decision.outcome == PolicyOutcome.HOLD
    assert second.policy_decision.reason_code == ReasonCode.APPROVAL_REQUIRED
    assert second.policy_decision.deterministic is True
    # Policy HOLD still runs the audit and attaches it as evidence.
    assert second.reasoning_audit is not None
    assert second.reasoning_audit.verdict == ReasoningAuditVerdict.NO_CONCERN.value
    assert second.approval_request is not None
    assert second.approval_request.status == ApprovalStatus.PENDING
    assert second.execution_receipt is None


def test_m1_invoice_06_scripted_escalation(invoice_env: dict[str, Any]):
    service: ScopewatchService = invoice_env["service"]
    data = _load("06_invoice_injection.json")

    # The write target stays inside allowed_paths, so policy alone would ALLOW.
    scope = data["task_scope"]
    third = data["actions"][2]
    assert any(
        third["resource"] == p or third["resource"].startswith(p.rstrip("/") + "/")
        for p in scope["allowed_paths"]
    )

    _, responses = _run(_submit_scripted(service, data))
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
    assert held.reasoning_audit.verdict == ReasoningAuditVerdict.CONCERN.value
    assert held.reasoning_audit.concern_type == AuditConcernType.INJECTION_FOLLOWING.value
    trace = third["exposed_reasoning_trace"]
    assert len(held.reasoning_audit.flagged_excerpts) > 0
    for excerpt in held.reasoning_audit.flagged_excerpts:
        assert excerpt in trace
    # Hold-before-effect: nothing was written.
    assert not (invoice_env["workspace"] / third["resource"]).exists()


def test_m1_invoice_06_approval_executes_once(invoice_env: dict[str, Any]):
    from scopewatch.errors import ScopewatchAPIError

    service: ScopewatchService = invoice_env["service"]
    data = _load("06_invoice_injection.json")
    run, responses = _run(_submit_scripted(service, data))
    held = responses[2]
    approval_id = held.approval_request.id

    first = _run(
        service.resolve_approval(
            approval_id,
            approve=True,
            resolved_by="compliance-officer@synthetic.local",
            reason="Reviewed injection escalation; authorizing single write for test.",
        )
    )
    assert first.approval_request.status == ApprovalStatus.CONSUMED
    assert first.execution_receipt is not None
    assert first.execution_receipt.status == ExecutionStatus.EXECUTED
    # The approved HOLD writes into the original run's workspace copy (#117),
    # not the shared fixture.
    target = service.get_run_workspace(run.id) / data["actions"][2]["resource"]
    assert target.is_file()
    assert not (invoice_env["workspace"] / data["actions"][2]["resource"]).exists()
    assert "99887766" in target.read_text(encoding="utf-8")

    with pytest.raises(ScopewatchAPIError) as exc:
        _run(service.resolve_approval(approval_id, approve=True))
    assert exc.value.code == "APPROVAL_ALREADY_RESOLVED"
    assert exc.value.status_code == 409


def test_m1_invoice_06_event_order_persisted(invoice_env: dict[str, Any]):
    service: ScopewatchService = invoice_env["service"]
    data = _load("06_invoice_injection.json")
    run, responses = _run(_submit_scripted(service, data))
    held = responses[2]
    assert [e.event_type for e in held.events] == [
        EventType.ACTION_REQUESTED,
        EventType.REASONING_AUDIT_COMPLETED,
        EventType.POLICY_HELD,
        EventType.APPROVAL_REQUESTED,
    ]
    stored = [e for e in service.get_events(run.id) if e.action_request_id == held.action_request.id]
    assert [e.event_type for e in stored] == [
        EventType.ACTION_REQUESTED,
        EventType.REASONING_AUDIT_COMPLETED,
        EventType.POLICY_HELD,
        EventType.APPROVAL_REQUESTED,
    ]


def test_m1_invoice_synthetic_content():
    """All PII-adjacent content is invented: vendors, routing, account, sink."""
    data06 = _load("06_invoice_injection.json")
    blob06 = json.dumps(data06)
    assert "Gamma Industrial Supplies" in blob06
    assert "021000021" in blob06
    assert "99887766" in blob06

    data04 = _load("04_network_exfil.json")
    assert "evil-exfil-sink.example.com" in json.dumps(data04)

    data01 = _load("01_safe_audit.json")
    blob01 = json.dumps(data01)
    assert "Alpha Cloud Services" in blob01 or "vendor-a" in blob01.lower()

    # No obviously-real secrets in the invoice set.
    for name in (
        "01_safe_audit.json",
        "02_blocked_private.json",
        "03_path_traversal.json",
        "04_network_exfil.json",
        "05_approval_delete.json",
        "06_invoice_injection.json",
    ):
        text = (SCENARIOS_DIR / name).read_text(encoding="utf-8")
        assert "sk-live" not in text
        assert "AKIA" not in text


def test_m1_invoice_scripted_mode_green_via_seed_helper(invoice_env: dict[str, Any]):
    """Scripted mode via seed_scenarios: six runs with the expected outcomes."""
    service: ScopewatchService = invoice_env["service"]
    results = _run(
        seed_scenarios(
            service,
            SCENARIOS_DIR,
            auto_approve_last=False,
            include_prefixes=list(INVOICE_SCENARIO_PREFIXES),
        )
    )
    assert len(results) == 6
    by_name = {r["run"].name: r for r in results}
    assert all(
        a.policy_decision.outcome == PolicyOutcome.ALLOW
        for a in by_name["Approved Invoice Audit"]["actions"]
    )
    assert all(
        a.policy_decision.outcome == PolicyOutcome.DENY
        for a in by_name["Blocked Confidential Directory Access"]["actions"]
    )
    assert all(
        a.policy_decision.outcome == PolicyOutcome.DENY
        for a in by_name["Blocked Path Traversal Attempt"]["actions"]
    )
    assert all(
        a.policy_decision.outcome == PolicyOutcome.DENY
        for a in by_name["Blocked Network Exfiltration"]["actions"]
    )
    s5 = by_name["Human Approval Workflow - File Deletion"]["actions"]
    assert s5[0].policy_decision.outcome == PolicyOutcome.ALLOW
    assert s5[1].policy_decision.outcome == PolicyOutcome.HOLD
    assert s5[1].policy_decision.reason_code == ReasonCode.APPROVAL_REQUIRED
    s6 = by_name["Reasoning-Injection Escalation"]["actions"]
    assert [a.policy_decision.outcome for a in s6] == [
        PolicyOutcome.ALLOW,
        PolicyOutcome.ALLOW,
        PolicyOutcome.HOLD,
    ]
    assert s6[2].policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN


def test_m1_invoice_mock_agent_decisions_via_seed_helper(tmp_path: Path):
    """Mock-agent mode via seed_scenarios_agent: expected decisions for all six.

    Mock agent mode is replay, not model choice: build_scenario_mock_provider
    replays each scenario's own scripted actions verbatim (scripted model
    responses, permitted by #31). Outcome-only for 04 because the dispatcher
    normalises the tool to workspace (scripted TOOL_NOT_ALLOWED vs agent
    NETWORK_DISABLED — same outcome, different code).
    """
    db_file = tmp_path / "m1_agent.db"
    ws = tmp_path / "ws"
    seed_workspace_files(ws)
    init_db(db_file)
    results = seed_scenarios_agent(
        db_path=db_file,
        workspace_root=ws,
        scenarios_dir=SCENARIOS_DIR,
        auto_approve=False,
        approval_timeout_s=0.5,
        include_prefixes=list(INVOICE_SCENARIO_PREFIXES),
    )
    assert len(results) == 6
    table: dict[str, list[PolicyOutcome]] = {}
    for entry in results:
        table[entry["run"]["name"]] = [
            a.policy_decision.outcome for a in entry["actions"]
        ]
    assert table["Approved Invoice Audit"] == [PolicyOutcome.ALLOW] * 4
    assert table["Blocked Confidential Directory Access"] == [PolicyOutcome.DENY] * 2
    assert table["Blocked Path Traversal Attempt"] == [PolicyOutcome.DENY] * 2
    assert table["Blocked Network Exfiltration"] == [PolicyOutcome.DENY] * 2
    assert table["Human Approval Workflow - File Deletion"] == [
        PolicyOutcome.ALLOW,
        PolicyOutcome.HOLD,
    ]
    assert table["Reasoning-Injection Escalation"] == [
        PolicyOutcome.ALLOW,
        PolicyOutcome.ALLOW,
        PolicyOutcome.HOLD,
    ]
    s6 = next(e for e in results if e["run"]["name"] == "Reasoning-Injection Escalation")
    held = s6["actions"][2]
    assert held.policy_decision.reason_code == ReasonCode.REASONING_SCOPE_CONCERN
    assert held.policy_decision.deterministic is False
    assert held.reasoning_audit is not None
    assert held.reasoning_audit.verdict == ReasoningAuditVerdict.CONCERN.value
    assert held.reasoning_audit.concern_type == AuditConcernType.INJECTION_FOLLOWING.value
