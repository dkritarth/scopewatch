"""Provider opt-in, request bounds and recorded replay checks. No network."""
import json

import httpx
import pytest

from test_runner import runner
from scopewatch.providers.profile import ProviderProfile


def profile():
    return ProviderProfile(name="synthetic", base_url="https://monitor.invalid/v1", model="fixture-model",
                           max_retries=9, extra_body={"n": 9, "best_of": 9, "messages": ["private"]},
                           auditor_body={"max_tokens": 99999}, reasoning={"enabled": True})


def test_provider_requires_consent_and_sends_only_bounded_allowlisted_request():
    from provider_monitor import ProviderMonitor, Caps
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"verdict":"NO_CONCERN"}'}}]})
    with pytest.raises(ValueError):
        ProviderMonitor(profile(), consent=False, transport=httpx.MockTransport(handler))
    with ProviderMonitor(profile(), consent=True, transport=httpx.MockTransport(handler),
                         caps=Caps(max_requests=2)) as monitor:
        report = runner.run(seeds=[17], monitor=monitor)
    assert len(seen) == 2
    for body in seen:
        assert set(body) == {"model", "messages", "max_tokens", "n", "temperature", "response_format", "seed"}
        assert body["model"] == "fixture-model"
        assert body["n"] == 1 and body["max_tokens"] == 128
        assert "private" not in json.dumps(body)
    assert report["summary"]["policy_reasoning"]["provider_calls"] == 1
    assert report["summary"]["policy_action_only"]["provider_calls"] == 1
    assert report["summary"]["policy_only"]["provider_calls"] == 0


def test_recorded_provider_artifact_checks_without_profile_credentials_or_inference(tmp_path, monkeypatch):
    from provider_monitor import ProviderMonitor, Caps
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"verdict":"CONCERN"}',
            "reasoning_content": "private response must not survive"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18,
                      "private": "must not survive"}})
    with ProviderMonitor(profile(), consent=True, caps=Caps(max_requests=2),
                         transport=httpx.MockTransport(handler)) as monitor:
        report = runner.run(seeds=[17], monitor=monitor)
    artifact = tmp_path / "provider.json"
    artifact.write_text(json.dumps(report))
    monkeypatch.setattr(ProviderMonitor, "__init__", lambda *a, **kw: pytest.fail("inference on check"))
    assert runner.check(artifact) == report
    assert "private response must not survive" not in artifact.read_text()
    assert "must not survive" not in artifact.read_text()
    assert report["provenance"]["reasoning"].startswith("SYNTHETIC_FIXTURE")
    summary = report["summary"]["policy_reasoning"]
    assert summary["provider_usage"]["observed"]["total_tokens"] == 18
    assert summary["provider_usage"]["estimated_reserved_tokens"] > 18


@pytest.mark.parametrize("mutation", ["input_hash", "action", "verdict", "denominator", "provenance",
                                     "usage", "extra_response", "invoked", "reserved", "cap", "policy"])
def test_rehashed_provider_fabrications_are_rejected(tmp_path, mutation):
    from provider_monitor import ProviderMonitor, Caps
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"verdict":"NO_CONCERN"}'}}]})
    with ProviderMonitor(profile(), consent=True, caps=Caps(max_requests=2),
                         transport=httpx.MockTransport(handler)) as monitor:
        report = runner.run(seeds=[17], monitor=monitor)
    step = next(s for r in report["runs"] for s in r["steps"] if s.get("provider") and s["provider"]["invoked"])
    if mutation == "input_hash": step["monitor_input_sha256"] = "0" * 64
    if mutation == "action": step["action"]["arguments"]["wall_ms"] = {"fabricated": True}
    if mutation == "verdict": step["provider"]["response"]["verdict"] = "CONCERN"
    if mutation == "denominator": report["summary"]["policy_only"]["task_replays"] += 1
    if mutation == "provenance": report["provenance"]["adapter_sha256"] = "0" * 64
    if mutation == "usage": step["provider"]["response"]["usage"]["total_tokens"] = -1
    if mutation == "extra_response": step["provider"]["response"]["raw_body"] = "fabrication"
    if mutation == "invoked": step["provider"]["invoked"] = False
    if mutation == "reserved": step["provider"]["reserved_tokens"] = 0
    if mutation == "cap": report["config"]["provider"]["caps"]["max_requests"] = 10000
    if mutation == "policy": step["policy_decision"] = "DENY"
    report["artifact_sha256"] = runner.artifact_hash(report)
    artifact = tmp_path / "forged.json"
    artifact.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        runner.check(artifact)


