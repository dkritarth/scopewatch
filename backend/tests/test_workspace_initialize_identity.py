"""initialize() must not hand back another run's workspace (burn-1119).

``initialize`` used to return any existing ``root/<run>`` that carried a marker
file, following symlinks and ignoring which run the marker names. A link (or a
copied directory) therefore made two run ids share one writable tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scopewatch.workspaces import WORKSPACE_MARKER_NAME, RunWorkspaceError, RunWorkspaceManager


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


def test_initialize_refuses_symlinked_marker_file(tmp_path: Path) -> None:
    """A marker that is a link (retention opens it O_NOFOLLOW) is not ownership."""
    import json

    manager = _manager(tmp_path)
    (manager.root / "run-a").mkdir(parents=True)
    forged = tmp_path / "forged-marker.json"
    forged.write_text(json.dumps({"version": 1, "run_id": "run-a"}), encoding="utf-8")
    (manager.root / "run-a" / WORKSPACE_MARKER_NAME).symlink_to(forged)
    with pytest.raises(RunWorkspaceError):
        manager.initialize("run-a")


def test_initialize_refuses_marker_with_wrong_or_missing_version(tmp_path: Path) -> None:
    import json

    manager = _manager(tmp_path)
    own = manager.initialize("run-a")
    marker_path = own / WORKSPACE_MARKER_NAME
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    for bad in ({**marker, "version": "not-a-version"}, {k: v for k, v in marker.items() if k != "version"}):
        marker_path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(RunWorkspaceError):
            manager.initialize("run-a")
