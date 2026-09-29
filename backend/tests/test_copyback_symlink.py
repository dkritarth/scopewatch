"""Copy-back symlink tolerance (issue #80-part1).

Regression: ``_sync_copy_back`` used
``shutil.copytree(..., symlinks=True, dirs_exist_ok=True)``, which raises
``FileExistsError``/``shutil.Error`` when the staged copy contains a symlink
that already exists in the destination workspace — turning a legitimate
container ``EXECUTED`` into a host-side ``FAILED``. All tests use synthetic
fixtures with a mocked Docker CLI (no daemon needed).
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import uuid

from scopewatch.executor import execute_action
from scopewatch.executor_docker import _sync_copy_back
from scopewatch.models import ExecutionStatus, PolicyOutcome, ReasonCode
from scopewatch.schemas import ActionRequest, PolicyDecision


def _allow_decision(action_id: str = "action-1") -> PolicyDecision:
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=datetime.now(timezone.utc).isoformat(),
        deterministic=True,
    )


def _action(
    operation: str, resource: str, action_id: str = "action-1", **kwargs: object
) -> ActionRequest:
    return ActionRequest(
        id=action_id,
        run_id="run-1",
        tool="workspace",
        operation=operation,
        resource=resource,
        arguments=kwargs.get("arguments", {}),  # type: ignore[arg-type]
        requested_at=datetime.now(timezone.utc).isoformat(),
    )


def test_sync_copy_back_replaces_existing_symlink(tmp_path: Path) -> None:
    """Pre-existing destination link must not raise; link stays a link."""
    staged = tmp_path / "staged"
    dest = tmp_path / "dest"
    (staged / "sub").mkdir(parents=True)
    dest.mkdir(parents=True)
    (staged / "real.txt").write_text("staged-content", encoding="utf-8")
    (staged / "link.txt").symlink_to("real.txt")
    (staged / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    # Destination already has the same symlink (the #80 repro collision).
    (dest / "real.txt").write_text("old-content", encoding="utf-8")
    (dest / "link.txt").symlink_to("real.txt")

    _sync_copy_back(staged, dest)  # must not raise

    assert (dest / "link.txt").is_symlink()
    assert (dest / "link.txt").read_text(encoding="utf-8") == "staged-content"
    assert (dest / "real.txt").read_text(encoding="utf-8") == "staged-content"
    assert (dest / "sub" / "nested.txt").read_text(encoding="utf-8") == "nested"


def test_sync_copy_back_preserves_dangling_link_and_new_files(tmp_path: Path) -> None:
    """Dangling staged links replicate as links; new regular files sync."""
    staged = tmp_path / "staged"
    dest = tmp_path / "dest"
    staged.mkdir()
    dest.mkdir()
    (staged / "dangling").symlink_to("nowhere-target.txt")
    (staged / "new-file.txt").write_text("fresh", encoding="utf-8")
    (dest / "untouched.txt").write_text("keep-me", encoding="utf-8")

    _sync_copy_back(staged, dest)

    assert (dest / "dangling").is_symlink()
    assert (dest / "new-file.txt").read_text(encoding="utf-8") == "fresh"
    # Files absent from the staged copy are left alone (dirs_exist_ok parity).
    assert (dest / "untouched.txt").read_text(encoding="utf-8") == "keep-me"


def _mocked_docker_run(monkeypatch, container_write) -> None:
    """Patch the Docker CLI: `docker info` succeeds; `docker run` simulates.

    ``container_write`` receives the staged workspace copy path parsed from
    the ``-v <copy>:/workspace:rw`` mount, mutates it like the container
    would, and the mock returns a JSON EXECUTED helper payload.
    """
    import scopewatch.executor_docker as docker_mod

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if isinstance(cmd, list) and cmd[:2] == ["docker", "run"]:
            copy_path = None
            for i, part in enumerate(cmd):
                if part == "-v" and i + 1 < len(cmd):
                    copy_path = Path(cmd[i + 1].split(":")[0])
                    break
            assert copy_path is not None and copy_path.is_dir()
            container_write(copy_path)
            payload = {
                "status": "EXECUTED",
                "result": {"operation": "run_command", "argv": ["true"], "exit_code": 0},
                "error_code": None,
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload).encode(), stderr=b"")
        if isinstance(cmd, list) and cmd[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_mod, "is_docker_available", lambda *a, **k: True)


def test_run_command_executed_in_symlink_workspace_syncs_back(
    monkeypatch, tmp_path: Path
) -> None:
    """Repro from #80: symlink workspace + container EXECUTED stays EXECUTED."""
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "data.txt").write_text("v1", encoding="utf-8")
    (ws / "link.txt").symlink_to("data.txt")

    def container_write(copy: Path) -> None:
        # Container modifies a regular file through the staged copy.
        (copy / "data.txt").write_text("v2-from-container", encoding="utf-8")

    _mocked_docker_run(monkeypatch, container_write)

    action = _action("run_command", ".", arguments={"command": "true"})
    receipt = execute_action(action, ws, policy_decision=_allow_decision())

    assert receipt.status == ExecutionStatus.EXECUTED, receipt.sanitized_result
    assert receipt.executor == "docker-executor"
    # Regular-file modification synced back through the symlink collision.
    assert (ws / "data.txt").read_text(encoding="utf-8") == "v2-from-container"
    # The link itself is intact and still a link (never followed/replaced).
    assert (ws / "link.txt").is_symlink()
    assert (ws / "link.txt").read_text(encoding="utf-8") == "v2-from-container"


def test_write_text_executed_in_symlink_workspace_stays_executed(
    monkeypatch, tmp_path: Path
) -> None:
    """write_text path also syncs through a pre-existing destination link."""
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "notes.txt").write_text("before", encoding="utf-8")
    (ws / "notes-link.txt").symlink_to("notes.txt")

    def container_write(copy: Path) -> None:
        (copy / "notes.txt").write_text("after", encoding="utf-8")

    _mocked_docker_run(monkeypatch, container_write)

    action = _action("write_text", "notes.txt", arguments={"content": "after"})
    receipt = execute_action(action, ws, policy_decision=_allow_decision())

    assert receipt.status == ExecutionStatus.EXECUTED, receipt.sanitized_result
    assert (ws / "notes.txt").read_text(encoding="utf-8") == "after"
    assert (ws / "notes-link.txt").is_symlink()
