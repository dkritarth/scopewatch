"""Direct tests of the one shared pre-dispatch gate (issue #105).

``backend/scopewatch/dispatch_gate.py`` is the only place the
approval-status and outcome rules are written. These tests exercise that
module directly, so the rules are pinned at their source rather than only
through each backend that renders a verdict.

``test_executor_gate_consistency.py`` is **kept** alongside this file, not
superseded. It answers a different question: whether the four call sites
(local, Docker, remote, and the ``executor-runner`` sidecar) still agree
end to end, including the parts the gate does not own — the runner's
signature, digest, and one-shot token checks, and the gateway's
``run_command`` digest/normalization. Those are per-backend by design, so
they still need an outside-in comparison. What this file adds is the
inside-out guarantee: the rules themselves have exactly one implementation,
and the runner loads that same file rather than a private copy.
"""

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import uuid

import pytest

from scopewatch.dispatch_gate import (
    APPROVAL_REQUIRED,
    DENY,
    NETWORK_FORBIDDEN,
    NO_DECISION,
    OK,
    OUTCOME_REFUSED,
    POLICY_DENY,
    PROCEED,
    REFUSE,
    authorize_dispatch,
)
from scopewatch.models import ApprovalStatus, PolicyOutcome
from scopewatch.schemas import ActionRequest, ApprovalRequest, PolicyDecision

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "backend" / "scopewatch" / "dispatch_gate.py"
RUNNER_PATH = REPO_ROOT / "deploy" / "executor-runner" / "runner.py"
RUNNER_DOCKERFILE = REPO_ROOT / "deploy" / "executor-runner" / "Dockerfile"
RUNNER_COMPOSE = REPO_ROOT / "deploy" / "executor-runner" / "compose.executor-runner.yaml"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _action(
    operation: str = "read_text",
    action_id: str = "action-1",
) -> ActionRequest:
    return ActionRequest(
        id=action_id,
        run_id="run-1",
        tool="workspace",
        operation=operation,
        resource="docs/file_a.txt",
        arguments={},
        requested_at=_now(),
    )


def _decision(
    outcome: PolicyOutcome = PolicyOutcome.ALLOW, action_id: str = "action-1"
) -> PolicyDecision:
    reason = {
        PolicyOutcome.ALLOW: "ALLOWED_TOOL_AND_RESOURCE",
        PolicyOutcome.HOLD: "APPROVAL_REQUIRED",
        PolicyOutcome.DENY: "BLOCKED_PATH",
    }[outcome]
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=outcome,
        reason_code=reason,
        explanation="probe",
        matched_rule="RULE_PROBE",
        decided_at=_now(),
        deterministic=True,
    )


def _approval(
    action_id: str = "action-1", status: ApprovalStatus = ApprovalStatus.APPROVED
) -> ApprovalRequest:
    return ApprovalRequest(
        id=str(uuid.uuid4()),
        run_id="run-1",
        action_request_id=action_id,
        policy_decision_id=str(uuid.uuid4()),
        status=status,
        requested_at=_now(),
        expires_at=_now(),
        resolved_by=None,
    )


# ---------------------------------------------------------------------------
# Rule 1: a decision must exist. Fail closed.
# ---------------------------------------------------------------------------


def test_missing_decision_refuses() -> None:
    verdict = authorize_dispatch(_action(), None)
    assert verdict.kind == REFUSE
    assert verdict.code == NO_DECISION
    assert verdict.reason == (
        "Direct execution without policy evidence is prohibited."
    )


# ---------------------------------------------------------------------------
# Rule 2: DENY is final, and only DENY reports as a deny verdict.
# ---------------------------------------------------------------------------


def test_deny_is_final() -> None:
    verdict = authorize_dispatch(_action(), _decision(PolicyOutcome.DENY))
    assert verdict.kind == DENY
    assert verdict.code == POLICY_DENY
    assert not verdict.proceed


def test_deny_is_final_even_with_an_approved_approval() -> None:
    """No approval can override a policy DENY."""
    verdict = authorize_dispatch(
        _action(),
        _decision(PolicyOutcome.DENY),
        _approval(status=ApprovalStatus.APPROVED),
    )
    assert verdict.kind == DENY
    assert verdict.code == POLICY_DENY


# ---------------------------------------------------------------------------
# Rule 3: only ALLOW and HOLD authorize; everything else fails closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["UNKNOWN", "allow", "hold", "DENIED", "", "ALLOW "])
def test_non_authorizing_outcome_refuses(outcome: str) -> None:
    """An unrecognised outcome must not fall through to execution.

    The runner already refused these (HTTP 403); unifying the rule means the
    gateway backends refuse them too instead of treating "not DENY, not HOLD"
    as authorized.
    """
    verdict = authorize_dispatch(
        {"id": "action-1", "operation": "read_text"},
        {"outcome": outcome},
    )
    assert verdict.kind == REFUSE
    assert verdict.code == OUTCOME_REFUSED


