"""Per-run workspace identity (issue #117).

Every run owns a workspace directory that is copied once from the read-only
synthetic scenario fixture. The run's workspace path is persisted on the run
row, and submit, approve, and execute resolve that path through the stored run
rather than the service-wide fixture root.

Consequences that matter for the domain invariants:

- Two runs never share a writable tree, so a run cannot read or overwrite
  another run's generated files.
- Container copy-back lands in the run's own workspace, never in the shared
  fixture, so the scenario baseline stays pristine across runs.
- The identity survives a service restart because it is stored, not derived
  from process state: an approved HOLD still finds its run's workspace after
  the process that created it is gone.

Managed-root resolution, in order: an explicit argument, the
``SCOPEWATCH_RUN_WORKSPACES_DIR`` environment variable, then
``<fixture>/.runs``.

The default lives INSIDE the fixture root on purpose. An earlier revision
defaulted to a ``-runs`` *sibling* of the fixture, which is a path no
deployment provides: the gateway container runs ``read_only: true`` with
volumes for ``/data``, ``/workspace`` and ``/runs`` only, so a sibling of
``/workspace`` can neither exist nor be created, seeding died on first start,
and every ``create_run`` then failed. A path *under* the fixture root is inside
a mount the deployment always provides, so the default works with no
configuration at all.

Two consequences of nesting, both handled here rather than left to operators:

- The managed root is excluded from every fixture copy (``initialize``), so a
  run never absorbs a previous run's workspace. Without this the nesting
  would silently break the isolation this module exists to provide.
- The fixture's own scenario content is still never written; ``.runs`` is a
  sibling of the fixture *content*, not a copy-back destination.

The shipped deployment overrides the default with ``/runs``, a dedicated named
volume. Two reasons beyond writability: the fixture volume is the copy SOURCE
(run workspaces nested in it would be copied into every later run), and the
executor-runner sidecar is a separate container that can only reach directories
it mounts itself. See ``deploy/RUNBOOK.md`` §3 and
``backend/tests/test_run_workspaces_deployment.py``, which asserts both the
default and the shipped layout against a simulated read-only container root.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import uuid
from typing import Any, Optional

# Marker file written into every initialized run workspace. Its presence is
# what makes initialization idempotent: a restart must not re-copy the fixture
# over a run that has already produced output.
WORKSPACE_MARKER_NAME = ".scopewatch-run-workspace.json"

RUN_WORKSPACES_ENV_VAR = "SCOPEWATCH_RUN_WORKSPACES_DIR"

# Error code for "the managed root itself is unusable". Distinct from
# RUN_WORKSPACE_UNAVAILABLE ("this one run's workspace is gone") because the
# two need different fixes: a mount, or a lost directory.
RUN_WORKSPACES_ROOT_UNAVAILABLE = "RUN_WORKSPACES_ROOT_UNAVAILABLE"

# Directory name of the managed root when it is nested inside the fixture
# root (the default). See the module docstring for why it nests.
RUN_WORKSPACES_DIRNAME = ".runs"

# Run ids are UUIDs, but the manager never trusts that: a stored row is data,
# and data does not get to choose an arbitrary directory name. ``fullmatch``
# rather than ``match``: ``$`` also matches just before a trailing newline,
# which would let ``"run\\n"`` name a directory.
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

MARKER_VERSION = 1


class RunWorkspaceError(Exception):
    """Raised when a run workspace cannot be created or resolved.

    Callers fail closed: a run whose workspace cannot be resolved never
    executes anything.

    ``code`` distinguishes a misconfigured deployment from a run whose
    workspace disappeared. A missing or unwritable *root* is an operator
    problem that every subsequent run will hit, and reporting it with its own
    code is what stops a bad mount from turning into an undifferentiated
    stream of per-run failures.
    """

    def __init__(self, message: str, code: str = "RUN_WORKSPACE_UNAVAILABLE") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class RunWorkspaceRootError(RunWorkspaceError):
    """The managed root itself is missing or not writable (configuration)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=RUN_WORKSPACES_ROOT_UNAVAILABLE)


