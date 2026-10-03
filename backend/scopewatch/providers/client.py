"""OpenAI-compatible chat completion provider client with normalized reasoning extraction."""

from collections.abc import Callable
import logging
import os
import time
from typing import Any, NamedTuple, Optional
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from scopewatch.budget_store import is_mock_profile, record_completion
from scopewatch.providers.errors import ProviderError, ProviderErrorCode
from scopewatch.providers.profile import ProviderProfile

logger = logging.getLogger("scopewatch.providers.client")

VALID_PROVENANCES = {
    "PROVIDER_EXPOSED_TRACE",
    "AGENT_AUTHORED_SUMMARY",
    "UNAVAILABLE",
    "SYNTHETIC_FIXTURE",
}

# Provider reasoning-detail block types we recognise (#120). A block whose type
# is not listed here is unknown: its payload may be encrypted, redacted, or
# something the provider has not documented, so it never becomes a raw trace.
RAW_REASONING_FIELDS = ("reasoning_content", "reasoning")
RAW_DETAIL_TYPES = {"reasoning.text"}
SUMMARY_DETAIL_TYPES = {"reasoning.summary"}


class ChatResult(BaseModel):
    """Normalized chat completion response with extracted reasoning."""

    model_config = ConfigDict(extra="forbid")

    content: Optional[str] = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_text: Optional[str] = None
    reasoning_provenance: str = "UNAVAILABLE"
    reasoning_detail_type: Optional[str] = None
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


class ReasoningExtraction(NamedTuple):
    """Normalized reasoning text with the detail type that classified it.

    ``detail_type`` keeps the response shape that earned the provenance, so
    downstream evidence can tell a raw provider field from a provider summary
    instead of seeing both as ``PROVIDER_EXPOSED_TRACE``.
    """

    text: Optional[str]
    provenance: str
    detail_type: Optional[str] = None


def _detail_text(detail: dict[str, Any], *keys: str) -> Optional[str]:
    """First non-blank string value among `keys` in a reasoning-detail block."""
    for key in keys:
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_detail(detail: Any) -> ReasoningExtraction:
    """Classify one reasoning-detail block.

    A recognised raw type is the only source of ``PROVIDER_EXPOSED_TRACE``. A
    recognised summary type is ``AGENT_AUTHORED_SUMMARY`` (ADR-0001 decision 5:
    summary-only evidence stays labelled as a summary). Everything else,
    including encrypted, redacted, untyped and unrecognised payloads, is
    ``UNAVAILABLE``: unknown detail never falls back to raw text (#120).
    """
    if not isinstance(detail, dict):
        return ReasoningExtraction(None, "UNAVAILABLE", None)

    detail_type = detail.get("type")
    if isinstance(detail_type, str) and detail_type:
        if detail_type in RAW_DETAIL_TYPES:
            return ReasoningExtraction(
                _detail_text(detail, "text"), "PROVIDER_EXPOSED_TRACE", detail_type
            )
        if detail_type in SUMMARY_DETAIL_TYPES:
            return ReasoningExtraction(
                _detail_text(detail, "summary"), "AGENT_AUTHORED_SUMMARY", detail_type
            )
        return ReasoningExtraction(None, "UNAVAILABLE", detail_type)

    # Untyped single-object payloads keep the meaning of their own field name.
    text = _detail_text(detail, "text")
    if text:
        return ReasoningExtraction(text, "PROVIDER_EXPOSED_TRACE", "reasoning.text")
    summary = _detail_text(detail, "summary")
    if summary:
        return ReasoningExtraction(summary, "AGENT_AUTHORED_SUMMARY", "reasoning.summary")
    # `content` is deliberately never mined: message content is not reasoning.
    return ReasoningExtraction(None, "UNAVAILABLE", None)


def _best(extractions: list[ReasoningExtraction]) -> ReasoningExtraction:
    """Prefer raw reasoning over summaries, matching provider block precedence."""
    for provenance in ("PROVIDER_EXPOSED_TRACE", "AGENT_AUTHORED_SUMMARY"):
        matches = [e for e in extractions if e.provenance == provenance and e.text]
        if matches:
            return ReasoningExtraction(
                "\n".join(e.text for e in matches),
                provenance,
                matches[0].detail_type,
            )
    # Nothing recognized. Keep the first detail type seen so reviewers can see
    # why reasoning is unavailable (encrypted, redacted, undocumented).
    untyped = next((e.detail_type for e in extractions if e.detail_type), None)
    return ReasoningExtraction(None, "UNAVAILABLE", untyped)


