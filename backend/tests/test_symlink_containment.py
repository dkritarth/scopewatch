"""Edge-case tests for PR #69 (symlink containment: policy + executor).

Scope under test mirrors ``test_bypass.py``:
allowed ``["invoices/approved", "outputs"]``,
blocked ``["invoices/private", "secrets"]``.

Policy must DENY (BLOCKED_PATH / PATH_NOT_ALLOWED / PATH_TRAVERSAL /
SYMLINK_ESCAPE) when the alias or the resolved target hits a blocked or
unallowed path; the executor must fail closed (NOT_EXECUTED on DENY,
FAILED on TOCTOU swap) with no secret bytes in any receipt.
"""

from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from scopewatch.executor import execute_action
from scopewatch.models import ExecutionStatus, PolicyOutcome, ReasonCode, RunStatus
from scopewatch.path_access import (
    is_descendant_or_equal,
    normalize_relative_path,
    scoped_path_reason,
)
from scopewatch.policy import evaluate_policy
from scopewatch.schemas import ActionRequest, Run, TaskScope

NOW = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# Fixtures (mirror test_bypass.py scope; distinct names to avoid clash)
# ---------------------------------------------------------------------


@pytest.fixture
def edge_workspace(tmp_path: Path) -> Path:
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
def edge_run() -> Run:
    scope = TaskScope(
        schema_version="1",
        task_description="Read approved invoices and write outputs.",
        allowed_paths=["invoices/approved", "outputs"],
        blocked_paths=["invoices/private", "secrets"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "write_text"],
        allowed_network_destinations=[],
        requires_approval=["delete_path"],
        created_at=NOW,
    )
    return Run(
        id=str(uuid.uuid4()),
        name="Edge Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


@pytest.fixture
def edge_cmd_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "cmd-workspace"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_auth.py").write_text("synthetic test", encoding="utf-8")
    (ws / "secrets").mkdir()
    (ws / "secrets" / "notes.txt").write_text("synthetic secret", encoding="utf-8")
    (ws / "outputs").mkdir()
    return ws


@pytest.fixture
def edge_cmd_run() -> Run:
    scope = TaskScope(
        schema_version="1",
        task_description="Run allowlisted synthetic test commands.",
        allowed_paths=["tests", "outputs"],
        blocked_paths=["secrets"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "run_command"],
        allowed_network_destinations=[],
        requires_approval=[],
        allowed_commands=[["python", "-m", "pytest"], ["pytest"], ["python"], ["ls"]],
        created_at=NOW,
    )
    return Run(
        id=str(uuid.uuid4()),
        name="Edge Command Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _read_action(run: Run, resource: str, operation: str = "read_text") -> ActionRequest:
    return ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation=operation,
        resource=resource,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


def _op_action(
    run: Run, resource: str, operation: str, arguments: dict | None = None
) -> ActionRequest:
    return ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation=operation,
        resource=resource,
        arguments=arguments or {},
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


def _run_command_action(
    run: Run, resource: str = ".", command=None, argv=None
) -> ActionRequest:
    arguments: dict = {}
    if command is not None:
        arguments["command"] = command
    if argv is not None:
        arguments["argv"] = argv
    return ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation="run_command",
        resource=resource,
        arguments=arguments,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


def _sensitive_snapshot(ws: Path) -> dict[str, str]:
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


def _assert_denied_and_not_executed(
    action: ActionRequest, run: Run, ws: Path, expected_reason: ReasonCode
):
    before = _sensitive_snapshot(ws)
    decision = evaluate_policy(action, run, ws)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == expected_reason
    receipt = execute_action(action, ws, policy_decision=decision)
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert _sensitive_snapshot(ws) == before
    assert "CONFIDENTIAL" not in str(receipt.sanitized_result)
    assert "synthetic secret" not in str(receipt.sanitized_result)
    return decision


# ---------------------------------------------------------------------
# A. File-symlink variants -> blocked content
# ---------------------------------------------------------------------


def test_edge_file_symlink_to_blocked_denied(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """Relative symlink alias under allowed dir -> blocked file: DENY."""
    (edge_workspace / "outputs" / "evil.txt").symlink_to(
        Path("..") / "invoices" / "private" / "payroll.txt"
    )
    _assert_denied_and_not_executed(
        _read_action(edge_run, "outputs/evil.txt"),
        edge_run,
        edge_workspace,
        ReasonCode.BLOCKED_PATH,
    )


def test_edge_absolute_target_symlink_denied(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """Absolute symlink target inside a blocked dir: DENY (no alias trick)."""
    (edge_workspace / "outputs" / "abs-evil.txt").symlink_to(
        edge_workspace / "invoices" / "private" / "payroll.txt"
    )
    _assert_denied_and_not_executed(
        _read_action(edge_run, "outputs/abs-evil.txt"),
        edge_run,
        edge_workspace,
        ReasonCode.BLOCKED_PATH,
    )


def test_edge_chained_symlinks_denied(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """link -> link -> blocked file: resolution follows the full chain."""
    (edge_workspace / "outputs" / "inner.txt").symlink_to(
        edge_workspace / "invoices" / "private" / "payroll.txt"
    )
    (edge_workspace / "outputs" / "outer.txt").symlink_to("inner.txt")
    _assert_denied_and_not_executed(
        _read_action(edge_run, "outputs/outer.txt"),
        edge_run,
        edge_workspace,
        ReasonCode.BLOCKED_PATH,
    )


def test_edge_symlink_to_secrets_denied_no_leak(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """Second blocked root (secrets/) is covered the same way."""
    (edge_workspace / "outputs" / "s.txt").symlink_to(
        Path("..") / "secrets" / "notes.txt"
    )
    decision = _assert_denied_and_not_executed(
        _read_action(edge_run, "outputs/s.txt"),
        edge_run,
        edge_workspace,
        ReasonCode.BLOCKED_PATH,
    )
    assert decision.matched_rule == "RULE_BLOCKED_PATH_MATCHED"


# ---------------------------------------------------------------------
# B. Symlinked directory + file beneath it, all operations
# ---------------------------------------------------------------------


def test_edge_symlinked_dir_read_denied(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    (edge_workspace / "outputs" / "private-link").symlink_to(
        edge_workspace / "invoices" / "private", target_is_directory=True
    )
    _assert_denied_and_not_executed(
        _read_action(edge_run, "outputs/private-link/payroll.txt"),
        edge_run,
        edge_workspace,
        ReasonCode.BLOCKED_PATH,
    )


def test_edge_symlinked_dir_list_denied(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    (edge_workspace / "outputs" / "private-link").symlink_to(
        edge_workspace / "invoices" / "private", target_is_directory=True
    )
    action = _read_action(edge_run, "outputs/private-link", operation="list_directory")
    decision = _assert_denied_and_not_executed(
        action, edge_run, edge_workspace, ReasonCode.BLOCKED_PATH
    )
    assert decision.outcome == PolicyOutcome.DENY


def test_edge_symlinked_dir_write_denied(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    (edge_workspace / "outputs" / "private-link").symlink_to(
        edge_workspace / "invoices" / "private", target_is_directory=True
    )
    action = _op_action(
        edge_run,
        "outputs/private-link/payroll.txt",
        "write_text",
        {"content": "pwned"},
    )
    _assert_denied_and_not_executed(
        action, edge_run, edge_workspace, ReasonCode.BLOCKED_PATH
    )


def test_edge_symlinked_dir_delete_denied_not_hold(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """delete_path needs approval, but a blocked target DENYs (never HOLDs)."""
    (edge_workspace / "outputs" / "private-link").symlink_to(
        edge_workspace / "invoices" / "private", target_is_directory=True
    )
    action = _read_action(
        edge_run, "outputs/private-link/payroll.txt", operation="delete_path"
    )
    decision = _assert_denied_and_not_executed(
        action, edge_run, edge_workspace, ReasonCode.BLOCKED_PATH
    )
    assert decision.reason_code != ReasonCode.APPROVAL_REQUIRED


def test_edge_write_via_file_symlink_does_not_modify_blocked(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    (edge_workspace / "outputs" / "evil.txt").symlink_to(
        Path("..") / "invoices" / "private" / "payroll.txt"
    )
    action = _op_action(edge_run, "outputs/evil.txt", "write_text", {"content": "pwned"})
    _assert_denied_and_not_executed(
        action, edge_run, edge_workspace, ReasonCode.BLOCKED_PATH
    )
    assert (edge_workspace / "invoices" / "private" / "payroll.txt").read_text(
        encoding="utf-8"
    ) == "CONFIDENTIAL"


# ---------------------------------------------------------------------
# C. Broken symlink + allow-control
# ---------------------------------------------------------------------


def test_edge_broken_symlink_into_blocked_denied(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """Dangling link whose lexical target sits under blocked dir: fail closed."""
    (edge_workspace / "outputs" / "broken.txt").symlink_to(
        Path("..") / "invoices" / "private" / "missing.txt"
    )
    _assert_denied_and_not_executed(
        _read_action(edge_run, "outputs/broken.txt"),
        edge_run,
        edge_workspace,
        ReasonCode.BLOCKED_PATH,
    )


def test_edge_allowed_file_symlink_executes(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """Control: allowed -> allowed alias must NOT be over-blocked."""
    (edge_workspace / "outputs" / "alias.txt").symlink_to("old.txt")
    action = _read_action(edge_run, "outputs/alias.txt")
    decision = evaluate_policy(action, edge_run, edge_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    receipt = execute_action(
        action, edge_workspace, policy_decision=decision, task_scope=edge_run.task_scope
    )
    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.sanitized_result is not None
    assert receipt.sanitized_result["preview"] == "old data"


def test_edge_allowed_dir_symlink_lists(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """Control: symlinked allowed directory lists through the alias."""
    (edge_workspace / "outputs" / "approved-link").symlink_to(
        edge_workspace / "invoices" / "approved", target_is_directory=True
    )
    action = _read_action(
        edge_run, "outputs/approved-link", operation="list_directory"
    )
    decision = evaluate_policy(action, edge_run, edge_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    receipt = execute_action(
        action, edge_workspace, policy_decision=decision, task_scope=edge_run.task_scope
    )
    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.sanitized_result is not None
    names = [item["name"] for item in receipt.sanitized_result["items"]]
    assert "vendor-a.txt" in names


# ---------------------------------------------------------------------
# D. Alias normalization: backslash + dot-dot
# ---------------------------------------------------------------------


def test_edge_backslash_alias_normalized_to_blocked(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    (edge_workspace / "outputs" / "evil.txt").symlink_to(
        Path("..") / "invoices" / "private" / "payroll.txt"
    )
    _assert_denied_and_not_executed(
        _read_action(edge_run, "outputs\\evil.txt"),
        edge_run,
        edge_workspace,
        ReasonCode.BLOCKED_PATH,
    )


def test_edge_dotdot_alias_rejected_traversal(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """'..' in the alias is rejected as traversal before symlink resolution."""
    (edge_workspace / "outputs" / "evil.txt").symlink_to(
        Path("..") / "invoices" / "private" / "payroll.txt"
    )
    _assert_denied_and_not_executed(
        _read_action(edge_run, "outputs/../outputs/evil.txt"),
        edge_run,
        edge_workspace,
        ReasonCode.PATH_TRAVERSAL,
    )


# ---------------------------------------------------------------------
# E. Blocked alias itself + unallowed paths (alias and target)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource",
    ["invoices/private/payroll.txt", "secrets/notes.txt"],
    ids=["invoices-private", "secrets"],
)
def test_edge_blocked_alias_direct_denied(
    edge_workspace: Path, edge_run: Run, resource: str, local_backend: None
) -> None:
    """No symlink needed: a resource directly under blocked path DENYs."""
    _assert_denied_and_not_executed(
        _read_action(edge_run, resource), edge_run, edge_workspace, ReasonCode.BLOCKED_PATH
    )


def test_edge_unallowed_direct_denied(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """Path under neither allowed nor blocked roots: PATH_NOT_ALLOWED."""
    (edge_workspace / "elsewhere").mkdir(parents=True)
    (edge_workspace / "elsewhere" / "file.txt").write_text(
        "ELSEWHERE-DATA", encoding="utf-8"
    )
    _assert_denied_and_not_executed(
        _read_action(edge_run, "elsewhere/file.txt"),
        edge_run,
        edge_workspace,
        ReasonCode.PATH_NOT_ALLOWED,
    )


def test_edge_allowed_alias_to_unallowed_target_denied(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """Allowed alias -> resolvable but unallowed target: PATH_NOT_ALLOWED."""
    (edge_workspace / "elsewhere").mkdir(parents=True)
    (edge_workspace / "elsewhere" / "file.txt").write_text(
        "ELSEWHERE-DATA", encoding="utf-8"
    )
    (edge_workspace / "outputs" / "link.txt").symlink_to(
        Path("..") / "elsewhere" / "file.txt"
    )
    action = _read_action(edge_run, "outputs/link.txt")
    before = _sensitive_snapshot(edge_workspace)
    decision = evaluate_policy(action, edge_run, edge_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_NOT_ALLOWED
    receipt = execute_action(action, edge_workspace, policy_decision=decision)
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert "ELSEWHERE-DATA" not in str(receipt.sanitized_result)
    assert _sensitive_snapshot(edge_workspace) == before


# ---------------------------------------------------------------------
# F. Executor TOCTOU: ALLOW then swap file -> symlink-to-blocked
# ---------------------------------------------------------------------


def test_edge_executor_toctou_with_scope_fails_closed(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    action = _read_action(edge_run, "outputs/old.txt")
    decision = evaluate_policy(action, edge_run, edge_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    (edge_workspace / "outputs" / "old.txt").unlink()
    (edge_workspace / "outputs" / "old.txt").symlink_to(
        edge_workspace / "invoices" / "private" / "payroll.txt"
    )
    receipt = execute_action(
        action,
        edge_workspace,
        policy_decision=decision,
        task_scope=edge_run.task_scope,
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert "CONFIDENTIAL" not in str(receipt.sanitized_result)
    assert (edge_workspace / "invoices" / "private" / "payroll.txt").read_text(
        encoding="utf-8"
    ) == "CONFIDENTIAL"


def test_edge_executor_toctou_without_scope_fails_closed(
    edge_workspace: Path, edge_run: Run, local_backend: None
) -> None:
    """Direct caller with no scope evidence: any alias fails closed."""
    action = _read_action(edge_run, "outputs/old.txt")
    decision = evaluate_policy(action, edge_run, edge_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    (edge_workspace / "outputs" / "old.txt").unlink()
    (edge_workspace / "outputs" / "old.txt").symlink_to(
        edge_workspace / "invoices" / "private" / "payroll.txt"
    )
    receipt = execute_action(action, edge_workspace, policy_decision=decision)
    assert receipt.status == ExecutionStatus.FAILED
    assert "CONFIDENTIAL" not in str(receipt.sanitized_result)


# ---------------------------------------------------------------------
# G. run_command argv / cwd symlink -> blocked (policy level)
# ---------------------------------------------------------------------


def test_edge_run_command_argv_symlink_blocked_denied(
    edge_cmd_workspace: Path, edge_cmd_run: Run, docker_backend: None
) -> None:
    (edge_cmd_workspace / "outputs" / "evil-link").symlink_to(
        Path("..") / "secrets" / "notes.txt"
    )
    action = _run_command_action(
        edge_cmd_run, argv=["python", "outputs/evil-link"]
    )
    decision = evaluate_policy(action, edge_cmd_run, edge_cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH


def test_edge_run_command_cwd_symlink_blocked_denied(
    edge_cmd_workspace: Path, edge_cmd_run: Run, docker_backend: None
) -> None:
    (edge_cmd_workspace / "outputs" / "evil-dir").symlink_to(
        edge_cmd_workspace / "secrets", target_is_directory=True
    )
    action = _run_command_action(edge_cmd_run, resource="outputs/evil-dir", argv=["ls"])
    decision = evaluate_policy(action, edge_cmd_run, edge_cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH


# ---------------------------------------------------------------------
# H. Docker executor: host-side revalidation precedes daemon touch
# ---------------------------------------------------------------------


def test_edge_docker_host_revalidation_before_daemon(
    edge_workspace: Path, edge_run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scopewatch.executor_docker import DockerExecutor

    action = _read_action(edge_run, "outputs/old.txt")
    decision = evaluate_policy(action, edge_run, edge_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    (edge_workspace / "outputs" / "old.txt").unlink()
    (edge_workspace / "outputs" / "old.txt").symlink_to(
        edge_workspace / "invoices" / "private" / "payroll.txt"
    )

    def fail_if_docker_checked() -> bool:
        pytest.fail("Docker was reached before path revalidation")

    monkeypatch.setattr(
        "scopewatch.executor_docker.is_docker_available", fail_if_docker_checked
    )
    receipt = DockerExecutor().execute(
        action,
        edge_workspace,
        policy_decision=decision,
        task_scope=edge_run.task_scope,
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert "CONFIDENTIAL" not in str(receipt.sanitized_result)


# ---------------------------------------------------------------------
# I. path_access unit tests
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("outputs\\evil.txt", Path("outputs/evil.txt")),
        ("a/./b", Path("a/b")),
        ("a//b", Path("a/b")),
        ("", Path(".")),
        ("   ", Path(".")),
        (".", Path(".")),
        ("outputs/", Path("outputs")),
    ],
    ids=[
        "backslash",
        "dot-segment",
        "double-slash",
        "empty",
        "whitespace",
        "dot",
        "trailing-slash",
    ],
)
def test_edge_normalize_relative_path_units(raw: str, expected: Path) -> None:
    assert normalize_relative_path(raw) == expected


def test_edge_is_descendant_or_equal_units() -> None:
    # Sibling-prefix trap: string prefix is NOT a path descendant.
    assert not is_descendant_or_equal(Path("invoices2/file.txt"), Path("invoices"))
    assert not is_descendant_or_equal(Path("invoices-private"), Path("invoices"))
    # Exact match and true descendants hold.
    assert is_descendant_or_equal(Path("invoices/private"), Path("invoices/private"))
    assert is_descendant_or_equal(
        Path("invoices/private/payroll.txt"), Path("invoices/private")
    )
    assert not is_descendant_or_equal(Path("invoices"), Path("invoices/private"))
    # Parent "." matches everything (workspace root convention).
    assert is_descendant_or_equal(Path("anything/at/all.txt"), Path("."))
    assert is_descendant_or_equal(Path("outputs"), Path("."))
    # Unrelated paths do not match.
    assert not is_descendant_or_equal(Path("outputs/x.txt"), Path("secrets"))


def test_edge_scoped_path_reason_units() -> None:
    allowed = ["invoices/approved", "outputs"]
    blocked = ["invoices/private", "secrets"]
    # Allowed alias + blocked canonical target -> BLOCKED_PATH (the PR #69 fix).
    reason = scoped_path_reason(
        Path("outputs/evil.txt"), Path("invoices/private/payroll.txt"), allowed, blocked
    )
    assert reason is not None and reason[0] == ReasonCode.BLOCKED_PATH
    # Blocked alias itself -> BLOCKED_PATH even with an allowed canonical.
    reason = scoped_path_reason(
        Path("secrets/notes.txt"), Path("outputs/copy.txt"), allowed, blocked
    )
    assert reason is not None and reason[0] == ReasonCode.BLOCKED_PATH
    # Allowed alias + unallowed-only canonical -> PATH_NOT_ALLOWED.
    reason = scoped_path_reason(
        Path("outputs/link.txt"), Path("elsewhere/file.txt"), allowed, blocked
    )
    assert reason is not None and reason[0] == ReasonCode.PATH_NOT_ALLOWED
    # Unallowed alias -> PATH_NOT_ALLOWED regardless of canonical.
    reason = scoped_path_reason(
        Path("elsewhere/file.txt"), Path("elsewhere/file.txt"), allowed, blocked
    )
    assert reason is not None and reason[0] == ReasonCode.PATH_NOT_ALLOWED
    # Both names allowed -> no error (None).
    assert (
        scoped_path_reason(
            Path("outputs/alias.txt"), Path("outputs/old.txt"), allowed, blocked
        )
        is None
    )
"""Compatibility round 2 for PR #69 (symlink containment).

Companion to ``test_pr69_symlink_edge.py`` (round 1: 32 tests — DO NOT
duplicate it; read it first). Round 2 covers COMPATIBILITY angles round 1
missed, with the same scope under test:

allowed ``["invoices/approved", "outputs"]``,
blocked ``["invoices/private", "secrets"]``
(and a ``["tests", "outputs"]`` / blocked ``["secrets"]`` command scope).

Conventions: policy DENY is final; executor without a DENY decision fails
closed; no network, no real docker (daemon touch is monkeypatched away).

These two cases used to escape as exceptions (RuntimeError for a symlink
loop, OSError for an overlong cwd); policy now returns DENY instead of
raising (fail-closed).
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


# ---------------------------------------------------------------------
# Fixtures (mirror round 1 scope; distinct names to avoid clash)
# ---------------------------------------------------------------------


@pytest.fixture
def compat2_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "compat2-workspace"
    (ws / "invoices" / "approved").mkdir(parents=True)
    (ws / "invoices" / "private").mkdir(parents=True)
    (ws / "outputs").mkdir(parents=True)
    (ws / "outputs" / "sub").mkdir(parents=True)
    (ws / "secrets").mkdir(parents=True)
    (ws / "invoices" / "private" / "payroll.txt").write_text(
        "CONFIDENTIAL", encoding="utf-8"
    )
    (ws / "secrets" / "notes.txt").write_text("synthetic secret", encoding="utf-8")
    (ws / "outputs" / "ok.txt").write_text("ok data", encoding="utf-8")
    (ws / "outputs" / "sub" / "x.txt").write_text("x data", encoding="utf-8")
    return ws


@pytest.fixture
def compat2_run() -> Run:
    scope = TaskScope(
        schema_version="1",
        task_description="Read approved invoices and write outputs.",
        allowed_paths=["invoices/approved", "outputs"],
        blocked_paths=["invoices/private", "secrets"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "write_text"],
        allowed_network_destinations=[],
        requires_approval=["delete_path"],
        created_at=NOW,
    )
    return Run(
        id=str(uuid.uuid4()),
        name="Compat2 Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


@pytest.fixture
def compat2_cmd_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "compat2-cmd-workspace"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_auth.py").write_text("synthetic test", encoding="utf-8")
    (ws / "secrets").mkdir()
    (ws / "secrets" / "notes.txt").write_text("synthetic secret", encoding="utf-8")
    (ws / "outputs").mkdir()
    return ws


@pytest.fixture
def compat2_cmd_run() -> Run:
    scope = TaskScope(
        schema_version="1",
        task_description="Run allowlisted synthetic test commands.",
        allowed_paths=["tests", "outputs"],
        blocked_paths=["secrets"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "run_command"],
        allowed_network_destinations=[],
        requires_approval=[],
        allowed_commands=[["python", "-m", "pytest"], ["pytest"], ["python"], ["ls"]],
        created_at=NOW,
    )
    return Run(
        id=str(uuid.uuid4()),
        name="Compat2 Command Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _read_action(run: Run, resource: str, operation: str = "read_text") -> ActionRequest:
    return ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation=operation,
        resource=resource,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


def _run_command_action(
    run: Run, resource: str = ".", command=None, argv=None
) -> ActionRequest:
    arguments: dict = {}
    if command is not None:
        arguments["command"] = command
    if argv is not None:
        arguments["argv"] = argv
    return ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation="run_command",
        resource=resource,
        arguments=arguments,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------
# 1. run_command in command-string form with symlink arg
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["python outputs/evil-link", "python secrets/notes.txt"],
    ids=["symlink-arg", "blocked-direct-arg"],
)
def test_compat2_run_command_string_form_blocked_denied(
    compat2_cmd_workspace: Path,
    compat2_cmd_run: Run,
    docker_backend: None,
    command: str,
) -> None:
    """String-form commands go through the same path rules as argv lists."""
    (compat2_cmd_workspace / "outputs" / "evil-link").symlink_to(
        Path("..") / "secrets" / "notes.txt"
    )
    action = _run_command_action(compat2_cmd_run, command=command)
    decision = evaluate_policy(action, compat2_cmd_run, compat2_cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH


# ---------------------------------------------------------------------
# 2. run_command malformed shapes: empty argv, missing args, whitespace
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [{"argv": []}, {}, {"command": "   "}],
    ids=["empty-argv", "missing-args", "whitespace-command"],
)
def test_compat2_run_command_malformed_shapes_denied(
    compat2_cmd_workspace: Path,
    compat2_cmd_run: Run,
    docker_backend: None,
    arguments: dict,
) -> None:
    """Malformed submissions DENY as MALFORMED_REQUEST; never raise."""
    action = _run_command_action(compat2_cmd_run, resource=".", argv=None, command=None)
    action.arguments = dict(arguments)
    decision = evaluate_policy(action, compat2_cmd_run, compat2_cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.MALFORMED_REQUEST


# ---------------------------------------------------------------------
# 3. run_command with overlong cwd must not raise (SOURCE BUG)
# ---------------------------------------------------------------------


def test_compat2_run_command_long_cwd_never_raises(
    compat2_cmd_workspace: Path, compat2_cmd_run: Run, docker_backend: None
) -> None:
    """Overlong cwd yields DENY instead of raising OSError.

    ``_check_run_command_path`` previously called ``test_path.is_symlink()``
    whose ``lstat`` raised ``OSError: [Errno 36] File name too long`` for a
    500-char component. Policy now returns DENY (fail-closed).
    """
    action = _run_command_action(
        compat2_cmd_run, resource="x" * 500, argv=["ls"]
    )
    decision = evaluate_policy(action, compat2_cmd_run, compat2_cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY


# ---------------------------------------------------------------------
# 4-5. Symlink loop: policy must not raise; executor fails closed
# ---------------------------------------------------------------------


def test_compat2_symlink_loop_policy_never_raises(
    compat2_workspace: Path, compat2_run: Run, local_backend: None
) -> None:
    """A symlink loop yields DENY instead of raising RuntimeError.

    ``outputs/loop-a <-> outputs/loop-b`` used to escape as
    ``RuntimeError: Symlink loop ...``; policy now returns DENY
    (fail-closed).
    """
    (compat2_workspace / "outputs" / "loop-a").symlink_to("loop-b")
    (compat2_workspace / "outputs" / "loop-b").symlink_to("loop-a")
    decision = evaluate_policy(
        _read_action(compat2_run, "outputs/loop-a"), compat2_run, compat2_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY


def test_compat2_symlink_loop_executor_failed_with_scope(
    compat2_workspace: Path, compat2_run: Run, local_backend: None
) -> None:
    """Executor re-resolution of a loop fails closed (FAILED, not raise)."""
    (compat2_workspace / "outputs" / "loop-a").symlink_to("loop-b")
    (compat2_workspace / "outputs" / "loop-b").symlink_to("loop-a")
    allow_decision = evaluate_policy(
        _read_action(compat2_run, "outputs/ok.txt"), compat2_run, compat2_workspace
    )
    assert allow_decision.outcome == PolicyOutcome.ALLOW
    receipt = execute_action(
        _read_action(compat2_run, "outputs/loop-a"),
        compat2_workspace,
        policy_decision=allow_decision,
        task_scope=compat2_run.task_scope,
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert "CONFIDENTIAL" not in str(receipt.sanitized_result)
    assert "synthetic secret" not in str(receipt.sanitized_result)


# ---------------------------------------------------------------------
# 6. Absolute path and dot-dot escape: DENY, never host access
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        ("/etc/passwd", ReasonCode.PATH_OUTSIDE_WORKSPACE),
        ("outputs/../../etc/passwd", ReasonCode.PATH_TRAVERSAL),
    ],
    ids=["absolute", "dotdot-escape"],
)
def test_compat2_absolute_and_dotdot_denied(
    compat2_workspace: Path,
    compat2_run: Run,
    local_backend: None,
    resource: str,
    expected: ReasonCode,
) -> None:
    decision = evaluate_policy(
        _read_action(compat2_run, resource), compat2_run, compat2_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == expected
    receipt = execute_action(
        _read_action(compat2_run, resource),
        compat2_workspace,
        policy_decision=decision,
    )
    assert receipt.status == ExecutionStatus.NOT_EXECUTED


# ---------------------------------------------------------------------
# 7. Boundary lengths: 255- vs 256-char components
# ---------------------------------------------------------------------


@pytest.mark.parametrize("length", [255, 256], ids=["len-255", "len-256"])
def test_compat2_boundary_component_lengths(
    compat2_workspace: Path,
    compat2_run: Run,
    local_backend: None,
    length: int,
) -> None:
    """No artificial length cap: both ALLOW at policy, never raise.

    The names do not exist, so a read fails closed at execution
    (FAILED, FileNotFound) with no secret bytes anywhere.
    """
    resource = f"outputs/{'x' * length}"
    decision = evaluate_policy(
        _read_action(compat2_run, resource), compat2_run, compat2_workspace
    )
    assert decision.outcome == PolicyOutcome.ALLOW
    receipt = execute_action(
        _read_action(compat2_run, resource),
        compat2_workspace,
        policy_decision=decision,
        task_scope=compat2_run.task_scope,
    )
    assert receipt.status == ExecutionStatus.FAILED
    assert "CONFIDENTIAL" not in str(receipt.sanitized_result)
    assert "synthetic secret" not in str(receipt.sanitized_result)


# ---------------------------------------------------------------------
# 8. Empty / dot / whitespace resources
# ---------------------------------------------------------------------


@pytest.mark.parametrize("resource", ["", ".", "   "], ids=["empty", "dot", "spaces"])
def test_compat2_empty_dot_whitespace_resources(
    compat2_workspace: Path,
    compat2_run: Run,
    local_backend: None,
    resource: str,
) -> None:
    """Degenerate resources normalize to workspace root: PATH_NOT_ALLOWED."""
    decision = evaluate_policy(
        _read_action(compat2_run, resource), compat2_run, compat2_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_NOT_ALLOWED
    receipt = execute_action(
        _read_action(compat2_run, resource),
        compat2_workspace,
        policy_decision=decision,
    )
    assert receipt.status == ExecutionStatus.NOT_EXECUTED


# ---------------------------------------------------------------------
# 9. Case variants: POSIX is case-sensitive, record actual outcome
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        ("INVOICES/PRIVATE/x", ReasonCode.PATH_NOT_ALLOWED),
        ("outputs/PRIVATE-LINK/..", ReasonCode.PATH_TRAVERSAL),
    ],
    ids=["uppercase-blocked", "uppercase-link-dotdot"],
)
def test_compat2_case_variants_documented(
    compat2_workspace: Path,
    compat2_run: Run,
    local_backend: None,
    resource: str,
    expected: ReasonCode,
) -> None:
    """Uppercase is NOT the blocked dir (case-sensitive match), so the first
    lands on PATH_NOT_ALLOWED; the second carries a literal '..' and is
    rejected as PATH_TRAVERSAL before any scope check."""
    decision = evaluate_policy(
        _read_action(compat2_run, resource), compat2_run, compat2_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == expected


# ---------------------------------------------------------------------
# 10. Sibling-prefix trap via policy: exact reason codes
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource",
    ["invoices2/evil.txt", "secrets-backup/x"],
    ids=["invoices2", "secrets-backup"],
)
def test_compat2_sibling_prefix_not_blocked(
    compat2_workspace: Path,
    compat2_run: Run,
    local_backend: None,
    resource: str,
) -> None:
    """String-prefix siblings are NOT blocked dirs: PATH_NOT_ALLOWED."""
    decision = evaluate_policy(
        _read_action(compat2_run, resource), compat2_run, compat2_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_NOT_ALLOWED
    assert decision.reason_code != ReasonCode.BLOCKED_PATH


# ---------------------------------------------------------------------
# 11. Trailing-slash and double-slash blocked aliases
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource",
    ["invoices/private/", "invoices//private//payroll.txt"],
    ids=["trailing-slash", "double-slash"],
)
def test_compat2_slash_variants_blocked(
    compat2_workspace: Path,
    compat2_run: Run,
    local_backend: None,
    resource: str,
) -> None:
    """Normalization collapses redundant separators: still BLOCKED_PATH."""
    decision = evaluate_policy(
        _read_action(compat2_run, resource), compat2_run, compat2_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH
    receipt = execute_action(
        _read_action(compat2_run, resource),
        compat2_workspace,
        policy_decision=decision,
    )
    assert receipt.status == ExecutionStatus.NOT_EXECUTED


# ---------------------------------------------------------------------
# 12-13. Dot segments: dot-only stays allowed; dot-dot is DENY by design
# ---------------------------------------------------------------------


def test_compat2_dot_segment_stays_allowed(
    compat2_workspace: Path, compat2_run: Run, local_backend: None
) -> None:
    """'./' aliases into an allowed dir must NOT be over-blocked."""
    action = _read_action(compat2_run, "outputs/./ok.txt")
    decision = evaluate_policy(action, compat2_run, compat2_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    receipt = execute_action(
        action,
        compat2_workspace,
        policy_decision=decision,
        task_scope=compat2_run.task_scope,
    )
    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.sanitized_result is not None
    assert receipt.sanitized_result["preview"] == "ok data"


def test_compat2_dotdot_alias_denied_by_design(
    compat2_workspace: Path, compat2_run: Run, local_backend: None
) -> None:
    """Any literal '..' in the raw resource is PATH_TRAVERSAL by design
    (Step 9 lexical check, pinned by round 1) — even when the resolved
    path would stay inside an allowed dir. This is intentional strictness,
    not over-blocking: callers must submit the canonical alias."""
    decision = evaluate_policy(
        _read_action(compat2_run, "outputs/./sub/../ok.txt"),
        compat2_run,
        compat2_workspace,
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_TRAVERSAL


# ---------------------------------------------------------------------
# 14-15. Direct executor calls WITHOUT scope (compatibility pins)
# ---------------------------------------------------------------------


def test_compat2_executor_without_scope_plain_allowed(
    compat2_workspace: Path, compat2_run: Run, local_backend: None
) -> None:
    """Current design: scope-less direct calls pass through plain
    (non-symlink) allowed files — canonical == requested, so the alias
    check has nothing to reject. Pinned for compatibility."""
    action = _read_action(compat2_run, "outputs/ok.txt")
    decision = evaluate_policy(action, compat2_run, compat2_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    receipt = execute_action(action, compat2_workspace, policy_decision=decision)
    assert receipt.status == ExecutionStatus.EXECUTED
    assert receipt.sanitized_result is not None
    assert receipt.sanitized_result["preview"] == "ok data"


def test_compat2_executor_without_scope_blocked_direct(
    compat2_workspace: Path, compat2_run: Run, local_backend: None
) -> None:
    """Current design gap (pinned, not fixed here): without task_scope the
    executor only rejects symlink aliases (canonical != requested) and does
    NOT enforce blocked_paths for direct paths. In the gateway this is
    unreachable — ``service.submit_action`` always passes the run's scope
    for ALLOW executions and DENY decisions never execute — but direct
    callers must supply scope evidence."""
    blocked = _read_action(compat2_run, "invoices/private/payroll.txt")
    allow_decision = evaluate_policy(
        _read_action(compat2_run, "outputs/ok.txt"), compat2_run, compat2_workspace
    )
    assert allow_decision.outcome == PolicyOutcome.ALLOW
    receipt = execute_action(
        blocked, compat2_workspace, policy_decision=allow_decision
    )
    assert receipt.status == ExecutionStatus.EXECUTED


# ---------------------------------------------------------------------
# 16. Docker staged-copy gap pin: DENY never reaches docker
# ---------------------------------------------------------------------


def test_compat2_docker_denied_argv_never_reaches_daemon(
    compat2_cmd_workspace: Path,
    compat2_cmd_run: Run,
    docker_backend: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy-DENY run_command (known #68 staged-copy risk) returns
    NOT_EXECUTED on the host before any docker helper runs."""
    (compat2_cmd_workspace / "outputs" / "evil-link").symlink_to(
        Path("..") / "secrets" / "notes.txt"
    )
    action = _run_command_action(
        compat2_cmd_run, argv=["python", "outputs/evil-link"]
    )
    decision = evaluate_policy(action, compat2_cmd_run, compat2_cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH

    def fail_if_docker_touched(*args, **kwargs):
        pytest.fail("Docker was reached for a policy-DENY action")

    monkeypatch.setattr(
        "scopewatch.executor_docker.build_docker_command", fail_if_docker_touched
    )
    monkeypatch.setattr(
        "scopewatch.executor_docker.is_docker_available", fail_if_docker_touched
    )
    from scopewatch.executor_docker import DockerExecutor

    receipt = DockerExecutor().execute(
        action,
        compat2_cmd_workspace,
        policy_decision=decision,
        task_scope=compat2_cmd_run.task_scope,
    )
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert "synthetic secret" not in str(receipt.sanitized_result)


# ---------------------------------------------------------------------
# 17. Local-backend fallback: run_command is docker-only
# ---------------------------------------------------------------------


def test_compat2_local_backend_run_command_unsupported(
    compat2_cmd_workspace: Path, compat2_cmd_run: Run, local_backend: None
) -> None:
    """With SCOPEWATCH_EXECUTOR unset, even a benign allowlisted command
    DENYs as UNSUPPORTED_OPERATION — no silent local execution fallback."""
    action = _run_command_action(
        compat2_cmd_run, resource=".", argv=["python", "tests/test_auth.py"]
    )
    decision = evaluate_policy(action, compat2_cmd_run, compat2_cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.UNSUPPORTED_OPERATION
    receipt = execute_action(
        action, compat2_cmd_workspace, policy_decision=decision
    )
    assert receipt.status == ExecutionStatus.NOT_EXECUTED


# ---------------------------------------------------------------------
# 18. Gateway end-to-end via service.submit_action
# ---------------------------------------------------------------------


def test_compat2_gateway_symlink_end_to_end(
    compat2_workspace: Path, compat2_run: Run, tmp_path: Path, local_backend: None
) -> None:
    """Symlink alias through the real gateway: DENY + NOT_EXECUTED, and no
    EXECUTION_* evidence events are recorded (decision events precede any
    effect; denied actions produce no execution events)."""
    import asyncio

    from scopewatch.db import init_db
    from scopewatch.models import EventType
    from scopewatch.schemas import SubmitActionRequest
    from scopewatch.service import ScopewatchService

    (compat2_workspace / "outputs" / "evil.txt").symlink_to(
        Path("..") / "invoices" / "private" / "payroll.txt"
    )
    db_path = tmp_path / "compat2.db"
    init_db(db_path)
    service = ScopewatchService(db_path=db_path, workspace_root=compat2_workspace)
    run, _ = service.create_run("Compat2 GW Run", compat2_run.task_scope)

    response = asyncio.run(
        service.submit_action(
            run.id,
            SubmitActionRequest(
                tool="workspace",
                operation="read_text",
                resource="outputs/evil.txt",
                arguments={},
            ),
        )
    )
    assert response.policy_decision.outcome == PolicyOutcome.DENY
    assert response.policy_decision.reason_code == ReasonCode.BLOCKED_PATH
    assert response.execution_receipt is not None
    assert response.execution_receipt.status == ExecutionStatus.NOT_EXECUTED

    events = service.get_events(run.id)
    types = [event.event_type for event in events]
    assert EventType.POLICY_DENIED in types
    assert EventType.EXECUTION_STARTED not in types
    assert EventType.EXECUTION_SUCCEEDED not in types
    assert EventType.EXECUTION_FAILED not in types
    assert "CONFIDENTIAL" not in str(response.execution_receipt.sanitized_result)
