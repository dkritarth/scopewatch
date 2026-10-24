"""Per-action lifecycle checks in the evidence-chain verifier (issue #190).

Before #190 the verifier treated *any* decision event (including
``POLICY_DENIED`` and ``POLICY_HELD``) as authorisation for execution, so a
tampered or buggy log that broke "no decision, no execution" still verified
OK. These tests hand-build chains with valid hashes but forbidden lifecycles
and expect ``ORDERING_VIOLATION`` at the first offending record, and check that
the lifecycles the gateway really produces (including seeded demo runs) still
verify.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

from scopewatch.evidence_chain import EvidenceChain, build_chain_from_events


RUN = "run-lifecycle-0001"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _chain(*events: tuple[str, str | None]) -> EvidenceChain:
    chain = EvidenceChain(run_id=RUN)
    for event_type, action_id in events:
        chain.append(event_type, action_id=action_id, payload={"e": event_type})
    return chain


def _assert_violation(chain: EvidenceChain, seq: int) -> None:
    result = chain.verify()
    assert result.ok is False, result.reason
    assert result.code == "ORDERING_VIOLATION", result.reason
    assert result.first_bad_seq == seq, result.reason


# ---------------------------------------------------------------------------
# The three chains named in the issue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "execution_event",
    ["EXECUTION_STARTED", "EXECUTION_SUCCEEDED", "EXECUTION_FAILED"],
)
def test_execution_after_policy_denied_is_a_violation(execution_event: str) -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_DENIED", "a1"),
        (execution_event, "a1"),
    )
    _assert_violation(chain, 3)


@pytest.mark.parametrize(
    "execution_event",
    ["EXECUTION_STARTED", "EXECUTION_SUCCEEDED", "EXECUTION_FAILED"],
)
def test_held_action_executed_without_approval_is_a_violation(execution_event: str) -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_HELD", "a1"),
        ("APPROVAL_REQUESTED", "a1"),
        (execution_event, "a1"),
    )
    _assert_violation(chain, 4)


def test_double_execution_started_is_a_violation() -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_ALLOWED", "a1"),
        ("EXECUTION_STARTED", "a1"),
        ("EXECUTION_SUCCEEDED", "a1"),
        ("EXECUTION_STARTED", "a1"),
        ("EXECUTION_SUCCEEDED", "a1"),
    )
    _assert_violation(chain, 5)


def test_two_terminal_execution_events_are_a_violation() -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_ALLOWED", "a1"),
        ("EXECUTION_STARTED", "a1"),
        ("EXECUTION_SUCCEEDED", "a1"),
        ("EXECUTION_FAILED", "a1"),
    )
    _assert_violation(chain, 5)


def test_double_execution_without_started_is_a_violation() -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_ALLOWED", "a1"),
        ("EXECUTION_SUCCEEDED", "a1"),
        ("EXECUTION_SUCCEEDED", "a1"),
    )
    _assert_violation(chain, 4)


def test_started_after_terminal_is_a_violation() -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_ALLOWED", "a1"),
        ("EXECUTION_FAILED", "a1"),
        ("EXECUTION_STARTED", "a1"),
    )
    _assert_violation(chain, 4)


# ---------------------------------------------------------------------------
# Neighbouring forgeries the same state machine must catch
# ---------------------------------------------------------------------------


def test_second_decision_cannot_relax_a_deny() -> None:
    """Appending POLICY_ALLOWED after POLICY_DENIED must not unlock execution."""
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_DENIED", "a1"),
        ("POLICY_ALLOWED", "a1"),
        ("EXECUTION_STARTED", "a1"),
    )
    _assert_violation(chain, 3)


def test_second_decision_cannot_skip_an_approval() -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_HELD", "a1"),
        ("POLICY_ALLOWED", "a1"),
        ("EXECUTION_STARTED", "a1"),
    )
    _assert_violation(chain, 3)


def test_approval_cannot_override_policy_deny() -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_DENIED", "a1"),
        ("APPROVAL_GRANTED", "a1"),
        ("EXECUTION_STARTED", "a1"),
    )
    _assert_violation(chain, 3)


def test_approval_without_a_hold_is_a_violation() -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("APPROVAL_GRANTED", "a1"),
    )
    _assert_violation(chain, 2)


def test_approval_is_single_use() -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_HELD", "a1"),
        ("APPROVAL_GRANTED", "a1"),
        ("APPROVAL_GRANTED", "a1"),
    )
    _assert_violation(chain, 4)


@pytest.mark.parametrize("closing_event", ["APPROVAL_DENIED", "APPROVAL_EXPIRED"])
def test_closed_approval_cannot_be_granted_later(closing_event: str) -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_HELD", "a1"),
        ("APPROVAL_REQUESTED", "a1"),
        (closing_event, "a1"),
        ("APPROVAL_GRANTED", "a1"),
    )
    _assert_violation(chain, 5)


@pytest.mark.parametrize("closing_event", ["APPROVAL_DENIED", "APPROVAL_EXPIRED"])
def test_held_action_with_rejected_approval_cannot_execute(closing_event: str) -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_HELD", "a1"),
        ("APPROVAL_REQUESTED", "a1"),
        (closing_event, "a1"),
        ("EXECUTION_STARTED", "a1"),
    )
    _assert_violation(chain, 5)


def test_approval_for_one_action_does_not_authorise_another() -> None:
    chain = _chain(
        ("POLICY_HELD", "a1"),
        ("POLICY_HELD", "a2"),
        ("APPROVAL_GRANTED", "a1"),
        ("EXECUTION_STARTED", "a2"),
    )
    _assert_violation(chain, 4)


# ---------------------------------------------------------------------------
# Lifecycles the gateway really produces must still verify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", ["EXECUTION_SUCCEEDED", "EXECUTION_FAILED"])
def test_allowed_lifecycle_verifies(terminal: str) -> None:
    chain = _chain(
        ("RUN_CREATED", None),
        ("ACTION_REQUESTED", "a1"),
        ("REASONING_AUDIT_COMPLETED", "a1"),
        ("POLICY_ALLOWED", "a1"),
        ("EXECUTION_STARTED", "a1"),
        (terminal, "a1"),
        ("RUN_COMPLETED", None),
    )
    assert chain.verify().ok is True


@pytest.mark.parametrize("terminal", ["EXECUTION_SUCCEEDED", "EXECUTION_FAILED"])
def test_held_then_approved_lifecycle_verifies(terminal: str) -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_HELD", "a1"),
        ("APPROVAL_REQUESTED", "a1"),
        ("APPROVAL_GRANTED", "a1"),
        ("EXECUTION_STARTED", "a1"),
        (terminal, "a1"),
    )
    assert chain.verify().ok is True


@pytest.mark.parametrize("closing_event", ["APPROVAL_DENIED", "APPROVAL_EXPIRED"])
def test_held_then_rejected_without_execution_verifies(closing_event: str) -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_HELD", "a1"),
        ("APPROVAL_REQUESTED", "a1"),
        (closing_event, "a1"),
    )
    assert chain.verify().ok is True


def test_denied_without_execution_verifies() -> None:
    chain = _chain(("ACTION_REQUESTED", "a1"), ("POLICY_DENIED", "a1"))
    assert chain.verify().ok is True


def test_interleaved_actions_each_follow_their_own_lifecycle() -> None:
    chain = _chain(
        ("ACTION_REQUESTED", "a1"),
        ("POLICY_HELD", "a1"),
        ("APPROVAL_REQUESTED", "a1"),
        ("ACTION_REQUESTED", "a2"),
        ("POLICY_ALLOWED", "a2"),
        ("EXECUTION_STARTED", "a2"),
        ("ACTION_REQUESTED", "a3"),
        ("POLICY_DENIED", "a3"),
        ("EXECUTION_SUCCEEDED", "a2"),
        ("APPROVAL_GRANTED", "a1"),
        ("EXECUTION_STARTED", "a1"),
        ("EXECUTION_FAILED", "a1"),
    )
    assert chain.verify().ok is True


# ---------------------------------------------------------------------------
# Real gateway runs: seeded demo scenarios and the approval API path
# ---------------------------------------------------------------------------


def _verify_all_runs(service) -> int:
    runs = service.list_runs()
    assert runs, "seeding produced no runs"
    for run in runs:
        events = service.get_events(run.id)
        result = build_chain_from_events(run.id, events).verify()
        assert result.ok, f"run {run.name!r}: {result.code} {result.reason}"
    return len(runs)


@pytest.mark.parametrize("auto_approve", [True, False])
def test_seeded_invoice_demo_chains_verify(tmp_path: Path, auto_approve: bool) -> None:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from seed_demo import INVOICE_SCENARIO_PREFIXES, seed_scenarios, seed_workspace_files

    from scopewatch.db import init_db
    from scopewatch.service import ScopewatchService

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seed_workspace_files(workspace)
    db_file = tmp_path / "seed.db"
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=workspace)

    asyncio.run(
        seed_scenarios(
            service,
            REPO_ROOT / "demo" / "scenarios",
            auto_approve_last=auto_approve,
            include_prefixes=INVOICE_SCENARIO_PREFIXES,
        )
    )
    assert _verify_all_runs(service) >= 6


def test_seeded_coding_demo_chains_verify(tmp_path: Path) -> None:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from seed_demo import CODING_SCENARIO_PREFIXES, seed_coding_workspace_files, seed_scenarios

    from scopewatch.db import init_db
    from scopewatch.service import ScopewatchService

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seed_coding_workspace_files(workspace)
    db_file = tmp_path / "seed.db"
    init_db(db_file)
    service = ScopewatchService(db_path=db_file, workspace_root=workspace)

    asyncio.run(
        seed_scenarios(
            service,
            REPO_ROOT / "demo" / "scenarios",
            auto_approve_last=True,
            include_prefixes=CODING_SCENARIO_PREFIXES,
        )
    )
    assert _verify_all_runs(service) >= 1