def default_run_workspaces_root(source_root: Path | str) -> Path:
    """Managed root for per-run workspaces: env override, else ``<fixture>/.runs``.

    The default is nested inside the fixture root because that is the one
    directory a deployment is guaranteed to have writable. See the module
    docstring for the deployment failure this replaces.
    """
    override = os.environ.get(RUN_WORKSPACES_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return Path(source_root) / RUN_WORKSPACES_DIRNAME


def workspace_segment(run_id: str) -> str:
    """Return ``run_id`` as a single safe path segment, or raise."""
    if not isinstance(run_id, str) or not _SAFE_SEGMENT.fullmatch(run_id):
        raise RunWorkspaceError("Run identifier is not usable as a workspace name.")
    return run_id


class RunWorkspaceManager:
    """Owns the lifecycle of per-run workspace directories.

    ``source_root`` is the synthetic scenario fixture: the read-only baseline
    every run starts from. It is never a copy-back destination.
    """

    def __init__(
        self,
        source_root: Path | str,
        root: Path | str | None = None,
    ) -> None:
        self.source_root = Path(source_root)
        self.root = Path(root) if root is not None else default_run_workspaces_root(
            self.source_root
        )

    # ---------------- Identity ----------------

    def path_for(self, run_id: str) -> Path:
        """Absolute path of the run's workspace, whether or not it exists."""
        return self.root / workspace_segment(run_id)

    def workspace_key(self, run_id: str) -> str:
        """The directory name, which is also the identity sent to executors."""
        return workspace_segment(run_id)

    # ---------------- Managed root ----------------

    def ensure_root(self) -> Path:
        """Create the managed root and prove it is writable.

        Called at startup so a deployment whose run-workspace path is not
        mounted fails fast and loudly, instead of answering every
        ``create_run`` with the same 503 and leaving the operator to guess.
        A read-only or missing root raises :class:`RunWorkspaceRootError`,
        whose code names it as configuration rather than a lost workspace.
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / f".writable-{uuid.uuid4().hex[:12]}"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise RunWorkspaceRootError(
                "Run workspace root is unavailable or not writable."
            ) from exc
        return self.root

    def _nested_in_source(self) -> bool:
        """True when the managed root lives inside the fixture it copies."""
        try:
            self.root.resolve().relative_to(self.source_root.resolve())
        except (OSError, ValueError):
            return False
        return True

    def _copy_ignore(self, src_dir: str, names: list[str]) -> set[str]:
        """``copytree`` ignore hook: drop ONLY the top-level managed root.

        ``shutil.ignore_patterns(name)`` would match that name at every depth,
        so a scenario that legitimately contains a nested directory with the
        same name would be silently dropped from every run's copy. This hook
        compares the directory copytree is currently walking, so exactly one
        entry — the managed root, and only when it is nested in the fixture —
        is excluded.
        """
        if Path(src_dir) != self.source_root or not self._nested_in_source():
            return set()
        return {self.root.name} if self.root.name in names else set()

    # ---------------- Initialization ----------------

    def initialize(self, run_id: str) -> Path:
        """Copy the scenario fixture into the run's workspace; idempotent.

        Returns the workspace path. An already-initialized workspace (marker
        present) is returned untouched so prior run output survives; a missing
        one is built by copying into a temporary sibling and renaming it into
        place, so a partially copied workspace is never observable.
        """
        segment = workspace_segment(run_id)
        target = self.root / segment
        if self.read_marker(target) is not None:
            return target
        if not self.source_root.is_dir():
            raise RunWorkspaceError("Scenario workspace fixture is unavailable.")

        self.ensure_root()

        staging = self.root / f".init-{segment}-{uuid.uuid4().hex[:12]}"
        # The managed root can nest inside the fixture (the default), and the
        # fixture is the copy source. Excluding just that one entry keeps a
        # run's workspace free of every other run's output, which is the whole
        # point of the copy — without it the nesting would silently reopen the
        # isolation gap this module exists to close.
        try:
            shutil.copytree(
                self.source_root, staging, symlinks=True, ignore=self._copy_ignore
            )
            marker = {
                "version": MARKER_VERSION,
                "run_id": segment,
                "source": str(self.source_root),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            (staging / WORKSPACE_MARKER_NAME).write_text(
                json.dumps(marker, sort_keys=True), encoding="utf-8"
            )
            os.replace(staging, target)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise RunWorkspaceError("Run workspace could not be initialized.") from exc
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return target

    # ---------------- Resolution ----------------

    def resolve(self, run_id: str, stored_path: Optional[str] = None) -> Path:
        """Resolve the workspace identity for a stored run.

        ``stored_path`` wins when present, because the run row is the record
        of which workspace this run owns. It must resolve to exactly this
        run's directory: inside the managed root, not the root itself, and
        with this run's own ``workspace_segment`` as its final segment. The
        name is allowlisted, but that alone is not enough — a row could
        still point at a sibling run's directory, at the managed root, or at a
        symlink inside the root that leads to either. Checking the resolved
        real path is what makes the containment promise true rather than
        merely likely, and it agrees with the executor-runner sidecar, which
        refuses the same shapes for the same property.

        When the row carries no path (a run created before per-run
        workspaces existed) the workspace is initialized lazily and returned
        for the caller to persist.
        """
        if stored_path:
            candidate = Path(stored_path)
            segment = workspace_segment(run_id)
            try:
                managed = self.root.resolve()
                resolved = candidate.resolve()
            except OSError as exc:
                raise RunWorkspaceError(
                    "Stored run workspace could not be resolved."
                ) from exc
            # One comparison carries all three parts of the property this module
            # docstring promises, because `managed` is already real and
            # `segment` is an allowlisted single name:
            #   * `expected` is strictly inside `managed` and never equal to
            #     it, so the managed root itself and anything outside it are out;
            #   * `expected`'s final segment IS this run's segment, so a sibling
            #     run's directory is out;
            #   * `resolved` follows symlinks while `expected` deliberately
            #     does not, so `root/<run>` being a link onto a sibling resolves
            #     to that sibling and no longer equals `expected` — out.
            #
            # `expected` is NOT resolved, and that asymmetry is load-bearing. If
            # it were resolved, a link onto a sibling would satisfy both sides
            # and be accepted; leaving it unresolved makes the symlink case fall
            # out of this same comparison. The executor-runner sidecar refuses
            # the same shapes for a different reason: it knows no run id, so it
            # has containment alone and must test `is_symlink()` separately.
            expected = managed / segment
            if resolved != expected:
                raise RunWorkspaceError(
                    "Stored run workspace is not this run's workspace."
                )
            if not resolved.is_dir():
                raise RunWorkspaceError("Stored run workspace is missing on disk.")
            return resolved
        return self.initialize(run_id)

    def read_marker(self, workspace: Path) -> Optional[dict[str, Any]]:
        """Return the run-workspace marker, or None when the path is not one."""
        marker = Path(workspace) / WORKSPACE_MARKER_NAME
        try:
            raw = marker.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None