@pytest.mark.parametrize("failure", ["timeout", "transport", "http", "json", "malformed", "usage", "overspend", "tools", "oversize", "inconsistent", "json_list", "json_null", "input_overspend", "total_overspend"])
def test_provider_failures_hold_without_retries_or_private_output(failure, tmp_path):
    from provider_monitor import ProviderMonitor, Caps
    requests = []
    def handler(request):
        requests.append(request)
        if failure == "timeout": raise httpx.ReadTimeout("private-key /private/path")
        if failure == "transport": raise httpx.ConnectError("private-key /private/path")
        if failure == "http": return httpx.Response(503, text="private-key /private/path")
        if failure == "json_list": return httpx.Response(200, json=["private-key"])
        if failure == "json_null": return httpx.Response(200, content="null")
        if failure == "json": return httpx.Response(200, text="private-key /private/path")
        message = {"content": '{"verdict":"NO_CONCERN"}'}
        usage = {}
        if failure == "malformed": message["content"] = '{"verdict":"ALLOW","secret":"private-key"}'
        if failure == "usage": usage = {"total_tokens": "private-key"}
        if failure == "overspend": usage = {"completion_tokens": 129}
        if failure == "input_overspend": usage = {"prompt_tokens": 100000}
        if failure == "total_overspend": usage = {"total_tokens": 100000}
        if failure == "tools": message["tool_calls"] = [{"private-key": "/private/path"}]
        if failure == "oversize": message["content"] += " " * 256
        if failure == "inconsistent": usage = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 99}
        return httpx.Response(200, json={"choices": [{"message": message}], "usage": usage})
    with ProviderMonitor(profile(), consent=True, caps=Caps(max_requests=2),
                         transport=httpx.MockTransport(handler)) as monitor:
        report = runner.run(seeds=[17], monitor=monitor)
    assert len(requests) == 2
    invoked = [s for r in report["runs"] for s in r["steps"] if s.get("provider") and s["provider"]["invoked"]]
    assert len(invoked) == 2
    assert all(s["decision"] == "HOLD" and s["monitor_status"] == "FAILED" for s in invoked)
    assert "private-key" not in json.dumps(report)
    assert "/private/path" not in json.dumps(report)
    artifact = tmp_path / "failed.json"
    artifact.write_text(json.dumps(report))
    runner.check(artifact)


@pytest.mark.parametrize("limits", [{"max_input_bytes": 1}, {"max_estimated_tokens": 2}])
def test_preflight_limits_prevent_all_requests_and_preserve_null_usage(limits):
    from provider_monitor import ProviderMonitor, Caps
    def handler(request):
        pytest.fail("request must be rejected before transport")
    with ProviderMonitor(profile(), consent=True, caps=Caps(**limits),
                         transport=httpx.MockTransport(handler)) as monitor:
        report = runner.run(seeds=[17], monitor=monitor)
    for arm in runner.ARMS:
        assert report["summary"][arm]["provider_calls"] == 0
        assert report["summary"][arm]["provider_usage"]["observed"]["total_tokens"] is None


def test_full_outbound_evidence_isolation_policy_precedence_and_paired_requests():
    from provider_monitor import ProviderMonitor
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"verdict":"NO_CONCERN"}'}}]})
    with ProviderMonitor(profile(), consent=True, transport=httpx.MockTransport(handler)) as monitor:
        report = runner.run(seeds=[17], monitor=monitor)
    requests = iter(bodies)
    for row in report["runs"]:
        assert len(row["steps"]) <= 3
        for step in row["steps"]:
            if step.get("provider") and step["provider"]["invoked"]:
                assert step["policy_decision"] == "ALLOW" and row["arm"] != "policy_only"
                body = next(requests)
                assert body["seed"] == row["seed"]
                evidence = json.loads(body["messages"][1]["content"])
                expected_keys = {"task", "action", "history"}
                if row["arm"] == "policy_reasoning": expected_keys.add("reasoning")
                assert set(evidence) == expected_keys
                assert all(set(h) == {"action", "decision"} for h in evidence["history"])
                if row["arm"] == "policy_action_only":
                    fixture = next(t for t in runner.load_dataset()["tasks"] if t["id"] == row["task_id"])
                    assert all(s["reasoning"] not in json.dumps(body) for s in fixture["steps"])
    assert next(requests, None) is None
    assert len(bodies) == 136  # All policy-allowed steps; n=1, no retries.


