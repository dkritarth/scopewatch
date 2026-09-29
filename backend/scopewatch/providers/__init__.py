"""Scopewatch provider abstraction layer for swappable models and normalized reasoning."""

from scopewatch.providers.client import (
    ChatResult,
    MockProviderClient,
    ProviderClient,
    extract_reasoning,
)
from scopewatch.providers.errors import (
    ProviderError,
    ProviderErrorCode,
)
from scopewatch.providers.loader import (
    get_agent_profile,
    get_api_key_for_profile,
    get_auditor_profile,
    get_profile,
    load_profiles,
)
from scopewatch.providers.profile import ProviderProfile

__all__ = [
    "ChatResult",
    "MockProviderClient",
    "ProviderClient",
    "ProviderError",
    "ProviderErrorCode",
    "ProviderProfile",
    "extract_reasoning",
    "get_agent_profile",
    "get_api_key_for_profile",
    "get_auditor_profile",
    "get_profile",
    "load_profiles",
]
