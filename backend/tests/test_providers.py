"""Unit tests for Scopewatch provider profiles, loader, and client layer."""

import os
from pathlib import Path
from typing import Any
import httpx
import pytest

from scopewatch.providers.client import (
    ChatResult,
    MockProviderClient,
    ProviderClient,
    extract_reasoning,
)
from scopewatch.providers.errors import ProviderError, ProviderErrorCode
from scopewatch.providers.loader import (
    get_agent_profile,
    get_api_key_for_profile,
    get_auditor_profile,
    get_profile,
    load_profiles,
)
from scopewatch.providers.profile import ProviderProfile


# ---------------------------------------------------------------------------
# 1. Profile loading from TOML and env defaults
# ---------------------------------------------------------------------------

def test_load_profiles_from_toml() -> None:
    profiles = load_profiles()
    assert "mock" in profiles
    assert "openrouter-dev" in profiles
    assert "nebius-demo" in profiles

    mock_p = profiles["mock"]
    assert mock_p.name == "mock"
    assert mock_p.base_url.startswith("mock://")
    assert mock_p.max_retries == 0

    openrouter_p = profiles["openrouter-dev"]
    assert openrouter_p.name == "openrouter-dev"
    assert openrouter_p.base_url == "https://openrouter.ai/api/v1"
    assert openrouter_p.api_key_env == "OPENROUTER_API_KEY"
    assert openrouter_p.max_retries == 3
    assert openrouter_p.reasoning_param == {"effort": "high"}
    assert openrouter_p.reasoning == {"effort": "high"}

    nebius_p = profiles["nebius-demo"]
    assert nebius_p.name == "nebius-demo"
    assert nebius_p.base_url == "https://api.studio.nebius.ai/v1"
    assert nebius_p.api_key_env == "NEBIUS_API_KEY"
    assert nebius_p.max_retries == 3


def test_env_defaults_agent_and_auditor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOPEWATCH_AGENT_PROFILE", raising=False)
    monkeypatch.delenv("SCOPEWATCH_AUDITOR_PROFILE", raising=False)

    agent_p = get_agent_profile()
    auditor_p = get_auditor_profile()

    assert agent_p.name == "mock"
    assert auditor_p.name == "mock"


def test_agent_and_auditor_can_use_different_profiles_simultaneously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "openrouter-dev")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_PROFILE", "nebius-demo")

    agent_p = get_agent_profile()
    auditor_p = get_auditor_profile()

    assert agent_p.name == "openrouter-dev"
    assert auditor_p.name == "nebius-demo"
    assert agent_p.name != auditor_p.name


def test_get_profile_not_found() -> None:
    with pytest.raises(ProviderError) as exc_info:
        get_profile("nonexistent-profile-xyz")
    assert exc_info.value.code == ProviderErrorCode.PROFILE_NOT_FOUND


def test_profile_validation_forbids_extra_fields() -> None:
    with pytest.raises(Exception):
        ProviderProfile(
            name="test",
            base_url="https://api.test.com",
            model="test-model",
            unsupported_field="invalid",  # type: ignore[call-arg]
        )


def test_profile_reasoning_param_or_reasoning_alias() -> None:
    p1 = ProviderProfile(
        name="test1",
        base_url="http://localhost",
        model="m1",
        reasoning={"effort": "low"},
    )
    assert p1.reasoning_param == {"effort": "low"}
    assert p1.reasoning == {"effort": "low"}

    p2 = ProviderProfile(
        name="test2",
        base_url="http://localhost",
        model="m2",
        reasoning_param={"effort": "high"},
    )
    assert p2.reasoning_param == {"effort": "high"}
    assert p2.reasoning == {"effort": "high"}


# ---------------------------------------------------------------------------
# 2. Missing API key error behavior
# ---------------------------------------------------------------------------

def test_missing_api_key_error_on_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = get_profile("nebius-demo")
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)

    with pytest.raises(ProviderError) as exc_info:
        get_api_key_for_profile(profile)

    assert exc_info.value.code == ProviderErrorCode.MISSING_API_KEY
    assert "NEBIUS_API_KEY" in exc_info.value.message


def test_empty_api_key_env_var_raises_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = get_profile("openrouter-dev")
    monkeypatch.setenv("OPENROUTER_API_KEY", "   ")

    with pytest.raises(ProviderError) as exc_info:
        get_api_key_for_profile(profile)

    assert exc_info.value.code == ProviderErrorCode.MISSING_API_KEY