def test_cli_never_activates_from_environment_or_partial_consent(tmp_path, monkeypatch):
    import subprocess
    import sys
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_PROFILE", "ambient-private-profile")
    monkeypatch.setenv("SCOPEWATCH_PROVIDERS_CONFIG", str(tmp_path / "absent-private-profile.toml"))
    command = [sys.executable, str(runner.ROOT / "runner.py")]
    artifact = tmp_path / "offline.json"
    result = subprocess.run(command + ["--output", str(artifact), "--seeds", "17"], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(artifact.read_text())["mode"] == "offline_synthetic_replay"
    for flags in (["--profile", "ambient-private-profile"], ["--consent-provider-calls"],
                  ["--profiles", str(tmp_path / "absent-private-profile.toml")],
                  ["--profile", "ambient-private-profile", "--profiles", str(tmp_path / "absent-private-profile.toml")]):
        result = subprocess.run(command + ["--output", str(artifact)] + flags, capture_output=True, text=True)
        assert result.returncode == 1
        assert "ambient-private-profile" not in result.stderr
        assert str(tmp_path) not in result.stderr
    # Complete opt-in but a missing configuration is also sanitized, no traceback.
    result = subprocess.run(command + ["--output", str(artifact), "--profile", "ambient-private-profile",
        "--profiles", str(tmp_path / "absent-private-profile.toml"), "--consent-provider-calls"], capture_output=True, text=True)
    assert result.returncode == 1 and "Traceback" not in result.stderr
    assert str(tmp_path) not in result.stderr
    result = subprocess.run(command + ["--check", str(artifact), "--consent-provider-calls"], capture_output=True, text=True)
    assert result.returncode == 1


@pytest.mark.parametrize("limits", [{"max_requests": 0}, {"max_requests": 3}, {"max_requests": 434},
    {"max_estimated_tokens": 2_000_002}, {"max_input_bytes": 32769}, {"max_completion_tokens": 513},
    {"timeout_s": 0.0}, {"timeout_s": 31.0}, {"timeout_s": float("nan")}, {"max_requests": True}])
def test_invalid_caps_are_rejected(limits):
    from provider_monitor import Caps
    with pytest.raises(ValueError):
        Caps(**limits)


def test_timeout_parameter_and_late_response_fail_closed(monkeypatch):
    from provider_monitor import ProviderMonitor, Caps
    import scopewatch.providers.client as client_module
    clock = [100.0]
    monkeypatch.setattr(client_module.time, "monotonic", lambda: clock[0])
    seen = []
    def handler(request):
        seen.append(request)
        assert 0 < request.extensions["timeout"]["read"] <= 0.5
        clock[0] += 1.0
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"verdict":"NO_CONCERN"}'}}]})
    with ProviderMonitor(profile(), consent=True, caps=Caps(max_requests=2, timeout_s=0.5),
                         transport=httpx.MockTransport(handler)) as monitor:
        report = runner.run(seeds=[17], monitor=monitor)
    assert len(seen) == 2
    invoked = [s for r in report["runs"] for s in r["steps"] if s.get("provider") and s["provider"]["invoked"]]
    assert all(s["decision"] == "HOLD" and s["provider"]["response"]["error"] == "TIMEOUT" for s in invoked)


def test_explicit_cli_provider_run_and_check_with_mock_transport(tmp_path, monkeypatch, capsys):
    from provider_monitor import ProviderMonitor
    import sys
    config = tmp_path / "synthetic.toml"
    config.write_text('[profiles.fixture]\nbase_url="https://monitor.invalid/v1"\nmodel="fixture-model"\n')
    artifact = tmp_path / "provider.json"
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"verdict":"CONCERN"}'}}]})
    def create_monitor(profile, **kwargs):
        return ProviderMonitor(profile, transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(runner, "ProviderMonitor", create_monitor)
    monkeypatch.setattr(sys, "argv", ["runner.py", "--output", str(artifact), "--profile", "fixture",
        "--profiles", str(config), "--consent-provider-calls", "--max-requests", "2", "--seeds", "17"])
    assert runner.main() == 0
    assert len(seen) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "provider_monitor_scripted_replay"
    assert set(output) == {"mode", "task_replays", "artifact_sha256", "checked"}
    monkeypatch.setattr(runner, "ProviderMonitor", lambda *a, **kw: pytest.fail("provider activation on check"))
    config.unlink()
    monkeypatch.setattr(sys, "argv", ["runner.py", "--check", str(artifact)])
    assert runner.main() == 0
    assert json.loads(capsys.readouterr().out)["checked"] is True
    assert len(seen) == 2


def test_cumulative_estimated_allowance_is_reserved_before_calls_and_equal_per_arm():
    from provider_monitor import ProviderMonitor, Caps
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"verdict":"NO_CONCERN"}'}}]})
    with ProviderMonitor(profile(), consent=True, caps=Caps(max_estimated_tokens=2000),
                         transport=httpx.MockTransport(handler)) as monitor:
        evidence = {"task": "synthetic", "action": {}, "history": []}
        for arm in ("policy_reasoning", "policy_action_only"):
            first = monitor.observe(evidence, arm, 17)
            second = monitor.observe(evidence, arm, 17)
            assert first["invoked"] is True
            assert second["invoked"] is False
            assert second["reserved_tokens"] == 0
            assert second["response"]["error"] == "BUDGET"
        assert len(seen) == 2
        assert sum(monitor.reserved.values()) <= 2000
