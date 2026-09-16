"""Check a saved evaluation report against the committed expected labels."""

import argparse
import json
from pathlib import Path


def check_report(report):
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "auditor_cases.json"
    expected = {case["id"]: case["expected"] for case in json.loads(fixtures.read_text())}
    rows = report["results"]
    if len(rows) != len(expected) or {row["id"] for row in rows} != set(expected):
        raise ValueError("Report must contain every expected case exactly once.")
    failures = [f"{row['id']}: expected {expected[row['id']]}, got {row['actual']}"
                for row in rows if row["actual"] != expected[row["id"]] or row.get("error_type")]
    if failures:
        raise ValueError("; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        check_report(json.loads(args.report.read_text()))
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"FAIL: {error}")
        return 1
    print("PASS: every actual classification matches its expected label.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
