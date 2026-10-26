"""Path-scope matching shared by the gateway and the runner (issue #184).

The remote executor backend dispatches to a sidecar in another container, so
the task scope has to travel with the dispatch (inside the signed payload, and
inside the action digest) and be re-applied there — against the *staged* copy
of the run's workspace, where a symlink swapped between the policy decision
and staging finally resolves. Without it, the remote path executed a symlink
pointing at a blocked file that the local and Docker backends refuse.

**Stdlib-only is a hard constraint, not a preference**, for the same reason as
``dispatch_gate.py`` and ``docker_job.py``: ``deploy/executor-runner/runner.py``
imports this module directly, and the sidecar image ships no gateway
dependencies. So this module imports nothing from ``scopewatch`` and nothing
outside the standard library; it speaks plain strings and tuples rather than
pydantic models. ``scopewatch.path_access`` maps its verdicts onto
``ReasonCode`` for the policy engine, and the runner maps the same verdicts
onto its own HTTP statuses. One authored copy, two renderings — the #104
lesson.

What lives here
---------------

- :func:`normalize_relative_segments` — one normalization for every caller.
- :func:`segments_within` — "is this path that prefix or below it", where the
  prefix ``"."`` matches everything.
- :func:`scoped_path_denial` — both names, both orders: an allowed alias can
  never authorize a blocked target, and neither the requested name nor the
  resolved target may sit outside the allowed set.
"""

from __future__ import annotations

import os
import tempfile
from typing import Iterable, Optional

# Denial codes, as plain strings (the gateway maps them to ReasonCode; the
# runner maps them to HTTP statuses). Ordered exactly as
# ``scopewatch.path_access.scoped_path_reason`` interprets them.
BLOCKED_PATH = "BLOCKED_PATH"
PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"


def _fold(segments: tuple[str, ...], case_insensitive: bool) -> tuple[str, ...]:
    """Case-fold segments for comparison only when the volume folds case.

    Folding is applied per-comparison, never stored: it changes how two paths
    are compared, not the path that gets executed. ``str.casefold`` is used
    rather than ``os.path.normcase`` because ``normcase`` is a no-op on POSIX
    even when the *volume* (not the OS) is case-insensitive — and a
    case-insensitive volume mounted on Linux is exactly the case #305 must
    still catch. The caller decides *whether* to fold from the real volume
    probe; this function only applies that decision.
    """
    if not case_insensitive:
        return segments
    return tuple(part.casefold() for part in segments)


def volume_is_case_insensitive(root: str) -> bool:
    """Probe whether ``root`` sits on a case-insensitive volume (#305).

    Returns ``True`` when a path's upper- and lower-case spellings name the
    same file on this volume (macOS default, Windows), ``False`` on a
    case-sensitive volume (typical Linux). The probe is empirical, not a
    platform guess: a case-sensitive volume on macOS, or a case-insensitive
    one mounted on Linux, is answered correctly. Any probe error fails
    closed to ``True`` so an undetectable volume is treated as the
    bypass-prone case. Stdlib-only, so the sidecar can call it too.
    """
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".scopewatch-case-", dir=root, delete=False
        ) as handle:
            probe = handle.name
        try:
            swapped = os.path.basename(probe).upper()
            if swapped == os.path.basename(probe):
                swapped = os.path.basename(probe).lower()
            return os.path.exists(os.path.join(os.path.dirname(probe), swapped))
        finally:
            os.unlink(probe)
    except OSError:
        return True


def normalize_relative_segments(path_str: str) -> tuple[str, ...]:
    """Return the normalized segments of a relative path.

    ``"."`` components and empty components are dropped, so ``"./a//b"`` and
    ``"a/b"`` normalize identically. Everything else — including ``".."`` and
    a leading ``"/"`` — is preserved, because refusing those is the caller's
    job (the policy engine, the runner's own lexical checks) and silently
    collapsing them here would hide a traversal from both.
    """
    raw = path_str.strip().replace("\\", "/")
    return tuple(part for part in raw.split("/") if part and part != ".")


def segments_within(
    path: Iterable[str], prefix: str, *, case_insensitive: bool = False
) -> bool:
    """True when ``path`` is ``prefix`` or a descendant of it.

    A prefix of ``"."`` (or anything that normalizes to no segments) matches
    every path, which is what the local backend's ``require_allowed=False``
    fallback list is.

    With ``case_insensitive=True`` the comparison folds case (#305), so an
    upper-case alias of a blocked path on a case-insensitive volume still
    matches its prefix. The default is case-sensitive, preserving Linux
    semantics where ``PRIVATE`` and ``private`` are genuinely distinct.
    """
    prefix_segments = normalize_relative_segments(prefix)
    path = tuple(path)
    if not prefix_segments:
        return True
    head = _fold(path[: len(prefix_segments)], case_insensitive)
    return head == _fold(prefix_segments, case_insensitive)


def scoped_path_denial(
    requested: Iterable[str],
    resolved: Iterable[str],
    allowed_paths: Iterable[str],
    blocked_paths: Iterable[str],
    *,
    case_insensitive: bool = False,
) -> Optional[tuple[str, Optional[str]]]:
    """Check both names of one target against the task scope.

    ``requested`` is the normalized alias the action asked for; ``resolved``
    is where it actually points, relative to the workspace root. Returns
    ``(BLOCKED_PATH, matched_prefix)`` or ``(PATH_NOT_ALLOWED, None)``, or
    ``None`` when the target is inside scope.

    Order matters and is deliberate: a blocked target is refused even when the
    requested name is allowed, and a path outside the allowed set is refused
    even when nothing blocked matched.

    ``case_insensitive`` folds case for every comparison (#305). The caller
    sets it from the workspace volume's real behaviour, so a blocked prefix
    matches an upper-case alias on a case-insensitive volume while an allowed
    alias on such a volume is still recognised as allowed. Both checks use the
    same flag, so blocked-matching never tightens without allowed-matching
    loosening in step — a case-variant target is blocked if blocked, and
    in-scope if merely allowed.
    """
    requested = tuple(requested)
    resolved = tuple(resolved)
    for path in (requested, resolved):
        for blocked in blocked_paths:
            if segments_within(path, blocked, case_insensitive=case_insensitive):
                return BLOCKED_PATH, blocked
    for path in (requested, resolved):
        if not any(
            segments_within(path, allowed, case_insensitive=case_insensitive)
            for allowed in allowed_paths
        ):
            return PATH_NOT_ALLOWED, None
    return None
