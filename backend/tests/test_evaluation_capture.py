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


@pytest.mark.parametrize("status", ["attempted", "allowed", "held", "denied", "approved", "executed", "failed"])
def test_observed_action_requires_evidence_reference(status):
    value = json.loads(EXAMPLE.read_text())
    value["capture_origin"] = "observed"
    value["turns"] = value["turns"][:1]
    value["turns"][0]["trace"]["provenance"] = "AGENT_SUMMARY"
    action = value["turns"][0]["actions"][0]
    action.update(status=status, evidence_event_id=None)
    with pytest.raises(ValueError, match="evidence"):
        LongTraceCapture.model_validate_json(json.dumps(value))
    action["evidence_event_id"] = "synthetic-event-reference"
    LongTraceCapture.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("mutation", ["observed_synthetic", "unavailable_text", "unavailable_size", "provider_field", "summary_field", "synthetic_private", "observed_execution", "complete_truncation"])
def test_standalone_json_schema_rejects_expressible_contradictions(mutation):
    from jsonschema import Draft202012Validator, FormatChecker

    value = json.loads(EXAMPLE.read_text())
    schema = json.loads((EXAMPLE.parent / "long-trace-capture.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(value)
    trace = value["turns"][0]["trace"]
    if mutation == "observed_synthetic":
        value["capture_origin"] = "observed"
    elif mutation == "unavailable_text":
        value["turns"][-1]["trace"]["text"] = "invented"
    elif mutation == "unavailable_size":
        value["turns"][-1]["trace"]["captured_chars"] = 1
    elif mutation == "provider_field":
        value["capture_origin"] = "observed"
        trace["provenance"] = "PROVIDER_EXPOSED_TRACE"
    elif mutation == "summary_field":
        value["capture_origin"] = "observed"
        trace.update(provenance="AGENT_SUMMARY", provider_field="message.reasoning")
    elif mutation == "synthetic_private":
        value["content_origin"] = "private"
    elif mutation == "observed_execution":
        value["capture_origin"] = "observed"
        value["turns"] = value["turns"][:1]
        trace["provenance"] = "AGENT_SUMMARY"
        value["turns"][0]["actions"][0].update(status="executed", evidence_event_id=None)
    else:
        trace["truncation"] = "capture_limit"
    assert list(validator.iter_errors(value))
