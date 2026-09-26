"""OpenAI-compatible chat completion provider client with normalized reasoning extraction."""

from collections.abc import Callable
import os
import time
from typing import Any, Optional
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from scopewatch.providers.errors import ProviderError, ProviderErrorCode
from scopewatch.providers.profile import ProviderProfile

VALID_PROVENANCES = {
    "PROVIDER_EXPOSED_TRACE",
    "AGENT_AUTHORED_SUMMARY",
    "UNAVAILABLE",
    "SYNTHETIC_FIXTURE",
}


class ChatResult(BaseModel):
    """Normalized chat completion response with extracted reasoning."""

    model_config = ConfigDict(extra="forbid")

    content: Optional[str] = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_text: Optional[str] = None
    reasoning_provenance: str = "UNAVAILABLE"
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = 0.0
    model: str
    profile: str

    @field_validator("reasoning_provenance")
    @classmethod
    def validate_provenance(cls, v: Any) -> str:
        val = v.value if hasattr(v, "value") else str(v)
        if val not in VALID_PROVENANCES:
            raise ValueError(
                f"Invalid reasoning_provenance: '{val}'. Must be one of {sorted(VALID_PROVENANCES)}"
            )
        return val


def extract_reasoning(message: dict[str, Any]) -> tuple[Optional[str], str]:
    """Extract normalized reasoning trace from provider message fields.

    Checks:
    1. choice["message"]["reasoning_content"]
    2. choice["message"]["reasoning"]
    3. choice["message"].get("reasoning_details")

    Sets PROVIDER_EXPOSED_TRACE only when raw reasoning came from a provider field.
    Sets UNAVAILABLE when absent or empty.
    Never synthesizes reasoning from the `content` field.
    """
    # 1. Check reasoning_content
    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        return reasoning_content.strip(), "PROVIDER_EXPOSED_TRACE"

    # 2. Check reasoning
    reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning.strip(), "PROVIDER_EXPOSED_TRACE"

    # 3. Check reasoning_details
    details = message.get("reasoning_details")
    if details:
        if isinstance(details, list):
            # Prefer reasoning.text blocks
            text_blocks = [
                d["text"].strip()
                for d in details
                if isinstance(d, dict)
                and d.get("type") == "reasoning.text"
                and isinstance(d.get("text"), str)
                and d["text"].strip()
            ]
            if text_blocks:
                return "\n".join(text_blocks), "PROVIDER_EXPOSED_TRACE"

            # Fall back to reasoning.summary blocks
            summary_blocks = [
                d["summary"].strip()
                for d in details
                if isinstance(d, dict)
                and d.get("type") == "reasoning.summary"
                and isinstance(d.get("summary"), str)
                and d["summary"].strip()
            ]
            if summary_blocks:
                return "\n".join(summary_blocks), "PROVIDER_EXPOSED_TRACE"

            # Fall back to any dict with text, summary, or content
            generic_blocks = [
                (d.get("text") or d.get("summary") or d.get("content") or "").strip()
                for d in details
                if isinstance(d, dict)
            ]
            generic_blocks = [b for b in generic_blocks if b]
            if generic_blocks:
                return "\n".join(generic_blocks), "PROVIDER_EXPOSED_TRACE"

            # Fall back to list of strings
            str_blocks = [d.strip() for d in details if isinstance(d, str) and d.strip()]
            if str_blocks:
                return "\n".join(str_blocks), "PROVIDER_EXPOSED_TRACE"

        elif isinstance(details, dict):
            extracted = details.get("text") or details.get("summary") or details.get("content")
            if isinstance(extracted, str) and extracted.strip():
                return extracted.strip(), "PROVIDER_EXPOSED_TRACE"

        elif isinstance(details, str) and details.strip():
            return details.strip(), "PROVIDER_EXPOSED_TRACE"

    return None, "UNAVAILABLE"


