"""Loader utilities for provider profiles configured in TOML."""

import os
from pathlib import Path
import tomllib
from typing import Optional

from scopewatch.config import BASE_DIR
from scopewatch.providers.errors import ProviderError, ProviderErrorCode
from scopewatch.providers.profile import ProviderProfile


def get_default_config_path() -> Path:
    """Resolve the default path to providers.toml."""
    env_path = os.environ.get("SCOPEWATCH_PROVIDERS_CONFIG")
    if env_path:
        return Path(env_path)
    backend_dir = Path(__file__).resolve().parent.parent.parent
    candidate1 = backend_dir / "config" / "providers.toml"
    if candidate1.is_file():
        return candidate1
    candidate2 = BASE_DIR / "backend" / "config" / "providers.toml"
    if candidate2.is_file():
        return candidate2
    return candidate1


def load_profiles(config_path: Optional[Path | str] = None) -> dict[str, ProviderProfile]:
    """Load all provider profiles defined in the TOML configuration file."""
    path = Path(config_path) if config_path else get_default_config_path()
    if not path.is_file():
        raise ProviderError(
            ProviderErrorCode.PROFILE_NOT_FOUND,
            f"Provider configuration file not found: {path.name}",
        )

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        raise ProviderError(
            ProviderErrorCode.MALFORMED_RESPONSE,
            f"Failed to parse provider configuration file: {path.name}",
        )

    profiles_data = data.get("profiles", data)
    profiles: dict[str, ProviderProfile] = {}
    for name, item in profiles_data.items():
        if isinstance(item, dict):
            entry = dict(item)
            if "name" not in entry:
                entry["name"] = name
            try:
                profiles[name] = ProviderProfile(**entry)
            except Exception:
                raise ProviderError(
                    ProviderErrorCode.INVALID_PROFILE,
                    f"Invalid provider profile configuration for '{name}'.",
                ) from None
    return profiles


def get_profile(name: str, config_path: Optional[Path | str] = None) -> ProviderProfile:
    """Retrieve a specific profile by name."""
    profiles = load_profiles(config_path)
    if name not in profiles:
        raise ProviderError(
            ProviderErrorCode.PROFILE_NOT_FOUND,
            f"Provider profile '{name}' not found in configuration.",
        )
    return profiles[name]


def get_agent_profile(config_path: Optional[Path | str] = None) -> ProviderProfile:
    """Retrieve the active agent profile specified by SCOPEWATCH_AGENT_PROFILE (default: 'mock')."""
    name = os.environ.get("SCOPEWATCH_AGENT_PROFILE", "mock")
    return get_profile(name, config_path=config_path)


def get_auditor_profile(config_path: Optional[Path | str] = None) -> ProviderProfile:
    """Retrieve the active auditor profile specified by SCOPEWATCH_AUDITOR_PROFILE (default: 'mock')."""
    name = os.environ.get("SCOPEWATCH_AUDITOR_PROFILE", "mock")
    return get_profile(name, config_path=config_path)


def get_api_key_for_profile(profile: ProviderProfile) -> Optional[str]:
    """Retrieve and validate the API key for a profile.

    If the profile specifies `api_key_env`, reads from that environment variable.
    Raises ProviderError(MISSING_API_KEY) if the variable is unset or empty.
    Returns None if `api_key_env` is not set or empty.
    """
    if not profile.api_key_env:
        return None
    key = os.environ.get(profile.api_key_env)
    if not key or not key.strip():
        raise ProviderError(
            ProviderErrorCode.MISSING_API_KEY,
            f"Required API key environment variable '{profile.api_key_env}' is missing or empty for profile '{profile.name}'.",
        )
    return key.strip()
