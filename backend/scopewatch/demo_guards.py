"""Gateway-native demo guards (issue #76).

Enforces the public-demo controls directly in the gateway so they hold for
every deployment, without the ``deploy/gate`` sidecar proxy:

  * Shared demo access token on every mutating ``/api/*`` request (401).
    ``GET``/``HEAD``/``OPTIONS`` (health, dashboard reads) stay public.
  * Per-IP sliding-window rate limit on run creation (429 + ``Retry-After``).
  * Cap on concurrently active runs (429).
  * Cap on turns (action submissions) per run (429).
  * Daily demo budgets: run creations + action submissions per UTC day (429).

Semantics intentionally mirror ``deploy/gate/gate.py`` so the sidecar gate
becomes redundant (kept only as a proxy): attempt counting, UTC-day
rollover, in-memory single-replica state that resets on restart.

Demo mode engages only when ``DEMO_TOKEN`` is set. Without it the gateway
behaves exactly as before (local development and the existing test suite).

Fail-closed behaviour (demo mode):

  * Missing/wrong token on mutating calls is denied (401/503), never proxied.
  * Malformed ``DEMO_*`` numerics mark the config invalid: new runs and new
    actions are refused (503 ``demo_guard_misconfigured``) while health and
    reads stay available.
  * Enforcement-query failures (e.g. the busy-run count cannot be read)
    refuse the creation instead of letting it through.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Runs in these gateway states count as "concurrent" for the cap.
BUSY_RUN_STATUSES = frozenset({"ACTIVE", "WAITING_FOR_APPROVAL"})


# --------------------------------------------------------------------------
# Configuration (same env names as deploy/.env.example)
# --------------------------------------------------------------------------

ENV_DEFAULTS: dict[str, int] = {
    "DEMO_MAX_CONCURRENT_RUNS": 5,
    "DEMO_MAX_TURNS_PER_RUN": 40,
    "DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP": 6,
    "DEMO_MAX_RUNS_PER_DAY": 200,
    "DEMO_MAX_ACTIONS_PER_DAY": 5000,
}

# Backwards-tolerant aliases accepted when the canonical name is unset.
ENV_ALIASES: dict[str, str] = {
    "DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP": "DEMO_RATE_LIMIT_PER_MIN",
    "DEMO_MAX_RUNS_PER_DAY": "DEMO_DAILY_RUN_BUDGET",
    "DEMO_MAX_ACTIONS_PER_DAY": "DEMO_DAILY_ACTION_BUDGET",
}


@dataclass
class DemoGuardConfig:
    """Demo-guard knobs snapshot from the environment."""

    enabled: bool = False  # True when DEMO_TOKEN is non-empty (demo mode).
    demo_token: str = ""
    max_concurrent_runs: int = ENV_DEFAULTS["DEMO_MAX_CONCURRENT_RUNS"]
    max_turns_per_run: int = ENV_DEFAULTS["DEMO_MAX_TURNS_PER_RUN"]
    rate_limit_runs_per_min_per_ip: int = ENV_DEFAULTS[
        "DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP"
    ]
    max_runs_per_day: int = ENV_DEFAULTS["DEMO_MAX_RUNS_PER_DAY"]
    max_actions_per_day: int = ENV_DEFAULTS["DEMO_MAX_ACTIONS_PER_DAY"]
    valid: bool = True  # False when a DEMO_* numeric is malformed (fail closed).
    error: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "DemoGuardConfig":
        src: dict[str, str] = dict(env) if env is not None else dict(os.environ)
        token = str(src.get("DEMO_TOKEN", "") or "")
        if not token.strip():
            return cls(enabled=False)

        values: dict[str, int] = {}
        for canonical, default in ENV_DEFAULTS.items():
            raw = src.get(canonical)
            if raw is None or str(raw).strip() == "":
                alias = ENV_ALIASES.get(canonical)
                raw = src.get(alias, "") if alias else ""
            if raw is None or str(raw).strip() == "":
                values[canonical] = default
                continue
            try:
                parsed = int(str(raw).strip())
            except (TypeError, ValueError):
                return cls(
                    enabled=True,
                    demo_token=token,
                    valid=False,
                    error=(
                        f"Demo guard misconfigured: {canonical}={str(raw).strip()!r} "
                        "is not an integer. New runs and actions are refused "
                        "until the operator fixes it."
                    ),
                )
            if parsed < 0:
                return cls(
                    enabled=True,
                    demo_token=token,
                    valid=False,
                    error=(
                        f"Demo guard misconfigured: {canonical}={parsed} "
                        "must be >= 0. New runs and actions are refused "
                        "until the operator fixes it."
                    ),
                )
            values[canonical] = parsed

        return cls(
            enabled=True,
            demo_token=token,
            max_concurrent_runs=values["DEMO_MAX_CONCURRENT_RUNS"],
            max_turns_per_run=values["DEMO_MAX_TURNS_PER_RUN"],
            rate_limit_runs_per_min_per_ip=values["DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP"],
            max_runs_per_day=values["DEMO_MAX_RUNS_PER_DAY"],
            max_actions_per_day=values["DEMO_MAX_ACTIONS_PER_DAY"],
            valid=True,
        )


# --------------------------------------------------------------------------
# Refusal type (rendered as flat {"error", "message"} JSON by app.py)
# --------------------------------------------------------------------------


class DemoGuardRefusal(Exception):
    """A denied demo-guard decision with an HTTP status and sanitized message."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        retry_after_s: int = 0,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after_s = retry_after_s


