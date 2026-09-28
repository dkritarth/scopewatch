"""Configuration settings for Scopewatch baseline."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_DIR = BASE_DIR / "runtime-data"
DEFAULT_WORKSPACE_DIR = BASE_DIR / "demo" / "workspace"

DB_PATH = Path(os.environ.get("SCOPEWATCH_DB_PATH", str(DEFAULT_DATA_DIR / "scopewatch.db")))
WORKSPACE_ROOT = Path(os.environ.get("SCOPEWATCH_WORKSPACE_ROOT", str(DEFAULT_WORKSPACE_DIR)))
DEMO_REVIEWER_ID = "demo-reviewer (synthetic)"
DEFAULT_EXPIRY_SECONDS = int(os.environ.get("SCOPEWATCH_APPROVAL_TTL", "300"))
MAX_READ_BYTES = 256 * 1024  # 256 KiB
MAX_WRITE_BYTES = 64 * 1024  # 64 KiB

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


def agent_profile_name(env: dict[str, str] | None = None) -> str:
    """Active agent provider profile (default: mock; never hard-code models)."""
    src = env if env is not None else os.environ
    return str(src.get("SCOPEWATCH_AGENT_PROFILE", "mock") or "mock")


def auditor_profile_name(env: dict[str, str] | None = None) -> str:
    """Active auditor provider profile (default: mock)."""
    src = env if env is not None else os.environ
    return str(src.get("SCOPEWATCH_AUDITOR_PROFILE", "mock") or "mock")
