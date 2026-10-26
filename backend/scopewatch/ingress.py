"""Ingress bounds for the public gateway API (issue #304).

``BodySizeLimitMiddleware`` is a pure ASGI middleware (no
``BaseHTTPMiddleware``): it counts the bytes of every ``http.request``
message as they arrive and rejects the request with a sanitized 413 before
the application ever runs, so an oversized body can never force an
unbounded JSON allocation, create an action, or produce any evidence.

Rules:
- The check is outermost: it runs before authentication, CORS, and every
  route handler, so unauthenticated callers cannot make the server allocate.
- ``GET`` and ``HEAD`` are exempt (no meaningful body; the SSE route is
  ``GET`` only and needs no path-level exemption).
- A declared ``content-length`` over the limit is rejected without reading
  the body. Bodies without ``content-length`` (chunked, or length
  understated) are still counted as they stream in.
- Within the limit, the (fully received, therefore bounded) body is
  buffered once and replayed to the application as a single message.

The limit comes from ``SCOPEWATCH_MAX_BODY_BYTES`` (default 256 KiB) via
``scopewatch.config.max_request_body_bytes``. No route today needs a
larger per-route limit; add one here as an explicit path table if a future
route does.
"""

from typing import Any, Optional

from scopewatch.config import DEFAULT_MAX_REQUEST_BODY_BYTES

_413_BODY = b'{"error": "request body too large"}'
_EXEMPT_METHODS = frozenset({"GET", "HEAD"})


def _is_exempt(scope: dict[str, Any]) -> bool:
    """GET and HEAD requests carry no meaningful body and are exempt."""
    return str(scope.get("method", "")).upper() in _EXEMPT_METHODS


def _declared_content_length(scope: dict[str, Any]) -> Optional[int]:
    """First parseable content-length header, or None when absent/malformed."""
    for name, value in scope.get("headers") or []:
        if name == b"content-length":
            try:
                return int(value.decode("latin-1").strip())
            except (ValueError, UnicodeDecodeError):
                return None
    return None


async def _send_too_large(send: Any) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_413_BODY)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _413_BODY})


class BodySizeLimitMiddleware:
    """Pure ASGI middleware bounding request bodies before the app runs (#304)."""

    def __init__(
        self,
        app: Any,
        max_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] != "http" or _is_exempt(scope):
            await self.app(scope, receive, send)
            return

        # Early reject on a declared length that is already too big, before
        # reading a single byte of body.
        declared = _declared_content_length(scope)
        if declared is not None and declared > self.max_body_bytes:
            await _send_too_large(send)
            return

        # Buffer the streamed body up to the limit. Counting happens before
        # the app is called, so chunked bodies and bodies with no
        # content-length (or an understated one) are still bounded.
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                return
            if message_type != "http.request":
                continue
            body = message.get("body", b"") or b""
            received += len(body)
            if received > self.max_body_bytes:
                await _send_too_large(send)
                return
            chunks.append(body)
            if not message.get("more_body"):
                break

        buffered = [
            {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        ]

        async def replay_receive() -> dict[str, Any]:
            if buffered:
                return buffered.pop(0)
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)
