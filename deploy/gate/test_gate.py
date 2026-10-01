"""Tests for the demo access gate (deploy/gate/gate.py).

Policy tests use a deterministic clock and stubbed run counter. The proxy
contract test uses loopback-only HTTP servers with synthetic credentials.
"""

import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gate import (  # noqa: E402
    DemoGatePolicy,
    GateConfig,
    action_run_id,
    denial_body,
    is_mutating_api_call,
    is_run_creation,
    serve,
    token_matches,
)


def make_policy(now=1_000_000.0, busy_runs=0, **overrides):
    env = {
        "DEMO_TOKEN": "test-token",
        "DEMO_MAX_CONCURRENT_RUNS": "5",
        "DEMO_MAX_TURNS_PER_RUN": "3",
        "DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP": "2",
        "DEMO_MAX_RUNS_PER_DAY": "4",
        "DEMO_MAX_ACTIONS_PER_DAY": "10",
    }
    env.update(overrides)
    config = GateConfig.from_env(env)
    clock = {"t": now}
    policy = DemoGatePolicy(
        config,
        now=lambda: clock["t"],
        count_busy_runs=lambda: busy_runs,
    )
    return policy, clock


def headers(token="test-token", ip="203.0.113.7"):
    return {"X-Demo-Token": token, "X-Forwarded-For": ip}


