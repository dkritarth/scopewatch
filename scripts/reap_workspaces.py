#!/usr/bin/env python3
"""Report or explicitly reap old terminal run workspaces. Defaults to dry-run."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from scopewatch.config import DB_PATH, WORKSPACE_ROOT
from scopewatch.retention import DEFAULT_RETENTION_DAYS, reap_workspaces
from scopewatch.workspaces import RunWorkspaceError, RunWorkspaceManager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--workspace-root", type=Path, default=WORKSPACE_ROOT,
                        help="Scenario fixture root, as configured for the gateway.")
    parser.add_argument("--run-workspaces-root", type=Path, default=None,
                        help="Managed root; defaults to gateway environment/default resolution.")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help="Positive whole days retained after both creation and terminal update (default: 7).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Delete eligible workspaces and append evidence.")
    mode.add_argument("--dry-run", action="store_true", help="Report only (the default).")
    args = parser.parse_args()
    try:
        report = reap_workspaces(
            args.db_path, RunWorkspaceManager(args.workspace_root, args.run_workspaces_root),
            retention_days=args.retention_days, apply=args.apply,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error, RunWorkspaceError):
        # No exception bodies: filesystem/SQLite diagnostics can contain host paths.
        print(json.dumps({"error": "Retention could not complete; check database, roots, age and permissions. "
                                   "Inspect attempt/result evidence before retrying apply."}), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
