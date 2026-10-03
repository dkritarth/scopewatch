"""openrouter-dev must request a JSON verdict without spending the cap on reasoning (#128)."""

import json

import httpx

from scopewatch.providers.client import ProviderClient
from scopewatch.providers.loader import get_profile


def _capture_payload(call) -> dict:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "synthetic",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    profile = get_profile("openrouter-dev")
    client = ProviderClient(profile, api_key="synthetic", transport=httpx.MockTransport(handler))
    call(client)
    assert len(seen) == 1
    return seen[0]


def test_auditor_call_disables_reasoning_so_json_fits_the_token_cap() -> None:
    payload = _capture_payload(lambda c: c.audit_chat([{"role": "user", "content": "synthetic"}]))
    assert payload["reasoning"] == {"enabled": False}
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 256


def test_agent_calls_still_request_the_profile_reasoning_effort() -> None:
    payload = _capture_payload(lambda c: c.complete([{"role": "user", "content": "synthetic"}]))
    assert payload["reasoning"] == {"effort": "high"}
