"""Sanitized error definitions for Scopewatch provider profiles and clients."""

from enum import Enum
from typing import Optional


class ProviderErrorCode(str, Enum):
    """Sanitized diagnostic codes for provider operations."""

    PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    MISSING_API_KEY = "MISSING_API_KEY"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    INVALID_PROFILE = "INVALID_PROFILE"


class ProviderError(Exception):
    """Sanitized provider exception.

    Error messages and exception strings intentionally use only local diagnostic
    codes and sanitized descriptions. Raw provider response bodies, headers,
    and potential secrets are strictly excluded.
    """

    def __init__(self, code: str | ProviderErrorCode, message: Optional[str] = None):
        self.code = str(code.value if isinstance(code, ProviderErrorCode) else code)
        self.message = message or f"Provider error: {self.code}"
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"ProviderError(code={self.code!r}, message={self.message!r})"
