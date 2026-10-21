#!/usr/bin/env python3
"""Export the invoice demo's real gateway decisions for the project website.

Seeds scenarios 01-06 into a throwaway database and workspace with the mock
provider (no keys, no network), then dumps what the gateway actually recorded
(policy decision, reasoning audit, approval, execution receipt) per action to
site/assets/demo-data.json. The website's demo page replays that file; it
never computes a decision itself.

Usage:
  python3 scripts/export_site_demo.py
  python3 scripts/export_site_demo.py --out site/assets/demo-data.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from scopewatch.policy import evaluate_policy  # noqa: E402
from scopewatch.repository import ScopewatchRepository  # noqa: E402

MAX_TEXT = 600


def _clip(value: str | None) -> str | None:
    if value is None:
        return None
    return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…"


def _load(raw: str | None) -> object:
    return json.loads(raw) if raw else None


def export(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    scenario_files = sorted((REPO_ROOT / "demo" / "scenarios").glob("0[1-6]_*.json"))
    by_name = {}
    for path in scenario_files:
        data = json.loads(path.read_text())
        by_name[data["name"]] = (path.name, data)

    runs = []
    for run in conn.execute("SELECT * FROM runs ORDER BY created_at"):
        file_name, scenario = by_name[run["name"]]
        run_model = ScopewatchRepository.get_run(conn, run["id"])
        actions = []
        rows = conn.execute(
            "SELECT * FROM action_requests WHERE run_id = ? ORDER BY requested_at",
            (run["id"],),
        )
        for action in rows:
            decision = conn.execute(
                "SELECT * FROM policy_decisions WHERE action_request_id = ?",
                (action["id"],),
            ).fetchone()
            audit = (
                conn.execute(
                    "SELECT * FROM reasoning_audits WHERE id = ?",
                    (action["reasoning_audit_id"],),
                ).fetchone()
                if action["reasoning_audit_id"]
                else None
            )
            approval = conn.execute(
                "SELECT status FROM approval_requests WHERE action_request_id = ?",
                (action["id"],),
            ).fetchone()
            # The stored decision is the gateway's final one (after any
            # reasoning escalation). Re-run the deterministic engine alone so
            # the page can show what policy said before the auditor spoke.
            deterministic = evaluate_policy(
                ScopewatchRepository.get_action_request(conn, action["id"]),
                run_model,
                Path(run["workspace_path"]),
            )
            receipt = conn.execute(
                "SELECT * FROM execution_receipts WHERE action_request_id = ?",
                (action["id"],),
            ).fetchone()
            actions.append(
                {
                    "tool": action["tool"],
                    "operation": action["operation"],
                    "resource": action["resource"],
                    "arguments": _load(action["arguments_json"]),
                    "reasoning_summary": _clip(action["reasoning_summary"]),
                    "exposed_reasoning_trace": _clip(action["exposed_reasoning_trace"]),
                    "reasoning_provenance": action["reasoning_provenance"],
                    "audit": None
                    if audit is None
                    else {
                        "verdict": audit["verdict"],
                        "concern_type": audit["concern_type"],
                        "flagged_excerpts": _load(audit["flagged_excerpts_json"]),
                        "explanation": _clip(audit["explanation"]),
                        "model": audit["model"],
                        "profile": audit["profile"],
                        "error_code": audit["error_code"],
                    },
                    "deterministic_policy": {
                        "outcome": deterministic.outcome.value,
                        "reason_code": deterministic.reason_code,
                    },
                    "gateway_decision": {
                        "outcome": decision["outcome"],
                        "reason_code": decision["reason_code"],
                        "explanation": _clip(decision["explanation"]),
                        "matched_rule": decision["matched_rule"],
                    },
                    "approval": approval["status"] if approval else None,
                    "execution": None
                    if receipt is None
                    else {
                        "status": receipt["status"],
                        "executor": receipt["executor"],
                        "result": _load(receipt["sanitized_result_json"]),
                        "error_code": receipt["error_code"],
                    },
                }
            )
        task_scope = json.loads(run["task_scope_json"])
        task_scope.pop("created_at", None)  # keeps the export reproducible
        runs.append(
            {
                "file": file_name,
                "name": run["name"],
                "description": scenario.get("description", ""),
                "task_scope": task_scope,
                "actions": actions,
            }
        )
    return {
        "generated_by": "scripts/export_site_demo.py",
        "provider_profile": "mock",
        "note": "Recorded by the real gateway (policy engine, mock-rules auditor, "
        "synthetic executor) seeding demo/scenarios/01-06. gateway_decision is "
        "the stored final decision; deterministic_policy re-runs the policy "
        "engine alone on the same action. Synthetic data only.",
        "runs": runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, default=REPO_ROOT / "site" / "assets" / "demo-data.json"
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        workspace = tmp_path / "workspace"
        shutil.copytree(REPO_ROOT / "demo" / "workspace", workspace)
        db_path = tmp_path / "demo.db"
        subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "seed_demo.py"),
                "--db-path",
                str(db_path),
                "--workspace-root",
                str(workspace),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        data = export(db_path)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1) + "\n")
    count = sum(len(run["actions"]) for run in data["runs"])
    print(f"Wrote {args.out} ({len(data['runs'])} runs, {count} actions)")


if __name__ == "__main__":
    main()
