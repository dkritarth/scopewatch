"""Evaluation CLI script for the hardened scope auditor.

Runs evaluation cases against HardenedRuleAuditorBackend (offline by default)
or HardenedOpenRouterBackend (when --live is specified).
Calculates accuracy, category breakdowns, latency statistics, and confusion matrix,
logging detailed output to a structured JSON file.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure repository root is on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
POC_DIR = SCRIPT_DIR.parent
if str(POC_DIR) not in sys.path:
    sys.path.insert(0, str(POC_DIR))

from scripts.live_union_alpha_probe import read_api_key
from src.diagnostics import AuditError, AuditErrorCode
from src.hardened_auditor import (
    DEFAULT_HARDENED_MODEL,
    HardenedOpenRouterBackend,
    HardenedRuleAuditorBackend,
    HardenedScopeAuditor,
)
from src.models import ReasoningTrace, ScopeClassification, TaskScope, TraceType


def load_cases(cases_path: Path) -> List[Dict[str, Any]]:
    """Load fixture test cases from JSON file."""
    if not cases_path.exists():
        raise FileNotFoundError(f"Cases fixture not found: {cases_path}")
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Cases file must contain a JSON array, got {type(data).__name__}")
    return data


def compute_metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute summary metrics including confusion matrix and category breakdowns."""
    total_cases = len(results)
    if total_cases == 0:
        return {
            "total_cases": 0,
            "matches": 0,
            "mismatches": 0,
            "accuracy": 0.0,
            "latency": {"min": 0, "max": 0, "avg": 0, "median": 0},
            "confusion_matrix": {},
            "category_breakdown": {},
        }

    matches = sum(1 for r in results if r["match"])
    mismatches = total_cases - matches
    accuracy = round((matches / total_cases) * 100.0, 2)

    latencies = [r["latency_ms"] for r in results]
    min_lat = round(min(latencies), 2)
    max_lat = round(max(latencies), 2)
    avg_lat = round(statistics.mean(latencies), 2)
    med_lat = round(statistics.median(latencies), 2)

    # Confusion matrix: expected -> actual -> count
    confusion_matrix: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # Category breakdown: category -> {total, matches, accuracy}
    category_data: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "matches": 0})

    for r in results:
        expected = r["expected"]
        actual = r["actual"]
        category = r.get("category", "unknown")

        confusion_matrix[expected][actual] += 1
        category_data[category]["total"] += 1
        if r["match"]:
            category_data[category]["matches"] += 1

    category_breakdown = {}
    for cat, data in sorted(category_data.items()):
        cat_total = data["total"]
        cat_matches = data["matches"]
        cat_acc = round((cat_matches / cat_total) * 100.0, 2) if cat_total > 0 else 0.0
        category_breakdown[cat] = {
            "total": cat_total,
            "matches": cat_matches,
            "mismatches": cat_total - cat_matches,
            "accuracy": cat_acc,
        }

    # Convert defaultdict to normal dict
    clean_confusion: Dict[str, Dict[str, int]] = {}
    for exp, actuals in sorted(confusion_matrix.items()):
        clean_confusion[exp] = dict(sorted(actuals.items()))

    return {
        "total_cases": total_cases,
        "matches": matches,
        "mismatches": mismatches,
        "accuracy": accuracy,
        "latency_ms": {
            "min": min_lat,
            "max": max_lat,
            "avg": avg_lat,
            "median": med_lat,
        },
        "confusion_matrix": clean_confusion,
        "category_breakdown": category_breakdown,
    }


def print_summary_table(metrics: Dict[str, Any], backend_name: str, model_name: str) -> None:
    """Print formatted evaluation summary to terminal."""
    print("\n" + "=" * 64)
    print(" HARDENED SCOPE AUDITOR EVALUATION SUMMARY")
    print("=" * 64)
    print(f"Backend:          {backend_name}")
    print(f"Model:            {model_name}")
    print(f"Total Cases:      {metrics['total_cases']}")
    print(f"Matches:          {metrics['matches']}")
    print(f"Mismatches:       {metrics['mismatches']}")
    print(f"Overall Accuracy: {metrics['accuracy']:.1f}%")
    lat = metrics["latency_ms"]
    print(f"Latency (ms):     min={lat['min']}ms, avg={lat['avg']}ms, median={lat['median']}ms, max={lat['max']}ms")
    print("-" * 64)
    print("Category Breakdown:")
    for cat, data in metrics["category_breakdown"].items():
        print(
            f"  - {cat:22s}: {data['matches']:2d}/{data['total']:2d} "
            f"({data['accuracy']:5.1f}%)"
        )
    print("-" * 64)
    print("Confusion Matrix (Rows: Expected, Columns: Actual):")
    all_statuses = sorted({
        status
        for row in metrics["confusion_matrix"].values()
        for status in row.keys()
    } | set(metrics["confusion_matrix"].keys()))
    header = f"{'Expected':<16}" + "".join(f"{s:>14}" for s in all_statuses)
    print(header)
    for exp in sorted(metrics["confusion_matrix"].keys()):
        row_str = f"{exp:<16}"
        for act in all_statuses:
            count = metrics["confusion_matrix"][exp].get(act, 0)
            row_str += f"{count:>14}"
        print(row_str)
    print("=" * 64 + "\n")


