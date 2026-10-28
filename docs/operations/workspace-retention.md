# Workspace retention

`scripts/reap_workspaces.py` reports and optionally removes old terminal run
workspaces. It makes no model calls. Nothing runs at gateway startup, on a timer,
or through a public endpoint. Existing deployment behavior stays unchanged until
an operator invokes the command.

## Policy

The default retention window is seven whole days. `--retention-days N` accepts a
positive integer. A workspace becomes eligible when both its ownership marker's
`created_at` and its run's `updated_at` are at or before the invocation's UTC
cutoff. The run must be `COMPLETED` or `FAILED`. A terminal run's last update
provides a conservative retention start even if its workspace was created long
before closure. Invalid or timezone-free timestamps prevent removal.

`PENDING` approvals block removal even if their expiry time has passed.
`APPROVED` approvals and execution starts without a matching execution terminal
event also block removal. Retention never expires or resolves approvals itself.
Active and waiting runs remain untouched regardless of age.

## Operator command

Run from the repository root with the same database, fixture root, and managed
root as the gateway. With no arguments, the command uses `SCOPEWATCH_DB_PATH`,
`SCOPEWATCH_WORKSPACE_ROOT`, and `SCOPEWATCH_RUN_WORKSPACES_DIR`, with the same
defaults as the gateway. Explicit path arguments override those settings.

```bash
python3 scripts/reap_workspaces.py --dry-run
python3 scripts/reap_workspaces.py --retention-days 14 --apply
```

Omitting both mode flags is a dry-run. JSON output includes `eligible_count`,
`eligible_bytes`, `reaped_count`, `reaped_bytes`, `failed_count`, `skipped_count`,
and per-run outcomes. Byte counts are logical regular-file lengths, including
the ownership marker. Eligible counts describe the initial scan; a later safety
check can refuse an initially eligible candidate. Byte counts estimate content
selected or removed, not measured free
disk space. Sparse files, hard links, and filesystem allocation affect space
actually reclaimed. Dry-run does not append evidence or initialize workspaces.

The gateway image includes the script. From `deploy/`, use:

```bash
docker compose exec gateway python scripts/reap_workspaces.py --dry-run
docker compose exec gateway python scripts/reap_workspaces.py --apply
```

Exit code `0` means the scan completed without removal failures. Skipped unsafe
or unavailable workspaces remain visible in the report. Exit code `1` means at
least one attempted removal failed. Exit code `2` means invalid arguments or an
operation-level error. Inspect stored attempt/result evidence before retrying
after an operation-level error, since an earlier candidate may already be gone.

## Safety and evidence

Only database-registered runs with a stored workspace identity and a matching
versioned ownership marker are candidates. Unknown directories, orphan trees,
legacy rows without workspace identities, symlinks anywhere in a candidate,
special files, and different-device subtrees are refused. A missing workspace
is never recreated. The fixture, the filesystem root, and ancestors of the
fixture are unsafe managed roots. The database must exist outside the managed
root. Retention requires a platform with descriptor-based, symlink-safe
`shutil.rmtree`; it fails closed without that support.

Inspection and removal belong to `RunWorkspaceManager`, which reuses workspace
identity resolution and anchors deletion to open directory descriptors.
Eligibility is checked under the same SQLite `BEGIN IMMEDIATE` lock as gateway
dispatch and approval handling. Each candidate's age, approvals, execution
state, and filesystem ownership are checked again immediately before removal.
The lock remains held until the result commits. Scans and large removals can
delay other gateway writes, so invoke retention during a quiet period.

`WORKSPACE_PRUNE_ATTEMPTED` commits before deletion. `WORKSPACE_PRUNED` records a
successful removal. `WORKSPACE_PRUNE_FAILED` records a failed attempt and warns
that removal may be partial. These events share an `attempt_id`, contain a
workspace key and byte count, and omit host paths and exception bodies. A crash
or database failure may leave only an attempt event, even after deletion. An
attempt alone never proves success, and the command does not invent a result
on a later run.

Run, action, policy decision, approval, receipt, and existing evidence records
remain in SQLite. The existing events API exposes retention evidence; the
offline evidence verifier accepts actionless retention events after run
closure. The dashboard labels attempts, successful pruning, and failures as
operator retention evidence, separately from agent actions and policy outcomes.
The failed filter includes retention failures; attempt and pruned filters keep
those outcomes separate. No public API or dashboard control is added.

The database lock coordinates gateway processes using the same SQLite file.
It does not coordinate manual host filesystem changes, volume remounts, or an
external tool writing directly into a workspace. Keep those operations separate
from a reap. This feature provides an age window and explicit cleanup, not an
automatic disk quota or a measurement of deployment growth rates.
