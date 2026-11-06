"""Issue #199: unexpected provider response shapes fail as sanitized MALFORMED_RESPONSE.

List-of-parts content is a legitimate OpenAI-compatible shape and is accepted.
Anything else that does not fit ChatResult must raise ProviderError with no
provider output in the message, repr, or exception chain, because that text
becomes the run's stored failure reason.
"""

from __future__ import annotations

import json
import traceback
from typing import Any
from pathlib import Path

import httpx
import pytest

from scopewatch.agent.loop import sanitize_failure_reason
from scopewatch.providers.client import ProviderClient
from scopewatch.providers.errors import ProviderError, ProviderErrorCode
from scopewatch.providers.profile import ProviderProfile

# Synthetic, key-shaped marker. Must never surface in an error.
MARKER = "sk-synthetic-LEAKMARKER-0123456789abcdef"


def _client(message: dict[str, Any], **top: Any) -> ProviderClient:
    return _response_client({"model": "test-model", "choices": [{"message": message}], **top})


def _response_client(body: Any) -> ProviderClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(body), headers={"content-type": "application/json"})

    profile = ProviderProfile(
        name="test-shapes",
        base_url="https://api.example.com",
        model="test-model",
        max_retries=0,
    )
    return ProviderClient(profile=profile, api_key="key", transport=httpx.MockTransport(handler))


def test_list_of_parts_content_is_joined() -> None:
    client = _client({
        "role": "assistant",
        "content": [
            {"type": "text", "text": "first part"},
            {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
            {"type": "text", "text": "second part"},
        ],
    })
    result = client.complete([{"role": "user", "content": "hi"}])
    assert result.content == "first part\nsecond part"


def test_non_dict_usage_is_ignored_not_fatal() -> None:
    client = _client({"role": "assistant", "content": "ok"}, usage=["not", "a", "dict"])
    result = client.complete([{"role": "user", "content": "hi"}])
    assert result.content == "ok"
    assert result.usage == {}


def _assert_sanitized_malformed(exc_info: pytest.ExceptionInfo[ProviderError]) -> None:
    exc = exc_info.value
    assert exc.code == ProviderErrorCode.MALFORMED_RESPONSE.value
    assert MARKER not in str(exc)
    assert MARKER not in repr(exc)
    # The chain is what a traceback log would print; it must not carry the body.
    assert MARKER not in "".join(traceback.format_exception(exc))
    # And the reason the agent loop would store for the run.
    assert MARKER not in sanitize_failure_reason(exc)


@pytest.mark.parametrize(
    "message",
    [
        # A single tool call object instead of a list: dropping it silently would
        # hide the agent's action and end the run as if it had nothing to do.
        {"role": "assistant", "content": None, "tool_calls": {"function": {"name": MARKER}}},
        {"role": "assistant", "content": None, "tool_calls": MARKER},
        # List of the wrong item type: reaches pydantic and must be wrapped.
        {"role": "assistant", "content": None, "tool_calls": [MARKER]},
        # Content that is neither text, parts, nor null.
        {"role": "assistant", "content": {"text": MARKER}},
    ],
    ids=["tool_calls_dict", "tool_calls_str", "tool_calls_items_str", "content_dict"],
)
def test_malformed_shapes_raise_sanitized_malformed_response(message: dict[str, Any]) -> None:
    client = _client(message)
    with pytest.raises(ProviderError) as exc_info:
        client.complete([{"role": "user", "content": "hi"}])
    _assert_sanitized_malformed(exc_info)


@pytest.mark.parametrize("part", [
    {"type": "text", "text": {"verdict": MARKER}},
    {"type": "text", "text": [MARKER]},
    {"type": "text", "text": None},
    {"type": "text"},
    {"text": MARKER},
    {"type": 3, "text": MARKER},
    MARKER,
])
def test_malformed_part_cannot_be_discarded_after_valid_text(part):
    with _client({"content": [{"type": "text", "text": "valid text"}, part]}) as client:
        with pytest.raises(ProviderError) as exc_info:
            client.complete([])
    _assert_sanitized_malformed(exc_info)


def test_nontext_part_does_not_contribute_incidental_text():
    with _client({"content": [
        {"type": "text", "text": "actual message"},
        {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}, "text": MARKER},
    ]}) as client:
        assert client.complete([]).content == "actual message"


