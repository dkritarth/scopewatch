"""Gap-close path tricks for issue #39 (second-pass evidence).

Supplements ``backend/tests/test_bypass.py`` — every case here was verified
absent from that suite (grep for ``def test_bypass_`` on origin/main). Policy
and executor sources are treated as READ-ONLY; this file only observes.

Conventions: synthetic fixtures only, fail-closed expectations, no network.
``DENY → NOT_EXECUTED`` asserts the decision plus a NOT_EXECUTED receipt and
byte-identical sensitive fixtures before/after.
"""

from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from scopewatch.executor import execute_action
from scopewatch.models import ExecutionStatus, PolicyOutcome, ReasonCode, RunStatus
from scopewatch.policy import evaluate_policy
from scopewatch.schemas import ActionRequest, Run, TaskScope


NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def gap_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "invoices" / "approved").mkdir(parents=True)
    (ws / "invoices" / "private").mkdir(parents=True)
    (ws / "outputs").mkdir(parents=True)
    (ws / "secrets").mkdir(parents=True)
    (ws / "invoices" / "approved" / "vendor-a.txt").write_text(
        "vendor a data", encoding="utf-8"
    )
    (ws / "invoices" / "private" / "payroll.txt").write_text(
        "CONFIDENTIAL", encoding="utf-8"
    )
    (ws / "secrets" / "notes.txt").write_text("synthetic secret", encoding="utf-8")
    (ws / "outputs" / "old.txt").write_text("old data", encoding="utf-8")
    return ws


