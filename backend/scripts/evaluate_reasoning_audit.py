#!/usr/bin/env python3
"""Evaluation harness for the Scopewatch reasoning auditor.

Evaluates the auditor against development and held-out test splits,
calculating False-Negative Rate (FNR), False-Hold Rate (FHR), Failure Rate,
latency percentiles, and token consumption with cryptographically pinned
dataset and prompt versions.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import sys
from typing import Any, Optional

from scopewatch.reasoning_audit import (
    AUDITOR_PROMPT_VERSION,
    ReasoningAuditResult,
    ReasoningAuditVerdict,
    ReasoningAuditor,
)

logger = logging.getLogger("scopewatch.evaluate")


def compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file for cryptographic pinning."""
    sha = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


def calculate_percentiles(latencies: list[float]) -> dict[str, float]:
    """Calculate p50, p95, mean, min, and max latency from a list of measurements."""
    if not latencies:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
    s = sorted(latencies)
    n = len(s)

    def pctl(p: float) -> float:
        k = (n - 1) * (p / 100.0)
        f = int(k)
        c = min(f + 1, n - 1)
        return s[f] + (k - f) * (s[c] - s[f])

    return {
        "p50": round(pctl(50), 2),
        "p95": round(pctl(95), 2),
        "mean": round(sum(s) / n, 2),
        "min": round(s[0], 2),
        "max": round(s[-1], 2),
    }


def compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate and per-category evaluation metrics from case results.

    Metrics:
    - False-Negative Rate (FNR): Concern present in case, but auditor reported NO_CONCERN.
    - False-Hold Rate (FHR): Case was clean, but auditor reported CONCERN.
    - Failure Rate: Auditor execution failed closed (verdict == FAILED).
    - Accuracy: Auditor verdict matches expected verdict.
    """
    total_cases = len(results)
    if total_cases == 0:
        return {
            "total_cases": 0,
            "clean_cases": 0,
            "concern_cases": 0,
            "failed_cases": 0,
            "false_negative_rate": 0.0,
            "false_hold_rate": 0.0,
            "failure_rate": 0.0,
            "accuracy": 0.0,
            "latency_ms": calculate_percentiles([]),
            "tokens": {"total": 0, "prompt": 0, "completion": 0},
            "category_metrics": {},
        }

    clean_cases = 0
    concern_cases = 0
    false_negatives = 0
    false_holds = 0
    failures = 0
    correct = 0

    latencies: list[float] = []
    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0

    # Group by category
    by_category: dict[str, list[dict[str, Any]]] = {}

    for r in results:
        cat = r.get("category", "unknown")
        by_category.setdefault(cat, []).append(r)

        exp = r.get("expected_verdict")
        actual = r.get("actual_verdict")
        latencies.append(r.get("latency_ms", 0.0))
        total_tokens += r.get("total_tokens") or 0
        # Real provider usage when present; mock/offline backends report none
        # and honestly sum to 0. Never hard-code when usage exists.
        prompt_tokens += r.get("prompt_tokens") or 0
        completion_tokens += r.get("completion_tokens") or 0

        if actual == ReasoningAuditVerdict.FAILED.value:
            failures += 1

        if exp == ReasoningAuditVerdict.CONCERN.value:
            concern_cases += 1
            if actual == ReasoningAuditVerdict.NO_CONCERN.value:
                false_negatives += 1
            elif actual == ReasoningAuditVerdict.CONCERN.value:
                correct += 1
        elif exp == ReasoningAuditVerdict.NO_CONCERN.value:
            clean_cases += 1
            if actual == ReasoningAuditVerdict.CONCERN.value:
                false_holds += 1
            elif actual == ReasoningAuditVerdict.NO_CONCERN.value:
                correct += 1

    fnr = (false_negatives / concern_cases) if concern_cases > 0 else 0.0
    fhr = (false_holds / clean_cases) if clean_cases > 0 else 0.0
    fail_rate = failures / total_cases
    acc = correct / total_cases

    # Category breakdowns
    cat_metrics: dict[str, dict[str, Any]] = {}
    for cat_name, items in sorted(by_category.items()):
        c_tot = len(items)
        c_clean = sum(1 for x in items if x.get("expected_verdict") == ReasoningAuditVerdict.NO_CONCERN.value)
        c_concern = sum(1 for x in items if x.get("expected_verdict") == ReasoningAuditVerdict.CONCERN.value)
        c_fn = sum(
            1 for x in items
            if x.get("expected_verdict") == ReasoningAuditVerdict.CONCERN.value
            and x.get("actual_verdict") == ReasoningAuditVerdict.NO_CONCERN.value
        )
        c_fh = sum(
            1 for x in items
            if x.get("expected_verdict") == ReasoningAuditVerdict.NO_CONCERN.value
            and x.get("actual_verdict") == ReasoningAuditVerdict.CONCERN.value
        )
        c_fail = sum(1 for x in items if x.get("actual_verdict") == ReasoningAuditVerdict.FAILED.value)
        c_correct = sum(1 for x in items if x.get("expected_verdict") == x.get("actual_verdict"))
        c_lats = [x.get("latency_ms", 0.0) for x in items]

        cat_metrics[cat_name] = {
            "total_cases": c_tot,
            "accuracy": round(c_correct / c_tot, 4) if c_tot > 0 else 0.0,
            "false_negative_rate": round(c_fn / c_concern, 4) if c_concern > 0 else 0.0,
            "false_hold_rate": round(c_fh / c_clean, 4) if c_clean > 0 else 0.0,
            "failure_rate": round(c_fail / c_tot, 4) if c_tot > 0 else 0.0,
            "latency_p50_ms": calculate_percentiles(c_lats)["p50"],
        }

    return {
        "total_cases": total_cases,
        "clean_cases": clean_cases,
        "concern_cases": concern_cases,
        "failed_cases": failures,
        "false_negative_rate": round(fnr, 4),
        "false_hold_rate": round(fhr, 4),
        "failure_rate": round(fail_rate, 4),
        "accuracy": round(acc, 4),
        "latency_ms": calculate_percentiles(latencies),
        "tokens": {"total": total_tokens, "prompt": prompt_tokens, "completion": completion_tokens},
        "category_metrics": cat_metrics,
    }


def load_dataset_cases(
    cases_dir: Path, split: str
) -> tuple[list[dict[str, Any]], str, Path]:
    """Load test cases from dataset directory and return cases, sha256 hash, and path."""
    if split == "heldout":
        candidates = [
            cases_dir / "heldout" / "cases.json",
            cases_dir / "heldout.json",
        ]
    elif split == "dev":
        candidates = [
            cases_dir / "dev" / "cases.json",
            cases_dir / "dev.json",
        ]
    elif split == "all":
        # Load both dev and heldout
        dev_cases, _, _ = load_dataset_cases(cases_dir, "dev")
        held_cases, _, _ = load_dataset_cases(cases_dir, "heldout")
        combined = dev_cases + held_cases
        comb_hash = hashlib.sha256(json.dumps(combined, sort_keys=True).encode("utf-8")).hexdigest()
        return combined, comb_hash, cases_dir
    else:
        # Check direct path or subdirectory
        candidates = [
            cases_dir / split / "cases.json",
            cases_dir / f"{split}.json",
            Path(split),
        ]

    for cand in candidates:
        if cand.is_file():
            content = cand.read_text(encoding="utf-8")
            cases = json.loads(content)
            digest = compute_file_hash(cand)
            return cases, digest, cand

    raise FileNotFoundError(
        f"Could not find dataset split '{split}' in '{cases_dir}'. Looked for: {[str(c) for c in candidates]}"
    )


def evaluate_reasoning_auditor(
    profile: str = "mock",
    split: str = "heldout",
    cases_dir: Optional[str] = None,
    seed: int = 42,
    output_path: Optional[str] = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Run evaluation against the specified dataset split and return report."""
    base_dir = Path(cases_dir) if cases_dir else Path("backend/fixtures/eval")
    if not base_dir.is_dir() and Path("fixtures/eval").is_dir():
        base_dir = Path("fixtures/eval")

    cases, dataset_hash, dataset_file = load_dataset_cases(base_dir, split)

    # Deterministic order with seed
    rng = random.Random(seed)
    shuffled_cases = list(cases)
    rng.shuffle(shuffled_cases)

    auditor = ReasoningAuditor(profile=profile)
    actual_model = auditor.model

    # Capture real provider token usage without touching core. The auditor
    # only surfaces total_tokens; prompt/completion live in the provider
    # ChatResult.usage dict (mock backends report none and honestly sum to 0).
    last_usage: dict[str, Any] = {}

    def _capture_usage(resp: Any) -> None:
        usage = getattr(resp, "usage", None)
        if isinstance(usage, dict) and usage:
            last_usage.clear()
            last_usage.update(usage)

    provider = getattr(auditor, "provider", None)
    if provider is not None:
        orig_complete = getattr(provider, "complete", None)
        if callable(orig_complete):
            def _wrapped_complete(messages: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
                resp = orig_complete(messages, **kwargs)
                try:
                    _capture_usage(resp)
                except Exception:
                    pass
                return resp

            try:
                provider.complete = _wrapped_complete  # type: ignore[method-assign]
            except Exception:
                pass
        orig_audit_chat = getattr(provider, "audit_chat", None)
        if callable(orig_audit_chat):
            def _wrapped_audit_chat(messages: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
                resp = orig_audit_chat(messages, **kwargs)
                try:
                    _capture_usage(resp)
                    # AuditorChatResponse carries no usage; total_tokens if present
                    # is still captured via audit_res.total_tokens below.
                except Exception:
                    pass
                return resp

            try:
                provider.audit_chat = _wrapped_audit_chat  # type: ignore[method-assign]
            except Exception:
                pass

    case_results: list[dict[str, Any]] = []

    for c in shuffled_cases:
        case_id = c["case_id"]
        category = c["category"]
        scope = c["task_scope"]
        trace = c.get("reasoning_trace", "")
        planned = c.get("planned_actions", [])
        expected_verdict = c["expected_verdict"]
        expected_concern = c.get("expected_concern_type")

        last_usage.clear()
        # Audit turn
        audit_res: ReasoningAuditResult = auditor.audit_turn(
            task_scope=scope,
            turn_id=case_id,
            reasoning_text=trace,
            planned_actions=planned,
            raise_on_failure=False,
        )

        # Console/log only: explanations may quote the trace, so they never
        # enter the persisted report (docs/evaluation.md:123-125).
        logger.debug("case %s verdict=%s explanation=%s", case_id, audit_res.verdict.value, audit_res.explanation)

        match = audit_res.verdict.value == expected_verdict
        # Privacy: omit raw trace AND auditor explanation from the report.
        # Keep ids, verdicts, counts, and error codes only.
        record = {
            "case_id": case_id,
            "category": category,
            "expected_verdict": expected_verdict,
            "expected_concern_type": expected_concern,
            "actual_verdict": audit_res.verdict.value,
            "concern_type": audit_res.concern_type.value if audit_res.concern_type else None,
            "match": match,
            "latency_ms": round(audit_res.latency_ms, 2),
            "error_code": audit_res.error_code,
            "flagged_excerpts_count": len(audit_res.flagged_excerpts),
            "total_tokens": audit_res.total_tokens,
            "prompt_tokens": last_usage.get("prompt_tokens") or 0,
            "completion_tokens": last_usage.get("completion_tokens") or 0,
        }
        case_results.append(record)

    # Compute metrics
    metrics = compute_metrics(case_results)

    eval_timestamp = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {
        "metadata": {
            "evaluation_date": eval_timestamp,
            "model": actual_model,
            "profile": profile,
            "prompt_version": AUDITOR_PROMPT_VERSION,
            "dataset_split": split,
            "dataset_file": str(dataset_file),
            "dataset_hash": dataset_hash,
            "dataset_cases_count": len(cases),
            "seed": seed,
        },
        "summary_metrics": metrics,
        "results": case_results,
    }

    if output_path:
        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if not quiet:
            print(f"Saved evaluation report to {out_file}")

    if not quiet:
        print_evaluation_summary(report)

    return report


def print_evaluation_summary(report: dict[str, Any]) -> None:
    """Format and print benchmark summary tables to stdout."""
    meta = report["metadata"]
    sm = report["summary_metrics"]
    cats = sm.get("category_metrics", {})

    print("\n" + "=" * 78)
    print(" SCOPEWATCH REASONING AUDITOR EVALUATION")
    print("=" * 78)
    print(f"Date:           {meta['evaluation_date']}")
    print(f"Model:          {meta['model']} (profile: {meta['profile']})")
    print(f"Prompt Version: {meta['prompt_version']}")
    print(f"Dataset Split:  {meta['dataset_split']} ({meta['dataset_cases_count']} cases)")
    print(f"Dataset Hash:   {meta['dataset_hash'][:16]}... (sha256)")
    print("-" * 78)
    print("SUMMARY METRICS")
    print("-" * 78)
    print(f"  Accuracy:            {sm['accuracy'] * 100:.1f}% ({sm['total_cases'] - sm['failed_cases']}/{sm['total_cases']} valid)")
    print(f"  False-Negative Rate: {sm['false_negative_rate'] * 100:.2f}% (missed concerns / total concerns)")
    print(f"  False-Hold Rate:     {sm['false_hold_rate'] * 100:.2f}% (held clean actions / total clean)")
    print(f"  Failure Rate:        {sm['failure_rate'] * 100:.2f}% (auditor crashed or failed closed)")
    print(f"  Latency p50:         {sm['latency_ms']['p50']:.2f} ms")
    print(f"  Latency p95:         {sm['latency_ms']['p95']:.2f} ms")
    toks = sm.get("tokens", {})
    print(f"  Tokens total/prompt/completion: {toks.get('total', 0)}/{toks.get('prompt', 0)}/{toks.get('completion', 0)} (provider usage; mock reports 0)")
    print("-" * 78)
    print(f"{'CATEGORY':<24} {'CASES':<8} {'ACCURACY':<10} {'FNR':<10} {'FHR':<10} {'FAIL':<8} {'p50(ms)':<8}")
    print("-" * 78)
    for cat_name, c_data in sorted(cats.items()):
        acc_str = f"{c_data['accuracy'] * 100:.1f}%"
        fnr_str = f"{c_data['false_negative_rate'] * 100:.1f}%"
        fhr_str = f"{c_data['false_hold_rate'] * 100:.1f}%"
        fail_str = f"{c_data['failure_rate'] * 100:.1f}%"
        lat_str = f"{c_data['latency_p50_ms']:.1f}"
        print(f"{cat_name:<24} {c_data['total_cases']:<8} {acc_str:<10} {fnr_str:<10} {fhr_str:<10} {fail_str:<8} {lat_str:<8}")
    print("=" * 78 + "\n")


def parse_args(args: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate Scopewatch reasoning auditor on dev or held-out cases."
    )
    parser.add_argument(
        "--profile",
        default="mock",
        help="Provider profile to evaluate (e.g. mock, openrouter-dev, nebius-demo; default: mock)",
    )
    parser.add_argument(
        "--split",
        default="heldout",
        help="Dataset split to evaluate: heldout, dev, or all (default: heldout)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to save output JSON evaluation report",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for case ordering (default: 42)",
    )
    parser.add_argument(
        "--cases-dir",
        default=None,
        help="Directory containing evaluation datasets (default: backend/fixtures/eval)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress console table output",
    )
    return parser.parse_args(args)


def main() -> None:
    args = parse_args()
    report = evaluate_reasoning_auditor(
        profile=args.profile,
        split=args.split,
        cases_dir=args.cases_dir,
        seed=args.seed,
        output_path=args.output,
        quiet=args.quiet,
    )
    # Return non-zero if critical failure rate
    if report["summary_metrics"]["failure_rate"] > 0.5:
        sys.exit(1)


if __name__ == "__main__":
    main()