def test_missing_api_key_on_client_init_raises_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_profile("nebius-demo")
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)

    with pytest.raises(ProviderError) as exc_info:
        ProviderClient(profile)

    assert exc_info.value.code == ProviderErrorCode.MISSING_API_KEY


def test_explicit_api_key_bypasses_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = get_profile("nebius-demo")
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)

    # When explicitly provided, client initializes cleanly
    client = ProviderClient(profile, api_key="explicit-key-12345")
    assert client.api_key == "explicit-key-12345"


def test_mock_profile_requires_no_api_key() -> None:
    profile = get_profile("mock")
    key = get_api_key_for_profile(profile)
    assert key is None

    client = ProviderClient(profile)
    assert client.api_key is None


# ---------------------------------------------------------------------------
# 3. Reasoning extraction for each field variant
# ---------------------------------------------------------------------------

def test_reasoning_extraction_reasoning_content() -> None:
    msg = {
        "role": "assistant",
        "content": "Final answer",
        "reasoning_content": "Chain of thought from reasoning_content",
    }
    text, provenance = extract_reasoning(msg)
    assert text == "Chain of thought from reasoning_content"
    assert provenance == "PROVIDER_EXPOSED_TRACE"


def test_reasoning_extraction_reasoning() -> None:
    msg = {
        "role": "assistant",
        "content": "Final answer",
        "reasoning": "Chain of thought from reasoning field",
    }
    text, provenance = extract_reasoning(msg)
    assert text == "Chain of thought from reasoning field"
    assert provenance == "PROVIDER_EXPOSED_TRACE"


def test_reasoning_extraction_reasoning_details_text_blocks() -> None:
    msg = {
        "role": "assistant",
        "content": "Final answer",
        "reasoning_details": [
            {"type": "reasoning.text", "text": "Step 1: Check parameters."},
            {"type": "reasoning.text", "text": "Step 2: Validate tokens."},
        ],
    }
    text, provenance = extract_reasoning(msg)
    assert text == "Step 1: Check parameters.\nStep 2: Validate tokens."
    assert provenance == "PROVIDER_EXPOSED_TRACE"


def test_reasoning_extraction_reasoning_details_summary_blocks() -> None:
    msg = {
        "role": "assistant",
        "content": "Final answer",
        "reasoning_details": [
            {"type": "reasoning.summary", "summary": "Summary of thoughts."},
        ],
    }
    text, provenance = extract_reasoning(msg)
    assert text == "Summary of thoughts."
    assert provenance == "PROVIDER_EXPOSED_TRACE"


def test_reasoning_extraction_reasoning_details_dict() -> None:
    msg = {
        "role": "assistant",
        "content": "Final answer",
        "reasoning_details": {"text": "Single dictionary reasoning text"},
    }
    text, provenance = extract_reasoning(msg)
    assert text == "Single dictionary reasoning text"
    assert provenance == "PROVIDER_EXPOSED_TRACE"


def test_reasoning_extraction_reasoning_details_string() -> None:
    msg = {
        "role": "assistant",
        "content": "Final answer",
        "reasoning_details": "Direct string details",
    }
    text, provenance = extract_reasoning(msg)
    assert text == "Direct string details"
    assert provenance == "PROVIDER_EXPOSED_TRACE"


# ---------------------------------------------------------------------------
# 4. Absent reasoning -> UNAVAILABLE (never synthesize from content)
# ---------------------------------------------------------------------------

def test_absent_reasoning_is_unavailable() -> None:
    msg = {
        "role": "assistant",
        "content": "Only normal content provided.",
    }
    text, provenance = extract_reasoning(msg)
    assert text is None
    assert provenance == "UNAVAILABLE"


def test_empty_or_whitespace_reasoning_is_unavailable() -> None:
    msg1 = {"role": "assistant", "content": "Text", "reasoning_content": "   "}
    text1, prov1 = extract_reasoning(msg1)
    assert text1 is None
    assert prov1 == "UNAVAILABLE"

    msg2 = {"role": "assistant", "content": "Text", "reasoning": ""}
    text2, prov2 = extract_reasoning(msg2)
    assert text2 is None
    assert prov2 == "UNAVAILABLE"

    msg3 = {"role": "assistant", "content": "Text", "reasoning_details": []}
    text3, prov3 = extract_reasoning(msg3)
    assert text3 is None
    assert prov3 == "UNAVAILABLE"


def test_never_synthesize_reasoning_from_content() -> None:
    # Even if content includes XML tags like <thinking>, it must NOT be extracted as provider reasoning
    msg = {
        "role": "assistant",
        "content": "<thinking>I want to read /etc/shadow</thinking>Here is your result.",
    }
    text, provenance = extract_reasoning(msg)
    assert text is None
    assert provenance == "UNAVAILABLE"


