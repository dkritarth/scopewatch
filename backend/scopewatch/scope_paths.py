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
- :func:`volume_is_case_insensitive` — empirical, three-state volume probe
  (insensitive / sensitive / unknown) that decides whether the comparisons
  fold case, cached per root and device.
"""

from __future__ import annotations

import os
import tempfile
import unicodedata
from typing import Iterable, Optional

# Denial codes, as plain strings (the gateway maps them to ReasonCode; the
# runner maps them to HTTP statuses). Ordered exactly as
# ``scopewatch.path_access.scoped_path_reason`` interprets them.
BLOCKED_PATH = "BLOCKED_PATH"
PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"

# Confirmed volume answers only; never holds an unknown (``None``) result.
# Keyed by (resolved root, device) so a different filesystem at the same path
# is re-probed. See :func:`volume_is_case_insensitive`.
_CASE_PROBE_CACHE: dict[tuple[str, int], bool] = {}


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
    # NFC before casefold: APFS (and some other default-macOS volumes) treat
    # the composed and decomposed spellings of one name as the same file, so
    # ``"e" + U+0301`` and ``"é"`` must fold together (#305).
    return tuple(
        unicodedata.normalize("NFC", part).casefold() for part in segments
    )


def volume_is_case_insensitive(root: str) -> Optional[bool]:
    """Probe whether ``root`` sits on a case-insensitive volume (#305).

    Three states, because "the probe failed" is not the same fact as
    "the volume folds case":

    - ``True`` — upper- and lower-case spellings of a name reach the same
      file (macOS default, Windows);
    - ``False`` — they are genuinely distinct (typical Linux, read-only
      case-sensitive mounts);
    - ``None`` — the volume could not be determined (read-only or full
      mount, missing root, permissions). Callers fail closed on ``None``:
      the blocked comparison folds, the allowed comparison does not.

    The probe is empirical, not a platform guess, so a case-sensitive volume
    on macOS or a case-insensitive one mounted on Linux is answered
    correctly. It is stdlib-only because the sidecar calls it too.

    Side effect: the probe creates a short-lived ``.scopewatch-case-*`` file
    inside ``root`` and deletes it. That is why confirmed answers are cached
    per ``(resolved root, os.stat(root).st_dev)``: repeated policy and
    dispatch checks on the same volume do not keep writing to the agent's
    workspace. Unknown (``None``) answers are never cached, so a volume that
    later becomes writable is re-probed.
    """
    try:
        resolved_root = os.path.realpath(root)
        device = os.stat(resolved_root).st_dev
    except OSError:
        return None
    key = (resolved_root, device)
    cached = _CASE_PROBE_CACHE.get(key)
    if cached is not None:
        return cached
    verdict = _probe_case_insensitivity(resolved_root)
    if verdict is not None:
        _CASE_PROBE_CACHE[key] = verdict
    return verdict


def _probe_case_insensitivity(root: str) -> Optional[bool]:
    """Create one file, ask for another spelling of it, delete it.

    Returns ``True`` (same file, so the volume folds case), ``False``
    (distinct, so it does not), or ``None`` when the probe cannot run or
    cannot produce two distinguishable spellings. The caller decides what
    ``None`` means; here it means "no evidence".
    """
    probe: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".scopewatch-case-", dir=root, delete=False
        ) as handle:
            probe = handle.name
        name = os.path.basename(probe)
        swapped = name.upper()
        if swapped == name:
            swapped = name.lower()
        if swapped == name:
            return None
        return os.path.exists(os.path.join(os.path.dirname(probe), swapped))
    except OSError:
        return None
    finally:
        if probe is not None:
            try:
                os.unlink(probe)
            except OSError:
                pass


def clear_case_probe_cache() -> None:
    """Forget every cached volume answer (tests do this between fixtures)."""
    _CASE_PROBE_CACHE.clear()


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
    matches its prefix. The default is case-sensitive. Callers that hold a
    three-state volume verdict use :func:`scoped_path_denial`, which applies
    the blocked and allowed sides differently.
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
    case_insensitive: Optional[bool] = None,
) -> Optional[tuple[str, Optional[str]]]:
    """Check both names of one target against the task scope.

    ``requested`` is the normalized alias the action asked for; ``resolved``
    is where it actually points, relative to the workspace root. Returns
    ``(BLOCKED_PATH, matched_prefix)`` or ``(PATH_NOT_ALLOWED, None)``, or
    ``None`` when the target is inside scope.

    Order matters and is deliberate: a blocked target is refused even when the
    requested name is allowed, and a path outside the allowed set is refused
    even when nothing blocked matched.

    ``case_insensitive`` is the volume's three-state verdict from
    :func:`volume_is_case_insensitive` (#305), and the two checks interpret
    it differently on purpose:

    - the *blocked* check folds case when the volume folds case (``True``)
      **or when the volume is unknown** (``None``), because a failed probe
      must never let an alias of a blocked path through;
    - the *allowed* check folds case only on ``True``, because folding an
      allowlist on a volume we could not confirm would widen the scope the
      policy authorized: ``allowed=["outputs"]`` must not accept ``OUTPUTS``
      on a case-sensitive volume.

    A caller that omits the flag gets ``None`` (unknown), which is the
    fail-closed default: no widening, and blocked matching at its strongest.
    """
    requested = tuple(requested)
    resolved = tuple(resolved)
    blocked_folds = case_insensitive is None or case_insensitive is True
    allowed_folds = case_insensitive is True
    for path in (requested, resolved):
        for blocked in blocked_paths:
            if segments_within(path, blocked, case_insensitive=blocked_folds):
                return BLOCKED_PATH, blocked
    for path in (requested, resolved):
        if not any(
            segments_within(path, allowed, case_insensitive=allowed_folds)
            for allowed in allowed_paths
        ):
            return PATH_NOT_ALLOWED, None
    return None
