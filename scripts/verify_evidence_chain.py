#!/usr/bin/env python3
"""Offline verifier for Scopewatch tamper-evident evidence chains.

Reads a JSONL chain exported by ``EvidenceChain.export_jsonl`` (one canonical
record per line) and verifies hash linkage, sequence continuity, and
DECISION-before-EXECUTION ordering.

Usage:
    PYTHONPATH=backend python3 scripts/verify_evidence_chain.py chain.jsonl
    PYTHONPATH=backend python3 scripts/verify_evidence_chain.py chain.jsonl --expected-head <hex>
    PYTHONPATH=backend python3 scripts/verify_evidence_chain.py chain.jsonl --expected-length 128 --run-id <run>

Exit codes:
    0  chain verifies (OK)
    1  chain fails verification (first failure printed as JSON)
    2  usage / I/O error (missing file, malformed JSONL)

This script is intentionally dependency-free (stdlib + the
``backend/scopewatch/evidence_chain.py`` module) so operators can run it
offline on exported evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a Scopewatch evidence hash chain (JSONL)."
    )
    parser.add_argument("chain", help="Path to JSONL chain file.")
    parser.add_argument("--expected-head", default=None, help="Pinned head hash for truncation detection.")
    parser.add_argument("--expected-length", type=int, default=None, help="Pinned record count for truncation detection.")
    parser.add_argument("--run-id", default=None, help="Expected run_id (default: inferred from chain).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    try:
        from scopewatch.evidence_chain import EvidenceChain
    except ImportError as exc:
        print(json.dumps({"ok": False, "code": "IMPORT_ERROR", "reason": str(exc)}))
        return 2

    path = Path(args.chain)
    if not path.is_file():
        print(json.dumps({"ok": False, "code": "FILE_NOT_FOUND", "reason": f"not a file: {path}"}))
        return 2

    try:
        chain = EvidenceChain.from_jsonl(path, run_id=args.run_id)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "code": "MALFORMED", "reason": f"malformed JSONL: {exc}"}))
        return 2

    result = chain.verify(
        expected_head=args.expected_head,
        expected_length=args.expected_length,
        expected_run_id=args.run_id,
    )
    output = {
        "ok": result.ok,
        "code": result.code,
        "reason": result.reason,
        "first_bad_seq": result.first_bad_seq,
        "head_hash": result.head_hash,
        "checked": result.checked,
        "run_id": chain.run_id,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