class ProviderClient:
    """HTTP client for OpenAI-compatible chat completion endpoints."""

    def __init__(
        self,
        profile: ProviderProfile,
        api_key: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
        transport: Optional[httpx.BaseTransport] = None,
        initial_backoff_s: float = 0.5,
        max_backoff_s: float = 8.0,
        backoff_factor: float = 2.0,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.profile = profile
        self.initial_backoff_s = initial_backoff_s
        self.max_backoff_s = max_backoff_s
        self.backoff_factor = backoff_factor
        self.sleep_fn = sleep_fn or time.sleep

        # Resolve API key
        if api_key is not None:
            self.api_key: Optional[str] = api_key
        elif profile.api_key_env:
            env_key = os.environ.get(profile.api_key_env)
            if not env_key or not env_key.strip():
                raise ProviderError(
                    ProviderErrorCode.MISSING_API_KEY,
                    f"Required environment variable '{profile.api_key_env}' is missing or empty for profile '{profile.name}'.",
                )
            self.api_key = env_key.strip()
        else:
            self.api_key = None

        self._transport = transport
        if self._transport is None and (profile.name == "mock" or profile.base_url.startswith("mock://")):
            # Provide safe default transport for mock profiles
            self._transport = httpx.MockTransport(self._default_mock_handler)

        self._external_client = http_client is not None
        self._client = http_client or httpx.Client(
            transport=self._transport,
            timeout=profile.timeout_s,
        )

    def _default_mock_handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "mock-cmpl-1",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": self.profile.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Mock response",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    def close(self) -> None:
        if not self._external_client:
            self._client.close()

    def __enter__(self) -> "ProviderClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _resolve_url(self) -> str:
        base = self.profile.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def complete(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> ChatResult:
        """Execute chat completion request with retry and reasoning extraction."""
        url = self._resolve_url()
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
        }
        if self.profile.extra_body:
            payload.update(self.profile.extra_body)
        if self.profile.reasoning_param:
            payload["reasoning"] = self.profile.reasoning_param
        if kwargs:
            payload.update(kwargs)

        attempt = 0
        max_retries = self.profile.max_retries

        while True:
            try:
                t0 = time.perf_counter()
                response = self._client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self.profile.timeout_s,
                )
                latency_ms = (time.perf_counter() - t0) * 1000.0

                if response.status_code == 200:
                    try:
                        data = response.json()
                    except Exception:
                        raise ProviderError(
                            ProviderErrorCode.MALFORMED_RESPONSE,
                            f"Provider '{self.profile.name}' returned invalid JSON.",
                        ) from None

                    if isinstance(data, dict) and "error" in data:
                        raise ProviderError(
                            ProviderErrorCode.PROVIDER_ERROR,
                            f"Provider '{self.profile.name}' returned error response envelope.",
                        )
                    return self._parse_chat_response(data, latency_ms)

                status_code = response.status_code
                is_retryable = status_code == 429 or status_code >= 500

                if is_retryable and attempt < max_retries:
                    delay = min(
                        self.max_backoff_s,
                        self.initial_backoff_s * (self.backoff_factor ** attempt),
                    )
                    self.sleep_fn(delay)
                    attempt += 1
                    continue

                # Non-retryable (e.g. 400) or retries exhausted
                if status_code == 429:
                    raise ProviderError(
                        ProviderErrorCode.PROVIDER_RATE_LIMIT,
                        f"Provider '{self.profile.name}' rate limit exceeded (HTTP 429).",
                    )
                elif status_code in (502, 503, 504):
                    raise ProviderError(
                        ProviderErrorCode.PROVIDER_UNAVAILABLE,
                        f"Provider '{self.profile.name}' service unavailable (HTTP {status_code}).",
                    )
                elif status_code >= 500:
                    raise ProviderError(
                        ProviderErrorCode.PROVIDER_ERROR,
                        f"Provider '{self.profile.name}' server error (HTTP {status_code}).",
                    )
                elif status_code == 400:
                    raise ProviderError(
                        ProviderErrorCode.PROVIDER_ERROR,
                        f"Provider '{self.profile.name}' rejected request with client error (HTTP 400).",
                    )
                else:
                    raise ProviderError(
                        ProviderErrorCode.PROVIDER_ERROR,
                        f"Provider '{self.profile.name}' request failed (HTTP {status_code}).",
                    )

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt < max_retries:
                    delay = min(
                        self.max_backoff_s,
                        self.initial_backoff_s * (self.backoff_factor ** attempt),
                    )
                    self.sleep_fn(delay)
                    attempt += 1
                    continue
                if isinstance(e, httpx.TimeoutException):
                    raise ProviderError(
                        ProviderErrorCode.PROVIDER_TIMEOUT,
                        f"Provider '{self.profile.name}' request timed out.",
                    ) from None
                raise ProviderError(
                    ProviderErrorCode.PROVIDER_UNAVAILABLE,
                    f"Provider '{self.profile.name}' network connection failed.",
                ) from None

    def _parse_chat_response(self, data: dict[str, Any], latency_ms: float) -> ChatResult:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError(
                ProviderErrorCode.MALFORMED_RESPONSE,
                f"Provider '{self.profile.name}' returned response without choices.",
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderError(
                ProviderErrorCode.MALFORMED_RESPONSE,
                f"Provider '{self.profile.name}' returned invalid choice format.",
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderError(
                ProviderErrorCode.MALFORMED_RESPONSE,
                f"Provider '{self.profile.name}' returned invalid message format.",
            )

        content = message.get("content")
        tool_calls = message.get("tool_calls") or []
        reasoning_text, reasoning_provenance = extract_reasoning(message)
        usage = data.get("usage") or {}
        model = data.get("model") or self.profile.model

        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            reasoning_text=reasoning_text,
            reasoning_provenance=reasoning_provenance,
            usage=usage,
            latency_ms=latency_ms,
            model=model,
            profile=self.profile.name,
        )


class MockProviderClient:
    """Deterministic, scriptable mock provider client for offline testing."""

    def __init__(
        self,
        profile: Optional[ProviderProfile] = None,
        responses: Optional[list[ChatResult | Exception]] = None,
    ) -> None:
        if profile is None:
            # Derive the mock model ID from providers.toml; core code never
            # hard-codes model IDs.
            from scopewatch.providers.loader import get_mock_model_name

            profile = ProviderProfile(
                name="mock",
                base_url="mock://localhost",
                model=get_mock_model_name(),
                timeout_s=5.0,
                max_retries=0,
            )
        self.profile = profile
        self._queue: list[ChatResult | Exception] = list(responses or [])
        self.call_history: list[dict[str, Any]] = []

    def enqueue(self, response: ChatResult | Exception) -> None:
        """Enqueue a canned ChatResult or Exception to be returned/raised on next call."""
        self._queue.append(response)

    def complete(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> ChatResult:
        """Return the next queued ChatResult or raise queued Exception."""
        self.call_history.append({"messages": messages, "kwargs": kwargs})
        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        # Default fallback response when queue is empty
        return ChatResult(
            content="Mock response",
            tool_calls=[],
            reasoning_text=None,
            reasoning_provenance="UNAVAILABLE",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            latency_ms=0.5,
            model=self.profile.model,
            profile=self.profile.name,
        )

    def reset(self) -> None:
        """Clear queued responses and recorded call history."""
        self._queue.clear()
        self.call_history.clear()