@pytest.mark.parametrize("outcome", ["ALLOW", "HOLD"])
def test_authorizing_outcomes_proceed(outcome: str) -> None:
    action = {"id": "action-1", "operation": "read_text"}
    approval = (
        {"status": "APPROVED", "action_request_id": "action-1"}
        if outcome == "HOLD"
        else None
    )
    verdict = authorize_dispatch(action, {"outcome": outcome}, approval)
    assert verdict.proceed
    assert verdict.code == OK
    assert verdict.outcome == outcome


# ---------------------------------------------------------------------------
# Rule 4: HOLD needs exactly an APPROVED approval bound to this action.
# ---------------------------------------------------------------------------


def test_hold_with_approved_approval_proceeds() -> None:
    verdict = authorize_dispatch(
        _action(),
        _decision(PolicyOutcome.HOLD),
        _approval(status=ApprovalStatus.APPROVED),
    )
    assert verdict.proceed
    assert verdict.outcome == "HOLD"


@pytest.mark.parametrize(
    "status",
    [
        ApprovalStatus.CONSUMED,
        ApprovalStatus.PENDING,
        ApprovalStatus.DENIED,
        ApprovalStatus.EXPIRED,
    ],
    ids=["consumed", "pending", "denied", "expired"],
)
def test_hold_refuses_every_non_approved_status(status: ApprovalStatus) -> None:
    """Only APPROVED authorizes. CONSUMED authorizes nothing (#66, #104)."""
    verdict = authorize_dispatch(
        _action(),
        _decision(PolicyOutcome.HOLD),
        _approval(status=status),
    )
    assert verdict.kind == REFUSE
    assert verdict.code == APPROVAL_REQUIRED
    assert verdict.reason == "Held action requires valid approved status to execute."


def test_hold_refuses_without_an_approval() -> None:
    verdict = authorize_dispatch(_action(), _decision(PolicyOutcome.HOLD))
    assert verdict.kind == REFUSE
    assert verdict.code == APPROVAL_REQUIRED


def test_hold_refuses_an_approval_bound_to_another_action() -> None:
    verdict = authorize_dispatch(
        _action(action_id="action-1"),
        _decision(PolicyOutcome.HOLD),
        _approval(action_id="action-2"),
    )
    assert verdict.kind == REFUSE
    assert verdict.code == APPROVAL_REQUIRED


def test_hold_refuses_when_the_action_has_no_id() -> None:
    """An empty action id cannot match an approval, so nothing proceeds."""
    verdict = authorize_dispatch(
        {"operation": "read_text"},
        {"outcome": "HOLD"},
        {"status": "APPROVED", "action_request_id": ""},
    )
    assert verdict.kind == REFUSE
    assert verdict.code == APPROVAL_REQUIRED


def test_allow_ignores_a_supplied_approval() -> None:
    """The approval rules apply to HOLD; an ALLOW carries none.

    The runner refuses an approval accompanying an ALLOW as signed-protocol
    hygiene; the gateway backends pass whatever they were given and must not
    refuse on it. The gate therefore does not invent a rule here.
    """
    verdict = authorize_dispatch(
        _action(),
        _decision(PolicyOutcome.ALLOW),
        _approval(status=ApprovalStatus.CONSUMED),
    )
    assert verdict.proceed


# ---------------------------------------------------------------------------
# Rule 5: network_request never executes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", [PolicyOutcome.ALLOW, PolicyOutcome.HOLD])
def test_network_request_never_proceeds(outcome: PolicyOutcome) -> None:
    approval = (
        _approval(status=ApprovalStatus.APPROVED) if outcome == PolicyOutcome.HOLD else None
    )
    verdict = authorize_dispatch(
        _action("network_request"), _decision(outcome), approval
    )
    assert verdict.kind == REFUSE
    assert verdict.code == NETWORK_FORBIDDEN
    assert verdict.reason == "Network requests are forbidden in synthetic executor."


# ---------------------------------------------------------------------------
# Input shapes: the gateway passes pydantic models, the runner passes dicts.
# ---------------------------------------------------------------------------


def test_mapping_and_model_inputs_agree() -> None:
    """Both input shapes reach the same verdict for the same case."""
    models = authorize_dispatch(
        _action(),
        _decision(PolicyOutcome.HOLD),
        _approval(status=ApprovalStatus.APPROVED),
    )
    mappings = authorize_dispatch(
        {"id": "action-1", "operation": "read_text"},
        {"outcome": "HOLD"},
        {"status": "APPROVED", "action_request_id": "action-1"},
    )
    assert (models.kind, models.code, models.outcome) == (
        mappings.kind,
        mappings.code,
        mappings.outcome,
    )


def test_enum_outcomes_and_statuses_are_unwrapped() -> None:
    """Enum members and their string values are the same input."""
    verdict = authorize_dispatch(
        {"id": "action-1", "operation": "read_text"},
        {"outcome": PolicyOutcome.HOLD},
        {"status": ApprovalStatus.APPROVED, "action_request_id": "action-1"},
    )
    assert verdict.proceed
    assert verdict.outcome == "HOLD"


