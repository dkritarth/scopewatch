"""Per-profile daily token metering (issue #77).

Counts model tokens per provider profile per UTC day (agent + auditor calls)
and refuses new work once a profile budget is exceeded. Spend is recorded at
the point of use; run creation checks the agent profile *before* any spend
(check-then-reserve), and each auditor call reserves an estimate before the
provider request and settles the actual usage afterwards.

Rules:

  * Budgets come from ``SCOPEWATCH_DAILY_TOKEN_BUDGET_<PROFILE>`` (profile
    name upper-cased, non-alphanumerics to ``_``), with
    ``SCOPEWATCH_TOKEN_BUDGET_DEFAULT`` as an optional fallback. Unset means
    unlimited. Malformed values fail closed to ``0`` (deny live runs) with a
    sanitized warning log.
  * The ``mock`` profile (or ``mock://`` base URLs) is scripted usage: it
    records 0 tokens, has no effective budget, and is never refused.
  * Only counts are stored/reported. Keys, prompts, traces, and response
    bodies are never logged or persisted here.
  * State is in-memory per process (thread-safe). When
    ``SCOPEWATCH_TOKEN_BUDGET_STORE`` points at a JSON file, counters are
    additionally persisted there (best-effort) so co-located processes (e.g.
    an agent loop and the gateway on the same demo host) share one ledger.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger("scopewatch.budget_store")

MOCK_PROFILE_NAME = "mock"

DEFAULT_RESERVE_PER_AUDIT = 8000
RESERVE_ENV = "SCOPEWATCH_TOKEN_RESERVE_PER_AUDIT"
DEFAULT_BUDGET_ENV = "SCOPEWATCH_TOKEN_BUDGET_DEFAULT"
STORE_ENV = "SCOPEWATCH_TOKEN_BUDGET_STORE"


def _sanitize_profile(profile: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (profile or "").upper()).strip("_")


def token_budget_env_name(profile: str) -> str:
    """Env var holding the daily token budget for a profile.

    Example: ``nebius-demo`` -> ``SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO``.
    """
    return f"SCOPEWATCH_DAILY_TOKEN_BUDGET_{_sanitize_profile(profile)}"


def is_mock_profile(name: str, base_url: str = "") -> bool:
    """Scripted usage is unaffected by token budgets (0 tokens, never refused)."""
    return (name or "").strip().lower() == MOCK_PROFILE_NAME or (
        base_url or ""
    ).startswith("mock://")


def daily_token_budget_for(profile: str, env: Mapping[str, str] | None = None) -> int | None:
    """Daily token budget for a profile, or None when unlimited.

    Malformed values fail closed to 0 (deny live runs) with a sanitized
    warning. The mock profile is always unlimited.
    """
    if is_mock_profile(profile):
        return None
    src = env if env is not None else os.environ
    raw = src.get(token_budget_env_name(profile))
    if raw is None or str(raw).strip() == "":
        raw = src.get(DEFAULT_BUDGET_ENV)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "Sanitized token-budget misconfiguration for profile '%s'; denying live runs.",
            profile,
        )
        return 0
    if value < 0:
        logger.warning(
            "Sanitized token-budget misconfiguration for profile '%s'; denying live runs.",
            profile,
        )
        return 0
    return value


def reserve_per_audit(env: Mapping[str, str] | None = None) -> int:
    """Estimate reserved before each auditor call (check-then-reserve)."""
    src = env if env is not None else os.environ
    try:
        return max(0, int(str(src.get(RESERVE_ENV, DEFAULT_RESERVE_PER_AUDIT))))
    except (TypeError, ValueError):
        return DEFAULT_RESERVE_PER_AUDIT


def total_tokens_from_usage(usage: Any) -> int:
    """Extract a token count from a provider ``usage`` payload (counts only)."""
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and total >= 0:
        return int(total)
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    if isinstance(prompt, (int, float)) and isinstance(completion, (int, float)):
        combined = int(prompt) + int(completion)
        return combined if combined >= 0 else 0
    return 0


def seconds_until_utc_midnight(now_s: float | None = None) -> float:
    now = now_s if now_s is not None else time.time()
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    midnight = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc) + timedelta(days=1)
    return max(1.0, (midnight - dt).total_seconds())


def format_rollover(reset_in_s: float) -> str:
    hours = int(reset_in_s // 3600)
    minutes = int((reset_in_s % 3600) // 60)
    if hours > 0:
        return f"in ~{hours}h {minutes}m"
    return f"in ~{max(1, minutes)}m"


@dataclass
class TokenDecision:
    allowed: bool
    used: int = 0
    budget: int | None = None
    reset_in_s: float = 0.0


_UNSET: Any = object()


class TokenLedger:
    """Thread-safe per-profile per-UTC-day token ledger.

    ``now`` is injectable for deterministic tests. ``store_path`` enables
    best-effort JSON persistence shared by co-located processes.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        store_path: str | Path | None = None,
    ) -> None:
        self._now = now or time.time
        self._store_path = Path(store_path) if store_path else None
        self._lock = threading.Lock()
        self._day: str = self._today()
        self._used: dict[str, int] = {}
        self._load_store()

    # -- time/day ----------------------------------------------------------

    def _today(self) -> str:
        return datetime.fromtimestamp(self._now(), tz=timezone.utc).strftime("%Y-%m-%d")

    def _rollover_locked(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._used = {}
            self._save_store_locked()

    # -- persistence (best-effort, counts only) -----------------------------

    def _load_store(self) -> None:
        if not self._store_path:
            return
        try:
            if not self._store_path.is_file():
                return
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
            if (
                isinstance(payload, dict)
                and payload.get("day") == self._day
                and isinstance(payload.get("used"), dict)
            ):
                self._used = {
                    str(k): max(0, int(v)) for k, v in payload["used"].items()
                }
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Sanitized token-store load failure [%s]", type(exc).__name__)

    def _save_store_locked(self) -> None:
        if not self._store_path:
            return
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store_path.with_suffix(self._store_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"day": self._day, "used": self._used}), encoding="utf-8"
            )
            tmp.replace(self._store_path)
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Sanitized token-store save failure [%s]", type(exc).__name__)

    # -- accounting ----------------------------------------------------------

    def used_today(self, profile: str) -> int:
        if is_mock_profile(profile):
            return 0
        with self._lock:
            self._rollover_locked()
            return self._used.get(profile, 0)

    def reset_in_seconds(self) -> float:
        return seconds_until_utc_midnight(self._now())

    def check(
        self,
        profile: str,
        estimate: int = 0,
        budget: int | None | Any = _UNSET,
    ) -> TokenDecision:
        """Check whether ``estimate`` more tokens fit in today's budget."""
        if is_mock_profile(profile):
            return TokenDecision(allowed=True, used=0, budget=None)
        eff_budget = daily_token_budget_for(profile) if budget is _UNSET else budget
        with self._lock:
            self._rollover_locked()
            used = self._used.get(profile, 0)
        if eff_budget is None:
            return TokenDecision(allowed=True, used=used, budget=None)
        return TokenDecision(
            allowed=used + max(0, estimate) <= eff_budget,
            used=used,
            budget=eff_budget,
            reset_in_s=self.reset_in_seconds(),
        )

    def add(self, profile: str, tokens: int) -> int:
        """Record spend (negative values release a prior reservation)."""
        if is_mock_profile(profile):
            return 0
        with self._lock:
            self._rollover_locked()
            updated = max(0, self._used.get(profile, 0) + int(tokens))
            self._used[profile] = updated
            self._save_store_locked()
            return updated

    def reset(self) -> None:
        """Clear all counters (tests and operator resets)."""
        with self._lock:
            self._used = {}
            self._day = self._today()
            self._save_store_locked()


# --------------------------------------------------------------------------
# Process-wide singleton (shares state between provider hook and gateway)
# --------------------------------------------------------------------------

_ledger: TokenLedger | None = None
_ledger_lock = threading.Lock()


def get_ledger() -> TokenLedger:
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            store = os.environ.get(STORE_ENV, "").strip() or None
            _ledger = TokenLedger(store_path=store)
        return _ledger


def reset_ledger() -> None:
    """Reset the process ledger (tests)."""
    global _ledger
    with _ledger_lock:
        if _ledger is not None:
            _ledger.reset()
        else:
            _ledger = TokenLedger()


def record_completion(profile: str, usage: Any) -> int:
    """Record one provider completion (counts only; mock profiles ignored)."""
    if is_mock_profile(profile):
        return 0
    return get_ledger().add(profile, total_tokens_from_usage(usage))
