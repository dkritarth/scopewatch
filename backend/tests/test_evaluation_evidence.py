"""Publication checks use synthetic result rows, never provider calls."""

import copy
import json
from pathlib import Path

import pytest

from scripts.evaluate_reasoning_audit import evaluate_reasoning_auditor
from scripts.evaluation_evidence import (
    ROOT, check_export, confidence_intervals, export_report, load_manifest, wilson_interval,
)

MANIFEST = ROOT / "backend/fixtures/eval/v2/manifest.json"


def test_intervals_use_concern_clean_and_total_denominators():
    rows = [
        {"expected_verdict": "CONCERN", "actual_verdict": "NO_CONCERN"},
        {"expected_verdict": "CONCERN", "actual_verdict": "FAILED"},
        {"expected_verdict": "CONCERN", "actual_verdict": "CONCERN"},
        {"expected_verdict": "NO_CONCERN", "actual_verdict": "CONCERN"},
    ]
    intervals = confidence_intervals(rows)
    assert intervals["false_negative_rate"]["numerator"] == 1
    assert intervals["false_negative_rate"]["denominator"] == 3
    assert intervals["false_hold_rate"]["denominator"] == 1
    assert intervals["failure_rate"]["denominator"] == 4
    assert intervals["accuracy"]["numerator"] == 1
    assert intervals["accuracy"]["denominator"] == 4


def test_wilson_zero_events_still_has_uncertainty_and_empty_is_missing():
    interval = wilson_interval(0, 12)
    assert interval["lower"] == 0
    assert interval["upper"] == pytest.approx(0.242494, abs=1e-6)
    assert wilson_interval(0, 0)["estimate"] is None
    assert wilson_interval(0, 0)["upper"] is None
    with pytest.raises(ValueError):
        wilson_interval(13, 12)


@pytest.fixture
def source():
    return evaluate_reasoning_auditor(profile="mock", split="dev",
                                      cases_dir=str(MANIFEST.parent), quiet=True)


def test_export_strips_raw_text_private_arguments_and_unknown_fields(source):
    source["credentials"] = "synthetic-secret-placeholder"
    source["metadata"]["headers"] = {"Authorization": "synthetic-secret-placeholder"}
    source["results"][0].update(explanation="SYNTHETIC_PRIVATE_TEXT",
                                reasoning_trace="SYNTHETIC_PRIVATE_TEXT",
                                planned_actions=[{"arguments": "SYNTHETIC_PRIVATE_TEXT"}])
    exported = export_report(source, MANIFEST)
    text = json.dumps(exported)
    assert "SYNTHETIC_PRIVATE_TEXT" not in text
    assert "synthetic-secret-placeholder" not in text
    assert exported["evidence_kind"] == "mock_structural"
    assert exported["split_status"] == "frozen_unmeasured"
    assert exported["confidence_intervals"]["false_negative_rate"]["denominator"] == 8
    assert exported["confidence_intervals"]["false_hold_rate"]["denominator"] == 4
    check_export(exported, MANIFEST)


@pytest.mark.parametrize("mutation", ["summary", "interval", "unknown", "row", "duplicate", "private_error", "nan", "model", "tuned"])
def test_checker_rejects_tampered_export(source, mutation):
    exported = export_report(source, MANIFEST)
    if mutation == "summary":
        exported["summary_metrics"]["accuracy"] = 123
    elif mutation == "interval":
        exported["confidence_intervals"]["false_negative_rate"]["denominator"] = 12
    elif mutation == "unknown":
        exported["metadata"]["private_trace"] = "synthetic private data"
    elif mutation == "row":
        exported["results"][0]["expected_verdict"] = "NO_CONCERN"
        exported["results"][0]["match"] = "wrong"
    elif mutation == "duplicate":
        exported["results"][0] = copy.deepcopy(exported["results"][1])
    elif mutation == "private_error":
        exported["results"][0]["error_code"] = "synthetic private message"
    elif mutation == "nan":
        exported["results"][0]["latency_ms"] = float("nan")
    elif mutation == "model":
        exported["metadata"]["model"] = "synthetic-secret-placeholder"
    else:
        exported["metadata"]["auditor_settings"]["max_trace_chars"] = "private"
    with pytest.raises(ValueError):
        check_export(exported, MANIFEST)


def test_manifest_pins_and_dataset_labels_cannot_be_silently_replaced(source, tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    manifest["splits"]["dev"]["sha256"] = "0" * 64
    bad = tmp_path / "manifest.json"
    bad.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="pin mismatch"):
        load_manifest(bad)
    source["results"][0]["category"] = "ambiguous"
    with pytest.raises(ValueError, match="pinned dataset"):
        export_report(source, MANIFEST)


def test_v2_splits_validate_as_scopewatch_cases_without_provider_feedback():
    from scopewatch.reasoning_audit import PlannedAction
    from scopewatch.schemas import TaskScope
    from scripts.audit_eval_splits import audit_splits

    load_manifest(MANIFEST)
    dev, heldout = [json.loads((MANIFEST.parent / s / "cases.json").read_text()) for s in ("dev", "heldout")]
    assert len(dev) == len(heldout) == 12
    assert audit_splits(dev, heldout)["clean_under_checks"]
    for case in dev + heldout:
        TaskScope.model_validate(case["task_scope"])
        for action in case["planned_actions"]:
            PlannedAction.model_validate(action)


def test_manifest_cannot_export_home_paths_or_free_text_versions(tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    manifest["splits"]["dev"]["file"] = str(ROOT / manifest["splits"]["dev"]["file"])
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="repository-relative"):
        load_manifest(path)
    manifest = json.loads(MANIFEST.read_text())
    manifest["dataset_version"] = "synthetic private text"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="dataset version"):
        load_manifest(path)


@pytest.mark.parametrize("error", ["AUDIT_TIMEOUT", "INVALID_AUDIT_OUTPUT"])
@pytest.mark.parametrize("verdict", ["NO_CONCERN", "CONCERN"])
def test_publication_rejects_errors_paired_with_success(source, error, verdict):
    from scripts.evaluate_reasoning_audit import compute_metrics

    row = source["results"][0]
    row.update(error_code=error, actual_verdict=verdict,
               match=row["expected_verdict"] == verdict)
    source["summary_metrics"] = compute_metrics(source["results"])
    with pytest.raises(ValueError):
        export_report(source, MANIFEST)


def test_publication_checker_rejects_timeout_even_with_recomputed_metrics(source):
    from scripts.evaluate_reasoning_audit import compute_metrics

    exported = export_report(source, MANIFEST)
    row = exported["results"][0]
    row.update(error_code="AUDIT_TIMEOUT", actual_verdict="NO_CONCERN",
               match=row["expected_verdict"] == "NO_CONCERN")
    exported["summary_metrics"] = compute_metrics(exported["results"])
    exported["confidence_intervals"] = confidence_intervals(exported["results"])
    with pytest.raises(ValueError, match="FAILED"):
        check_export(exported, MANIFEST)
