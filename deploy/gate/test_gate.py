"""Tests for the demo access gate (deploy/gate/gate.py).

Policy tests use a deterministic clock and stubbed run counter. Proxy contract
tests replace the standard-library transport and use synthetic credentials.
"""

import io
import sys
import urllib.request
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


class FakeUpstreamResponse:
    status = 201
    headers = {"Content-Type": "application/json", "Content-Length": "2"}

    def __init__(self):
        self._body = io.BytesIO(b"{}")

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def make_handler(request_headers):
    from gate import GateHandler

    handler = GateHandler.__new__(GateHandler)
    handler.command = "POST"
    handler.path = "/api/v1/runs"
    handler.headers = request_headers
    handler.client_address = ("203.0.113.7", 12345)
    handler.policy = make_policy()[0]
    handler.upstream = "http://gateway:8000"
    handler.timeout_s = 2
    handler.rfile = io.BytesIO()
    handler.wfile = io.BytesIO()
    handler.response_statuses = []
    handler.send_response = handler.response_statuses.append
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None
    return handler


def test_authenticated_mutation_forwards_demo_token_to_gateway(monkeypatch):
    forwarded = []

    def record_request(request, timeout):
        forwarded.append((request, timeout))
        return FakeUpstreamResponse()

    monkeypatch.setattr(urllib.request, "urlopen", record_request)
    handler = make_handler({
        "Content-Length": "0",
        "Content-Type": "application/json",
        "X-Demo-Token": "test-token",
    })

    handler._handle()

    assert handler.response_statuses == [201]
    assert len(forwarded) == 1
    assert forwarded[0][0].get_header("X-demo-token") == "test-token"


@pytest.mark.parametrize("token", [None, "wrong-token"], ids=["missing", "invalid"])
def test_unauthenticated_mutation_is_never_forwarded(monkeypatch, token):
    forwarded = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: forwarded.append(True),
    )
    request_headers = {"Content-Length": "0", "Content-Type": "application/json"}
    if token is not None:
        request_headers["X-Demo-Token"] = token
    handler = make_handler(request_headers)

    handler._handle()

    assert handler.response_statuses == [401]
    assert forwarded == []
