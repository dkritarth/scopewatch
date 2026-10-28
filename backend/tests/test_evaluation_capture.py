"""Capture validation never authenticates a provider observation."""

import copy
import hashlib
import json

import pytest

from scripts.evaluation_capture import LongTraceCapture
from scripts.evaluation_evidence import ROOT

EXAMPLE = ROOT / "docs/evaluation/synthetic-long-capture.json"


def test_synthetic_long_trace_and_schema_are_valid():
    capture = LongTraceCapture.model_validate_json(EXAMPLE.read_text())
    assert capture.capture_origin == "synthetic"
    assert max(t.trace.captured_chars for t in capture.turns) > 16000
    assert any(t.trace.provenance == "UNAVAILABLE" for t in capture.turns)
    schema = json.loads((EXAMPLE.parent / "long-trace-capture.schema.json").read_text())
    assert schema == LongTraceCapture.model_json_schema()


@pytest.mark.parametrize("mutation", ["pretend_observed", "wrong_length", "wrong_hash", "missing_filled", "complete_truncation", "extra", "duplicate_turn"])
def test_provenance_and_capture_integrity_cannot_be_silently_changed(mutation):
    value = json.loads(EXAMPLE.read_text())
    if mutation == "pretend_observed":
        value["capture_origin"] = "observed"
    elif mutation == "wrong_length":
        value["turns"][0]["trace"]["captured_chars"] += 1
    elif mutation == "wrong_hash":
        value["turns"][0]["trace"]["sha256"] = "0" * 64
    elif mutation == "missing_filled":
        value["turns"][-1]["trace"]["text"] = "invented reasoning"
    elif mutation == "complete_truncation":
        value["turns"][0]["trace"]["truncation"] = "capture_limit"
    elif mutation == "extra":
        value["credentials"] = "synthetic-placeholder"
    else:
        value["turns"].append(copy.deepcopy(value["turns"][-1]))
    with pytest.raises(ValueError):
        LongTraceCapture.model_validate_json(json.dumps(value))


def test_observed_summary_is_distinct_from_dedicated_provider_reasoning():
    value = json.loads(EXAMPLE.read_text())
    value["capture_origin"] = "observed"
    value["turns"] = value["turns"][:1]
    trace = value["turns"][0]["trace"]
    trace["provenance"] = "AGENT_SUMMARY"
    LongTraceCapture.model_validate_json(json.dumps(value))
    trace["provenance"] = "PROVIDER_EXPOSED_TRACE"
    with pytest.raises(ValueError):
        LongTraceCapture.model_validate_json(json.dumps(value))
    trace["provider_field"] = "message.reasoning"
    LongTraceCapture.model_validate_json(json.dumps(value))


def test_truncated_trace_preserves_unknown_original_size():
    value = json.loads(EXAMPLE.read_text())
    trace = value["turns"][0]["trace"]
    trace.update(text="bounded", captured_chars=7, original_chars=None, complete=False,
                 truncation="provider_length", sha256=hashlib.sha256(b"bounded").hexdigest())
    LongTraceCapture.model_validate_json(json.dumps(value))
