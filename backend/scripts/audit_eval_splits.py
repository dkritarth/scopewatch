"""Deterministic cross-split leakage audit; emits identifiers/counts, never traces."""

from __future__ import annotations

import argparse
import ast
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sys
from typing import Any

from scripts.evaluate_reasoning_audit import compute_file_hash

MOCK_SOURCE = Path(__file__).resolve().parents[1] / "scopewatch/reasoning_audit.py"


def mock_phrases(source: Path = MOCK_SOURCE) -> list[str]:
    """Read actual mock rule lists through AST, without duplicating the rules."""
    tree = ast.parse(source.read_text())
    provider = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MockAuditorProvider")
    phrases = []
    for node in provider.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id.endswith(("_PATTERNS", "_INDICATORS")):
                phrases.extend(ast.literal_eval(node.value))
    if not phrases:
        raise ValueError("no mock rules found")
    return sorted(set(phrases))


def normalized(text: str) -> str:
    # Normalize case, punctuation, paths and numeric substitutions. This catches
    # simple fixture variants; it cannot establish semantic independence.
    return " ".join(re.findall(r"[a-z]+", re.sub(r"\S*[/\\]\S*|\d+", " SLOT ", text.lower())))


def audit_splits(dev: list[dict[str, Any]], heldout: list[dict[str, Any]],
                 *, threshold: float = 0.65, phrase_words: int = 6) -> dict[str, Any]:
    if not 0 < threshold <= 1 or phrase_words < 3:
        raise ValueError("invalid audit thresholds")
    ids = [c["case_id"] for c in dev + heldout]
    duplicates = sorted({cid for cid in ids if ids.count(cid) > 1})
    pairs = []
    for a in dev:
        left = normalized(a["reasoning_trace"])
        words = left.split()
        shingles = {tuple(words[i:i + phrase_words]) for i in range(len(words) - phrase_words + 1)}
        for b in heldout:
            right = normalized(b["reasoning_trace"])
            other_words = right.split()
            other = {tuple(other_words[i:i + phrase_words]) for i in range(len(other_words) - phrase_words + 1)}
            shared = len(shingles & other)
            similarity = SequenceMatcher(None, left, right, autojunk=False).ratio()
            same_family = bool(a.get("scenario_family") and a.get("scenario_family") == b.get("scenario_family"))
            if left == right or similarity >= threshold or shared or same_family:
                pairs.append({"dev_case_id": a["case_id"], "heldout_case_id": b["case_id"],
                              "normalized_exact": left == right, "similarity": round(similarity, 6),
                              "shared_phrase_count": shared, "same_family": same_family})
    phrases = mock_phrases()
    overlaps = []
    for split, cases in (("dev", dev), ("heldout", heldout)):
        for c in cases:
            hits = sum(phrase.lower() in c["reasoning_trace"].lower() for phrase in phrases)
            if hits:
                overlaps.append({"split": split, "case_id": c["case_id"], "matched_rule_count": hits})
    return {"schema_version": 1, "method": "normalized_sequence_and_word_shingles",
            "threshold": threshold, "phrase_words": phrase_words,
            "counts": {"dev": len(dev), "heldout": len(heldout)},
            "duplicate_case_ids": duplicates, "cross_split_pairs": pairs,
            "mock_rule_overlaps": overlaps,
            "mock_source_sha256": compute_file_hash(MOCK_SOURCE),
            "clean_under_checks": not (duplicates or pairs or overlaps)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    try:
        files = {s: args.cases_dir / s / "cases.json" for s in ("dev", "heldout")}
        result = audit_splits(*(json.loads(files[s].read_text()) for s in ("dev", "heldout")))
        result["dataset_hashes"] = {s: compute_file_hash(p) for s, p in files.items()}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    except Exception:
        print("SPLIT AUDIT FAILED: invalid input", file=sys.stderr)
        return 1
    print(f"SPLIT AUDIT: {len(result['cross_split_pairs'])} cross-split pairs; "
          f"{len(result['mock_rule_overlaps'])} cases overlap mock rules")
    return int(args.require_clean and not result["clean_under_checks"])


if __name__ == "__main__":
    sys.exit(main())
