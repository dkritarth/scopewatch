"""Destination directory aliases must never redirect copy-back writes (#167)."""

from pathlib import Path

import pytest

from scopewatch.docker_job import sync_copy_back


def test_copyback_refuses_directory_link_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "run-a"
    foreign = workspace / "other-output"
    foreign.mkdir(parents=True)
    (foreign / "existing.txt").write_text("original")
    (workspace / "outputs").symlink_to("other-output", target_is_directory=True)
    staged = tmp_path / "staged"
    (staged / "outputs").mkdir(parents=True)
    (staged / "outputs" / "existing.txt").write_text("overwritten")
    (staged / "outputs" / "new.txt").write_text("planted")

    refused = sync_copy_back(staged, workspace)

    assert (foreign / "existing.txt").read_text() == "original"
    assert not (foreign / "new.txt").exists()
    assert (workspace / "outputs").is_symlink()
    assert refused == ["outputs"]


@pytest.mark.parametrize("depth", [0, 1, 4])
@pytest.mark.parametrize("link_target", ["inside", "foreign", "dangling", "loop"])
def test_copyback_refuses_destination_links_at_any_depth(
    tmp_path: Path, depth: int, link_target: str
) -> None:
    workspace = tmp_path / "run-a"
    foreign = tmp_path / "run-b" / "outputs"
    foreign.mkdir(parents=True)
    (foreign / "existing.txt").write_text("foreign-original")
    inside = workspace / "untouched"
    inside.mkdir(parents=True)
    (inside / "existing.txt").write_text("inside-original")
    relative = Path(*(["nested"] * depth), "outputs")
    link = workspace / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    targets = {
        "inside": inside,
        "foreign": foreign,
        "dangling": Path("missing"),
        "loop": Path("outputs"),
    }
    link.symlink_to(targets[link_target], target_is_directory=True)
    staged = tmp_path / "staged"
    (staged / relative / "deeper").mkdir(parents=True)
    (staged / relative / "existing.txt").write_text("overwritten")
    (staged / relative / "deeper" / "new.txt").write_text("planted")
    (staged / "safe.txt").write_text("safe")

    assert sync_copy_back(staged, workspace) == [relative.as_posix()]

    assert (foreign / "existing.txt").read_text() == "foreign-original"
    assert (inside / "existing.txt").read_text() == "inside-original"
    assert sorted(p.name for p in foreign.iterdir()) == ["existing.txt"]
    assert sorted(p.name for p in inside.iterdir()) == ["existing.txt"]
    assert link.is_symlink()
    assert not (link.parent / "missing").exists()
    assert (workspace / "safe.txt").read_text() == "safe"


@pytest.mark.parametrize("root_kind", ["link", "ancestor-link", "dangling", "file"])
def test_copyback_refuses_aliased_or_non_directory_root(
    tmp_path: Path, root_kind: str
) -> None:
    foreign = tmp_path / "run-b"
    (foreign / "subdir").mkdir(parents=True)
    (foreign / "existing.txt").write_text("original")
    root = tmp_path / "run-a"
    if root_kind == "file":
        root.write_text("original-file")
    else:
        root.symlink_to("missing" if root_kind == "dangling" else foreign,
                        target_is_directory=True)
    if root_kind == "ancestor-link":
        root = root / "subdir"
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "existing.txt").write_text("overwritten")

    assert sync_copy_back(staged, root) == ["."]

    assert (foreign / "existing.txt").read_text() == "original"
    assert list((foreign / "subdir").iterdir()) == []
    assert not (tmp_path / "missing").exists()
    if root_kind == "file":
        assert root.read_text() == "original-file"


def test_copyback_preserves_normal_copy_and_file_directory_rules(tmp_path: Path) -> None:
    workspace = tmp_path / "run-a"
    workspace.mkdir()
    (workspace / "directory-conflict").write_text("retain-file")
    (workspace / "file-conflict").mkdir()
    (workspace / "file-conflict" / "old.txt").write_text("old")
    foreign = tmp_path / "run-b.txt"
    foreign.write_text("foreign-original")
    (workspace / "file-link").symlink_to(foreign)
    staged = tmp_path / "staged"
    (staged / "new" / "nested").mkdir(parents=True)
    (staged / "empty").mkdir()
    (staged / "new" / "nested" / "ok.txt").write_text("normal-copy")
    (staged / "directory-conflict").mkdir()
    (staged / "directory-conflict" / "ignored.txt").write_text("ignored")
    (staged / "file-conflict").write_text("new-file")
    (staged / "file-link").write_text("replace-link")
    (staged / "dangling-source-link").symlink_to("missing")

    assert sync_copy_back(staged, workspace) == ["directory-conflict"]

    assert (workspace / "new" / "nested" / "ok.txt").read_text() == "normal-copy"
    assert (workspace / "empty").is_dir()
    assert (workspace / "directory-conflict").read_text() == "retain-file"
    assert (workspace / "file-conflict").read_text() == "new-file"
    assert not (workspace / "file-link").is_symlink()
    assert (workspace / "file-link").read_text() == "replace-link"
    assert foreign.read_text() == "foreign-original"
    assert (workspace / "dangling-source-link").is_symlink()