@dataclass
class Decision:
    allowed: bool
    status: int = 200
    code: str = ""
    message: str = ""
    retry_after_s: int = 0

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise DemoGuardRefusal(self.status, self.code, self.message, self.retry_after_s)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def is_mutating_api_call(method: str, path: str) -> bool:
    """Mutating gateway API calls require the demo token (demo mode only).

    Health, dashboard reads, and static assets stay public.
    """
    if not path.startswith("/api/"):
        return False
    return method.upper() in MUTATING_METHODS


def token_matches(provided: str | None, expected: str) -> bool:
    """Constant-time token comparison; never true when either side is empty."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.strip(), expected)


def token_fingerprint(token: str) -> str:
    """Non-reversible fingerprint for logs (never log the token itself)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def client_ip_from(headers: dict[str, str], peer: str) -> str:
    """Best-effort client IP: X-Forwarded-For first entry, else direct peer."""
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return peer or "unknown"


# --------------------------------------------------------------------------
# Policy (thread-safe, in-memory; ``now`` injectable for deterministic tests)
# --------------------------------------------------------------------------


@dataclass
class DemoGuardPolicy:
    """In-memory enforcement of demo rate/cap/budget limits.

    Attempt counting mirrors the deploy gate: a run creation that passes the
    token check consumes rate window + daily run budget; an action submission
    consumes the daily action budget + one run turn. State resets on restart
    (single replica, same as the gate).
    """

    config: DemoGuardConfig = field(default_factory=DemoGuardConfig)
    now: Callable[[], float] = field(default_factory=lambda: time.time)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._run_creations_by_ip: dict[str, deque[float]] = {}
        self._day: str = self._today()
        self._runs_today: int = 0
        self._actions_today: int = 0
        self._turns_by_run: dict[str, int] = {}

    # -- time/day helpers -------------------------------------------------

    def _today(self) -> str:
        return datetime.fromtimestamp(self.now(), tz=timezone.utc).strftime("%Y-%m-%d")

    def _rollover(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._runs_today = 0
            self._actions_today = 0

    # -- token gate ---------------------------------------------------------

    def check_token(self, provided: str | None) -> Decision:
        """Decide a mutating call on the demo token alone."""
        if not self.config.enabled:
            return Decision(allowed=True)
        if not self.config.demo_token:
            return Decision(
                allowed=False,
                status=503,
                code="demo_guard_misconfigured",
                message=(
                    "Demo access is not configured on this host "
                    "(DEMO_TOKEN unset). Please contact the demo operator."
                ),
            )
        if not token_matches(provided, self.config.demo_token):
            return Decision(
                allowed=False,
                status=401,
                code="demo_token_required",
                message=(
                    "Demo access token required. Send it as the X-Demo-Token "
                    "header (see the submission's testing instructions). "
                    "Health and dashboard reads stay public."
                ),
            )
        return Decision(allowed=True)

    def check_config_valid(self) -> Decision:
        """Fail closed on malformed DEMO_* numerics (deny, keep reads up)."""
        if self.config.enabled and not self.config.valid:
            return Decision(
                allowed=False,
                status=503,
                code="demo_guard_misconfigured",
                message=self.config.error or "Demo guards are misconfigured.",
            )
        return Decision(allowed=True)

    # -- run creation: rate limit + daily run budget (atomic reserve) -------

    def try_begin_run(self, ip: str | None) -> Decision:
        """Check and reserve one run creation (rate window + daily budget).

        ``ip`` may be None for non-HTTP callers: the per-IP rate limit is
        skipped but the daily budget still applies. The HTTP layer always
        passes an IP, so the public path is fully guarded.
        """
        if not self.config.enabled:
            return Decision(allowed=True)
        invalid = self.check_config_valid()
        if not invalid.allowed:
            return invalid
        now = self.now()
        with self._lock:
            self._rollover()
            if ip is not None:
                window = self._run_creations_by_ip.setdefault(ip, deque())
                while window and window[0] <= now - 60.0:
                    window.popleft()
                if len(window) >= self.config.rate_limit_runs_per_min_per_ip:
                    retry = max(1, int(window[0] + 60.0 - now))
                    return Decision(
                        allowed=False,
                        status=429,
                        code="rate_limited",
                        message=(
                            "Too many run creations from this address "
                            f"({self.config.rate_limit_runs_per_min_per_ip}/min). "
                            "Please wait and retry."
                        ),
                        retry_after_s=retry,
                    )
            if self._runs_today >= self.config.max_runs_per_day:
                return Decision(
                    allowed=False,
                    status=429,
                    code="budget_exhausted",
                    message=(
                        "Daily demo budget exhausted: no new agent runs until "
                        "the UTC day rolls over. Scripted (mock) scenarios do "
                        "not consume model budget."
                    ),
                )
            if ip is not None:
                self._run_creations_by_ip.setdefault(ip, deque()).append(now)
            self._runs_today += 1
        return Decision(allowed=True)

    def release_run(self) -> None:
        """Refund one daily-run reservation (creation failed after reserving)."""
        with self._lock:
            self._runs_today = max(0, self._runs_today - 1)

    # -- concurrency cap (DB count + in-flight reservations, service-held) ---

    def check_concurrent(self, busy: int) -> Decision:
        if not self.config.enabled:
            return Decision(allowed=True)
        invalid = self.check_config_valid()
        if not invalid.allowed:
            return invalid
        if busy >= self.config.max_concurrent_runs:
            return Decision(
                allowed=False,
                status=429,
                code="concurrent_cap",
                message=(
                    f"Demo is at capacity ({busy} active runs, cap "
                    f"{self.config.max_concurrent_runs}). Complete or fail "
                    "an old run, or wait for another judge to finish."
                ),
            )
        return Decision(allowed=True)

    # -- action submission: daily action budget + per-run turn cap ----------

    def try_action(self, run_id: str) -> Decision:
        """Check and reserve one action submission (budget + turn cap)."""
        if not self.config.enabled:
            return Decision(allowed=True)
        invalid = self.check_config_valid()
        if not invalid.allowed:
            return invalid
        with self._lock:
            self._rollover()
            if self._actions_today >= self.config.max_actions_per_day:
                return Decision(
                    allowed=False,
                    status=429,
                    code="budget_exhausted",
                    message=(
                        "Daily demo budget exhausted: no further agent actions "
                        "until the UTC day rolls over."
                    ),
                )
            used = self._turns_by_run.get(run_id, 0)
            if used >= self.config.max_turns_per_run:
                return Decision(
                    allowed=False,
                    status=429,
                    code="turn_cap",
                    message=(
                        f"Run turn cap reached ({used}/"
                        f"{self.config.max_turns_per_run} turns). Start a new "
                        "run for further exploration."
                    ),
                )
            self._turns_by_run[run_id] = used + 1
            self._actions_today += 1
        return Decision(allowed=True)

    def release_action(self, run_id: str) -> None:
        """Refund one action reservation (submission failed after reserving)."""
        with self._lock:
            self._actions_today = max(0, self._actions_today - 1)
            used = self._turns_by_run.get(run_id, 0)
            if used <= 1:
                self._turns_by_run.pop(run_id, None)
            else:
                self._turns_by_run[run_id] = used - 1

    # -- introspection -------------------------------------------------------

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "day": self._day,
                "runs_today": self._runs_today,
                "actions_today": self._actions_today,
                "tracked_runs": len(self._turns_by_run),
            }
