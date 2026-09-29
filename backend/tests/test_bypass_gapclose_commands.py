"""Gap-close command tricks for issue #39 (second-pass evidence).

Supplements ``backend/tests/test_bypass.py`` section B. All string-form
``--rootdir=``/``--confcutdir=`` cases there use ``=``; the space-separated
argv forms, cwd (resource) validation (R8), ``=``-split positives, extra
metacharacters, malformed shapes, and ``${HOME}`` literals are new here.

Policy sources are READ-ONLY; this file only calls ``evaluate_policy``.
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
def gap_cmd_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "cmd-workspace"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_auth.py").write_text("synthetic test", encoding="utf-8")
    (ws / "secrets").mkdir()
    (ws / "secrets" / "notes.txt").write_text("synthetic secret", encoding="utf-8")
    (ws / "outputs").mkdir()
    return ws


@pytest.fixture
def gap_cmd_run() -> Run:
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
        name="Command Gapclose Run",
        task_scope=scope,
        status=RunStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


def _cmd(
    run: Run,
    command: str | None = None,
    argv: list[str] | None = None,
    resource: str = ".",
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


def _snapshot(ws: Path) -> dict[str, str]:
    p = ws / "secrets" / "notes.txt"
    return {str(p): p.read_text(encoding="utf-8")}


def _assert_cmd_denied(
    ws: Path,
    run: Run,
    reason: ReasonCode,
    command: str | None = None,
    argv: list[str] | None = None,
    resource: str = ".",
) -> None:
    before = _snapshot(ws)
    decision = evaluate_policy(_cmd(run, command, argv, resource), run, ws)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == reason
    receipt = execute_action(_cmd(run, command, argv, resource), ws, policy_decision=decision)
    assert receipt.status == ExecutionStatus.NOT_EXECUTED
    assert _snapshot(ws) == before


# --- R8: cwd (resource) validation — entirely uncovered in test_bypass.py ---


@pytest.mark.parametrize(
    ("cwd", "reason"),
    [
        ("secrets", ReasonCode.BLOCKED_PATH),
        ("secrets/notes.txt", ReasonCode.BLOCKED_PATH),
        ("/", ReasonCode.PATH_OUTSIDE_WORKSPACE),
        ("../..", ReasonCode.PATH_TRAVERSAL),
    ],
    ids=["cwd-blocked", "cwd-blocked-file", "cwd-absolute", "cwd-traversal"],
)
def test_gapclose_run_command_cwd_denied(
    gap_cmd_workspace: Path, gap_cmd_run: Run, cwd: str, reason: ReasonCode,
    docker_backend: None,
) -> None:
    _assert_cmd_denied(
        gap_cmd_workspace, gap_cmd_run, reason, command="pytest tests/", resource=cwd
    )


@pytest.mark.parametrize("cwd", ["tests", "outputs", "."], ids=["tests", "outputs", "dot"])
def test_gapclose_run_command_cwd_allowed_control(
    gap_cmd_workspace: Path, gap_cmd_run: Run, cwd: str, docker_backend: None
) -> None:
    decision = evaluate_policy(
        _cmd(gap_cmd_run, command="pytest tests/", resource=cwd),
        gap_cmd_run,
        gap_cmd_workspace,
    )
    assert decision.outcome == PolicyOutcome.ALLOW


# --- Space-separated flag values (test_bypass.py covers only "=" string form) ---


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["pytest", "--rootdir", "/", "tests/"], ReasonCode.PATH_OUTSIDE_WORKSPACE),
        (["pytest", "--rootdir", "secrets", "tests/"], ReasonCode.BLOCKED_PATH),
        (["pytest", "--rootdir", "../..", "tests/"], ReasonCode.PATH_TRAVERSAL),
        (["pytest", "--confcutdir", "/", "tests/"], ReasonCode.PATH_OUTSIDE_WORKSPACE),
        (["pytest", "--confcutdir", "secrets", "tests/"], ReasonCode.BLOCKED_PATH),
    ],
    ids=[
        "rootdir-space-absolute",
        "rootdir-space-blocked",
        "rootdir-space-traversal",
        "confcutdir-space-absolute",
        "confcutdir-space-blocked",
    ],
)
def test_gapclose_space_separated_flag_values_denied(
    gap_cmd_workspace: Path, gap_cmd_run: Run, argv: list[str], reason: ReasonCode,
    docker_backend: None,
) -> None:
    _assert_cmd_denied(gap_cmd_workspace, gap_cmd_run, reason, argv=argv)


# --- "=" split: positives and controls ---


def test_gapclose_junitxml_absolute_denied_via_equals_split(
    gap_cmd_workspace: Path, gap_cmd_run: Run, docker_backend: None
) -> None:
    """The value after ``=`` is checked as well as the whole token."""
    _assert_cmd_denied(
        gap_cmd_workspace,
        gap_cmd_run,
        ReasonCode.PATH_OUTSIDE_WORKSPACE,
        command="pytest --junitxml=/tmp/out.xml tests/",
    )
    _assert_cmd_denied(
        gap_cmd_workspace,
        gap_cmd_run,
        ReasonCode.PATH_OUTSIDE_WORKSPACE,
        argv=["pytest", "--junitxml=/tmp/out.xml", "tests/"],
    )


def test_gapclose_junitxml_relative_allowed_control(
    gap_cmd_workspace: Path, gap_cmd_run: Run, docker_backend: None
) -> None:
    decision = evaluate_policy(
        _cmd(gap_cmd_run, command="pytest --junitxml=outputs/out.xml tests/"),
        gap_cmd_run,
        gap_cmd_workspace,
    )
    assert decision.outcome == PolicyOutcome.ALLOW


def test_gapclose_basetemp_absolute_denied(
    gap_cmd_workspace: Path, gap_cmd_run: Run, docker_backend: None
) -> None:
    _assert_cmd_denied(
        gap_cmd_workspace,
        gap_cmd_run,
        ReasonCode.PATH_OUTSIDE_WORKSPACE,
        argv=["pytest", "--basetemp=/tmp", "tests/"],
    )


def test_gapclose_deselect_value_unchecked_documents_gap(
    gap_cmd_workspace: Path, gap_cmd_run: Run, docker_backend: None
) -> None:
    """Same V1 limitation as ``-p`` (``test_bypass_pytest_plugin_flag_...``):
    non-path option values such as ``-k``/``--deselect`` carry no path signal
    and pass; containment rests on the Docker sandbox."""
    for argv in (
        ["pytest", "-k", "test_auth", "tests/"],
        ["pytest", "--deselect", "tests/test_auth.py::test_x", "tests/"],
        ["pytest", "-p", "evilplugin", "tests/"],
    ):
        decision = evaluate_policy(_cmd(gap_cmd_run, argv=argv), gap_cmd_run, gap_cmd_workspace)
        assert decision.outcome == PolicyOutcome.ALLOW


# --- Metacharacters beyond the three in test_bypass.py ---


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests/ && id",
        "pytest tests/ || id",
        "pytest tests/ | cat",
        "pytest tests/ > /tmp/x",
        "pytest tests/ < /etc/passwd",
        "pytest tests/ & sleep 1",
        "pytest tests/\n id",
    ],
    ids=["and", "or", "pipe", "redirect-out", "redirect-in", "amp", "newline"],
)
def test_gapclose_extra_shell_metacharacters_denied(
    gap_cmd_workspace: Path, gap_cmd_run: Run, docker_backend: None, command: str
) -> None:
    _assert_cmd_denied(
        gap_cmd_workspace, gap_cmd_run, ReasonCode.SHELL_METACHARACTER, command=command
    )


def test_gapclose_quoted_semicolon_still_denied(
    gap_cmd_workspace: Path, gap_cmd_run: Run, docker_backend: None
) -> None:
    """Fail-closed: the metacharacter screen runs on the raw string before
    shlex, so quoting cannot smuggle ``;`` past the gateway."""
    _assert_cmd_denied(
        gap_cmd_workspace,
        gap_cmd_run,
        ReasonCode.SHELL_METACHARACTER,
        command="pytest 'tests/;id' tests/",
    )


def test_gapclose_null_byte_in_command_is_metacharacter(
    gap_cmd_workspace: Path, gap_cmd_run: Run, docker_backend: None
) -> None:
    """``\\x00`` in a command string trips the pre-parse metacharacter screen
    (SHELL_METACHARACTER), not the path null-byte rule — either way DENY and
    nothing executes."""
    _assert_cmd_denied(
        gap_cmd_workspace,
        gap_cmd_run,
        ReasonCode.SHELL_METACHARACTER,
        command="pytest --rootdir=secrets\x00 tests/",
    )


def test_gapclose_null_byte_in_argv_path_is_malformed(
    gap_cmd_workspace: Path, gap_cmd_run: Run, docker_backend: None
) -> None:
    """In argv form each item is screened for metacharacters first (``\\x00``
    is in that set), so this is also SHELL_METACHARACTER. Documents the
    precedence; the resource-path MALFORMED rule covers ``read_text``."""
    _assert_cmd_denied(
        gap_cmd_workspace,
        gap_cmd_run,
        ReasonCode.SHELL_METACHARACTER,
        argv=["pytest", "tests/\x00", "tests/"],
    )


# --- Malformed shapes ---


@pytest.mark.parametrize(
    ("command", "argv"),
    [("", None), (None, [])],
    ids=["empty-string", "empty-argv"],
)
def test_gapclose_empty_command_malformed(
    gap_cmd_workspace: Path, gap_cmd_run: Run, docker_backend: None,
    command: str | None, argv: list[str] | None,
) -> None:
    _assert_cmd_denied(
        gap_cmd_workspace, gap_cmd_run, ReasonCode.MALFORMED_REQUEST,
        command=command, argv=argv,
    )


# --- Env expansion literals ---


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        ("pytest ${HOME} tests/", None),
        ("pytest $HOME tests/", None),
        (None, ["pytest", "${HOME}", "tests/"]),
    ],
    ids=["brace-string", "bare-string", "brace-argv"],
)
def test_gapclose_env_tokens_stay_literal(
    gap_cmd_workspace: Path, gap_cmd_run: Run, docker_backend: None,
    command: str | None, argv: list[str] | None,
) -> None:
    """Bare ``$VAR``/``${VAR}`` (without parens) pass the metacharacter screen
    but execution uses argv with ``shell=False`` — they stay literal relative
    paths. ``test_bypass.py`` covers ``$HOME`` argv only; the ``${HOME}`` and
    string forms are new here."""
    decision = evaluate_policy(
        _cmd(gap_cmd_run, command=command, argv=argv),
        gap_cmd_run,
        gap_cmd_workspace,
    )
    assert decision.outcome == PolicyOutcome.ALLOW
    assert decision.reason_code == ReasonCode.ALLOWED_TOOL_AND_RESOURCE


@pytest.mark.parametrize(
    "command",
    ["python -m pytest tests/", "ls tests/", "pytest tests/"],
    ids=["python-m-pytest", "ls", "pytest"],
)
def test_gapclose_allowlisted_prefix_controls(
    gap_cmd_workspace: Path, gap_cmd_run: Run, docker_backend: None, command: str
) -> None:
    decision = evaluate_policy(
        _cmd(gap_cmd_run, command=command), gap_cmd_run, gap_cmd_workspace
    )
    assert decision.outcome == PolicyOutcome.ALLOW
