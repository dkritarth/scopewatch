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

Resolution of the managed root, in order: an explicit argument, the
``SCOPEWATCH_RUN_WORKSPACES_DIR`` environment variable, then a ``-runs``
sibling of the fixture root (``demo/workspace`` -> ``demo/workspace-runs``).
The sibling default keeps each test or scratch deployment self-contained; the
environment override lets operators move run state onto runtime storage.
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

# Run ids are UUIDs, but the manager never trusts that: a stored row is data,
# and data does not get to choose an arbitrary directory name.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

MARKER_VERSION = 1


class RunWorkspaceError(Exception):
    """Raised when a run workspace cannot be created or resolved.

    Callers fail closed: a run whose workspace cannot be resolved never
    executes anything.
    """


def default_run_workspaces_root(source_root: Path | str) -> Path:
    """Managed root for per-run workspaces: env override, else fixture sibling."""
    override = os.environ.get(RUN_WORKSPACES_ENV_VAR, "").strip()
    if override:
        return Path(override)
    source = Path(source_root)
    return source.parent / f"{source.name}-runs"


def workspace_segment(run_id: str) -> str:
    """Return ``run_id`` as a single safe path segment, or raise."""
    if not isinstance(run_id, str) or not _SAFE_SEGMENT.match(run_id):
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

        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunWorkspaceError("Run workspace root is unavailable.") from exc

        staging = self.root / f".init-{segment}-{uuid.uuid4().hex[:12]}"
        try:
            shutil.copytree(self.source_root, staging, symlinks=True)
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
        of which workspace this run owns. It must resolve inside the managed
        root: a stored row must not be able to point execution at an
        arbitrary directory on the host. When the row carries no path (a run
        created before per-run workspaces existed) the workspace is
        initialized lazily and returned for the caller to persist.
        """
        if stored_path:
            candidate = Path(stored_path)
            try:
                resolved = candidate.resolve()
                managed = self.root.resolve()
            except OSError as exc:
                raise RunWorkspaceError(
                    "Stored run workspace could not be resolved."
                ) from exc
            try:
                resolved.relative_to(managed)
            except ValueError as exc:
                raise RunWorkspaceError(
                    "Stored run workspace is outside the managed run workspace root."
                ) from exc
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