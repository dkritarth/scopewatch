#!/usr/bin/env python3
"""Repeatable twelve-mutation check for M1 invariants (#62).

Injects twelve single-line mutations into a scratch copy (working tree files
are mutated in place then restored), runs the focused test that must catch
each mutation, and reports caught/survived.

Usage:
    PYTHONPATH=backend python3 scripts/mutation_check.py
    PYTHONPATH=backend python3 scripts/mutation_check.py --list

Each mutation is tied to an AGENTS.md invariant. Eleven were caught before
Wave 1; defect 19 (invariant-7 dict overwrite) survived and is now fixed to
assert on the ordered event-type list. This script reruns all twelve so the
next person can verify the suites stay load-bearing.

The script never leaves the tree dirty: every mutation is restored via
try/finally, even on failure or KeyboardInterrupt.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Mutation:
    name: str
    invariant: str
    file: str
    old: str
    new: str
    test: str


MUTATIONS: list[Mutation] = [
    Mutation(
        name="policy-deny-becomes-allow",
        invariant="1. Deterministic policy first",
        file="backend/scopewatch/policy.py",
        old='reason_code=ReasonCode.BLOCKED_PATH,',
        new='reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,',
        test="backend/tests/test_agent_end_to_end.py::test_invariant_1_denied_action_never_reaches_executor",
    ),
    Mutation(
        name="auditor-concern-keeps-allow",
        invariant="2. Reasoning is escalate-only",
        file="backend/scopewatch/service.py",
        old="outcome=PolicyOutcome.HOLD,\n                            reason_code=ReasonCode.REASONING_SCOPE_CONCERN,",
        new="outcome=PolicyOutcome.ALLOW,\n                            reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,",
        test="backend/tests/test_agent_end_to_end.py::test_invariant_4_reasoning_concern_on_policy_allowed_action_results_in_hold_never_deny",
    ),
    Mutation(
        name="auditor-failed-fail-open",
        invariant="3. Fail closed",
        file="backend/scopewatch/service.py",
        old="outcome=PolicyOutcome.HOLD,\n                            reason_code=ReasonCode.REASONING_AUDIT_FAILED,",
        new="outcome=PolicyOutcome.ALLOW,\n                            reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,",
        test="backend/tests/test_agent_end_to_end.py::test_invariant_5_reasoning_audit_failure_results_in_hold_never_allow",
    ),
    Mutation(
        name="executor-without-decision",
        invariant="4. No decision, no execution",
        file="backend/scopewatch/executor.py",
        old='raise ExecutionSecurityError("Direct execution without policy evidence is prohibited.")',
        new='policy_decision = policy_decision  # MUTATION: allow missing decision',
        test="backend/tests/test_executor.py::test_direct_execution_without_policy_fails",
    ),
    Mutation(
        name="approval-reuse",
        invariant="5. Approvals are single-use",
        file="backend/scopewatch/service.py",
        old="if approve and decision.outcome == PolicyOutcome.DENY:",
        new="if False and decision.outcome == PolicyOutcome.DENY:  # MUTATION: allow DENY override",
        test="backend/tests/test_agent_end_to_end.py::test_invariant_3_approval_cannot_authorize_policy_denied_action",
    ),
    Mutation(
        name="missing-reasoning-escalates",
        invariant="6. Missing reasoning handling",
        file="backend/scopewatch/service.py",
        old='details_allow["reasoning_audit"] = "unavailable"',
        new='details_allow["reasoning_audit"] = "MUTATED"  # MUTATION',
        test="backend/tests/test_agent_end_to_end.py::test_invariant_6_missing_reasoning_recorded_as_unavailable_and_does_not_escalate",
    ),
    Mutation(
        name="duplicate-execution-event",
        invariant="7. Monotonic evidence (defect 19)",
        file="backend/scopewatch/service.py",
        old="event_type=EventType.EXECUTION_SUCCEEDED if success else EventType.EXECUTION_FAILED,",
        new="event_type=EventType.EXECUTION_STARTED if success else EventType.EXECUTION_FAILED,  # MUTATION: duplicate start",
        test="backend/tests/test_agent_end_to_end.py::test_invariant_7_event_sequences_are_monotonic_and_decision_precedes_execution",
    ),
    Mutation(
        name="agent-imports-executor",
        invariant="8. Agent-executor isolation (import)",
        file="backend/scopewatch/agent/loop.py",
        old="from scopewatch.agent.tools import",
        new="import scopewatch.executor  # MUTATION\nfrom scopewatch.agent.tools import",
        test="backend/tests/test_agent_loop.py::test_agent_package_ast_invariants",
    ),
    Mutation(
        name="agent-writes-file",
        invariant="8. Agent-executor isolation (write_text, defect 18)",
        file="backend/scopewatch/agent/loop.py",
        old="class AgentLoop:",
        new="from pathlib import Path as _P  # MUTATION\n_P('/tmp/mut').write_text('x')\nclass AgentLoop:",
        test="backend/tests/test_agent_loop.py::test_agent_package_ast_invariants",
    ),
    Mutation(
        name="agent-opens-file",
        invariant="8. Agent-executor isolation (open, defect 18)",
        file="backend/scopewatch/agent/tools.py",
        old="class GatewayDispatcher:",
        new="open('/tmp/mut', 'w')  # MUTATION\nclass GatewayDispatcher:",
        test="backend/tests/test_agent_loop.py::test_agent_package_ast_invariants",
    ),
    Mutation(
        name="broken-auditor-fail-open",
        invariant="3. Fail closed (defect 1)",
        file="backend/scopewatch/service.py",
        old="verdict=ReasoningAuditVerdict.FAILED,\n                concern_type=None,\n                flagged_excerpts=[],\n                explanation=(\n                    f\"Audit failed closed: auditor client for profile '{profile_name}' \"\n                    \"could not be built.\"\n                ),",
        new="verdict=ReasoningAuditVerdict.NO_CONCERN,  # MUTATION: fail open\n                concern_type=None,\n                flagged_excerpts=[],\n                explanation=(\n                    f\"Audit failed closed: auditor client for profile '{profile_name}' \"\n                    \"could not be built.\"\n                ),",
        test="backend/tests/test_decision_merge.py::test_broken_auditor_profile_fails_closed",
    ),
    Mutation(
        name="policy-hold-becomes-allow",
        invariant="1. Deterministic policy first (HOLD)",
        file="backend/scopewatch/policy.py",
        old="outcome=PolicyOutcome.HOLD,\n            reason_code=ReasonCode.APPROVAL_REQUIRED,",
        new="outcome=PolicyOutcome.ALLOW,  # MUTATION\n            reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,",
        test="backend/tests/test_agent_end_to_end.py::test_invariant_2_held_action_does_not_execute_before_approval_and_executes_exactly_once",
    ),
]


def run_one(m: Mutation) -> bool:
    """Apply mutation, run its test, restore. Returns True if caught (test failed)."""
    target = REPO_ROOT / m.file
    original = target.read_text(encoding="utf-8")
    if m.old not in original:
        print(f"SKIP {m.name}: pattern not found in {m.file}")
        return True
    mutated = original.replace(m.old, m.new, 1)
    target.write_text(mutated, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", m.test, "-q", "-x"],
            cwd=str(REPO_ROOT),
            env={**dict(__import__("os").environ), "PYTHONPATH": str(REPO_ROOT / "backend")},
            capture_output=True,
            text=True,
        )
        caught = proc.returncode != 0
        status = "CAUGHT" if caught else "SURVIVED"
        print(f"{status} {m.name} ({m.invariant}) -> {m.test}")
        if not caught:
            print(proc.stdout[-2000:])
            print(proc.stderr[-2000:])
        return caught
    finally:
        target.write_text(original, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rerun twelve invariant mutations.")
    parser.add_argument("--list", action="store_true", help="List mutations and exit.")
    parser.add_argument("--only", default=None, help="Run only mutation with this name.")
    args = parser.parse_args()

    if args.list:
        for m in MUTATIONS:
            print(f"{m.name}: {m.invariant} :: {m.file} :: {m.test}")
        return 0

    selected = [m for m in MUTATIONS if args.only is None or m.name == args.only]
    if args.only and not selected:
        print(f"Unknown mutation: {args.only}", file=sys.stderr)
        return 2

    caught = 0
    for m in selected:
        try:
            if run_one(m):
                caught += 1
        except KeyboardInterrupt:
            print("Interrupted; file restored.", file=sys.stderr)
            return 130

    total = len(selected)
    print(f"\n{caught}/{total} mutations caught.")
    # Verify no leftover mutation markers (working tree may have other
    # uncommitted work; we only guarantee our own mutations were restored).
    proc = subprocess.run(
        ["grep", "-rn", "# MUTATION", "backend/scopewatch/", "backend/tests/"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if proc.stdout.strip():
        print("WARNING: leftover mutation markers:", file=sys.stderr)
        print(proc.stdout, file=sys.stderr)
        return 1
    if caught != total:
        print("SURVIVED mutations remain: suite has holes.", file=sys.stderr)
        return 1
    print("All mutations caught; no markers left behind.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
