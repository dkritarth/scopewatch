"""Issue #199: unexpected provider response shapes fail as sanitized MALFORMED_RESPONSE.

List-of-parts content is a legitimate OpenAI-compatible shape and is accepted.
Anything else that does not fit ChatResult must raise ProviderError with no
provider output in the message, repr, or exception chain, because that text
becomes the run's stored failure reason.
"""

from __future__ import annotations

import traceback
from typing import Any

import httpx
import pytest

from scopewatch.agent.loop import sanitize_failure_reason
from scopewatch.providers.client import ProviderClient
from scopewatch.providers.errors import ProviderError, ProviderErrorCode
from scopewatch.providers.profile import ProviderProfile

# Synthetic, key-shaped marker. Must never surface in an error.
MARKER = "sk-synthetic-LEAKMARKER-0123456789abcdef"


def _client(message: dict[str, Any], **top: Any) -> ProviderClient:
    body = {"model": "test-model", "choices": [{"message": message}], **top}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

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
