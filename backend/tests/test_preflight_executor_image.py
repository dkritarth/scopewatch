"""Unit tests for preflight_executor_image() (issue #302 review)."""

import subprocess

import pytest

from scopewatch.executor_docker import preflight_executor_image


class FakeRunner:
    """A fake docker runner returning scripted results."""

    def __init__(self, results):
        self.results = list(results)
        self.commands = []

    def run(self, argv):
        self.commands.append(list(argv))
        value = self.results.pop(0)
        if isinstance(value, BaseException):
            raise value
        return subprocess.CompletedProcess(argv, value)

    def __call__(self, argv):
        return self.run(argv)


def test_preflight_success():
    runner = FakeRunner([0, 0])
    ok, message = preflight_executor_image("scopewatch-executor:ci", runner=runner)
    assert ok is True
    assert "ready" in message.lower()
    # probe command must carry the network isolation flags
    probe = runner.commands[1]
    assert "--network" in probe
    assert "none" in probe
    idx = probe.index("--network")
    assert probe[idx + 1] == "none"


def test_preflight_image_missing():
    runner = FakeRunner([1])
    ok, message = preflight_executor_image("scopewatch-executor:ci", runner=runner)
    assert ok is False
    assert "not available" in message.lower()


def test_preflight_pytest_import_failing():
    runner = FakeRunner([0, 1])
    ok, message = preflight_executor_image("scopewatch-executor:ci", runner=runner)
    assert ok is False
    assert "pytest" in message.lower()


def test_preflight_docker_binary_missing():
    runner = FakeRunner([FileNotFoundError("docker: not found")])
    ok, message = preflight_executor_image("scopewatch-executor:ci", runner=runner)
    assert ok is False
    assert "docker" in message.lower()
    assert "unavailable" in message.lower() or "failed" in message.lower()


def test_preflight_docker_binary_erroring():
    runner = FakeRunner([RuntimeError("boom")])
    ok, message = preflight_executor_image("scopewatch-executor:ci", runner=runner)
    assert ok is False
    assert "docker" in message.lower()


def test_preflight_docker_error_during_probe():
    runner = FakeRunner([0, OSError("probe failed")])
    ok, message = preflight_executor_image("scopewatch-executor:ci", runner=runner)
    assert ok is False
    assert "pytest" in message.lower() or "failed" in message.lower()


def test_preflight_accepts_runner_object():
    class ObjRunner:
        def __init__(self):
            self.calls = 0

        def run(self, argv):
            self.calls += 1
            return subprocess.CompletedProcess(argv, 0)

    runner = ObjRunner()
    ok, _ = preflight_executor_image("scopewatch-executor:ci", runner=runner)
    assert ok is True
    assert runner.calls == 2
