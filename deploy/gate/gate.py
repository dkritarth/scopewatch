"""Demo access gate for the Scopewatch hosted demo.

A tiny dependency-free (stdlib only) reverse proxy that sits between the
public reverse proxy (Caddy) and the gateway. It enforces the public-demo
controls required by issue #42 WITHOUT touching gateway code:

  * Shared demo access token on every mutating ``/api/*`` request (401
    without it). ``GET``/``HEAD`` (health, dashboard reads) stay public so
    judges can load the dashboard and health checks keep working.
  * Per-IP rate limit on run creation (429 + ``Retry-After``).
  * Cap on concurrently active runs (429).
  * Cap on turns (action submissions) per run (429).
  * Daily demo budget (run creations + action submissions) as a conservative
    cost proxy for the configured billable provider profile. When the
    deployment uses ``mock`` profiles, no model calls are made at all, so
    scripted scenarios keep working regardless of the budget.

The budget/turn accounting here is deliberately coarse: the gate cannot see
model token usage from HTTP alone. Exact per-profile token metering belongs
in the gateway itself (follow-up issue filed from #42). The gate refuses
with a CLEAR message that names the exhausted limit.

State is in-memory (single replica) and resets on restart; the runbook
documents this. Fail-closed: a missing ``DEMO_TOKEN`` denies ALL mutating
traffic, and enforcement-query failures return 503 instead of letting
traffic through.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

HOP_BY_HOP_REQUEST = frozenset(
    {"host", "connection", "keep-alive", "proxy-connection",
     "transfer-encoding", "upgrade", "trailer"}
)
HOP_BY_HOP_RESPONSE = frozenset(
    {"connection", "keep-alive", "proxy-connection",
     "transfer-encoding", "upgrade", "trailer"}
)

# Runs in these gateway states count as "concurrent" for the cap.
BUSY_RUN_STATUSES = frozenset({"ACTIVE", "WAITING_FOR_APPROVAL"})


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass
class GateConfig:
    """All knobs, read from the environment (see deploy/.env.example)."""

    upstream: str = "http://gateway:8000"
    listen_port: int = 8080
    demo_token: str = ""  # fail closed when empty
    # Active-run cap. 0 = no new runs allowed (maintenance mode).
    max_concurrent_runs: int = 5
    # Turns (POST .../actions) allowed per run.
    max_turns_per_run: int = 40
    # Run creations allowed per IP per minute.
    rate_limit_runs_per_min_per_ip: int = 6
    # Daily demo budgets (UTC day). Conservative proxy for model cost.
    max_runs_per_day: int = 200
    max_actions_per_day: int = 5000
    upstream_timeout_s: float = 30.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "GateConfig":
        src = env if env is not None else os.environ

        def _int(name: str, default: int) -> int:
            try:
                return max(0, int(str(src.get(name, default))))
            except (TypeError, ValueError):
                return default

        def _float(name: str, default: float) -> float:
            try:
                return max(1.0, float(src.get(name, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            upstream=str(src.get("GATE_UPSTREAM", "http://gateway:8000")).rstrip("/"),
            listen_port=_int("GATE_PORT", 8080),
            demo_token=str(src.get("DEMO_TOKEN", "")),
            max_concurrent_runs=_int("DEMO_MAX_CONCURRENT_RUNS", 5),
            max_turns_per_run=_int("DEMO_MAX_TURNS_PER_RUN", 40),
            rate_limit_runs_per_min_per_ip=_int(
                "DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP", 6
            ),
            max_runs_per_day=_int("DEMO_MAX_RUNS_PER_DAY", 200),
            max_actions_per_day=_int("DEMO_MAX_ACTIONS_PER_DAY", 5000),
            upstream_timeout_s=_float("GATE_UPSTREAM_TIMEOUT_S", 30.0),
        )


# --------------------------------------------------------------------------
# Decision model (pure logic; fully unit-testable without sockets)
# --------------------------------------------------------------------------

@dataclass
class Decision:
    allowed: bool
    status: int = 200
    error_code: str = ""
    message: str = ""
    retry_after_s: int = 0


def is_mutating_api_call(method: str, path: str) -> bool:
    """Mutating gateway API calls require the demo token.

    ``/healthz`` is served by the gate itself and never proxied.
    """
    if path == "/healthz" or path.startswith("/healthz?"):
        return False
    return method.upper() in MUTATING_METHODS and path.startswith("/api/")


def is_run_creation(method: str, path: str) -> bool:
    return method.upper() == "POST" and _norm(path) == "/api/v1/runs"


def action_run_id(method: str, path: str) -> str | None:
    """Extract ``{run_id}`` from ``POST /api/v1/runs/{run_id}/actions``."""
    if method.upper() != "POST":
        return None
    parts = _norm(path).split("/")
    # ['', 'api', 'v1', 'runs', '{run_id}', 'actions']
    if len(parts) == 6 and parts[1:4] == ["api", "v1", "runs"] and parts[5] == "actions":
        run_id = urllib.parse.unquote(parts[4])
        return run_id or None
    return None


def _norm(path: str) -> str:
    return "/" + urllib.parse.urlsplit(path).path.strip("/")


def token_matches(provided: str | None, expected: str) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.strip(), expected)


class DemoGatePolicy:
    """Thread-safe in-memory enforcement of demo limits.

    ``now`` and ``count_busy_runs`` are injectable for deterministic tests.
    """

    def __init__(
        self,
        config: GateConfig,
        *,
        now: "callable[[], float] | None" = None,
        count_busy_runs: "callable[[], int] | None" = None,
    ) -> None:
        self.config = config
        self._now = now or time.time
        self._count_busy_runs = count_busy_runs
        self._lock = threading.Lock()
        # Per-IP sliding window of run-creation timestamps.
        self._run_creations_by_ip: dict[str, deque[float]] = {}
        # Daily counters keyed by UTC date.
        self._day: str = self._today()
        self._runs_today: int = 0
        self._actions_today: int = 0
        # Turns consumed per run id (only what passed through this gate).
        self._turns_by_run: dict[str, int] = {}

    # -- helpers ---------------------------------------------------------

    def _today(self) -> str:
        return datetime.fromtimestamp(self._now(), tz=timezone.utc).strftime("%Y-%m-%d")

    def _rollover(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._runs_today = 0
            self._actions_today = 0

    @staticmethod
    def _client_ip(headers: dict[str, str], peer: str) -> str:
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or peer
        return peer

    # -- main entry point ------------------------------------------------

    def decide(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        peer: str,
    ) -> Decision:
        """Decide whether to proxy (allow) or refuse (deny with status)."""
        lowered = {k.lower(): v for k, v in headers.items()}
        if not is_mutating_api_call(method, path):
            return Decision(allowed=True)

        # 1. Demo token (fail closed when unconfigured or mismatched).
        if not token_matches(lowered.get("x-demo-token"), self.config.demo_token):
            if not self.config.demo_token:
                return Decision(
                    allowed=False,
                    status=503,
                    error_code="gate_misconfigured",
                    message=(
                        "Demo access is not configured on this host "
                        "(DEMO_TOKEN unset). Please contact the demo operator."
                    ),
                )
            return Decision(
                allowed=False,
                status=401,
                error_code="demo_token_required",
                message=(
                    "Demo access token required. Send it as the "
                    "X-Demo-Token header (see the submission's testing "
                    "instructions). Health and dashboard reads stay public."
                ),
            )

        # 2. Run creation: rate limit, concurrency cap, daily budget.
        if is_run_creation(method, path):
            return self._decide_run_creation(lowered, peer)

        # 3. Action submission: turn cap + daily action budget.
        run_id = action_run_id(method, path)
        if run_id is not None:
            return self._decide_action(run_id)

        # Other mutating calls (approvals, complete/fail, demo reset):
        # token already verified; proxy them.
        return Decision(allowed=True)

    def _decide_run_creation(self, lowered: dict[str, str], peer: str) -> Decision:
        ip = self._client_ip(lowered, peer)
        now = self._now()
        with self._lock:
            self._rollover()
            window = self._run_creations_by_ip.setdefault(ip, deque())
            while window and window[0] <= now - 60.0:
                window.popleft()
            if len(window) >= self.config.rate_limit_runs_per_min_per_ip:
                retry = max(1, int(window[0] + 60.0 - now))
                return Decision(
                    allowed=False,
                    status=429,
                    error_code="rate_limited",
                    message=(
                        f"Too many run creations from this address "
                        f"({self.config.rate_limit_runs_per_min_per_ip}/min). "
                        "Please wait and retry."
                    ),
                    retry_after_s=retry,
                )
            if self._runs_today >= self.config.max_runs_per_day:
                return Decision(
                    allowed=False,
                    status=429,
                    error_code="budget_exhausted",
                    message=(
                        "Daily demo budget exhausted: no new agent runs until "
                        "the UTC day rolls over. Scripted (mock) scenarios do "
                        "not consume model budget. If you are a judge, mention "
                        "this in your notes and retry tomorrow."
                    ),
                )
        # Concurrency check outside the lock (does network I/O).
        busy: int | None = None
        if self._count_busy_runs is not None:
            try:
                busy = self._count_busy_runs()
            except Exception:
                busy = None
        if busy is None:
            busy = self._query_busy_runs()
        if busy is None:
            return Decision(
                allowed=False,
                status=503,
                error_code="gateway_unreachable",
                message=(
                    "Gateway is temporarily unreachable, so the demo gate "
                    "cannot verify run capacity. Please retry shortly."
                ),
            )
        with self._lock:
            self._rollover()
            if busy >= self.config.max_concurrent_runs:
                return Decision(
                    allowed=False,
                    status=429,
                    error_code="concurrent_cap",
                    message=(
                        f"Demo is at capacity ({busy} active runs, cap "
                        f"{self.config.max_concurrent_runs}). Complete or fail "
                        "an old run, or wait for another judge to finish."
                    ),
                )
            # Re-check budget under the lock (another thread may have spent it).
            if self._runs_today >= self.config.max_runs_per_day:
                return Decision(
                    allowed=False,
                    status=429,
                    error_code="budget_exhausted",
                    message=(
                        "Daily demo budget exhausted: no new agent runs until "
                        "the UTC day rolls over."
                    ),
                )
            window = self._run_creations_by_ip.setdefault(ip, deque())
            window.append(now)
            self._runs_today += 1
        return Decision(allowed=True)

    def _decide_action(self, run_id: str) -> Decision:
        with self._lock:
            self._rollover()
            if self._actions_today >= self.config.max_actions_per_day:
                return Decision(
                    allowed=False,
                    status=429,
                    error_code="budget_exhausted",
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
                    error_code="turn_cap",
                    message=(
                        f"Run turn cap reached ({used}/"
                        f"{self.config.max_turns_per_run} turns). Start a new "
                        "run for further exploration."
                    ),
                )
            self._turns_by_run[run_id] = used + 1
            self._actions_today += 1
        return Decision(allowed=True)

    # -- upstream introspection ------------------------------------------

    def _query_busy_runs(self) -> int | None:
        """Count busy runs via the gateway (None = unreachable)."""
        url = self.config.upstream + "/api/v1/runs"
        try:
            with urllib.request.urlopen(url, timeout=self.config.upstream_timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        if not isinstance(payload, list):
            return None
        return sum(1 for r in payload if isinstance(r, dict) and r.get("status") in BUSY_RUN_STATUSES)

    # -- introspection for tests/operators --------------------------------

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "day": self._day,
                "runs_today": self._runs_today,
                "actions_today": self._actions_today,
                "tracked_runs": len(self._turns_by_run),
            }


# --------------------------------------------------------------------------
# Reverse-proxy HTTP plumbing (thin; all decisions come from DemoGatePolicy)
# --------------------------------------------------------------------------

def denial_body(decision: Decision) -> bytes:
    return json.dumps(
        {"detail": decision.message, "error_code": decision.error_code}
    ).encode("utf-8")


class GateHandler(BaseHTTPRequestHandler):
    policy: DemoGatePolicy  # set by serve()
    upstream: str = ""
    timeout_s: float = 30.0

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D102
        # Deliberately minimal: method, path, status. Never log tokens.
        print(f"gate: {self.command} {self.path.split('?')[0]} -> {fmt % args}")

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        if _norm(self.path) == "/healthz":
            body = json.dumps({"status": "ok", "service": "demo-gate"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        headers = {k: v for k, v in self.headers.items()}
        peer = self.client_address[0] if self.client_address else "unknown"
        decision = self.policy.decide(self.command, self.path, headers, peer)
        if not decision.allowed:
            body = denial_body(decision)
            self.send_response(decision.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if decision.retry_after_s:
                self.send_header("Retry-After", str(decision.retry_after_s))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        self._proxy(headers)

    # -- proxying ----------------------------------------------------------

    def _proxy(self, headers: dict[str, str]) -> None:
        length = 0
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else None

        target = self.upstream + self.path
        forward = {
            k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_REQUEST
        }
        # Never forward the demo token upstream: the gateway does not know it
        # (extra="forbid" schemas would reject unknown fields if echoed, and
        # secrets must not travel further than the gate).
        forward.pop("X-Demo-Token", None)
        forward.pop("x-demo-token", None)

        request = urllib.request.Request(target, data=body, method=self.command)
        for key, value in forward.items():
            request.add_header(key, value)
        try:
            upstream = urllib.request.urlopen(request, timeout=self.timeout_s)
        except urllib.error.HTTPError as exc:
            self._copy_error(exc)
            return
        except Exception:
            body = json.dumps(
                {"detail": "Gateway is temporarily unreachable. Please retry shortly.",
                 "error_code": "gateway_unreachable"}
            ).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        with upstream:
            self.send_response(upstream.status)
            passthrough = {
                k: v
                for k, v in upstream.headers.items()
                if k.lower() not in HOP_BY_HOP_RESPONSE
            }
            streaming = "content-length" not in {k.lower() for k in passthrough}
            for key, value in passthrough.items():
                self.send_header(key, value)
            # No content-length from upstream (e.g. SSE stream): delimit by
            # connection close so browsers render the event stream correctly.
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = upstream.read(8192)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    if streaming:
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break

    def _copy_error(self, exc: urllib.error.HTTPError) -> None:
        try:
            payload = exc.read()
        except Exception:
            payload = b'{"detail":"Upstream request failed."}'
        self.send_response(exc.code)
        for key, value in exc.headers.items():
            if key.lower() not in HOP_BY_HOP_RESPONSE and key.lower() != "content-length":
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


def serve(config: GateConfig, policy: DemoGatePolicy | None = None) -> ThreadingHTTPServer:
    """Build (but do not start) the gate server."""
    active = policy or DemoGatePolicy(config)

    class BoundHandler(GateHandler):
        pass

    BoundHandler.policy = active
    BoundHandler.upstream = config.upstream
    BoundHandler.timeout_s = config.upstream_timeout_s

    server = ThreadingHTTPServer(("0.0.0.0", config.listen_port), BoundHandler)
    server.daemon_threads = True
    return server


def main() -> None:
    config = GateConfig.from_env()
    if not config.demo_token:
        # Fail closed but stay up so /healthz reports and the operator sees
        # 503s with a clear message instead of silent refusal.
        print("gate: WARNING: DEMO_TOKEN is unset; all mutating API calls will get 503.")
    print(
        f"gate: listening on :{config.listen_port}, upstream={config.upstream}, "
        f"concurrent_cap={config.max_concurrent_runs}, "
        f"turn_cap={config.max_turns_per_run}, "
        f"rate={config.rate_limit_runs_per_min_per_ip}/min/ip, "
        f"daily_budget={config.max_runs_per_day} runs/"
        f"{config.max_actions_per_day} actions"
    )
    serve(config).serve_forever()


if __name__ == "__main__":
    main()
