"""Local long-trace capture contract. Raw observed captures must stay private.

This is an evaluation artifact schema, not a gateway schema or a capture hook.
Only the synthetic example and the JSON Schema belong in source control.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Digest = str


class CaptureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Usage(CaptureModel):
    """None means unreported. Zero means an observed or synthetic explicit zero."""

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class TraceCapture(CaptureModel):
    # JSON Schema consumers receive the same expressible provenance rules.
    # Python validation additionally checks hashes and cross-field lengths.
    model_config = ConfigDict(json_schema_extra={"allOf": [{
        "if": {"properties": {"provenance": {"const": "UNAVAILABLE"}}},
        "then": {"properties": {
            "text": {"type": "null"}, "sha256": {"type": "null"},
            "original_chars": {"type": "null"}, "provider_field": {"type": "null"},
            "captured_chars": {"const": 0}, "complete": {"const": False},
            "truncation": {"const": "unavailable"},
        }},
        "else": {"allOf": [
            {"properties": {"text": {"type": "string"}, "sha256": {"type": "string"}}},
            {"if": {"properties": {"complete": {"const": True}}},
             "then": {"properties": {"truncation": {"const": "none"},
                                      "original_chars": {"type": "integer"}}},
             "else": {"properties": {"truncation": {"enum": ["capture_limit", "provider_length", "interrupted"]}}}},
            {"if": {"properties": {"provenance": {"const": "PROVIDER_EXPOSED_TRACE"}}},
             "then": {"properties": {"provider_field": {"type": "string"}}},
             "else": {"properties": {"provider_field": {"type": "null"}}}},
        ]},
    }]})
    provenance: Literal["PROVIDER_EXPOSED_TRACE", "AGENT_SUMMARY", "SYNTHETIC_TRACE", "UNAVAILABLE"]
    provider_field: Literal["message.reasoning", "message.reasoning_content", "message.reasoning_details"] | None
    text: str | None
    sha256: Digest | None = Field(pattern=r"^[a-f0-9]{64}$")
    original_chars: int | None = Field(ge=0)
    captured_chars: int = Field(ge=0)
    complete: bool
    truncation: Literal["none", "capture_limit", "provider_length", "interrupted", "unavailable"]

    @model_validator(mode="after")
    def consistent_trace(self) -> TraceCapture:
        if self.provenance == "UNAVAILABLE":
            if any(v is not None for v in (self.text, self.sha256, self.original_chars, self.provider_field)) or self.captured_chars or self.complete or self.truncation != "unavailable":
                raise ValueError("unavailable trace must remain missing")
            return self
        if self.text is None or self.sha256 != hashlib.sha256(self.text.encode()).hexdigest() or len(self.text) != self.captured_chars:
            raise ValueError("trace length/hash mismatch")
        if self.original_chars is not None and self.original_chars < self.captured_chars:
            raise ValueError("original trace shorter than capture")
        if self.complete:
            if self.truncation != "none" or self.original_chars != self.captured_chars:
                raise ValueError("complete trace must include original length")
        elif self.truncation in {"none", "unavailable"}:
            raise ValueError("incomplete trace needs an explicit truncation reason")
        if (self.provenance == "PROVIDER_EXPOSED_TRACE") != (self.provider_field is not None):
            raise ValueError("provider provenance requires a dedicated reasoning field")
        return self


class ActionCapture(CaptureModel):
    action_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,96}$")
    action_sha256: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    # Status must come from evidence. Planned actions are never executions.
    status: Literal["planned", "attempted", "allowed", "held", "denied", "approved", "executed", "failed"]
    evidence_event_id: str | None = Field(pattern=r"^[a-zA-Z0-9_.-]{1,96}$")


class TurnCapture(CaptureModel):
    turn_index: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    trace: TraceCapture
    usage: Usage
    finish_reason: Literal["stop", "tool_calls", "length", "error", "unknown"]
    context_window_tokens: int | None = Field(ge=1)
    output_budget_tokens: int | None = Field(ge=1)
    recent_action_ids: list[str]
    actions: list[ActionCapture]


class LongTraceCapture(CaptureModel):
    model_config = ConfigDict(json_schema_extra={"allOf": [{
        "if": {"properties": {"capture_origin": {"const": "synthetic"}}},
        "then": {"properties": {
            "content_origin": {"const": "synthetic"},
            "turns": {"items": {"properties": {"trace": {"properties": {
                "provenance": {"enum": ["SYNTHETIC_TRACE", "UNAVAILABLE"]},
            }}}}},
        }},
        "else": {"properties": {"turns": {"items": {"properties": {
            "trace": {"properties": {"provenance": {
                "enum": ["PROVIDER_EXPOSED_TRACE", "AGENT_SUMMARY", "UNAVAILABLE"],
            }}},
            "actions": {"items": {
                "if": {"properties": {"status": {"not": {"const": "planned"}}}},
                "then": {"properties": {"evidence_event_id": {"type": "string"}}},
            }},
        }}}}},
    }]})
    schema_version: Literal[1]
    capture_origin: Literal["observed", "synthetic"]
    content_origin: Literal["synthetic", "private"]
    captured_at: datetime
    profile: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,96}$")
    model: str = Field(pattern=r"^[a-zA-Z0-9_./:-]{1,160}$")
    task_scope_sha256: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    capture_limit_chars: int = Field(ge=1)
    turns: list[TurnCapture] = Field(min_length=1)

    @model_validator(mode="after")
    def provenance_is_consistent(self) -> LongTraceCapture:
        if self.captured_at.utcoffset() is None:
            raise ValueError("capture date needs timezone")
        if self.capture_origin == "synthetic" and self.content_origin != "synthetic":
            raise ValueError("synthetic capture cannot claim private observation")
        indices = [turn.turn_index for turn in self.turns]
        if indices != sorted(set(indices)):
            raise ValueError("turn indices must be unique and ordered")
        for turn in self.turns:
            provenance = turn.trace.provenance
            if provenance != "UNAVAILABLE" and ((self.capture_origin == "synthetic") != (provenance == "SYNTHETIC_TRACE")):
                raise ValueError("synthetic text cannot claim observed provenance")
            if self.capture_origin == "observed":
                for action in turn.actions:
                    if action.status != "planned" and action.evidence_event_id is None:
                        raise ValueError("observed action status requires an evidence reference")
            if turn.trace.captured_chars > self.capture_limit_chars:
                raise ValueError("capture exceeds declared limit")
        return self


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    try:
        LongTraceCapture.model_validate_json(args.capture.read_text())
    except Exception:
        print("CAPTURE CHECK FAILED: schema, counts or provenance invalid", file=sys.stderr)
        return 1
    print("CAPTURE CHECK PASSED; schema validation does not prove provider observation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
