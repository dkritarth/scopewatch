#!/usr/bin/env python3
"""List backlog issues an agent can pick up right now.

An issue is ready when it is open, labelled `agent: ready`, not labelled
`agent: in-progress`, `agent: needs-human`, or `status: blocked`, and every
issue in its GitHub "blocked by" list is closed. Results are ordered by
milestone due date, then issue number.

Requires the GitHub CLI (`gh`) to be installed and authenticated.

Usage:
    python3 scripts/agents/next_issues.py            # ready issues
    python3 scripts/agents/next_issues.py --all      # also show blocked and claimed ones
    python3 scripts/agents/next_issues.py --milestone "M1" --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

READY = "agent: ready"
SKIP = {"agent: in-progress", "agent: needs-human", "status: blocked"}


def gh_api(path: str) -> list | dict:
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", path], capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.exit(f"gh api {path} failed: {result.stderr.strip()}")
    pages = json.loads(result.stdout)
    if pages and isinstance(pages[0], list):
        return [item for page in pages for item in page]
    return pages[0] if pages else []


def repo_name() -> str:
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit("Could not determine repository; run inside the clone or pass --repo.")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", help="owner/name (default: current clone)")
    parser.add_argument("--milestone", help="only milestones whose title contains this text")
    parser.add_argument("--all", action="store_true", help="include blocked and claimed issues")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args()

    repo = args.repo or repo_name()
    issues = gh_api(f"repos/{repo}/issues?state=open&labels={READY.replace(' ', '%20')}&per_page=100")

    rows = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        milestone = issue.get("milestone") or {}
        if args.milestone and args.milestone.lower() not in milestone.get("title", "").lower():
            continue
        labels = {label["name"] for label in issue["labels"]}
        blockers = gh_api(f"repos/{repo}/issues/{issue['number']}/dependencies/blocked_by")
        open_blockers = [b["number"] for b in blockers if b.get("state") == "open"]
        skipped = sorted(labels & SKIP)
        status = "ready"
        if open_blockers:
            status = "blocked by " + ", ".join(f"#{n}" for n in open_blockers)
        elif skipped:
            status = ", ".join(skipped)
        if status != "ready" and not args.all:
            continue
        rows.append(
            {
                "number": issue["number"],
                "title": issue["title"],
                "milestone": milestone.get("title", "(none)"),
                "due": milestone.get("due_on") or "9999",
                "second_pass": "review: second-pass" in labels,
                "status": status,
                "url": issue["html_url"],
            }
        )

    rows.sort(key=lambda r: (r["due"], r["number"]))
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No ready issues. Try --all to see what is blocked.")
        return 0
    for r in rows:
        flag = " [second-pass]" if r["second_pass"] else ""
        print(f"#{r['number']:<4} {r['milestone'][:34]:<34} {r['status']:<22} {r['title']}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