@pytest.fixture
def gap_run() -> Run:
    scope = TaskScope(
        schema_version="1",
        task_description="Read approved invoices and write outputs.",
        allowed_paths=["invoices/approved", "outputs"],
        blocked_paths=["invoices/private", "secrets"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "write_text"],
        allowed_network_destinations=[],
        requires_approval=[],
        created_at=NOW,
    )
    return Run(
        id=str(uuid.uuid4()),
        name="Gapclose Path Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


def _read(run: Run, resource: str, operation: str = "read_text") -> ActionRequest:
    return ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation=operation,
        resource=resource,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


def _snapshot(ws: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    for rel in (
        "invoices/private/payroll.txt",
        "secrets/notes.txt",
        "outputs/old.txt",
    ):
        p = ws / rel
        if p.exists() and not p.is_symlink():
            snap[rel] = p.read_text(encoding="utf-8")
    return snap


def _assert_denied(run: Run, ws: Path, resource: str, reason: ReasonCode) -> None:
    before = _snapshot(ws)
    decision = evaluate_policy(_read(run, resource), run, ws)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == reason
    receipt = execute_action(_read(run, resource), ws, policy_decision=decision)
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert _snapshot(ws) == before


def test_gapclose_backslash_traversal_is_inert(gap_workspace: Path, gap_run: Run) -> None:
    """Backslash ``..`` traversal is inert — and since issues #69/#70 it is
    inert by DENY, not by executor luck. ``normalize_relative_path`` folds
    ``\\`` to ``/`` before matching, so both variants resolve into the
    blocked ``invoices/private`` directory and policy returns
    ``DENY BLOCKED_PATH`` with a ``NOT_EXECUTED`` receipt.

    Inverted from the pre-#69 assertion (ALLOW + executor FAILED): back then
    the lexical ``..`` screen missed the backslash form (POSIX ``Path.parts``
    does not split on ``\\``) and only the Linux executor's literal-filename
    lookup kept the read from succeeding. The guarantee now fails closed at
    the policy, before any execution attempt. Same inert class as the
    encoded-traversal cases in ``test_bypass.py`` (which covers ``%2e``
    forms, not backslashes)."""
    for resource in (
        "invoices\\approved\\..\\private\\payroll.txt",
        "invoices/approved\\..\\private/payroll.txt",
    ):
        _assert_denied(gap_run, gap_workspace, resource, ReasonCode.BLOCKED_PATH)
    assert (gap_workspace / "invoices" / "private" / "payroll.txt").read_text(
        encoding="utf-8"
    ) == "CONFIDENTIAL"


def test_gapclose_double_slash_blocked(gap_workspace: Path, gap_run: Run) -> None:
    """Redundant separators normalize away: still under the blocked prefix."""
    _assert_denied(
        gap_run, gap_workspace, "invoices//private/payroll.txt", ReasonCode.BLOCKED_PATH
    )


def test_gapclose_dot_segment_blocked(gap_workspace: Path, gap_run: Run) -> None:
    """A ``/./`` segment is filtered at normalization; the blocked match holds."""
    _assert_denied(
        gap_run,
        gap_workspace,
        "invoices/private/./payroll.txt",
        ReasonCode.BLOCKED_PATH,
    )


def test_gapclose_tilde_is_not_a_homedir(gap_workspace: Path, gap_run: Run) -> None:
    """``~`` is a literal relative component, not a homedir: default-deny."""
    _assert_denied(gap_run, gap_workspace, "~/secrets", ReasonCode.PATH_NOT_ALLOWED)


@pytest.mark.parametrize("resource", ["", "."], ids=["empty", "dot"])
def test_gapclose_empty_and_dot_are_not_allowed(
    gap_workspace: Path, gap_run: Run, resource: str
) -> None:
    """Empty and ``.`` name the workspace root, which is in no allowlist."""
    _assert_denied(gap_run, gap_workspace, resource, ReasonCode.PATH_NOT_ALLOWED)


def test_gapclose_allowed_dot_and_doubleslash_stay_allowed(
    gap_workspace: Path, gap_run: Run
) -> None:
    """Control: ``./`` and ``//`` inside an allowed dir still name allowed files."""
    for resource in (
        "invoices/approved/./vendor-a.txt",
        "invoices/approved//vendor-a.txt",
    ):
        decision = evaluate_policy(_read(gap_run, resource), gap_run, gap_workspace)
        assert decision.outcome == PolicyOutcome.ALLOW
        assert decision.reason_code == ReasonCode.ALLOWED_TOOL_AND_RESOURCE


@pytest.mark.parametrize(
    ("resource", "decoy_rel"),
    [
        ("Secrets/notes.txt", "Secrets/notes.txt"),
        ("INVOICES/private/payroll.txt", "INVOICES/private/payroll.txt"),
    ],
    ids=["secrets-capitalized", "invoices-upper-blocked-leaf"],
)
def test_gapclose_case_variants_are_distinct_dirs(
    gap_workspace: Path, resource: str, decoy_rel: str
) -> None:
    """Blocked matching is case-sensitive (V1 documented limitation, safe on
    case-sensitive Linux). Each variant names a *different* directory holding
    decoy bytes — never the blocked bytes. ``test_bypass.py`` covers only
    ``invoices/PRIVATE``; these are the ``Secrets`` and fully-upper-leaf
    variants."""
    (gap_workspace / Path(decoy_rel).parent).mkdir(parents=True, exist_ok=True)
    (gap_workspace / decoy_rel).write_text("DECOY", encoding="utf-8")
    scope = TaskScope(
        schema_version="1",
        task_description="Case-variant probe.",
        allowed_paths=["invoices", "Secrets", "INVOICES"],
        blocked_paths=["invoices/private", "secrets"],
        allowed_tools=["workspace"],
        allowed_operations=["read_text"],
        allowed_network_destinations=[],
        requires_approval=[],
        created_at=NOW,
    )
    run = Run(
        id=str(uuid.uuid4()),
        name="Case Gap Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )
    action = _read(run, resource)
    decision = evaluate_policy(action, run, gap_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    receipt = execute_action(action, gap_workspace, policy_decision=decision)
    assert receipt.status == ExecutionStatus.EXECUTED
    assert "DECOY" in str(receipt.sanitized_result.get("preview", ""))
    assert "CONFIDENTIAL" not in str(receipt.sanitized_result.get("preview", ""))
    assert "synthetic secret" not in str(receipt.sanitized_result.get("preview", ""))


@pytest.mark.parametrize(
    "resource",
    [
        "invoices/approved\u200b/vendor-a.txt",  # zero-width space
        "invoices/approv\u0435d/vendor-a.txt",  # Cyrillic е in middle
    ],
    ids=["zero-width-space", "cyrillic-e-mid"],
)
def test_gapclose_more_unicode_confusables_denied(
    gap_workspace: Path, gap_run: Run, resource: str
) -> None:
    """Default-deny holds for confusables beyond the three in
    ``test_bypass.py`` (Cyrillic а, fullwidth slashes)."""
    _assert_denied(gap_run, gap_workspace, resource, ReasonCode.PATH_NOT_ALLOWED)


def test_gapclose_symlink_dir_read_of_blocked_is_denied(
    gap_workspace: Path, gap_run: Run,
) -> None:
    """Regression for issue #63 (fixed by PR #69; dir-symlink read variant of
    ``test_bypass_symlink_to_blocked_denied``): a symlinked
    *directory* inside an allowed path pointing at a blocked directory is
    DENIED. Policy resolves the link and checks the canonical target, so
    ``outputs/linkdir/payroll.txt`` matches blocked ``invoices/private``.
    Inverted from the pre-#69 assertion (ALLOW + EXECUTED + blocked preview
    served): the alias can no longer authorize a blocked target. Asserts
    DENY, NOT_EXECUTED, no disclosure, and byte-identical blocked bytes."""
    (gap_workspace / "outputs" / "linkdir").symlink_to(
        "../invoices/private", target_is_directory=True
    )
    before = (gap_workspace / "invoices" / "private" / "payroll.txt").read_text(
        encoding="utf-8"
    )
    action = _read(gap_run, "outputs/linkdir/payroll.txt")
    decision = evaluate_policy(action, gap_run, gap_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH
    receipt = execute_action(action, gap_workspace, policy_decision=decision)
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert "CONFIDENTIAL" not in str(receipt.sanitized_result)
    assert (gap_workspace / "invoices" / "private" / "payroll.txt").read_text(
        encoding="utf-8"
    ) == before


def test_gapclose_write_through_symlink_dir_is_denied(
    gap_workspace: Path, gap_run: Run,
) -> None:
    """Regression for issue #63 (fixed by PR #69; write variant): ``write_text``
    through a symlinked directory inside an allowed path no longer lands in
    the blocked directory. The resolved target resolves to
    ``invoices/private/evil.txt``, so policy returns ``DENY BLOCKED_PATH``.
    Inverted from the pre-#69 assertion (ALLOW + EXECUTED + file written into
    the blocked dir): asserts DENY, NOT_EXECUTED, no file created in the
    blocked directory, and byte-identical blocked bytes."""
    (gap_workspace / "outputs" / "wlink").symlink_to(
        "../invoices/private", target_is_directory=True
    )
    before = (gap_workspace / "invoices" / "private" / "payroll.txt").read_text(
        encoding="utf-8"
    )
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=gap_run.id,
        tool="workspace",
        operation="write_text",
        resource="outputs/wlink/evil.txt",
        arguments={"content": "pwned"},
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, gap_run, gap_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH
    receipt = execute_action(action, gap_workspace, policy_decision=decision)
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert not (gap_workspace / "invoices" / "private" / "evil.txt").exists()
    assert (gap_workspace / "invoices" / "private" / "payroll.txt").read_text(
        encoding="utf-8"
    ) == before


def test_gapclose_allowed_write_creates_regular_file(
    gap_workspace: Path, gap_run: Run,
) -> None:
    """Control for the 'allowed-write-then-read-through symlink' case in issue
    #39: no gateway operation mints symlinks — an allowed ``write_text``
    creates a regular file, so the symlink-read hole needs a pre-planted link
    (outside-the-gateway filesystem access), matching the reachability note in
    ``test_bypass.py``."""
    action = ActionRequest(
        id=str(uuid.uuid4()),
        run_id=gap_run.id,
        tool="workspace",
        operation="write_text",
        resource="outputs/fresh.txt",
        arguments={"content": "fresh synthetic"},
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = evaluate_policy(action, gap_run, gap_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    receipt = execute_action(action, gap_workspace, policy_decision=decision)
    assert receipt.status == ExecutionStatus.EXECUTED
    target = gap_workspace / "outputs" / "fresh.txt"
    assert target.is_file() and not target.is_symlink()
    assert target.read_text(encoding="utf-8") == "fresh synthetic"
