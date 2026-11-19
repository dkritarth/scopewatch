"""pytest ``@file`` argument files must not smuggle options past the flag allowlist.

pytest (argparse ``fromfile_prefix_chars="@"``) expands any argv token that
starts with ``@`` into the lines of that file *before* option parsing, in any
position, including option values and after ``--``. The gateway only inspects
tokens that start with ``-``, so ``@outputs/args.txt`` carried arbitrary
options (``-p``, ``-o addopts=...``, ``--rootdir=/``) past the default-deny flag
allowlist. All fixtures are synthetic.
"""

from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from scopewatch.models import PolicyOutcome, ReasonCode, RunStatus
from scopewatch.policy import evaluate_policy, revalidate_run_command_in_workspace
from scopewatch.schemas import ActionRequest, Run, TaskScope

NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    (root / "outputs").mkdir()
    (root / "outputs" / "args.txt").write_text("-p\nno:cacheprovider\n", encoding="utf-8")
    return root


@pytest.fixture
def run() -> Run:
    scope = TaskScope(
        task_description="Run pytest.",
        allowed_paths=["tests", "outputs"],
        blocked_paths=["secrets"],
        allowed_tools=["workspace"],
        allowed_operations=["run_command"],
        allowed_commands=[["pytest"], ["python", "-m", "pytest"]],
        created_at=NOW,
    )
    return Run(
        id=str(uuid.uuid4()), name="r", task_scope=scope, status=RunStatus.ACTIVE,
        created_at=NOW, updated_at=NOW,
    )


def _action(run: Run, command: str) -> ActionRequest:
    return ActionRequest(
        id=str(uuid.uuid4()), run_id=run.id, tool="workspace", operation="run_command",
        resource=".", arguments={"command": command}, requested_at=NOW,
    )


@pytest.mark.parametrize(
    "command",
    [
        "pytest @outputs/args.txt",
        "pytest tests/ @outputs/args.txt",
        "python -m pytest @outputs/args.txt",
        "pytest -o @outputs/args.txt",
        "pytest --rootdir @outputs/args.txt",
        "pytest -- @outputs/args.txt",
    ],
)
def test_pytest_argument_file_denied(
    ws: Path, run: Run, docker_backend: None, command: str
) -> None:
    action = _action(run, command)
    decision = evaluate_policy(action, run, ws)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.COMMAND_NOT_ALLOWED
    reval = revalidate_run_command_in_workspace(action, run.task_scope, ws)
    assert reval is not None and reval[0] == ReasonCode.COMMAND_NOT_ALLOWED


def test_pytest_plain_paths_still_allowed(ws: Path, run: Run, docker_backend: None) -> None:
    decision = evaluate_policy(_action(run, "pytest tests/"), run, ws)
    assert decision.outcome == PolicyOutcome.ALLOW