class RecordingUpstream(BaseHTTPRequestHandler):
    received_headers: dict[str, str] = {}

    def do_POST(self) -> None:  # noqa: N802
        type(self).received_headers = dict(self.headers.items())
        body = json.dumps({"accepted": True}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def start_server(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


# -- routing helpers -------------------------------------------------------

def test_mutating_api_calls_require_token_but_reads_do_not():
    assert is_mutating_api_call("POST", "/api/v1/runs")
    assert is_mutating_api_call("POST", "/api/v1/demo/reset")
    assert is_mutating_api_call("DELETE", "/api/v1/runs/abc")
    assert not is_mutating_api_call("GET", "/api/v1/health")
    assert not is_mutating_api_call("GET", "/api/v1/runs")
    assert not is_mutating_api_call("GET", "/")
    assert not is_mutating_api_call("POST", "/healthz")
    # Non-API mutating paths are not gateway calls; gate passes them through.
    assert not is_mutating_api_call("POST", "/something-else")


def test_run_creation_and_action_routing():
    assert is_run_creation("POST", "/api/v1/runs")
    assert is_run_creation("POST", "/api/v1/runs?x=1")
    assert not is_run_creation("GET", "/api/v1/runs")
    assert not is_run_creation("POST", "/api/v1/runs/abc/complete")
    assert action_run_id("POST", "/api/v1/runs/abc-123/actions") == "abc-123"
    assert action_run_id("POST", "/api/v1/runs/abc-123/actions?x=1") == "abc-123"
    assert action_run_id("GET", "/api/v1/runs/abc-123/actions") is None
    assert action_run_id("POST", "/api/v1/runs") is None


def test_token_compare_is_constant_time_and_strict():
    assert token_matches("test-token", "test-token")
    assert not token_matches("wrong", "test-token")
    assert not token_matches(None, "test-token")
    assert not token_matches("", "test-token")
    assert not token_matches("test-token", "")
    assert token_matches("  test-token  ", "test-token")  # stripped


# -- token enforcement -----------------------------------------------------

def test_public_reads_pass_without_token():
    policy, _ = make_policy()
    for method, path in [
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/runs"),
        ("GET", "/"),
        ("GET", "/api/v1/runs/abc/events"),
    ]:
        decision = policy.decide(method, path, {}, "10.0.0.1")
        assert decision.allowed, (method, path)


def test_run_creation_without_token_is_401():
    policy, _ = make_policy()
    decision = policy.decide("POST", "/api/v1/runs", {}, "10.0.0.1")
    assert not decision.allowed
    assert decision.status == 401
    assert decision.error_code == "demo_token_required"
    body = denial_body(decision)
    assert b"X-Demo-Token" in body


def test_run_creation_with_token_is_allowed_and_counted():
    policy, _ = make_policy()
    decision = policy.decide("POST", "/api/v1/runs", headers(), "10.0.0.1")
    assert decision.allowed
    assert policy.snapshot()["runs_today"] == 1


def test_missing_demo_token_fails_closed_with_503():
    config = GateConfig.from_env({"DEMO_TOKEN": ""})
    policy = DemoGatePolicy(config, now=lambda: 1_000_000.0,
                            count_busy_runs=lambda: 0)
    decision = policy.decide("POST", "/api/v1/runs",
                             {"X-Demo-Token": "anything"}, "10.0.0.1")
    assert not decision.allowed
    assert decision.status == 503
    # Reads still pass even when misconfigured.
    assert policy.decide("GET", "/api/v1/health", {}, "10.0.0.1").allowed


# -- rate limiting ---------------------------------------------------------

def test_run_creation_rate_limited_per_ip():
    policy, clock = make_policy()
    assert policy.decide("POST", "/api/v1/runs", headers(ip="10.1.1.1"), "x").allowed
    assert policy.decide("POST", "/api/v1/runs", headers(ip="10.1.1.1"), "x").allowed
    denied = policy.decide("POST", "/api/v1/runs", headers(ip="10.1.1.1"), "x")
    assert not denied.allowed
    assert denied.status == 429
    assert denied.error_code == "rate_limited"
    assert denied.retry_after_s > 0
    # A different IP still has quota.
    assert policy.decide("POST", "/api/v1/runs", headers(ip="10.2.2.2"), "x").allowed
    # Window slides: after 61s the first IP is allowed again.
    clock["t"] += 61.0
    assert policy.decide("POST", "/api/v1/runs", headers(ip="10.1.1.1"), "x").allowed


# -- concurrency cap -------------------------------------------------------

def test_concurrent_run_cap_refuses_new_runs():
    policy, _ = make_policy(busy_runs=5)
    decision = policy.decide("POST", "/api/v1/runs", headers(), "10.0.0.1")
    assert not decision.allowed
    assert decision.status == 429
    assert decision.error_code == "concurrent_cap"


def test_unreachable_upstream_fails_closed_for_new_runs():
    config = GateConfig.from_env({"DEMO_TOKEN": "t"})
    policy = DemoGatePolicy(config, now=lambda: 1.0,
                            count_busy_runs=lambda: (_ for _ in ()).throw(IOError("down")))
    # count_busy_runs raising -> falls back to real query which also fails
    # (no gateway at default upstream in unit tests) -> 503.
    decision = policy.decide("POST", "/api/v1/runs", {"X-Demo-Token": "t"}, "1.1.1.1")
    assert not decision.allowed
    assert decision.status == 503


# -- turn cap --------------------------------------------------------------

def test_turn_cap_per_run():
    policy, _ = make_policy()
    path = "/api/v1/runs/run-1/actions"
    for _ in range(3):
        assert policy.decide("POST", path, headers(), "10.0.0.1").allowed
    capped = policy.decide("POST", path, headers(), "10.0.0.1")
    assert not capped.allowed
    assert capped.status == 429
    assert capped.error_code == "turn_cap"
    # Other runs are unaffected.
    assert policy.decide("POST", "/api/v1/runs/run-2/actions",
                         headers(), "10.0.0.1").allowed


# -- daily budgets ---------------------------------------------------------

def test_daily_run_budget_exhaustion_has_clear_message():
    policy, _ = make_policy(**{"DEMO_MAX_RUNS_PER_DAY": "1"})
    assert policy.decide("POST", "/api/v1/runs", headers(ip="10.0.0.1"), "x").allowed
    clock_ip = "10.9.9.9"  # fresh IP so the rate limiter is not the cause
    exhausted = policy.decide("POST", "/api/v1/runs", headers(ip=clock_ip), "x")
    assert not exhausted.allowed
    assert exhausted.status == 429
    assert exhausted.error_code == "budget_exhausted"
    assert "budget exhausted" in exhausted.message.lower()


def test_daily_action_budget_exhaustion():
    policy, _ = make_policy(**{"DEMO_MAX_ACTIONS_PER_DAY": "2"})
    path = "/api/v1/runs/run-9/actions"
    assert policy.decide("POST", path, headers(), "10.0.0.1").allowed
    assert policy.decide("POST", path, headers(), "10.0.0.1").allowed
    exhausted = policy.decide("POST", path, headers(), "10.0.0.1")
    assert not exhausted.allowed
    assert exhausted.error_code == "budget_exhausted"


def test_budgets_reset_on_utc_day_rollover():
    policy, clock = make_policy(**{"DEMO_MAX_RUNS_PER_DAY": "1"})
    assert policy.decide("POST", "/api/v1/runs", headers(), "10.0.0.1").allowed
    assert not policy.decide("POST", "/api/v1/runs",
                             headers(ip="10.5.5.5"), "x").allowed
    clock["t"] += 24 * 3600 + 5  # next UTC day
    assert policy.decide("POST", "/api/v1/runs",
                         headers(ip="10.5.5.5"), "x").allowed


def test_other_mutating_calls_need_token_only():
    policy, _ = make_policy()
    denied = policy.decide("POST", "/api/v1/approvals/x/approve", {}, "10.0.0.1")
    assert not denied.allowed and denied.status == 401
    allowed = policy.decide("POST", "/api/v1/approvals/x/approve", headers(), "10.0.0.1")
    assert allowed.allowed


def test_authenticated_mutation_forwards_demo_token_to_gateway():
    RecordingUpstream.received_headers = {}
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingUpstream)
    upstream_thread = start_server(upstream)
    upstream_url = f"http://127.0.0.1:{upstream.server_port}"

    config = GateConfig.from_env({
        "GATE_UPSTREAM": upstream_url,
        "GATE_PORT": "0",
        "DEMO_TOKEN": "test-token",
    })
    policy = DemoGatePolicy(config, count_busy_runs=lambda: 0)
    gate = serve(config, policy)
    gate_thread = start_server(gate)

    request = urllib.request.Request(
        f"http://127.0.0.1:{gate.server_port}/api/v1/runs",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "X-Demo-Token": "test-token"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 201
        assert RecordingUpstream.received_headers["X-Demo-Token"] == "test-token"
    finally:
        gate.shutdown()
        upstream.shutdown()
        gate.server_close()
        upstream.server_close()
        gate_thread.join(timeout=2)
        upstream_thread.join(timeout=2)
