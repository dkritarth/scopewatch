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

from typing import Iterable, Optional

# Denial codes, as plain strings (the gateway maps them to ReasonCode; the
# runner maps them to HTTP statuses). Ordered exactly as
# ``scopewatch.path_access.scoped_path_reason`` interprets them.
BLOCKED_PATH = "BLOCKED_PATH"
PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"


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


def segments_within(path: Iterable[str], prefix: str) -> bool:
    """True when ``path`` is ``prefix`` or a descendant of it.

    A prefix of ``"."`` (or anything that normalizes to no segments) matches
    every path, which is what the local backend's ``require_allowed=False``
    fallback list is.
    """
    prefix_segments = normalize_relative_segments(prefix)
    path = tuple(path)
    if not prefix_segments:
        return True
    return path[: len(prefix_segments)] == prefix_segments


def scoped_path_denial(
    requested: Iterable[str],
    resolved: Iterable[str],
    allowed_paths: Iterable[str],
    blocked_paths: Iterable[str],
) -> Optional[tuple[str, Optional[str]]]:
    """Check both names of one target against the task scope.

    ``requested`` is the normalized alias the action asked for; ``resolved``
    is where it actually points, relative to the workspace root. Returns
    ``(BLOCKED_PATH, matched_prefix)`` or ``(PATH_NOT_ALLOWED, None)``, or
    ``None`` when the target is inside scope.

    Order matters and is deliberate: a blocked target is refused even when the
    requested name is allowed, and a path outside the allowed set is refused
    even when nothing blocked matched.
    """
    requested = tuple(requested)
    resolved = tuple(resolved)
    for path in (requested, resolved):
        for blocked in blocked_paths:
            if segments_within(path, blocked):
                return BLOCKED_PATH, blocked
    for path in (requested, resolved):
        if not any(segments_within(path, allowed) for allowed in allowed_paths):
            return PATH_NOT_ALLOWED, None
    return None