def test_missing_fields_fail_closed() -> None:
    """Malformed input is a refusal, never a crash and never a proceed.

    Covers every field the gate actually reads. The action ``id`` is read only
    on the ``HOLD`` path (to bind the approval) and ``operation`` only to spot
    ``network_request``, so an ``ALLOW`` with either absent is not this gate's
    business: the backends require both from the request schema, and the runner
    binds the decision to the action separately. Adding rules here would be a
    behaviour change, which issue #105 explicitly rules out.
    """
    for action, decision, approval in (
        # A held action whose approval cannot bind to an absent action id.
        ({"operation": "read_text"}, {"outcome": "HOLD"}, {"status": "APPROVED"}),
        (
            {"operation": "read_text"},
            {"outcome": "HOLD"},
            {"status": "APPROVED", "action_request_id": "action-1"},
        ),
        # A decision with no readable outcome.
        ({"id": "action-1", "operation": "read_text"}, {}, None),
        ({"id": "action-1", "operation": "read_text"}, {"outcome": 7}, None),
        ({"id": "action-1", "operation": "read_text"}, {"outcome": None}, None),
        # An approval whose status is not readable text.
        (
            {"id": "action-1", "operation": "read_text"},
            {"outcome": "HOLD"},
            {"status": 7, "action_request_id": "action-1"},
        ),
        (
            {"id": "action-1", "operation": "read_text"},
            {"outcome": "HOLD"},
            {"action_request_id": "action-1"},
        ),
    ):
        verdict = authorize_dispatch(action, decision, approval)
        assert verdict.kind == REFUSE, (action, decision, approval)


# ---------------------------------------------------------------------------
# The gate is stdlib-only, because the runner imports it (issue #105).
# ---------------------------------------------------------------------------


def test_gate_is_stdlib_only() -> None:
    """No ``scopewatch`` import: runner.py must not need pydantic or httpx."""
    source = GATE_PATH.read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert not stripped.startswith("from scopewatch"), line
            assert not stripped.startswith("import scopewatch"), line


# ---------------------------------------------------------------------------
# The runner loads this same file, not a private copy.
# ---------------------------------------------------------------------------


def _load_runner():
    spec = importlib.util.spec_from_file_location("dispatch_gate_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_loads_the_authored_gate_file() -> None:
    """The sidecar resolves the gate to its single authored location.

    A vendored second copy is the drift this issue removes, so pin the
    resolution rather than trusting the import to land on the right file.
    """
    runner_mod = _load_runner()
    loaded = Path(runner_mod.dispatch_gate.__file__).resolve()
    assert loaded == GATE_PATH.resolve()


def test_runner_refusals_cover_every_gate_refusal_code() -> None:
    """Each refusing verdict code has an HTTP rendering; none falls through.

    An unmapped code would reach dispatch, so this is a fail-closed check on
    the mapping rather than a formatting assertion.
    """
    runner_mod = _load_runner()
    for code in (NO_DECISION, POLICY_DENY, OUTCOME_REFUSED, APPROVAL_REQUIRED, NETWORK_FORBIDDEN):
        assert code in runner_mod.GATE_REFUSALS, code
        status, payload = runner_mod.GATE_REFUSALS[code]
        assert 400 <= status < 500
        assert set(payload) == {"error"} and payload["error"]


def test_runner_image_copies_the_authored_gate() -> None:
    """The image must ship the gate from its single authored location.

    The runner imports ``scopewatch.dispatch_gate`` inside the container, so
    the Dockerfile has to copy the backend file into the package directory
    next to ``docker_job.py``. The build context must therefore be the repo
    root, not this directory.
    """
    dockerfile = RUNNER_DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "COPY backend/scopewatch/dispatch_gate.py "
        "/app/scopewatch/dispatch_gate.py" in dockerfile
    )
    assert "COPY deploy/executor-runner/runner.py /app/runner.py" in dockerfile
    compose = RUNNER_COMPOSE.read_text(encoding="utf-8")
    assert "context: .." in compose
    assert "dockerfile: deploy/executor-runner/Dockerfile" in compose


def test_no_vendored_gate_copy_in_the_runner_directory() -> None:
    """One authored copy: nothing else in deploy/ may re-declare the gate."""
    runner_dir = REPO_ROOT / "deploy" / "executor-runner"
    vendored = [
        path.name
        for path in runner_dir.glob("*.py")
        if path.name not in ("runner.py", "test_gate.py")
    ]
    assert vendored == [], vendored


def test_runner_does_not_restate_the_gate_rules() -> None:
    """No gate rule may be re-implemented inside the sidecar.

    Checks the concrete rule literals and the outcome/approval comparisons that
    encode them, so a future change cannot quietly add a second copy.
    """
    source = RUNNER_PATH.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    for literal in (
        "Direct execution without policy evidence",
        "Held action requires valid approved status to execute",
        "Network requests are forbidden",
        '"APPROVED"',
        '"DENY"',
        '"ALLOW", "HOLD"',
        '== "network_request"',
    ):
        assert literal not in body, literal