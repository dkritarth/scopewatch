"""Shared fixtures for the backend test suite.

Backend-selection fixtures live here so Docker-gated suites share one
definition: `requires_docker` skips cleanly without a daemon,
`docker_backend` / `local_backend` select the executor under test.
"""

import pytest

from scopewatch.executor_docker import is_docker_available


@pytest.fixture
def requires_docker() -> None:
    if not is_docker_available():
        pytest.skip(
            "Docker daemon unavailable; skipping Docker integration test."
        )


@pytest.fixture
def docker_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "docker")


@pytest.fixture
def local_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOPEWATCH_EXECUTOR", raising=False)