def extract_reasoning(message: dict[str, Any]) -> ReasoningExtraction:
    """Extract normalized reasoning trace from provider message fields.

    Checks, in order:
    1. choice["message"]["reasoning_content"]
    2. choice["message"]["reasoning"]
    3. choice["message"].get("reasoning_details") block types

    Sets PROVIDER_EXPOSED_TRACE only for raw reasoning from a recognised raw
    provider field, and AGENT_AUTHORED_SUMMARY for provider summary blocks.
    Sets UNAVAILABLE when absent, blank, or of an unknown detail variant.
    Never synthesizes reasoning from the `content` field.
    """
    # 1 & 2. Recognized raw reasoning fields
    for field in RAW_REASONING_FIELDS:
        value = message.get(field)
        if isinstance(value, str) and value.strip():
            return ReasoningExtraction(value.strip(), "PROVIDER_EXPOSED_TRACE", field)

    # 3. Reasoning detail blocks
    details = message.get("reasoning_details")
    if details:
        if isinstance(details, list):
            return _best([_normalize_detail(d) for d in details])
        if isinstance(details, dict):
            return _normalize_detail(details)
        # A bare string carries no detail type, so it cannot be shown to be a
        # raw trace rather than a summary or an opaque blob.

    return ReasoningExtraction(None, "UNAVAILABLE", None)


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

    def _record_token_metering(self, result: "ChatResult") -> None:
        """Record usage into the per-profile daily ledger (metering only).

        Scripted (mock) profiles record nothing. Metering never raises and
        never logs keys, prompts, or response bodies — counts only.
        """
        try:
            if is_mock_profile(self.profile.name, self.profile.base_url):
                return
            record_completion(self.profile.name, result.usage)
        except Exception as exc:  # noqa: BLE001 - metering must never break requests
            logger.debug("Sanitized token-metering skip [%s]", type(exc).__name__)

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
        request_timeout = kwargs.pop("request_timeout", None)
        if kwargs:
            payload.update(kwargs)

        attempt = 0
        max_retries = self.profile.max_retries
        call_start = time.monotonic()
        call_deadline = (
            call_start + request_timeout
            if request_timeout is not None
            else None
        )

        while True:
            if call_deadline is not None:
                remaining_call = call_deadline - time.monotonic()
                if remaining_call <= 0:
                    raise ProviderError(
                        ProviderErrorCode.PROVIDER_TIMEOUT,
                        f"Provider '{self.profile.name}' call deadline exceeded before completion.",
                    )
                timeout_val = min(self.profile.timeout_s, max(0.001, remaining_call))
            else:
                timeout_val = self.profile.timeout_s

            try:
                t0 = time.perf_counter()
                response = self._client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=timeout_val,
                )
                latency_ms = (time.perf_counter() - t0) * 1000.0

                if call_deadline is not None and time.monotonic() > call_deadline:
                    raise ProviderError(
                        ProviderErrorCode.PROVIDER_TIMEOUT,
                        f"Provider '{self.profile.name}' call deadline exceeded during request execution.",
                    )

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
                    result = self._parse_chat_response(data, latency_ms)
                    self._record_token_metering(result)
                    return result

                status_code = response.status_code
                is_retryable = status_code == 429 or status_code >= 500

                if is_retryable and attempt < max_retries:
                    delay = min(
                        self.max_backoff_s,
                        self.initial_backoff_s * (self.backoff_factor ** attempt),
                    )
                    if call_deadline is not None and (time.monotonic() + delay) > call_deadline:
                        raise ProviderError(
                            ProviderErrorCode.PROVIDER_TIMEOUT,
                            f"Provider '{self.profile.name}' retry delay ({delay:.2f}s) would exceed call deadline.",
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
                    if call_deadline is not None and (time.monotonic() + delay) > call_deadline:
                        raise ProviderError(
                            ProviderErrorCode.PROVIDER_TIMEOUT,
                            f"Provider '{self.profile.name}' retry delay would exceed call deadline.",
                        ) from e
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

    def audit_chat(
        self,
        messages: list[dict[str, Any]],
        **_: Any,
    ) -> ChatResult:
        """Request a bounded JSON verdict using auditor-only provider options."""
        request_options: dict[str, Any] = {
            "response_format": {"type": "json_object"},
            "max_tokens": 256,
        }
        request_options.update(self.profile.auditor_body)
        return self.complete(messages, **request_options)

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
        extraction = extract_reasoning(message)
        usage = data.get("usage") or {}
        model = data.get("model") or self.profile.model

        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            reasoning_text=extraction.text,
            reasoning_provenance=extraction.provenance,
            reasoning_detail_type=extraction.detail_type,
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
