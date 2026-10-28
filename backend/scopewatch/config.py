"""Configuration settings for Scopewatch baseline."""

import hmac
import logging
import os
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger("scopewatch.config")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_DIR = BASE_DIR / "runtime-data"
DEFAULT_WORKSPACE_DIR = BASE_DIR / "demo" / "workspace"

DB_PATH = Path(os.environ.get("SCOPEWATCH_DB_PATH", str(DEFAULT_DATA_DIR / "scopewatch.db")))
WORKSPACE_ROOT = Path(os.environ.get("SCOPEWATCH_WORKSPACE_ROOT", str(DEFAULT_WORKSPACE_DIR)))
DEMO_REVIEWER_ID = "demo-reviewer (synthetic)"
DEFAULT_EXPIRY_SECONDS = int(os.environ.get("SCOPEWATCH_APPROVAL_TTL", "300"))
MAX_READ_BYTES = 64 * 1024  # 64 KiB
MAX_WRITE_BYTES = 64 * 1024  # 64 KiB
MAX_DIR_ENTRIES = 1000
MAX_COPY_BACK_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_COPY_BACK_FILES = 10000
MAX_CONTAINER_OUTPUT_BYTES = 2 * 1024 * 1024  # 2 MiB


# --- Public request-body bounds (issue #304) ---------------------------------
#
# Unauthenticated callers can force big in-memory allocations just by sending
# a large body. The ingress middleware counts streamed bytes before the app
# sees them; the default bounds a request body at 256 KiB, well above any
# payload the gateway's own routes legitimately accept (the largest is an
# action submission carrying reasoning traces and arguments). Override with
# SCOPEWATCH_MAX_BODY_BYTES; a malformed or non-positive value is ignored
# with a warning rather than disabling the bound.
DEFAULT_MAX_REQUEST_BODY_BYTES = 256 * 1024  # 256 KiB
REQUEST_BODY_LIMIT_ENV_VAR = "SCOPEWATCH_MAX_BODY_BYTES"


