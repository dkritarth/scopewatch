"""Run synthetic cases offline by default; --live explicitly sends them to OpenRouter."""

import argparse
import json
from pathlib import Path
import time

from scripts.live_union_alpha_probe import read_api_key
from src.models import ReasoningTrace, TaskScope, TraceType
from src.scope_auditor import ScopeAuditor, MockAuditorBackend
from src.openrouter_backend import OpenRouterAuditorBackend
from src.diagnostics import AuditError, AuditErrorCode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("logs/auditor_evaluation.json"))
    args = parser.parse_args()
    try:
        backend = OpenRouterAuditorBackend(read_api_key()) if args.live else MockAuditorBackend()
    except Exception:
        print("Unable to initialize auditor; check local credentials.")
        return 2
    auditor = ScopeAuditor(backend)
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    task = json.loads((fixtures / "tasks.json").read_text())[0]
    scope = TaskScope(task_description=task["description"], allowed_paths=task["allowed_paths"],
                      blocked_paths=task["blocked_paths"], allowed_tools=task["allowed_tools"])
    cases = json.loads((fixtures / "auditor_cases.json").read_text())
    results = []
    for case in cases:
        started = time.monotonic()
        trace = ReasoningTrace(raw_text=case["trace"], source_model="synthetic-fixture", trace_type=TraceType.SUMMARY)
        try:
            classification = auditor.audit(scope, trace)
            status = classification.status.value
            error_code = None
        except AuditError as error:
            status = "HOLD"
            error_code = error.error_code.value
        except Exception:
            status = "HOLD"
            error_code = AuditErrorCode.AUDITOR_ERROR.value
        results.append({"id": case["id"], "expected": case["expected"], "actual": status,
                        "match": error_code is None and status == case["expected"], "error_code": error_code,
                        "latency_seconds": round(time.monotonic() - started, 3)})
        print(json.dumps(results[-1]), flush=True)
    report = {"backend": "stealth/union-alpha" if args.live else "mock",
              "synthetic": True, "cases": len(results), "matches": sum(row["match"] for row in results),
              "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["matches"] == report["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
