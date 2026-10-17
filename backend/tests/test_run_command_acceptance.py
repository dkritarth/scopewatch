"""M2 #36 acceptance matrix: allowlisted run_command (offline, no daemon).

Each test maps to an acceptance bullet from issue #36. Policy behaviour is
exercised through ``evaluate_policy`` (with the ``docker_backend`` /
``local_backend`` fixtures selecting the executor), and execution limits
through the REAL in-container helper source run locally with
``SCOPEWATCH_WORKSPACE`` pointed at a synthetic workspace — the exact
timeout, truncation, and exit-code logic that ships in the container.
Daemon round-trips stay in ``test_executor_run_command.py`` behind
``requires_docker``.

Ownership: issues #66/#68 (revalidation at dispatch), #78/#80 (remote,
copy-back), and #39 (bypass gap-close: equals-split smuggling,
space-separated flags, quoted semicolons, env tokens, deselect) are owned
by other threads — none of those cases are duplicated here. Where this
file touches neighbouring ground it uses distinct cases (argv-list
metacharacters, CR rejection, empty-string cwd, NUL cwd, stderr
truncation, NaN/Inf clamps).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

import pytest

from scopewatch.executor import ExecutionSecurityError, execute_action
from scopewatch.executor_docker import (
    RUN_COMMAND_DEFAULT_TIMEOUT_S,
    RUN_COMMAND_MAX_TIMEOUT_S,
    RUN_COMMAND_MIN_TIMEOUT_S,
    RUN_COMMAND_OUTPUT_LIMIT,
    _helper_code,
    clamp_run_command_timeout,
    extract_run_command_argv,
)
from scopewatch.models import ExecutionStatus, PolicyOutcome, ReasonCode, RunStatus
from scopewatch.policy import evaluate_policy
from scopewatch.schemas import ActionRequest, Run, TaskScope


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def cmd_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_auth.py").write_text("synthetic test", encoding="utf-8")
    (ws / "secrets").mkdir()
    (ws / "secrets" / "notes.txt").write_text("synthetic secret", encoding="utf-8")
    (ws / "outputs").mkdir()
    (ws / "sub").mkdir()
    return ws


@pytest.fixture
def cmd_run() -> Run:
    scope = TaskScope(
        schema_version="1",
        task_description="Run allowlisted synthetic test commands.",
        allowed_paths=["tests", "outputs", "sub"],
        blocked_paths=["secrets"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "run_command"],
        allowed_network_destinations=[],
        requires_approval=[],
        allowed_commands=[["python", "-m", "pytest"], ["pytest"], ["ls"]],
        created_at=_now(),
    )
    return Run(
        id=str(uuid.uuid4()),
        name="Command Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=_now(),
        updated_at=_now(),
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


def _cmd(
    run: Run,
    resource: str = ".",
    command: str | None = None,
    argv: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> ActionRequest:
    arguments: dict[str, Any] = dict(extra or {})
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
        requested_at=_now(),
    )


def _allow(action_id: str = "action-cmd"):
    from scopewatch.schemas import PolicyDecision

    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted (M2 acceptance fixture).",
        matched_rule="RULE_ALLOWED",
        decided_at=_now(),
        deterministic=True,
    )


def _run_helper_local(
    operation: str, resource: str, args_dict: dict, workspace: Path
) -> dict:
    env = dict(os.environ, SCOPEWATCH_WORKSPACE=str(workspace))
    proc = subprocess.run(
        [sys.executable, "-c", _helper_code(), operation, resource, json.dumps(args_dict)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        env=env,
        cwd=str(workspace),
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return json.loads(proc.stdout.decode("utf-8", errors="replace"))


def _argv(*parts: str) -> dict:
    return {"argv": [sys.executable, "-c", *parts]}


# ---------------- C1: shlex argv, no shell ----------------


def test_m2_shlex_string_parsed_to_argv() -> None:
    assert extract_run_command_argv(
        ActionRequest(
            id="a", run_id="r", tool="workspace", operation="run_command",
            resource=".", arguments={"command": "python -m pytest tests/"},
            requested_at=_now(),
        )
    ) == ["python", "-m", "pytest", "tests/"]


def test_m2_shlex_quoting_preserved() -> None:
    argv = extract_run_command_argv(
        ActionRequest(
            id="a", run_id="r", tool="workspace", operation="run_command",
            resource=".", arguments={"command": 'pytest -k "test_login flow"'},
            requested_at=_now(),
        )
    )
    assert argv == ["pytest", "-k", "test_login flow"]


def test_m2_list_form_passes_through() -> None:
    argv = extract_run_command_argv(
        ActionRequest(
            id="a", run_id="r", tool="workspace", operation="run_command",
            resource=".", arguments={"argv": ["pytest", "tests/"]},
            requested_at=_now(),
        )
    )
    assert argv == ["pytest", "tests/"]


def test_m2_string_form_wins_when_both_present() -> None:
    """Gateway parses the string form; a conflicting list is ignored."""
    argv = extract_run_command_argv(
        ActionRequest(
            id="a", run_id="r", tool="workspace", operation="run_command",
            resource=".",
            arguments={"command": "ls .", "argv": ["pytest", "tests/"]},
            requested_at=_now(),
        )
    )
    assert argv == ["ls", "."]


@pytest.mark.parametrize("bad", [{}, {"command": ""}, {"argv": []}, {"argv": ["ok", 3]}])
def test_m2_malformed_argv_returns_none(bad: dict) -> None:
    action = ActionRequest(
        id="a", run_id="r", tool="workspace", operation="run_command",
        resource=".", arguments=bad, requested_at=_now(),
    )
    assert extract_run_command_argv(action) is None


def test_m2_helper_runs_argv_without_shell(cmd_workspace: Path) -> None:
    """Shell syntax in an argument stays literal (no shell=True anywhere)."""
    payload = _run_helper_local(
        "run_command", ".",
        {"argv": [sys.executable, "-c", "import sys; print(sys.argv[1])", "a;b|c&d"]},
        cmd_workspace,
    )
    assert payload["status"] == "EXECUTED"
    assert payload["result"]["stdout"] == "a;b|c&d\n"
    assert "shell=False" in _helper_code()


# ---------------- C2: pre-parse metacharacter rejection ----------------


@pytest.mark.parametrize(
    "metachar",
    [";", "&", "|", ">", "<", "`", "$(", "\n", "\r", "\x00"],
)
def test_m2_each_metacharacter_denied_string_form(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None, metachar: str
) -> None:
    """All ten code markers (incl. CR, which the older suite omits) deny."""
    decision = evaluate_policy(
        _cmd(cmd_run, command=f"pytest tests/ {metachar} ls"), cmd_run, cmd_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.SHELL_METACHARACTER


@pytest.mark.parametrize(
    "evil", ["a;b", "a|b", "a&b", "a>b", "a<b", "a`b", "a$(b", "a\nb", "a\rb", "a\x00b"]
)
def test_m2_argv_list_items_with_metacharacters_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None, evil: str
) -> None:
    """The list form is scanned item-by-item (distinct from string quoting)."""
    decision = evaluate_policy(
        _cmd(cmd_run, argv=["pytest", "tests/", evil]), cmd_run, cmd_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.SHELL_METACHARACTER


# ---------------- C3: allowlist prefix match ----------------


def test_m2_allowed_prefix_with_extra_args(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    decision = evaluate_policy(
        _cmd(cmd_run, command="python -m pytest tests/ -q"), cmd_run, cmd_workspace
    )
    assert decision.outcome == PolicyOutcome.ALLOW


def test_m2_prefix_mismatch_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    decision = evaluate_policy(
        _cmd(cmd_run, command='python -c "print(1)"'), cmd_run, cmd_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.COMMAND_NOT_ALLOWED


def test_m2_empty_allowlist_denies_everything(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    cmd_run.task_scope.allowed_commands = []
    decision = evaluate_policy(_cmd(cmd_run, command="ls ."), cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.COMMAND_NOT_ALLOWED


def test_m2_prefix_longer_than_argv_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    """A bare `python` does not satisfy the `python -m pytest` prefix."""
    decision = evaluate_policy(_cmd(cmd_run, command="python"), cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.COMMAND_NOT_ALLOWED


def test_m2_empty_prefix_entries_ignored(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    """Empty prefix entries never match (no wildcard allow)."""
    cmd_run.task_scope.allowed_commands = [[], ["ls"]]
    allowed = evaluate_policy(_cmd(cmd_run, command="ls ."), cmd_run, cmd_workspace)
    assert allowed.outcome == PolicyOutcome.ALLOW
    denied = evaluate_policy(
        _cmd(cmd_run, command="python -m pytest tests/"), cmd_run, cmd_workspace
    )
    assert denied.outcome == PolicyOutcome.DENY


# ---------------- C4: path args + cwd ----------------


def test_m2_traversal_arg_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    decision = evaluate_policy(
        _cmd(cmd_run, command="pytest ../../etc"), cmd_run, cmd_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_TRAVERSAL


def test_m2_absolute_arg_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    decision = evaluate_policy(_cmd(cmd_run, command="ls /home"), cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_OUTSIDE_WORKSPACE


def test_m2_blocked_arg_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    decision = evaluate_policy(
        _cmd(cmd_run, command="ls secrets/notes.txt"), cmd_run, cmd_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH


def test_m2_symlinked_arg_escaping_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None, tmp_path: Path
) -> None:
    """A workspace symlink pointing outside is caught by containment."""
    secret = tmp_path / "host-secret.txt"
    secret.write_text("synthetic host secret", encoding="utf-8")
    link = cmd_workspace / "tests" / "evil-link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("Cannot create symlinks on this platform.")
    decision = evaluate_policy(
        _cmd(cmd_run, command="pytest tests/evil-link.txt"), cmd_run, cmd_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.SYMLINK_ESCAPE


def test_m2_plain_flags_are_not_paths(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    """Non-path arguments (dashes, single tokens) pass the path rules."""
    decision = evaluate_policy(
        _cmd(cmd_run, command="pytest tests/ -q --tb=short"),
        cmd_run,
        cmd_workspace,
    )
    assert decision.outcome == PolicyOutcome.ALLOW


@pytest.mark.parametrize("cwd", ["", "."])
def test_m2_empty_and_dot_cwd_allowed(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None, cwd: str
) -> None:
    decision = evaluate_policy(_cmd(cmd_run, resource=cwd, command="ls ."), cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW


def test_m2_subdir_cwd_allowed(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    decision = evaluate_policy(
        _cmd(cmd_run, resource="sub", command="ls ."), cmd_run, cmd_workspace
    )
    assert decision.outcome == PolicyOutcome.ALLOW


@pytest.mark.parametrize(
    ("cwd", "code"),
    [
        ("/tmp", ReasonCode.PATH_OUTSIDE_WORKSPACE),
        ("../..", ReasonCode.PATH_TRAVERSAL),
        ("secrets", ReasonCode.BLOCKED_PATH),
        ("a\x00b", ReasonCode.MALFORMED_REQUEST),
    ],
)
def test_m2_bad_cwd_denied(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None, cwd: str, code: ReasonCode
) -> None:
    decision = evaluate_policy(_cmd(cmd_run, resource=cwd, command="ls ."), cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == code


@pytest.mark.parametrize("raw", ["", "   ", "\t  "])
def test_m2_empty_command_malformed(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None, raw: str
) -> None:
    decision = evaluate_policy(_cmd(cmd_run, command=raw), cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.MALFORMED_REQUEST


# ---------------- C5: docker-only gate ----------------


def test_m2_local_backend_unsupported_policy(
    cmd_workspace: Path, cmd_run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "local")
    decision = evaluate_policy(_cmd(cmd_run, command="pytest tests/"), cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.UNSUPPORTED_OPERATION


def test_m2_local_executor_refuses_run_command(
    cmd_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: the local executor refuses even with an ALLOW decision."""
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "local")
    action = ActionRequest(
        id="action-cmd", run_id="run-1", tool="workspace", operation="run_command",
        resource=".", arguments={"argv": ["pytest", "tests/"]}, requested_at=_now(),
    )
    with pytest.raises(ExecutionSecurityError, match="requires the Docker executor"):
        execute_action(action, cmd_workspace, policy_decision=_allow("action-cmd"))