def test_malformed_auditor_part_holds_action_before_execution(tmp_path, monkeypatch):
    import asyncio
    import json
    from scopewatch.db import init_db
    from scopewatch.reasoning_audit import ReasoningAuditor
    from scopewatch.schemas import SubmitActionRequest, TaskScope
    from scopewatch.service import ScopewatchService

    monkeypatch.setenv("SCOPEWATCH_REASONING_AUDIT", "true")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_ROUTING", "single")
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "mock")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_PROFILE", "mock")
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "local")
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    (workspace / "outputs").mkdir(parents=True)
    clean = json.dumps({"verdict": "NO_CONCERN", "concern_type": None,
                        "flagged_excerpts": [], "explanation": "clean"})
    with _client({"content": [
        {"type": "text", "text": clean},
        {"type": "text", "text": {"verdict": MARKER}},
    ]}) as client:
        auditor = ReasoningAuditor(provider=client, profile="test-shapes", model="test-model")
        service = ScopewatchService(db_path=database, workspace_root=workspace, auditor=auditor)
        run, _ = service.create_run(name="Synthetic malformed auditor", task_scope=TaskScope(
            task_description="Write a synthetic output", allowed_paths=["outputs"],
            allowed_tools=["workspace"], allowed_operations=["write_text"],
            created_at="2026-11-05T00:00:00Z"))
        result = asyncio.run(service.submit_action(run.id, SubmitActionRequest(
            tool="workspace", operation="write_text", resource="outputs/result.txt",
            arguments={"content": "synthetic output"},
            exposed_reasoning_trace="I will write the permitted output only.",
            reasoning_provenance="SYNTHETIC_FIXTURE")))
    assert result.reasoning_audit.verdict == "FAILED"
    assert result.reasoning_audit.error_code == "MALFORMED_RESPONSE"
    assert MARKER not in result.reasoning_audit.model_dump_json()
    assert result.policy_decision.outcome == "HOLD"
    assert result.approval_request is not None
    assert result.execution_receipt is None
    assert not (Path(run.workspace_path) / "outputs/result.txt").exists()


@pytest.mark.parametrize("body", [None, [], [MARKER], MARKER, 1, 1.5, True],
                         ids=["null", "empty-array", "array", "string", "integer", "fraction", "boolean"])
def test_nonobject_envelopes_raise_sanitized_provider_error(body):
    with _response_client(body) as client:
        with pytest.raises(ProviderError) as exc_info:
            client.complete([])
    _assert_sanitized_malformed(exc_info)


@pytest.mark.parametrize("body", [None, [MARKER], MARKER], ids=["null", "array", "string"])
def test_nonobject_auditor_envelopes_record_malformed_failure(body):
    from scopewatch.reasoning_audit import ReasoningAuditor
    from scopewatch.schemas import TaskScope

    with _response_client(body) as client:
        auditor = ReasoningAuditor(provider=client, profile="test-shapes", model="test-model")
        result = auditor.audit_turn(task_scope=TaskScope(
            task_description="Read synthetic output", allowed_paths=["outputs"],
            allowed_tools=["workspace"], allowed_operations=["read_text"],
            created_at="2026-11-06T00:00:00Z"), turn_id="synthetic-envelope",
            reasoning_text="I will read the permitted output only.", reasoning_provenance="SYNTHETIC_FIXTURE")
    assert result.verdict.value == "FAILED"
    assert result.error_code == "MALFORMED_RESPONSE"
    assert result.total_tokens is None
    assert MARKER not in result.model_dump_json()