def max_request_body_bytes(env: Optional[Mapping[str, str]] = None) -> int:
    """Resolve the public request-body limit in bytes (#304)."""
    src = env if env is not None else os.environ
    raw = str(src.get(REQUEST_BODY_LIMIT_ENV_VAR, "") or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
        logger.warning(
            "Ignoring malformed %s=%r; using %d bytes.",
            REQUEST_BODY_LIMIT_ENV_VAR,
            raw,
            DEFAULT_MAX_REQUEST_BODY_BYTES,
        )
    return DEFAULT_MAX_REQUEST_BODY_BYTES



# --- Canonical / Backwards-compatible Configuration Resolvers (issue #185) ---

def _first_nonempty(src: Mapping[str, str], keys: list[str]) -> str:
    """Return the first candidate value whose stripped string is non-empty."""
    for k in keys:
        raw = src.get(k)
        if raw is not None:
            val = str(raw).strip()
            if val:
                return val
    return ""


def get_api_key(env: Mapping[str, str] | None = None) -> str:
    """Return configured agent API key. Canonical: SCOPEWATCH_API_KEY, alias: API_KEY."""
    src = env if env is not None else os.environ
    return _first_nonempty(src, ["SCOPEWATCH_API_KEY", "API_KEY"])


def get_reviewer_token(env: Mapping[str, str] | None = None) -> str:
    """Return explicit reviewer token. Canonical: SCOPEWATCH_REVIEWER_TOKEN, aliases: REVIEWER_TOKEN, SCOPEWATCH_REVIEWER_KEY."""
    src = env if env is not None else os.environ
    return _first_nonempty(
        src,
        ["SCOPEWATCH_REVIEWER_TOKEN", "REVIEWER_TOKEN", "SCOPEWATCH_REVIEWER_KEY"],
    )


def get_reviewer_identity(env: Mapping[str, str] | None = None) -> str:
    """Return default reviewer identity label. Canonical: SCOPEWATCH_REVIEWER_ID, aliases: DEFAULT_REVIEWER_IDENTITY, REVIEWER_ID."""
    src = env if env is not None else os.environ
    return _first_nonempty(
        src,
        ["SCOPEWATCH_REVIEWER_ID", "DEFAULT_REVIEWER_IDENTITY", "REVIEWER_ID"],
    )


def get_cors_allow_origins(env: Mapping[str, str] | None = None) -> list[str]:
    """Return list of allowed CORS origins. Canonical: SCOPEWATCH_CORS_ALLOW_ORIGINS, alias: CORS_ALLOW_ORIGINS."""
    src = env if env is not None else os.environ
    raw = _first_nonempty(src, ["SCOPEWATCH_CORS_ALLOW_ORIGINS", "CORS_ALLOW_ORIGINS"])
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def get_cors_allow_credentials(env: Mapping[str, str] | None = None) -> bool:
    """Return whether CORS credentials are allowed. Canonical: SCOPEWATCH_CORS_ALLOW_CREDENTIALS, alias: CORS_ALLOW_CREDENTIALS."""
    src = env if env is not None else os.environ
    raw = (_first_nonempty(src, ["SCOPEWATCH_CORS_ALLOW_CREDENTIALS", "CORS_ALLOW_CREDENTIALS"]) or "false").lower()
    return raw in ("true", "1", "yes")


def get_allow_demo_reset(env: Mapping[str, str] | None = None) -> bool:
    """Return whether demo reset is permitted. Canonical: SCOPEWATCH_ALLOW_RESET, alias: ALLOW_DEMO_RESET."""
    src = env if env is not None else os.environ
    raw = (_first_nonempty(src, ["SCOPEWATCH_ALLOW_RESET", "ALLOW_DEMO_RESET"]) or "false").lower()
    return raw in ("true", "1", "yes")


def get_enable_hsts(env: Mapping[str, str] | None = None) -> bool:
    """Return whether HSTS header is enabled. Canonical: SCOPEWATCH_ENABLE_HSTS, alias: ENABLE_HSTS."""
    src = env if env is not None else os.environ
    raw = (_first_nonempty(src, ["SCOPEWATCH_ENABLE_HSTS", "ENABLE_HSTS"]) or "false").lower()
    return raw in ("true", "1", "yes")


def validate_auth_configuration(env: Mapping[str, str] | None = None) -> None:
    """Fail closed at startup if non-empty agent and reviewer credentials match.

    Non-empty agent credentials and reviewer credentials must be distinct.
    Never echoes tokens or keys in exception messages.
    """
    src = env if env is not None else os.environ
    demo_tok = _first_nonempty(src, ["DEMO_TOKEN"])
    agent_k = get_api_key(src)
    rev_tok = get_reviewer_token(src)

    agent_creds = [c for c in (demo_tok, agent_k) if c]
    if rev_tok:
        for a in agent_creds:
            if hmac.compare_digest(a, rev_tok):
                raise ValueError(
                    "Invalid authentication configuration: Agent credential and "
                    "reviewer credential must be distinct."
                )


# CORS and Security (issue #185)
CORS_ALLOW_ORIGINS = get_cors_allow_origins()
CORS_ALLOW_CREDENTIALS = get_cors_allow_credentials()
API_KEY = get_api_key()
REVIEWER_TOKEN = get_reviewer_token()
DEFAULT_REVIEWER_IDENTITY = get_reviewer_identity()
ALLOW_DEMO_RESET = get_allow_demo_reset()
ENABLE_HSTS = get_enable_hsts()


def reviewer_token_expected(env: Mapping[str, str] | None = None) -> str:
    """Return explicit expected reviewer token. Derived reviewer token fallbacks are removed."""
    src = env if env is not None else os.environ
    return get_reviewer_token(src)


def is_auth_configured(env: Mapping[str, str] | None = None) -> bool:
    """True when any authentication (demo guards, API key, or reviewer token) is configured."""
    src = env if env is not None else os.environ
    return bool(
        _first_nonempty(src, ["DEMO_TOKEN"])
        or get_api_key(src)
        or get_reviewer_token(src)
    )



# --- Gateway-native demo guards (issues #76 / #77) ---------------------------
DEMO_DEFAULT_MAX_CONCURRENT_RUNS = 5
DEMO_DEFAULT_MAX_TURNS_PER_RUN = 40
DEMO_DEFAULT_RATE_LIMIT_RUNS_PER_MIN_PER_IP = 6
DEMO_DEFAULT_MAX_RUNS_PER_DAY = 200
DEMO_DEFAULT_MAX_ACTIONS_PER_DAY = 5000
TOKEN_DEFAULT_RESERVE_PER_AUDIT = 8000


def is_demo_mode(env: Mapping[str, str] | None = None) -> bool:
    """True when DEMO_TOKEN is configured (demo guards engage)."""
    src = env if env is not None else os.environ
    return bool(_first_nonempty(src, ["DEMO_TOKEN"]))


# --- Agent wall-clock budget (issue #149) -----------------------------------
AGENT_WALL_CLOCK_ENV_VAR = "SCOPEWATCH_AGENT_WALL_CLOCK_S"
REPLAY_AGENT_WALL_CLOCK_S = 120.0
LIVE_AGENT_WALL_CLOCK_S = 600.0


def agent_wall_clock_timeout_s(
    profile_name: str = "",
    profile_base_url: str = "",
    env: Optional[Mapping[str, str]] = None,
) -> float:
    """Resolve the agent loop wall-clock budget in seconds."""
    from scopewatch.budget_store import is_mock_profile

    src: Mapping[str, str] = env if env is not None else os.environ
    raw = str(src.get(AGENT_WALL_CLOCK_ENV_VAR, "") or "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            logger.warning(
                "Ignoring non-numeric %s; using the profile-derived budget.",
                AGENT_WALL_CLOCK_ENV_VAR,
            )
        else:
            if value > 0:
                return value
            logger.warning(
                "Ignoring non-positive %s=%s; using the profile-derived budget.",
                AGENT_WALL_CLOCK_ENV_VAR,
                raw,
            )

    if profile_name or profile_base_url:
        if is_mock_profile(profile_name, profile_base_url):
            return REPLAY_AGENT_WALL_CLOCK_S
        return LIVE_AGENT_WALL_CLOCK_S
    return REPLAY_AGENT_WALL_CLOCK_S


def agent_profile_name(env: Mapping[str, str] | None = None) -> str:
    """Active agent provider profile (default: mock; never hard-code models)."""
    src = env if env is not None else os.environ
    return str(src.get("SCOPEWATCH_AGENT_PROFILE", "mock") or "mock")


def auditor_profile_name(env: Mapping[str, str] | None = None) -> str:
    """Active auditor provider profile (default: mock)."""
    src = env if env is not None else os.environ
    return str(src.get("SCOPEWATCH_AUDITOR_PROFILE", "mock") or "mock")
