"""Configuration settings for Scopewatch baseline."""

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

# CORS and Security (issue #185)
# Comma-separated list of allowed origins, e.g. "https://app.example.com,https://dashboard.example.com"
# Defaults to empty list for default same-origin behavior (no cross-origin access allowed)
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("SCOPEWATCH_CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
]
# Whether to allow credentials (cookies, auth headers) in CORS requests
CORS_ALLOW_CREDENTIALS = os.environ.get("SCOPEWATCH_CORS_ALLOW_CREDENTIALS", "false").lower() == "true"
# API key for production authentication (when not in demo mode)
API_KEY = os.environ.get("SCOPEWATCH_API_KEY", "")

# Reviewer authentication (issue #185)
# Distinct credential held only by human reviewers/judges to approve or deny holds.
# The agent loop never receives this token.
REVIEWER_TOKEN = (
    os.environ.get("SCOPEWATCH_REVIEWER_TOKEN")
    or os.environ.get("REVIEWER_TOKEN")
    or os.environ.get("SCOPEWATCH_REVIEWER_KEY", "")
)

# Reviewer identity label override
DEFAULT_REVIEWER_IDENTITY = os.environ.get("SCOPEWATCH_REVIEWER_ID", "")

# Flag controlling demo state reset (issue #185)
ALLOW_DEMO_RESET = os.environ.get("SCOPEWATCH_ALLOW_RESET", "false").lower() in ("true", "1", "yes")

# Flag controlling HSTS (issue #185)
ENABLE_HSTS = os.environ.get("SCOPEWATCH_ENABLE_HSTS", "false").lower() in ("true", "1", "yes")


def reviewer_token_expected(env: dict[str, str] | None = None) -> str:
    """Return the expected reviewer token, if any is configured or required."""
    src = env if env is not None else os.environ
    explicit = (
        src.get("SCOPEWATCH_REVIEWER_TOKEN")
        or src.get("REVIEWER_TOKEN")
        or src.get("SCOPEWATCH_REVIEWER_KEY", "")
    )
    if explicit:
        return explicit.strip()
    demo_tok = str(src.get("DEMO_TOKEN", "") or "").strip()
    if demo_tok:
        return f"{demo_tok}-reviewer"
    api_k = str(src.get("SCOPEWATCH_API_KEY", "") or "").strip()
    if api_k:
        return f"{api_k}-reviewer"
    return ""


def is_auth_configured(env: dict[str, str] | None = None) -> bool:
    """True when any authentication (demo guards, API key, or reviewer token) is configured."""
    src = env if env is not None else os.environ
    return bool(
        str(src.get("DEMO_TOKEN", "") or "").strip()
        or str(src.get("SCOPEWATCH_API_KEY", "") or "").strip()
        or str(src.get("SCOPEWATCH_REVIEWER_TOKEN", "") or "").strip()
        or str(src.get("REVIEWER_TOKEN", "") or "").strip()
    )


# --- Gateway-native demo guards (issues #76 / #77) ---------------------------
# Demo mode engages when DEMO_TOKEN is set; see scopewatch.demo_guards for the
# enforced semantics (same env names as deploy/.env.example). Token budgets
# (SCOPEWATCH_DAILY_TOKEN_BUDGET_<PROFILE>) apply regardless of demo mode but
# never affect the mock profile. Full parsing lives in demo_guards.py and
# budget_store.py; the defaults below document the demo-mode behaviour.
DEMO_DEFAULT_MAX_CONCURRENT_RUNS = 5
DEMO_DEFAULT_MAX_TURNS_PER_RUN = 40
DEMO_DEFAULT_RATE_LIMIT_RUNS_PER_MIN_PER_IP = 6
DEMO_DEFAULT_MAX_RUNS_PER_DAY = 200
DEMO_DEFAULT_MAX_ACTIONS_PER_DAY = 5000
TOKEN_DEFAULT_RESERVE_PER_AUDIT = 8000


def is_demo_mode(env: dict[str, str] | None = None) -> bool:
    """True when DEMO_TOKEN is configured (demo guards engage)."""
    src = env if env is not None else os.environ
    return bool(str(src.get("DEMO_TOKEN", "") or "").strip())


# --- Agent wall-clock budget (issue #149) -----------------------------------
#
# The agent loop's deadline bounds one whole run, so its right value depends on
# how long one model turn actually costs. Scripted replay (the `mock` profile)
# answers instantly and keeps the historical 120 s, so CI and mock-agent runs
# are unchanged. A live tool-calling turn costs seconds, not milliseconds, so
# live profiles get LIVE_AGENT_WALL_CLOCK_S. Measured turn costs and how the
# number was chosen are recorded in docs/operations/judge-runbook.md.
#
# `SCOPEWATCH_AGENT_WALL_CLOCK_S` overrides both for one run and is the only
# knob an operator needs. A malformed or non-positive override is ignored with
# a warning rather than crashing or silently truncating every run.
AGENT_WALL_CLOCK_ENV_VAR = "SCOPEWATCH_AGENT_WALL_CLOCK_S"

#: Scripted replay budget, unchanged from before this setting existed.
REPLAY_AGENT_WALL_CLOCK_S = 120.0

#: Live-model budget: the slower measured profile (openrouter-dev, p50 11.5 s
#: / p95 14.4 s per tool-calling turn on 2026-10-03) needs ~290 s for the
#: default 20-turn bound at p95, plus one observed ~39 s tail turn and auditor
#: calls, so 600 s leaves roughly 2x headroom while still bounding a stuck run.
LIVE_AGENT_WALL_CLOCK_S = 600.0


def agent_wall_clock_timeout_s(
    profile_name: str = "",
    profile_base_url: str = "",
    env: Optional[Mapping[str, str]] = None,
) -> float:
    """Resolve the agent loop wall-clock budget in seconds.

    Order: explicit ``SCOPEWATCH_AGENT_WALL_CLOCK_S`` override, then the live
    default for any non-mock profile, then the scripted-replay default.

    ``profile_name`` / ``profile_base_url`` describe the agent model in use, so
    the budget follows the model rather than the gateway. An unknown profile
    (no name, or a test double with no profile) is treated as scripted replay
    and gets the short budget.
    """
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


def agent_profile_name(env: dict[str, str] | None = None) -> str:
    """Active agent provider profile (default: mock; never hard-code models)."""
    src = env if env is not None else os.environ
    return str(src.get("SCOPEWATCH_AGENT_PROFILE", "mock") or "mock")


def auditor_profile_name(env: dict[str, str] | None = None) -> str:
    """Active auditor provider profile (default: mock)."""
    src = env if env is not None else os.environ
    return str(src.get("SCOPEWATCH_AUDITOR_PROFILE", "mock") or "mock")