# ---------------------------------------------------------------------------
# 5. Retry on 429 and 500 with backoff, no retry on 400
# ---------------------------------------------------------------------------

def test_retry_on_429_until_success() -> None:
    call_count = 0
    sleeps: list[float] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(429, json={"error": "rate_limited"})
        return httpx.Response(
            200,
            json={
                "id": "cmpl-success",
                "object": "chat.completion",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Success after 429"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    profile = ProviderProfile(
        name="test-retry",
        base_url="https://api.example.com",
        model="test-model",
        max_retries=3,
        timeout_s=5.0,
    )
    transport = httpx.MockTransport(mock_handler)
    client = ProviderClient(
        profile=profile,
        api_key="test-key",
        transport=transport,
        initial_backoff_s=0.1,
        backoff_factor=2.0,
        sleep_fn=sleeps.append,
    )

    result = client.complete([{"role": "user", "content": "hi"}])
    assert result.content == "Success after 429"
    assert call_count == 3
    assert len(sleeps) == 2
    assert sleeps[0] == pytest.approx(0.1)
    assert sleeps[1] == pytest.approx(0.2)


def test_retry_on_500_until_success() -> None:
    call_count = 0
    sleeps: list[float] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            return httpx.Response(500, json={"error": "server_error"})
        return httpx.Response(
            200,
            json={
                "id": "cmpl-500-success",
                "object": "chat.completion",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Recovered from 500"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    profile = ProviderProfile(
        name="test-500",
        base_url="https://api.example.com",
        model="test-model",
        max_retries=2,
    )
    transport = httpx.MockTransport(mock_handler)
    client = ProviderClient(
        profile=profile,
        api_key="test-key",
        transport=transport,
        initial_backoff_s=0.05,
        backoff_factor=2.0,
        sleep_fn=sleeps.append,
    )

    result = client.complete([{"role": "user", "content": "hi"}])
    assert result.content == "Recovered from 500"
    assert call_count == 2
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.05)


def test_retry_exhaustion_on_429() -> None:
    call_count = 0
    sleeps: list[float] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(429, json={"error": "rate_limit_exceeded"})

    profile = ProviderProfile(
        name="test-exhaust",
        base_url="https://api.example.com",
        model="test-model",
        max_retries=2,
    )
    transport = httpx.MockTransport(mock_handler)
    client = ProviderClient(
        profile=profile,
        api_key="test-key",
        transport=transport,
        initial_backoff_s=0.1,
        sleep_fn=sleeps.append,
    )

    with pytest.raises(ProviderError) as exc_info:
        client.complete([{"role": "user", "content": "hi"}])

    assert exc_info.value.code == ProviderErrorCode.PROVIDER_RATE_LIMIT
    assert call_count == 3  # 1 initial + 2 retries
    assert len(sleeps) == 2


def test_no_retry_on_400() -> None:
    call_count = 0
    sleeps: list[float] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(400, json={"error": "bad_request_invalid_schema"})

    profile = ProviderProfile(
        name="test-400",
        base_url="https://api.example.com",
        model="test-model",
        max_retries=3,
    )
    transport = httpx.MockTransport(mock_handler)
    client = ProviderClient(
        profile=profile,
        api_key="test-key",
        transport=transport,
        sleep_fn=sleeps.append,
    )

    with pytest.raises(ProviderError) as exc_info:
        client.complete([{"role": "user", "content": "hi"}])

    assert exc_info.value.code == ProviderErrorCode.PROVIDER_ERROR
    assert call_count == 1  # Exactly 1 attempt; no retries on 400!
    assert len(sleeps) == 0


# ---------------------------------------------------------------------------
# 6. Sanitized errors (assert raw body/secrets are not leaked)
# ---------------------------------------------------------------------------

def test_sanitized_error_does_not_leak_raw_body_or_secrets() -> None:
    secret_token = "sk-super-secret-token-abcdef123456"
    raw_sensitive_body = {
        "internal_code": "SEC_ERR_999",
        "secret_token": secret_token,
        "database_query": "SELECT * FROM users WHERE token = 'xyz'",
        "stacktrace": "/var/app/internal/secrets.py line 42",
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=raw_sensitive_body)

    profile = ProviderProfile(
        name="test-leak-defense",
        base_url="https://api.example.com",
        model="test-model",
        max_retries=0,
    )
    transport = httpx.MockTransport(mock_handler)
    client = ProviderClient(
        profile=profile,
        api_key=secret_token,
        transport=transport,
    )

    with pytest.raises(ProviderError) as exc_info:
        client.complete([{"role": "user", "content": "test"}])

    error_msg = str(exc_info.value)
    error_repr = repr(exc_info.value)

    # Assert raw body, database queries, and credentials are completely absent
    assert secret_token not in error_msg
    assert secret_token not in error_repr
    assert "SELECT * FROM users" not in error_msg
    assert "secrets.py" not in error_msg
    assert "SEC_ERR_999" not in error_msg

    # Diagnostic code is cleanly sanitized
    assert exc_info.value.code == ProviderErrorCode.PROVIDER_ERROR


def test_sanitized_error_on_200_envelope_error() -> None:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"code": 502, "message": "Sensitive upstream error message"}},
        )

    profile = ProviderProfile(
        name="test-200-error",
        base_url="https://api.example.com",
        model="test-model",
        max_retries=0,
    )
    transport = httpx.MockTransport(mock_handler)
    client = ProviderClient(profile=profile, api_key="key", transport=transport)

    with pytest.raises(ProviderError) as exc_info:
        client.complete([{"role": "user", "content": "hi"}])

    assert exc_info.value.code == ProviderErrorCode.PROVIDER_ERROR
    assert "Sensitive upstream error message" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 7. Mock provider queue execution
