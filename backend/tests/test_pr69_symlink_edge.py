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
