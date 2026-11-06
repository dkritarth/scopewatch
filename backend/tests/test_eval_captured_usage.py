"""Real provider usage must survive evaluation and publication without coercion."""

import copy
import json

import httpx
import pytest

from scopewatch.providers.client import ProviderClient
from scopewatch.providers.loader import get_profile
from scopewatch.reasoning_audit import ReasoningAuditor
from scripts import evaluate_reasoning_audit as harness
from scripts import evaluation_evidence as publication


def generate(tmp_path, monkeypatch, usages):
    scope = {"task_description": "Read synthetic outputs", "allowed_paths": ["outputs"],
             "allowed_tools": ["workspace"], "allowed_operations": ["read_text"],
             "created_at": "2026-11-06T00:00:00Z"}
    cases = [{"case_id": f"synthetic-{index}", "category": "clean", "expected_verdict": "NO_CONCERN",
              "expected_concern_type": None, "task_scope": scope, "reasoning_trace": "I will read outputs only.",
              "planned_actions": [{"tool": "workspace", "operation": "read_text", "resource": "outputs/a.txt"}]}
             for index in range(len(usages))]
    (tmp_path / "dev").mkdir()
    dataset = tmp_path / "dev/cases.json"
    dataset.write_text(json.dumps(cases))
    unused = tmp_path / "unused-synthetic.json"
    unused.write_text(json.dumps([{**cases[0], "case_id": "synthetic-unused"}]))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "content_origin": "synthetic", "dataset_version": "v2",
        "split_status": "frozen_unmeasured", "splits": {
            "dev": {"file": "dev/cases.json", "sha256": harness.compute_file_hash(dataset), "count": len(cases)},
            "heldout": {"file": "unused-synthetic.json", "sha256": harness.compute_file_hash(unused), "count": 1}}}))
    profile = get_profile("mock").model_copy(update={"base_url": "https://synthetic.invalid", "max_retries": 0})
    calls = []
    content = json.dumps({"verdict": "NO_CONCERN", "concern_type": None, "flagged_excerpts": [], "explanation": "Within scope."})

    def handler(request):
        usage = usages[len(calls)]
        calls.append(request)
        return httpx.Response(200, content=json.dumps({"choices": [{"message": {"content": content}}], "usage": usage}))

    with ProviderClient(profile, transport=httpx.MockTransport(handler)) as client:
        auditor = ReasoningAuditor(provider=client, model=profile.model)
        monkeypatch.setattr(harness, "ReasoningAuditor", lambda **kwargs: auditor)
        output = tmp_path / "report.json"
        report = harness.evaluate_reasoning_auditor(profile="mock", split="dev", cases_dir=str(tmp_path),
                                                     output_path=str(output), quiet=True)
    assert len(calls) == len(usages)
    assert all(row["actual_verdict"] == "NO_CONCERN" for row in report["results"])
    assert json.loads(output.read_text()) == report
    monkeypatch.setattr(publication, "ROOT", tmp_path)
    return report, manifest


