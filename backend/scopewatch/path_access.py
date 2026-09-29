"""Shared scope checks for the requested path and its resolved target."""

from pathlib import Path
from typing import Iterable

from scopewatch.models import ReasonCode


def normalize_relative_path(path_str: str) -> Path:
    raw = path_str.strip().replace("\\", "/")
    parts = [part for part in raw.split("/") if part and part != "."]
    return Path(*parts) if parts else Path(".")


def is_descendant_or_equal(child: Path, parent: Path) -> bool:
    if parent == Path("."):
        return True
    return child.parts[: len(parent.parts)] == parent.parts


def matching_path(path: Path, prefixes: Iterable[str]) -> str | None:
    for prefix in prefixes:
        if is_descendant_or_equal(path, normalize_relative_path(prefix)):
            return prefix
    return None


def scoped_path_reason(
    requested: Path,
    resolved: Path,
    allowed_paths: Iterable[str],
    blocked_paths: Iterable[str],
) -> tuple[ReasonCode, str | None] | None:
    """Check both names; an allowed alias cannot authorize a blocked target."""
    for path in (requested, resolved):
        blocked = matching_path(path, blocked_paths)
        if blocked is not None:
            return ReasonCode.BLOCKED_PATH, blocked
    if (
        matching_path(requested, allowed_paths) is None
        or matching_path(resolved, allowed_paths) is None
    ):
        return ReasonCode.PATH_NOT_ALLOWED, None
    return None
