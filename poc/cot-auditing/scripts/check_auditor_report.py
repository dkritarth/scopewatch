"""Check a saved evaluation report against the committed expected labels."""

import argparse
import json
from pathlib import Path

from src.diagnostics import AuditErrorCode
from src.models import ScopeClassificationEnum


def check_report(report):
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "auditor_cases.json"
    expected = {case["id"]: case["expected"] for case in json.loads(fixtures.read_text())}
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        raise ValueError("Report must contain a results list.")
    rows = report["results"]
    seen = set()
    failures = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("Report contains a malformed row.")
        case_id = row["id"]
        if case_id not in expected or case_id in seen:
            raise ValueError("Report contains an unknown or duplicate case.")
        seen.add(case_id)
        actual = row.get("actual")
        if not isinstance(actual, str) or actual not in {status.value for status in ScopeClassificationEnum}:
            raise ValueError("Report contains an invalid classification.")
        if "error_code" not in row:
            raise ValueError("Report row is missing error_code.")
        error_code = row["error_code"]
        if error_code is not None and (
            not isinstance(error_code, str) or error_code not in {code.value for code in AuditErrorCode}
        ):
            raise ValueError("Report contains an invalid error_code.")
        if actual != expected[case_id] or error_code is not None or row.get("error_type") is not None:
            failures.append(f"{case_id}: expected {expected[case_id]}, got {actual}")
    if seen != set(expected):
        raise ValueError("Report must contain every expected case exactly once.")
    if failures:
        raise ValueError("; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        check_report(json.loads(args.report.read_text()))
    except (ValueError, KeyError, TypeError, OSError):
        print("FAIL: report is unreadable, malformed, or contains mismatches or audit errors.")
        return 1
    print("PASS: every actual classification matches its expected label.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