CASES = [
    ({"prompt_tokens": "12", "completion_tokens": "3", "total_tokens": "15"}, (12, 3, 15), 15),
    ({"prompt_tokens": "12", "completion_tokens": "3"}, (12, 3, 15), 15),
    ({"prompt_tokens": 12.0, "completion_tokens": 3.0, "total_tokens": 15.0}, (12, 3, 15), 15),
    ({"prompt_tokens": True, "completion_tokens": 3, "total_tokens": 15}, (None, 3, 15), 15),
    ({"prompt_tokens": 12, "completion_tokens": False, "total_tokens": 15}, (12, None, 15), 15),
    ({"prompt_tokens": -2, "completion_tokens": 3, "total_tokens": 15}, (None, 3, 15), 15),
    ({"prompt_tokens": 1.5, "completion_tokens": 3, "total_tokens": 15}, (None, 3, 15), 15),
    ({"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": True}, (12, 3, 15), 15),
    ({"total_tokens": True}, (None, None, None), 0),
    ({"total_tokens": "bad"}, (None, None, None), 0),
    (None, (None, None, None), 0),
    ({"prompt_tokens": 12}, (12, None, None), 12),
    ({"completion_tokens": 3}, (None, 3, None), 3),
    ({"prompt_tokens": 0}, (0, None, None), 0),
    ({"total_tokens": 15}, (None, None, 15), 15),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 10}, (0, 0, 10), 10),
    ({"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 99}, (12, 3, 99), 15),
    ({"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 0}, (12, 3, 0), 15),
    ({"prompt_tokens": 12, "total_tokens": 12}, (12, None, 12), 12),
    ({"prompt_tokens": 12, "total_tokens": 15}, (12, None, 15), 15),
]


@pytest.mark.parametrize("usage,expected,total", CASES)
def test_usage_generation_and_publication(tmp_path, monkeypatch, usage, expected, total):
    report, manifest = generate(tmp_path, monkeypatch, [usage])
    row = report["results"][0]
    assert tuple(row[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")) == expected
    assert all(value is None or type(value) is int for value in (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]))
    assert report["summary_metrics"]["tokens"]["total"] == total
    if expected[2] is None and (expected[0] is not None or expected[1] is not None):
        assert report["summary_metrics"]["tokens"]["partial"] is True
    assert harness.verify_evaluation_report(report) == []
    exported = publication.export_report(report, manifest)
    publication.check_export(exported, manifest)


def test_mixed_usage_keeps_partial_coverage_and_rejects_tampering(tmp_path, monkeypatch):
    report, manifest = generate(tmp_path, monkeypatch, [
        {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 99},
        {"total_tokens": 7}, {"prompt_tokens": 2}, {},
    ])
    assert report["summary_metrics"]["tokens"] == {
        "total": 24, "prompt": 14, "completion": 3, "aggregation": "available_case_usage",
        "partial": True, "populated_cases": 3, "component_cases": 1}
    assert harness.verify_evaluation_report(report) == []
    exported = publication.export_report(report, manifest)
    publication.check_export(exported, manifest)
    for key in ("total", "populated_cases"):
        changed = copy.deepcopy(exported)
        changed["summary_metrics"]["tokens"][key] += 1
        with pytest.raises(ValueError): publication.check_export(changed, manifest)


@pytest.mark.parametrize("total", [0, 11])
def test_partial_breakdown_cannot_exceed_available_total(tmp_path, monkeypatch, total):
    with pytest.raises(ValueError, match="incomplete"):
        generate(tmp_path, monkeypatch, [{"prompt_tokens": 12, "total_tokens": total}])
    assert not (tmp_path / "report.json").exists()


def test_publication_rejects_row_contradiction_before_other_rows_can_hide_it():
    row = {"case_id": "synthetic", "category": "clean", "expected_verdict": "NO_CONCERN", "actual_verdict": "NO_CONCERN",
           "match": True, "latency_ms": 1.0, "error_code": None, "flagged_excerpts_count": 0,
           "prompt_tokens": 12, "completion_tokens": None, "total_tokens": 11}
    with pytest.raises(ValueError, match="incomplete"):
        publication.PublicationRow.model_validate(row)


@pytest.mark.parametrize("measured,expected", [
    ({"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
     {"total": 15, "prompt": 12, "completion": 3, "aggregation": "available_case_usage", "partial": True, "populated_cases": 1, "component_cases": 1}),
    ({"total_tokens": 0},
     {"total": 0, "prompt": 0, "completion": 0, "aggregation": "available_case_usage", "partial": True, "populated_cases": 1, "component_cases": 0}),
], ids=["complete-and-unknown", "zero-and-unknown"])
def test_unknown_usage_always_marks_aggregate_partial(tmp_path, monkeypatch, measured, expected):
    report, manifest = generate(tmp_path, monkeypatch, [measured, {}])
    assert report["summary_metrics"]["tokens"] == expected
    assert harness.verify_evaluation_report(report) == []
    publication.check_export(publication.export_report(report, manifest), manifest)
