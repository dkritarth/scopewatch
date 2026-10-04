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

Two properties this module guarantees to its callers rather than inheriting
from the environment (#172, #173), both found by the second-pass review of
#117:

- **A symlink loop fails closed as a documented error.** ``Path.resolve()``
  raises ``RuntimeError`` for ELOOP and ``OSError`` for everything else, so
  ``resolve`` catches both and every other resolution failure arrives as a
  ``RunWorkspaceError`` — a 503 on the service path, the runner's structured
  refusal on the sidecar. Nothing here can escape as an unhandled 500 or a
  dropped connection.
- **The run workspace is writable because it is a run workspace.** ``copytree``
  copies the source's permission bits onto every directory it copies, so the
  copy used to inherit the fixture's mode. A run workspace's mode is now set
  deliberately (see ``RUN_WORKSPACE_DIR_MODE`` /
  ``RUN_WORKSPACE_FILE_MODE``), which is what lets the deployment treat the
  scenario fixture as genuinely read-only.
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

# --- Run-workspace permissions (#173) -------------------------------------
#
# A run workspace is writable state: the run writes outputs into it, creates
# and traverses subdirectories in it, and rewrites baseline files it copied.
# Those needs are a property of the ROLE the directory plays, so the mode is
# set here rather than inherited from whatever the fixture happened to carry.
#
# ``copytree`` ends each directory with ``copystat``, so a read-only fixture
# hands the copy a read-only mode and every ``create_run`` then fails — at the
# marker write, inside a directory nobody can write to. The fix therefore has
# to cover every copied directory, not just the top one.
#
# Directories: a fixed ``0o700``. A run must create, list and traverse
# everything under its own workspace with no exceptions, so nothing about the
# baseline should be able to change that.
#
# Files: owner read and write are guaranteed and every other bit is kept.
# Files differ from directories because an execute bit on a copied file is
# load-bearing: ``stage_workspace_copy`` opens the *staging* copy to world
# access with ``S_IRWXO`` on directories and ``S_IROTH|S_IWOTH`` on files, so a
# fixture-shipped script at ``0o700`` reaches the container still executable
# (``0o706``) while a forced ``0o600`` would arrive as ``0o606`` and stop being
# runnable. Widening fixes the read-only case without taking away a capability
# ``run_command`` may legitimately rely on.
#
# Nothing here widens: every operator bit is already guaranteed, so the only
# bits added are ones the fixture denied. The one principal that reads these
# trees is the gateway uid — the sidecar declares no ``USER`` and runs as root,
# which mode bits do not constrain, and the container never sees this tree at
# all, only the staging copy.
RUN_WORKSPACE_DIR_MODE = 0o700
RUN_WORKSPACE_FILE_MODE = 0o600

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

    def _apply_workspace_modes(self, staging: Path) -> None:
        """Give the copied tree its own permissions instead of the fixture's (#173).

        ``copytree`` finishes every directory it copies with ``copystat``, so a
        read-only fixture produces a read-only copy and the marker write below
        fails on a directory the gateway owns but cannot write to. That surfaced
        as every ``create_run`` returning 503 "could not initialize an isolated
        workspace for this run", which blames the run rather than the source
        tree's mode.

        Runs before the marker write, because that write is the first thing
        that needs the directory to be writable.

        Symlinks are skipped, as in ``docker_job.make_world_accessible``: chmod
        follows a link on Linux, so touching one would change the mode of a
        host target the fixture merely points at. ``followlinks=False`` keeps a
        symlinked directory out of the descent for the same reason.

        A failure here is an ``OSError`` and is handled by the caller's
        existing cleanup, exactly as a copy failure is: a workspace whose mode
        cannot be set is not one this gateway should execute against.
        """
        os.chmod(staging, RUN_WORKSPACE_DIR_MODE)
        for dirpath, dirnames, filenames in os.walk(staging, followlinks=False):
            for name in dirnames:
                entry = Path(dirpath) / name
                if entry.is_symlink():
                    continue
                os.chmod(entry, RUN_WORKSPACE_DIR_MODE)
            for name in filenames:
                entry = Path(dirpath) / name
                if entry.is_symlink():
                    continue
                # Keep whatever the copy chose beyond the owner bits -- in
                # particular an execute bit, which `run_command` may need --
                # and only guarantee owner read/write on top of it.
                mode = os.stat(entry).st_mode
                os.chmod(entry, RUN_WORKSPACE_FILE_MODE | (mode & 0o111))

    # ---------------- Initialization ----------------

    def initialize(self, run_id: str) -> Path:
        """Copy the scenario fixture into the run's workspace; idempotent.

        Returns the workspace path. An already-initialized workspace (marker
        present) is returned untouched so prior run output survives; a missing
        one is built by copying into a temporary sibling and renaming it into
        place, so a partially copied workspace is never observable.

        The copy's permissions are set to the run workspace's own modes rather
        than the fixture's (#173), so a read-only baseline still yields a
        workspace this gateway can write into.
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
            self._apply_workspace_modes(staging)
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

        A symlink loop on either path fails closed as a
        :class:`RunWorkspaceError`, not as a ``RuntimeError``: ``resolve()``
        raises ``RuntimeError`` for ELOOP and ``OSError`` for everything else
        it can hit, so both are caught here (#172). This matches the four
        ``resolve()`` guards in ``policy.py``, which already handle both; only
        ``except OSError`` left a loop escaping as an unhandled 500.
        """
        if stored_path:
            candidate = Path(stored_path)
            segment = workspace_segment(run_id)
            try:
                managed = self.root.resolve()
                resolved = candidate.resolve()
            except (OSError, RuntimeError) as exc:
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