def test_m2_docker_unavailable_run_command_fails_closed(
    cmd_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scopewatch.executor_docker as docker_mod

    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: False)
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    action = ActionRequest(
        id="action-cmd", run_id="run-1", tool="workspace", operation="run_command",
        resource=".", arguments={"argv": ["pytest", "tests/"]}, requested_at=_now(),
    )
    receipt = execute_action(action, cmd_workspace, policy_decision=_allow("action-cmd"))
    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.error_code == "EXECUTION_FAILED"
    assert receipt.executor == "docker-executor"


# ---------------- C6: requires_approval ----------------


def test_m2_scope_wide_approval_holds(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    cmd_run.task_scope.requires_approval = ["run_command"]
    decision = evaluate_policy(_cmd(cmd_run, command="pytest tests/"), cmd_run, cmd_workspace)
    assert decision.outcome == PolicyOutcome.HOLD
    assert decision.reason_code == ReasonCode.APPROVAL_REQUIRED


def test_m2_per_command_approval_selective(
    cmd_workspace: Path, cmd_run: Run, docker_backend: None
) -> None:
    cmd_run.task_scope.commands_requiring_approval = [["python", "-m", "pytest"]]
    held = evaluate_policy(
        _cmd(cmd_run, command="python -m pytest tests/"), cmd_run, cmd_workspace
    )
    assert held.outcome == PolicyOutcome.HOLD
    allowed = evaluate_policy(_cmd(cmd_run, command="ls ."), cmd_run, cmd_workspace)
    assert allowed.outcome == PolicyOutcome.ALLOW


# ---------------- C7: executor limits via the real helper ----------------


def test_m2_helper_success_records_exit_code(cmd_workspace: Path) -> None:
    payload = _run_helper_local(
        "run_command", ".", _argv("print('synthetic-hello')"), cmd_workspace
    )
    assert payload["status"] == "EXECUTED"
    assert payload["error_code"] is None
    assert payload["result"]["exit_code"] == 0
    assert payload["result"]["stdout"] == "synthetic-hello\n"
    assert payload["result"]["timed_out"] is False


def test_m2_helper_nonzero_exit_is_failure_not_denial(cmd_workspace: Path) -> None:
    payload = _run_helper_local(
        "run_command", ".", _argv("import sys; sys.exit(3)"), cmd_workspace
    )
    assert payload["status"] == "FAILED"
    assert payload["error_code"] == "NONZERO_EXIT"
    assert payload["result"]["exit_code"] == 3
    assert payload["error_code"] not in {c.value for c in ReasonCode}


def test_m2_helper_timeout_kills_and_marks(cmd_workspace: Path) -> None:
    payload = _run_helper_local(
        "run_command", ".",
        {**_argv("import time; time.sleep(30)"), "timeout_s": 1},
        cmd_workspace,
    )
    assert payload["status"] == "FAILED"
    assert payload["error_code"] == "COMMAND_TIMEOUT"
    assert payload["result"]["timed_out"] is True


def test_m2_helper_stdout_truncated_with_marker(cmd_workspace: Path) -> None:
    payload = _run_helper_local(
        "run_command", ".", _argv("import sys; sys.stdout.write('A' * 200000)"),
        cmd_workspace,
    )
    assert payload["result"]["truncated_stdout"] is True
    assert "...[truncated" in payload["result"]["stdout"]
    assert len(payload["result"]["stdout"].encode()) <= RUN_COMMAND_OUTPUT_LIMIT + 256


def test_m2_helper_stderr_truncated_with_marker(cmd_workspace: Path) -> None:
    """Stderr has its own 64 KiB cap (the older suite only covers stdout)."""
    payload = _run_helper_local(
        "run_command", ".", _argv("import sys; sys.stderr.write('E' * 200000)"),
        cmd_workspace,
    )
    assert payload["result"]["truncated_stderr"] is True
    assert "...[truncated" in payload["result"]["stderr"]
    assert len(payload["result"]["stderr"].encode()) <= RUN_COMMAND_OUTPUT_LIMIT + 256


def test_m2_helper_rejects_bad_cwd(cmd_workspace: Path) -> None:
    for bad in ("/tmp", "../.."):
        payload = _run_helper_local("run_command", bad, _argv("print('x')"), cmd_workspace)
        assert payload["status"] == "FAILED"


def test_m2_helper_rejects_malformed_and_null(cmd_workspace: Path) -> None:
    assert _run_helper_local(
        "run_command", ".", {"argv": "not-a-list"}, cmd_workspace
    )["status"] == "FAILED"
    assert _run_helper_local(
        "run_command", ".", {"argv": ["pytest", "a\x00b"]}, cmd_workspace
    )["status"] == "FAILED"
    # NUL in the resource cannot travel through subprocess argv (the OS
    # rejects embedded null bytes before the helper starts), so the
    # helper's resource guard is asserted statically: the policy layer
    # already denies NUL cwd with MALFORMED_REQUEST (see bad-cwd matrix).
    assert "'\\x00' in resource" in _helper_code()


def test_m2_helper_forbids_network_and_unknown(cmd_workspace: Path) -> None:
    assert _run_helper_local(
        "network_request", "https://example.invalid", {}, cmd_workspace
    )["status"] == "FAILED"
    assert _run_helper_local(
        "bogus_op", ".", {}, cmd_workspace
    )["status"] == "FAILED"


# ---------------- C8: timeout clamp ----------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (10, 10.0),
        ("25", 25.0),
        (-5, RUN_COMMAND_MIN_TIMEOUT_S),
        (100000, RUN_COMMAND_MAX_TIMEOUT_S),
        ("nope", RUN_COMMAND_DEFAULT_TIMEOUT_S),
        (None, RUN_COMMAND_DEFAULT_TIMEOUT_S),
        (float("nan"), RUN_COMMAND_DEFAULT_TIMEOUT_S),
        (float("inf"), RUN_COMMAND_DEFAULT_TIMEOUT_S),
    ],
)
def test_m2_timeout_clamp(given: object, expected: float) -> None:
    """Default 60 s, hard bounds [1, 300]; garbage/NaN/Inf fall back to default."""
    assert clamp_run_command_timeout(given) == expected
    assert RUN_COMMAND_DEFAULT_TIMEOUT_S == 60.0
    assert RUN_COMMAND_OUTPUT_LIMIT == 64 * 1024
