"""Raw report checking rejects invented or malformed numeric measurements."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.evaluate_reasoning_audit import compute_file_hash, compute_metrics, verify_evaluation_report

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def report(tmp_path):
    cases = [{"case_id": f"synthetic-{index}", "category": "clean", "expected_verdict": "NO_CONCERN",
              "expected_concern_type": None} for index in range(23)]
    dataset = tmp_path / "synthetic.json"
    dataset.write_text(json.dumps(cases))
    rows = [{**case, "actual_verdict": "NO_CONCERN", "concern_type": None, "match": True,
             "latency_ms": 2.0, "error_code": None, "flagged_excerpts_count": 0,
             "total_tokens": None, "prompt_tokens": 0, "completion_tokens": 0} for case in cases]
    return {"metadata": {"evaluation_date": "2026-11-06T00:00:00+00:00", "model": "synthetic-model",
                         "profile": "mock", "prompt_version": "synthetic-prompt", "dataset_split": "dev",
                         "dataset_file": str(dataset), "dataset_hash": compute_file_hash(dataset),
                         "dataset_cases_count": len(cases), "seed": 42},
            "results": rows, "summary_metrics": compute_metrics(rows)}


def reload_and_check_cli(report, tmp_path):
    # Persist NaN/Infinity exactly as the current JSON loader accepts them.
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    loaded = json.loads(path.read_text())
    checked = subprocess.run([sys.executable, str(ROOT / "backend/scripts/evaluate_reasoning_audit.py"),
                              "--check", str(path)], env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
                              capture_output=True, text=True, timeout=10)
    return loaded, checked


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "total_tokens", "flagged_excerpts_count"])
@pytest.mark.parametrize("value", [True, -1, 1.5, float("nan"), float("inf"), [], {}, ""],
                         ids=["boolean", "negative", "fraction", "nan", "infinite", "list", "object", "empty-string"])
def test_invalid_counts_rejected_before_recomputation(report, tmp_path, field, value):
    report["results"][-1][field] = value
    try: report["summary_metrics"] = compute_metrics(report["results"])
    except (TypeError, ValueError, OverflowError): pass
    loaded, checked = reload_and_check_cli(report, tmp_path)
    errors = verify_evaluation_report(loaded)
    assert any(f"results[22].{field}" in error for error in errors)
    assert checked.returncode == 1
    assert "REPORT CHECK FAILED" in checked.stderr
    assert "Traceback" not in checked.stderr


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), None, [], "2"])
def test_invalid_latency_rejected_even_with_recomputed_summary(report, tmp_path, value):
    report["results"][-1]["latency_ms"] = value
    try: report["summary_metrics"] = compute_metrics(report["results"])
    except (TypeError, ValueError, OverflowError): pass
    loaded, checked = reload_and_check_cli(report, tmp_path)
    assert any("results[22].latency_ms" in error for error in verify_evaluation_report(loaded))
    assert checked.returncode == 1
    assert "REPORT CHECK FAILED" in checked.stderr
    assert "Traceback" not in checked.stderr


@pytest.mark.parametrize("field", ["case_id", "category", "expected_verdict", "actual_verdict"])
@pytest.mark.parametrize("value", [[], {}], ids=["list", "object"])
def test_malformed_enum_and_identity_shapes_return_errors(report, tmp_path, field, value):
    report["results"][0][field] = value
    loaded, checked = reload_and_check_cli(report, tmp_path)
    assert any(field in error for error in verify_evaluation_report(loaded))
    assert checked.returncode == 1
    assert "REPORT CHECK FAILED" in checked.stderr
    assert "Traceback" not in checked.stderr


@pytest.mark.parametrize("counts", [
    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": None},
    {"prompt_tokens": None, "completion_tokens": None, "total_tokens": 7},
    {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 99},
])
def test_valid_nullable_usage_and_integer_latency_keep_existing_aggregation(report, tmp_path, counts):
    report["results"][0].update(counts, latency_ms=2)
    report["summary_metrics"] = compute_metrics(report["results"])
    loaded, checked = reload_and_check_cli(report, tmp_path)
    assert verify_evaluation_report(loaded) == []
    assert checked.returncode == 0
    assert "REPORT CHECK PASSED" in checked.stdout
    expected = 5 if counts["prompt_tokens"] is not None else counts["total_tokens"] or 0
    assert loaded["summary_metrics"]["tokens"]["total"] == expected


@pytest.mark.parametrize("actual,match", [("NO_CONCERN", False), ("CONCERN", True)])
def test_match_cannot_contradict_recorded_verdicts(report, tmp_path, actual, match):
    report["results"][0].update(actual_verdict=actual, match=match)
    report["summary_metrics"] = compute_metrics(report["results"])
    loaded, checked = reload_and_check_cli(report, tmp_path)
    assert any("results[0].match" in error for error in verify_evaluation_report(loaded))
    assert checked.returncode == 1
    assert "REPORT CHECK FAILED" in checked.stderr
    assert "Traceback" not in checked.stderr