def run_evaluation(
    cases_path: Path,
    output_path: Path,
    live: bool = False,
    model: str = DEFAULT_HARDENED_MODEL,
    limit: Optional[int] = None,
) -> int:
    """Execute evaluation against cases and save report."""
    cases = load_cases(cases_path)
    if limit is not None and limit > 0:
        cases = cases[:limit]

    # Initialize backend
    if live:
        try:
            api_key = read_api_key()
        except Exception as err:
            print(f"Error loading OpenRouter API key: {err}", file=sys.stderr)
            return 2
        backend = HardenedOpenRouterBackend(api_key=api_key, model=model)
        backend_name = "OpenRouter"
    else:
        backend = HardenedRuleAuditorBackend()
        backend_name = "HardenedRuleAuditorBackend (Offline)"
        model = "semantic-rules-v1"

    auditor = HardenedScopeAuditor(backend)
    results: List[Dict[str, Any]] = []

    print(f"Starting evaluation of {len(cases)} cases using {backend_name} ({model})...\n")

    for idx, case in enumerate(cases, start=1):
        case_id = case.get("id", f"case-{idx}")
        category = case.get("category", "unspecified")
        expected = case.get("expected", "UNKNOWN")
        trace_text = case.get("trace", "")
        rationale = case.get("rationale", "")

        # Build TaskScope: check if scope dictionary exists, else fallback to default
        if "scope" in case and isinstance(case["scope"], dict):
            sc = case["scope"]
            scope = TaskScope(
                task_description=sc.get("task_description", ""),
                allowed_paths=sc.get("allowed_paths", []),
                blocked_paths=sc.get("blocked_paths", []),
                allowed_tools=sc.get("allowed_tools", []),
            )
        else:
            scope = TaskScope(
                task_description="Default task description",
                allowed_paths=["/workspace/project/src", "/workspace/project/tests"],
                blocked_paths=["/workspace/project/config"],
                allowed_tools=["view_file", "replace_file_content", "run_command"],
            )

        trace = ReasoningTrace(
            raw_text=trace_text,
            source_model="synthetic-fixture",
            trace_type=TraceType.SUMMARY,
        )

        started = time.perf_counter()
        error_code: Optional[str] = None
        reason: Optional[str] = None
        flagged_excerpts: List[str] = []

        try:
            classification = auditor.audit(scope, trace)
            actual = classification.status.value
            confidence = classification.confidence
            reason = classification.reason
            flagged_excerpts = classification.flagged_excerpts
        except AuditError as error:
            actual = "HOLD"
            error_code = error.error_code.value
            confidence = 0.0
            reason = f"AuditError: {error_code}"
        except Exception as exc:
            actual = "HOLD"
            error_code = AuditErrorCode.AUDITOR_ERROR.value
            confidence = 0.0
            reason = f"Unexpected Exception: {str(exc)}"

        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        is_match = (error_code is None and actual == expected)

        res_entry = {
            "id": case_id,
            "category": category,
            "expected": expected,
            "actual": actual,
            "match": is_match,
            "confidence": confidence,
            "reason": reason,
            "flagged_excerpts": flagged_excerpts,
            "error_code": error_code,
            "latency_ms": latency_ms,
            "rationale": rationale,
        }
        results.append(res_entry)

        # Print live progress
        status_symbol = "✓ PASS" if is_match else "✗ FAIL"
        print(
            f"[{idx:2d}/{len(cases):2d}] {case_id:<36} {status_symbol} "
            f"(exp: {expected:<12} act: {actual:<12} {latency_ms:6.1f}ms)"
        )
        sys.stdout.flush()

    # Compute metrics
    metrics = compute_metrics(results)
    print_summary_table(metrics, backend_name, model)

    # Prepare report output
    report = {
        "evaluation_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backend": backend_name,
        "model": model,
        "live": live,
        "cases_source": str(cases_path),
        "summary": metrics,
        "results": results,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Report saved to: {output_path.resolve()}\n")

    return 0 if metrics["matches"] == metrics["total_cases"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run evaluation on the hardened scope auditor with live or offline backends."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run against OpenRouter live API using the configured key.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_HARDENED_MODEL,
        help=f"Model identifier to query on OpenRouter (default: {DEFAULT_HARDENED_MODEL})",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=POC_DIR / "fixtures" / "hardened_cases.json",
        help="Path to the JSON fixture containing test cases.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=POC_DIR / "logs" / "hardened_auditor_evaluation.json",
        help="Path where the structured JSON evaluation report should be saved.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional upper bound on number of cases to evaluate.",
    )

    args = parser.parse_args()
    return run_evaluation(
        cases_path=args.cases,
        output_path=args.output,
        live=args.live,
        model=args.model,
        limit=args.limit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
