#!/usr/bin/env python3
"""Seed demo scenarios and synthetic workspace files into Scopewatch."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys

# Ensure backend package is in python path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from scopewatch.db import init_db
from scopewatch.models import (
    ApprovalStatus,
    PolicyOutcome,
    ReasoningProvenance,
    RunStatus,
)
from scopewatch.schemas import (
    CreateRunRequest,
    SubmitActionRequest,
    TaskScope,
)
from scopewatch.service import ScopewatchService

logger = logging.getLogger("scopewatch.seed")


def seed_workspace_files(workspace_root: Path) -> None:
    """Create synthetic demonstration files inside the designated workspace root."""
    approved_dir = workspace_root / "invoices" / "approved"
    private_dir = workspace_root / "invoices" / "private"
    outputs_dir = workspace_root / "outputs"

    approved_dir.mkdir(parents=True, exist_ok=True)
    private_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    vendor_a = approved_dir / "vendor-a.txt"
    if not vendor_a.exists():
        vendor_a.write_text(
            "INVOICE #INV-2026-001\nVendor: Alpha Cloud Services\nAmount: $4,500.00\nStatus: Approved for payment\nApproved by: Accounting Team\n",
            encoding="utf-8",
        )

    vendor_b = approved_dir / "vendor-b.txt"
    if not vendor_b.exists():
        vendor_b.write_text(
            "INVOICE #INV-2026-002\nVendor: Beta Office Logistics\nAmount: $1,250.00\nStatus: Approved for payment\nApproved by: Procurement Lead\n",
            encoding="utf-8",
        )

    salaries = private_dir / "executive-salaries.txt"
    if not salaries.exists():
        salaries.write_text(
            "CONFIDENTIAL - RESTRICTED ACCESS\nExecutive Compensation Schedule FY2026\nCEO: $350,000\nCTO: $320,000\nCFO: $310,000\nUnauthorized access or exfiltration is strictly prohibited.\n",
            encoding="utf-8",
        )

    payroll = private_dir / "payroll.txt"
    if not payroll.exists():
        payroll.write_text(
            "CONFIDENTIAL PAYROLL SUMMARY\nTotal Disbursed: $1,840,000\nPay Period: August 2026\nRestricted to HR personnel only.\n",
            encoding="utf-8",
        )

    archive = outputs_dir / "archive_2025.txt"
    if not archive.exists():
        archive.write_text(
            "Legacy Annual Financial Summary 2025\nAudited Status: Archived\nRetention Requirement: 7 Years\nDeletion requires dual administrative approval.\n",
            encoding="utf-8",
        )


async def seed_scenarios(
    service: ScopewatchService,
    scenarios_dir: Path,
    auto_approve_last: bool = False,
) -> list[dict[str, object]]:
    """Load JSON scenarios from disk and submit them through ScopewatchService."""
    scenario_files = sorted(scenarios_dir.glob("*.json"))
    if not scenario_files:
        print(f"No scenario files found in {scenarios_dir}")
        return []

    results = []

    for scenario_file in scenario_files:
        try:
            data = json.loads(scenario_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Error parsing scenario {scenario_file.name}: {exc}")
            continue

        name = data.get("name", scenario_file.stem)
        task_scope_data = data.get("task_scope", {})
        task_scope = TaskScope(
            task_description=task_scope_data.get("task_description", "Synthetic demo run"),
            allowed_paths=task_scope_data.get("allowed_paths", []),
            blocked_paths=task_scope_data.get("blocked_paths", []),
            allowed_tools=task_scope_data.get("allowed_tools", []),
            allowed_operations=task_scope_data.get("allowed_operations", []),
            allowed_network_destinations=task_scope_data.get("allowed_network_destinations", []),
            requires_approval=task_scope_data.get("requires_approval", []),
            created_at=task_scope_data.get("created_at", datetime.now(timezone.utc).isoformat()),
        )

        run, _ = service.create_run(name=name, task_scope=task_scope)
        print(f"\n[RUN] Created run '{run.name}' (id: {run.id})")

        actions = data.get("actions", [])
        scenario_actions = []

        for act in actions:
            action_req = SubmitActionRequest(
                tool=act["tool"],
                operation=act["operation"],
                resource=act["resource"],
                arguments=act.get("arguments", {}),
                requested_by=act.get("requested_by", "synthetic-agent"),
                reasoning_summary=act.get("reasoning_summary"),
                reasoning_provenance=ReasoningProvenance.AGENT_AUTHORED_SUMMARY
                if act.get("reasoning_summary")
                else None,
            )

            res = await service.submit_action(run.id, action_req)
            outcome = res.policy_decision.outcome
            reason = res.policy_decision.reason_code
            print(f"  -> Action: {act['tool']}.{act['operation']}({act['resource']})")
            print(f"     Outcome: {outcome.value} [{reason.value}]")

            if outcome == PolicyOutcome.HOLD:
                approval = res.approval_request
                if approval:
                    print(f"     Approval generated: {approval.id} (Status: {approval.status.value})")
                    if auto_approve_last:
                        resolved = await service.resolve_approval(
                            approval.id,
                            approve=True,
                            resolved_by="compliance-officer@synthetic.local",
                            reason="Authorized operational cleanup after audit verification",
                        )
                        print(f"     Auto-resolved approval: {resolved.approval_request.status.value}")

            scenario_actions.append(res)

        results.append({"run": run, "actions": scenario_actions})

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Scopewatch synthetic demo scenarios.")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=REPO_ROOT / "runtime-data" / "scopewatch.db",
        help="Path to SQLite database file",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=REPO_ROOT / "demo" / "workspace",
        help="Path to synthetic demo workspace root",
    )
    parser.add_argument(
        "--scenarios-dir",
        type=Path,
        default=REPO_ROOT / "demo" / "scenarios",
        help="Path to directory containing scenario JSON files",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve hold actions instead of leaving them pending",
    )

    args = parser.parse_args()

    print("==================================================================")
    print("Scopewatch Demo Seeder")
    print("Mediation baseline for synthetic demo actions only.")
    print("==================================================================")

    # 1. Initialize SQLite database
    args.db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(args.db_path)
    print(f"Database initialized: {args.db_path}")

    # 2. Seed workspace files
    seed_workspace_files(args.workspace_root)
    print(f"Workspace fixtures seeded: {args.workspace_root}")

    # 3. Initialize service and seed scenarios
    service = ScopewatchService(db_path=args.db_path, workspace_root=args.workspace_root)
    results = asyncio.run(
        seed_scenarios(
            service,
            scenarios_dir=args.scenarios_dir,
            auto_approve_last=args.auto_approve,
        )
    )

    print("\n------------------------------------------------------------------")
    print(f"Seeding completed: {len(results)} scenarios loaded successfully.")
    print("------------------------------------------------------------------\n")


if __name__ == "__main__":
    main()