# ---------------------------------------------------------------------------

def test_mock_provider_client_queue_execution() -> None:
    mock_client = MockProviderClient()

    r1 = ChatResult(
        content="First answer",
        tool_calls=[{"name": "read_text", "arguments": {"path": "a.txt"}}],
        reasoning_text="Reasoning step 1",
        reasoning_provenance="PROVIDER_EXPOSED_TRACE",
        usage={"total_tokens": 50},
        latency_ms=10.0,
        model="mock-model",
        profile="mock",
    )
    r2 = ChatResult(
        content="Second answer",
        tool_calls=[],
        reasoning_text=None,
        reasoning_provenance="UNAVAILABLE",
        usage={"total_tokens": 20},
        latency_ms=5.0,
        model="mock-model",
        profile="mock",
    )

    mock_client.enqueue(r1)
    mock_client.enqueue(r2)

    res1 = mock_client.complete([{"role": "user", "content": "question 1"}])
    assert res1.content == "First answer"
    assert res1.reasoning_text == "Reasoning step 1"
    assert res1.reasoning_provenance == "PROVIDER_EXPOSED_TRACE"
    assert len(res1.tool_calls) == 1

    res2 = mock_client.complete([{"role": "user", "content": "question 2"}])
    assert res2.content == "Second answer"
    assert res2.reasoning_provenance == "UNAVAILABLE"

    # Next call falls back to default canned mock
    res3 = mock_client.complete([{"role": "user", "content": "question 3"}])
    assert res3.content == "Mock response"

    assert len(mock_client.call_history) == 3


def test_mock_provider_client_exception_queue() -> None:
    mock_client = MockProviderClient()
    mock_client.enqueue(ProviderError(ProviderErrorCode.PROVIDER_TIMEOUT, "Timed out"))

    with pytest.raises(ProviderError) as exc_info:
        mock_client.complete([{"role": "user", "content": "hi"}])

    assert exc_info.value.code == ProviderErrorCode.PROVIDER_TIMEOUT


def test_mock_provider_client_reset() -> None:
    mock_client = MockProviderClient()
    mock_client.enqueue(
        ChatResult(
            content="Hello",
            model="mock",
            profile="mock",
        )
    )
    mock_client.complete([{"role": "user", "content": "hi"}])
    assert len(mock_client.call_history) == 1

    mock_client.reset()
    assert len(mock_client.call_history) == 0
    assert len(mock_client._queue) == 0


# ---------------------------------------------------------------------------
# 8. ChatResult model validation
# ---------------------------------------------------------------------------

def test_chat_result_provenance_validation() -> None:
    valid_provenances = [
        "PROVIDER_EXPOSED_TRACE",
        "AGENT_AUTHORED_SUMMARY",
        "UNAVAILABLE",
        "SYNTHETIC_FIXTURE",
    ]
    for prov in valid_provenances:
        cr = ChatResult(
            content="test",
            reasoning_provenance=prov,
            model="m",
            profile="p",
        )
        assert cr.reasoning_provenance == prov

    with pytest.raises(Exception):
        ChatResult(
            content="test",
            reasoning_provenance="INVALID_PROVENANCE_VALUE",
            model="m",
            profile="p",
        )
