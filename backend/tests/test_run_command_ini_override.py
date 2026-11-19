"""``-o=KEY=VALUE`` must not smuggle path or ini overrides past run_command rules.

argparse hands pytest ``-o=VALUE`` as ``VALUE``, so ``-o=cache_dir=/tmp/x`` is
the ini override ``cache_dir=/tmp/x``. The path checks used to split each token
on the first ``=`` only and saw the non-absolute ``cache_dir=/tmp/x``. All
fixtures are synthetic.
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


DENIED = [
    "pytest -o=cache_dir=/tmp/x",
    "pytest -o cache_dir=/tmp/x",
    "pytest -o=pythonpath=/opt/evil tests",
    "pytest -o=confcutdir=/",
    "pytest -o=testpaths=/usr/lib",
    "pytest -o=cache_dir=secrets/c",
    "pytest -o cache_dir=secrets/c",
    "pytest -o=cache_dir=../escape",
    "pytest -o=pythonpath=outputs tests",
    "pytest -o pythonpath=outputs tests",
    "pytest -o=required_plugins=foo",
    "pytest -o=addopts=-q",
    "pytest -ocache_dir=/tmp/x",
    "pytest -o=cache_dir",
    "python -m pytest -o=cache_dir=/tmp/x",
    "python -m pytest -o=pythonpath=/opt/evil tests",
    # pytest expands ~ and $VAR in cache_dir and --junitxml before resolving.
    "pytest tests -o=cache_dir=~/x",
    "pytest tests -o cache_dir=~/x",
    "pytest tests -o cache_dir=$HOME/x",
    "pytest tests -o=cache_dir=${HOME}/x",
    "pytest tests -o=cache_dir=$TMPDIR",
    "pytest tests --junitxml=~/j.xml",
    "pytest tests --junitxml=$HOME/j.xml",
    "pytest tests --junitxml ~/j.xml",
    "pytest tests --rootdir=$HOME",
    "pytest tests --basetemp=~/t",
    "pytest tests --confcutdir $TMPDIR",
]

ALLOWED = [
    "pytest tests/ -o strict_xfail=True",
    "pytest tests/ -o=strict_xfail=True",
    "pytest tests/ -o cache_dir=outputs/.cache",
    "pytest tests/ -o=cache_dir=outputs/.cache",
    "python -m pytest tests/ -o=xfail_strict=true",
    # Positional arguments are passed literally (no expansion), so "$" there
    # stays allowed; only expanded option values are denied.
    "pytest tests/ -o cache_dir=outputs/.cache tests/test_$x.py",
]


@pytest.mark.parametrize("command", DENIED)
def test_ini_override_denied_at_both_layers(
    ws: Path, run: Run, docker_backend: None, command: str
) -> None:
    action = _action(run, command)
    decision = evaluate_policy(action, run, ws)
    assert decision.outcome == PolicyOutcome.DENY
    reval = revalidate_run_command_in_workspace(action, run.task_scope, ws)
    assert reval is not None and isinstance(reval[0], ReasonCode)


@pytest.mark.parametrize("command", ALLOWED)
def test_safe_ini_override_still_allowed(
    ws: Path, run: Run, docker_backend: None, command: str
) -> None:
    action = _action(run, command)
    assert evaluate_policy(action, run, ws).outcome == PolicyOutcome.ALLOW
    assert revalidate_run_command_in_workspace(action, run.task_scope, ws) is None
