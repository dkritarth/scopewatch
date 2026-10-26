"""Shared scope checks for the requested path and its resolved target.

The matching rules live in :mod:`scopewatch.scope_paths`, which is stdlib-only
because the ``executor-runner`` sidecar imports it too (issue #184: the remote
backend has to re-apply the scope on its own side of the wire). This module
keeps the gateway-side vocabulary — ``pathlib`` paths in, ``ReasonCode`` out —
and holds no second copy of the rules.
"""

from pathlib import Path
from typing import Iterable

from scopewatch.models import ReasonCode
from scopewatch.scope_paths import (
    BLOCKED_PATH,
    PATH_NOT_ALLOWED,
    clear_case_probe_cache,
    normalize_relative_segments as _normalize_segments,
    scoped_path_denial as _scoped_path_denial,
    segments_within as _segments_within,
    volume_is_case_insensitive,
)


def normalize_relative_path(path_str: str) -> Path:
    """Normalize a relative path (``".`` and empty components dropped)."""
    return Path(*_normalize_segments(path_str))


def is_descendant_or_equal(
    child: Path, parent: Path, *, case_insensitive: bool = False
) -> bool:
    """True when ``child`` is ``parent`` or below it.

    With ``case_insensitive=True`` the comparison folds case (and NFC) first,
    so a case-variant alias still counts as the same path (#305). Blocked
    callers pass the volume verdict's folded value; allowed callers never do.
    """
    return _segments_within(child.parts, str(parent), case_insensitive=case_insensitive)


def matching_path(path: Path, prefixes: Iterable[str]) -> str | None:
    """Return the first prefix the path falls under, else ``None``."""
    for prefix in prefixes:
        if is_descendant_or_equal(path, normalize_relative_path(prefix)):
            return prefix
    return None


def scoped_path_reason(
    requested: Path,
    resolved: Path,
    allowed_paths: Iterable[str],
    blocked_paths: Iterable[str],
    *,
    case_insensitive: bool | None = None,
) -> tuple[ReasonCode, str | None] | None:
    """Check both names; an allowed alias cannot authorize a blocked target.

    ``case_insensitive`` is the volume's three-state verdict from
    :func:`scopewatch.scope_paths.volume_is_case_insensitive` (#305):
    ``True`` (folds case), ``False`` (does not), or ``None``/omitted
    (unknown, which folds the blocked check only so a failed probe never
    widens the allowed scope).
    """
    denial = _scoped_path_denial(
        requested.parts,
        resolved.parts,
        allowed_paths,
        blocked_paths,
        case_insensitive=case_insensitive,
    )
    if denial is None:
        return None
    code, blocked = denial
    reason = ReasonCode.BLOCKED_PATH if code == BLOCKED_PATH else ReasonCode.PATH_NOT_ALLOWED
    return reason, blocked


__all__ = [
    "BLOCKED_PATH",
    "PATH_NOT_ALLOWED",
    "clear_case_probe_cache",
    "is_descendant_or_equal",
    "matching_path",
    "normalize_relative_path",
    "scoped_path_reason",
    "volume_is_case_insensitive",
]
