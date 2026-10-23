"""Run and approval ordering regressions for issue #268, stacked on #190."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scopewatch.evidence_chain import EvidenceChain, KNOWN_EVENT_TYPES

if TYPE_CHECKING:
    from scopewatch.schemas import ApprovalRequest, Run
    from scopewatch.service import ScopewatchService


def _chain(*events: tuple[str, str | None]) -> EvidenceChain:
    chain = EvidenceChain("run-268-synthetic")
    for event_type, action_id in events:
        chain.append(event_type, action_id=action_id)
    return chain


@pytest.mark.parametrize("event_type", ["execution_started", "EXECUTION_FUTURE", "", " POLICY_ALLOWED"])
@pytest.mark.parametrize("action_id", ["a1", None])
def test_unknown_event_type_fails_even_with_valid_hashes(event_type: str, action_id: str | None) -> None:
    chain = _chain(("POLICY_DENIED", "a1"), (event_type, action_id))
    result = EvidenceChain.from_jsonl_string(chain.to_jsonl()).verify()
    assert not result.ok
    assert result.code == "UNKNOWN_EVENT_TYPE"
    assert result.first_bad_seq == 2
    assert result.checked == 1


def test_offline_event_catalog_matches_gateway() -> None:
    from scopewatch.models import EventType

    assert KNOWN_EVENT_TYPES == {event.value for event in EventType}


def _assert_ordering_violation(chain: EvidenceChain, seq: int) -> None:
    result = chain.verify()
    assert not result.ok, result.reason
    assert result.code == "ORDERING_VIOLATION", result.reason
    assert result.first_bad_seq == seq, result.reason
    assert result.checked == seq - 1


@pytest.mark.parametrize("terminal", ["RUN_COMPLETED", "SYSTEM_ERROR"])
@pytest.mark.parametrize("late_event", [
    "RUN_CREATED", "ACTION_REQUESTED", "POLICY_ALLOWED", "POLICY_DENIED", "POLICY_HELD",
    "REASONING_AUDIT_COMPLETED", "REASONING_AUDIT_FAILED",
    "APPROVAL_REQUESTED", "APPROVAL_GRANTED", "APPROVAL_DENIED",
    "EXECUTION_STARTED", "EXECUTION_SUCCEEDED", "EXECUTION_FAILED",
])
def test_action_lifecycle_cannot_continue_after_run_terminal(terminal: str, late_event: str) -> None:
    chain = _chain(
        ("RUN_CREATED", None),
        ("POLICY_ALLOWED", "a1"),
        ("POLICY_HELD", "a2"),
        ("APPROVAL_REQUESTED", "a2"),
        (terminal, None),
        (late_event, "a1"),
    )
    _assert_ordering_violation(chain, 6)
    assert f"after terminal run event {terminal}" in chain.verify().reason


def test_omitting_action_id_cannot_bypass_terminal_run_check() -> None:
    _assert_ordering_violation(_chain(
        ("RUN_COMPLETED", None), ("POLICY_ALLOWED", None),
    ), 2)


@pytest.mark.parametrize("terminal", ["RUN_COMPLETED", "SYSTEM_ERROR"])
@pytest.mark.parametrize("action_id", [None, "unknown-action"])
def test_post_terminal_expiry_requires_a_preexisting_pending_approval(
    terminal: str, action_id: str | None,
) -> None:
    _assert_ordering_violation(_chain(
        (terminal, None), ("APPROVAL_EXPIRED", action_id),
    ), 2)


@pytest.mark.parametrize("terminal", ["RUN_COMPLETED", "SYSTEM_ERROR"])
def test_pending_approval_can_expire_after_terminal_run(terminal: str) -> None:
    chain = _chain(
        ("RUN_CREATED", None),
        ("POLICY_HELD", "a1"),
        ("APPROVAL_REQUESTED", "a1"),
        (terminal, None),
        ("APPROVAL_EXPIRED", "a1"),
    )
    assert chain.verify().ok


@pytest.mark.parametrize("request_action", [None, "a2"])
def test_grant_requires_a_request_for_the_same_action(request_action: str | None) -> None:
    events = [("POLICY_HELD", "a1")]
    if request_action is not None:
        events.extend([("POLICY_HELD", request_action), ("APPROVAL_REQUESTED", request_action)])
    events.append(("APPROVAL_GRANTED", "a1"))
    _assert_ordering_violation(_chain(*events), len(events))


def test_approval_request_must_precede_grant_not_follow_it() -> None:
    _assert_ordering_violation(_chain(
        ("POLICY_HELD", "a1"),
        ("APPROVAL_GRANTED", "a1"),
        ("APPROVAL_REQUESTED", "a1"),
    ), 2)


def test_grant_without_action_id_cannot_bypass_request_check() -> None:
    _assert_ordering_violation(_chain(("APPROVAL_GRANTED", None)), 1)


@pytest.mark.parametrize("closing_event", ["APPROVAL_EXPIRED", "APPROVAL_DENIED"])
@pytest.mark.parametrize("started", [True, False])
def test_granted_approval_cannot_later_be_closed(closing_event: str, started: bool) -> None:
    events = [
        ("POLICY_HELD", "a1"),
        ("APPROVAL_REQUESTED", "a1"),
        ("APPROVAL_GRANTED", "a1"),
    ]
    if started:
        events.append(("EXECUTION_STARTED", "a1"))
    bad_seq = len(events) + 1
    events.append((closing_event, "a1"))
    if not started:
        events.append(("EXECUTION_STARTED", "a1"))
    events.append(("EXECUTION_SUCCEEDED", "a1"))
    _assert_ordering_violation(_chain(*events), bad_seq)


@pytest.fixture
def held_run(tmp_path: Path) -> tuple[ScopewatchService, Run, ApprovalRequest]:
    from scopewatch.db import init_db
    from scopewatch.schemas import SubmitActionRequest, TaskScope
    from scopewatch.service import ScopewatchService

    workspace = tmp_path / "workspace"
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "outputs" / "synthetic.txt").write_text("Synthetic invoice", encoding="utf-8")
    db_path = tmp_path / "evidence.db"
    init_db(db_path)
    service = ScopewatchService(db_path=db_path, workspace_root=workspace)
    run, _ = service.create_run(name="Synthetic evidence lifecycle", task_scope=TaskScope(
        task_description="Review a synthetic output deletion",
        allowed_paths=["outputs"],
        allowed_tools=["workspace"],
        allowed_operations=["delete_path"],
        requires_approval=["delete_path"],
        created_at=datetime.now(timezone.utc).isoformat(),
    ))
    response = asyncio.run(service.submit_action(run.id, SubmitActionRequest(
        tool="workspace", operation="delete_path", resource="outputs/synthetic.txt",
    )))
    assert response.approval_request is not None
    return service, run, response.approval_request


@pytest.mark.parametrize("closure", ["complete_run", "fail_run"])
@pytest.mark.parametrize("resolution", ["deny", "expire", "expire_after_closure"])
def test_real_denied_and_expired_completed_chains_verify(
    held_run: tuple[ScopewatchService, Run, ApprovalRequest],
    monkeypatch: pytest.MonkeyPatch,
    closure: str,
    resolution: str,
) -> None:
    from scopewatch.evidence_chain import build_chain_from_events

    service, run, approval = held_run
    if resolution == "deny":
        asyncio.run(service.resolve_approval(approval.id, approve=False))
    if resolution == "expire_after_closure":
        getattr(service, closure)(run.id)
    if resolution.startswith("expire"):
        class ExpiredClock(datetime):
            @classmethod
            def now(cls, tz=None) -> datetime:
                return datetime.fromisoformat(approval.expires_at) + timedelta(seconds=1)

        monkeypatch.setattr("scopewatch.service.datetime", ExpiredClock)
        expired = service.reconcile_expired_approvals(run_id=run.id)
        assert [item.id for item in expired] == [approval.id]
    if resolution != "expire_after_closure":
        getattr(service, closure)(run.id)
    # The current close API permits repeated close/fail calls. They never reopen
    # the run or authorise a new action, so the verifier preserves these chains.
    service.complete_run(run.id)
    service.fail_run(run.id, reason="Synthetic late failure")
    chain = build_chain_from_events(run.id, service.get_events(run.id))
    result = EvidenceChain.from_jsonl_string(chain.to_jsonl()).verify()
    assert result.ok, result.reason


def test_expired_approval_cannot_be_repeated_after_run_terminal() -> None:
    chain = _chain(
        ("POLICY_HELD", "a1"),
        ("APPROVAL_REQUESTED", "a1"),
        ("RUN_COMPLETED", None),
        ("APPROVAL_EXPIRED", "a1"),
        ("APPROVAL_EXPIRED", "a1"),
    )
    _assert_ordering_violation(chain, 5)


def test_hash_tampering_precedes_unknown_event_validation() -> None:
    chain = _chain(("RUN_CREATED", None))
    records = chain.to_records()
    records[0].event_type = "run_created"
    result = EvidenceChain(chain.run_id, records).verify()
    assert result.code == "BAD_HASH"
    assert result.first_bad_seq == 1
