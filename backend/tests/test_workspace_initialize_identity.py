"""initialize() must not hand back another run's workspace (burn-1119).

``initialize`` used to return any existing ``root/<run>`` that carried a marker
file, following symlinks and ignoring which run the marker names. A link (or a
copied directory) therefore made two run ids share one writable tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scopewatch.workspaces import RunWorkspaceError, RunWorkspaceManager


def _manager(tmp_path: Path) -> RunWorkspaceManager:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "a.txt").write_text("synthetic", encoding="utf-8")
    return RunWorkspaceManager(fixture, tmp_path / "runs")


def test_initialize_refuses_symlink_onto_sibling_workspace(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    sibling = manager.initialize("run-b")
    (manager.root / "run-a").symlink_to(sibling, target_is_directory=True)
    with pytest.raises(RunWorkspaceError):
        manager.initialize("run-a")


def test_initialize_refuses_marker_naming_another_run(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    sibling = manager.initialize("run-b")
    (manager.root / "run-a").mkdir()
    (manager.root / "run-a" / ".scopewatch-run-workspace.json").write_bytes(
        (sibling / ".scopewatch-run-workspace.json").read_bytes()
    )
    with pytest.raises(RunWorkspaceError):
        manager.initialize("run-a")


def test_initialize_still_idempotent_for_own_workspace(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = manager.initialize("run-a")
    (first / "out.txt").write_text("kept", encoding="utf-8")
    assert manager.initialize("run-a") == first
    assert (first / "out.txt").read_text(encoding="utf-8") == "kept"
