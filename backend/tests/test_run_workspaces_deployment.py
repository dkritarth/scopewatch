"""The run-workspace root must be writable in the real deployment layout (#117).

Two independent reviewers' worth of blocking defects on PR #163 came from one
root cause: the managed root was invented with a ``-runs`` **sibling** default
of the scenario fixture and then never wired into the deployment.

- B1: the gateway container runs ``read_only: true`` with volumes for ``/data``
  and ``/workspace`` only, so ``/workspace-runs`` can neither exist nor be
  created. ``docker-entrypoint.sh`` is ``set -euo pipefail`` and seeds on first
  start, so the container ABORTED, and every later ``create_run`` 503'd.
- B2: the runner resolved ``run_workspace`` under the FIXTURE volume while the
  gateway wrote run workspaces elsewhere, so every remote dispatch failed closed
  with 500 and the remote backend was 100% unavailable.

Nothing in CI caught either, because the suite runs as root-of-the-repo on a
normal filesystem and every Docker-boundary test mocks the docker CLI. So these
tests do two things a passing suite previously did not:

1. **Simulate the container layout for real.** A temporary "container root" is
   made read-only (mode 0555) with exactly the volumes compose declares as the
   only writable subdirectories, then the real ``RunWorkspaceManager`` is
   pointed at it. No root required: a read-only parent directory denies
   ``mkdir`` for an unprivileged uid exactly as it does inside the container.
2. **Read the shipped compose files** and assert the wiring, so the fix cannot
   be reverted in YAML while the Python side stays green.

The mutation checks that give these teeth are recorded in
``test_b1_read_only_root_rejects_the_old_sibling_default`` and
``test_b2_runner_root_equals_the_gateway_announced_root``: both construct the
broken layout explicitly and assert that it fails.

No Docker daemon was used. ``docker compose config`` on a real daemon is the
authoritative check for the YAML and was NOT run.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import re
import stat
from types import ModuleType

import pytest

from scopewatch.db import init_db
from scopewatch.schemas import TaskScope
from scopewatch.service import ScopewatchService
from scopewatch.workspaces import (
    RUN_WORKSPACES_DIRNAME,
    RUN_WORKSPACES_ENV_VAR,
    RUN_WORKSPACES_ROOT_UNAVAILABLE,
    RunWorkspaceError,
    RunWorkspaceManager,
    RunWorkspaceRootError,
    default_run_workspaces_root,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
RUNNER_COMPOSE = DEPLOY / "executor-runner" / "compose.executor-runner.yaml"
RUNBOOK = DEPLOY / "RUNBOOK.md"
ENV_EXAMPLE = DEPLOY / ".env.example"

# The gateway image's env (deploy/Dockerfile) and the sidecar's
# (deploy/executor-runner/Dockerfile). The in-image default matters because a
# compose file that forgets the variable should still not silently point at the
# fixture.
GATEWAY_FIXTURE_ROOT = "/workspace"
RUN_WORKSPACES_MOUNT = "/runs"

RUN_ID = "9544ecaf-1111-4222-8333-444444444444"
SIBLING_RUN_ID = "9544ecaf-1111-4222-8333-555555555555"


# --------------------------------------------------------------------------
# A simulated container root: read-only, with only compose's volumes writable
# --------------------------------------------------------------------------


def _seed_fixture(fixture: Path) -> Path:
    """A synthetic scenario fixture. Never real data."""
    (fixture / "invoices" / "approved").mkdir(parents=True)
    (fixture / "outputs").mkdir(parents=True)
    (fixture / "invoices" / "approved" / "vendor-a.txt").write_text(
        "INVOICE #INV-2026-001\nAmount: $4,500.00\n", encoding="utf-8"
    )
    (fixture / "outputs" / "archive.txt").write_text(
        "Legacy archived summary.\n", encoding="utf-8"
    )
    return fixture


def _container_root(tmp_path: Path, *, writable_volumes: tuple[str, ...]) -> Path:
    """Build a stand-in for the gateway container's filesystem.

    The returned directory is mode 0555 — read-only for this uid, which is what
    ``read_only: true`` gives the container — and every path inside it other
    than the named volumes is unwritable. Each writable volume is a real,
    writable directory standing in for a named volume mounted at that path.
    """
    root = tmp_path / "container"
    root.mkdir()
    _seed_fixture(root / "workspace")
    (root / "data").mkdir()
    for name in writable_volumes:
        (root / name.lstrip("/")).mkdir(exist_ok=True)
    root.chmod(stat.S_IRUSR | stat.S_IXUSR)  # 0555: read + traverse, no write
    return root


def _make_writable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)


@pytest.fixture
def writable_container(tmp_path: Path) -> Path:
    """A container root with the volumes the shipped compose declares."""
    root = _container_root(
        tmp_path, writable_volumes=("data", "workspace", RUN_WORKSPACES_MOUNT.lstrip("/"))
    )
    try:
        yield root
    finally:
        _make_writable(root)


RUNNER_PATH = DEPLOY / "executor-runner" / "runner.py"


def _load_runner() -> ModuleType:
    """Import the real sidecar module from its authored location.

    Same approach as the other runner-exercising tests: the file is not a
    package member, so it is loaded by path. Importing the REAL module matters
    here — these assertions are about the resolver's real refusal rules, and a
    local copy of them would only prove the copy is self-consistent.
    """
    spec = importlib.util.spec_from_file_location("scopewatch_runner_117_deploy", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _host_path(container: Path, container_path: str) -> Path:
    """Map an in-container absolute path onto the simulated root."""
    return container / container_path.lstrip("/")


def _scope() -> TaskScope:
    return TaskScope(
        task_description="Synthetic deployment-layout probe.",
        allowed_paths=["invoices", "outputs"],
        blocked_paths=[],
        allowed_tools=["workspace"],
        allowed_operations=["read_text"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _service(fixture: Path, db_path: Path | None = None) -> ScopewatchService:
    """A service over the simulated fixture, with no explicit run-workspaces root.

    No ``run_workspaces_root`` on purpose: the deployment path under test is the
    one where the root comes from the environment.
    """
    if db_path is None:
        # Under /data, not beside the fixture: the simulated container root is
        # read-only, and the DB lives in its own volume in the real deployment.
        db_path = fixture.parent / "data" / "scopewatch.db"
    init_db(db_path)
    return ScopewatchService(db_path=db_path, workspace_root=fixture)


# --------------------------------------------------------------------------
# Compose parsing (no YAML dependency; same approach as test_compose_layout.py)
# --------------------------------------------------------------------------


def _service_block(text: str, name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\s*$", text, re.MULTILINE)
    assert match, f"service {name!r} not found"
    rest = text[match.end():]
    end = re.search(r"^  \S", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def _env_value(block: str, key: str) -> str | None:
    """Read one ``environment:`` entry from a service block.

    Handles both ``KEY: value`` and ``KEY: ${VAR:-default}``. Comments are
    stripped so a value mentioned in a comment cannot be mistaken for the
    setting.
    """
    for line in block.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        match = re.match(rf"^\s+{re.escape(key)}:\s*(\S.*?)\s*$", stripped)
        if not match:
            continue
        value = match.group(1).strip().strip('"').strip("'")
        # Unwrap a ${VAR:-default} / ${VAR} substitution to its default.
        interpolation = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*(:-([^}]*))?\}", value)
        if interpolation:
            return (interpolation.group(2) or "").strip() or None
        return value
    return None


def _volume_targets(block: str) -> set[str]:
    """Container-side mount points from a service's ``volumes:`` list."""
    match = re.search(r"^    volumes:\s*$", block, re.MULTILINE)
    assert match, "service has no volumes: list"
    targets: set[str] = set()
    for line in block[match.end():].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue  # blank line or a comment inside the list
        entry = re.match(r"^      - (\S+?)\s*(?:#.*)?$", line)
        if not entry:
            break  # left the list (next key at 4-space indent)
        parts = entry.group(1).split(":")
        if len(parts) >= 2:
            targets.add(parts[1])
    return targets


def _named_volumes(text: str) -> set[str]:
    """Volume names declared in the top-level ``volumes:`` block."""
    match = re.search(r"^volumes:\s*$", text, re.MULTILINE)
    assert match, "compose declares no top-level volumes:"
    names: set[str] = set()
    for line in text[match.end():].splitlines():
        entry = re.match(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):\s*$", line)
        if entry:
            names.add(entry.group(1))
    return names


def _declared_volume_names(text: str) -> dict[str, str | None]:
    """Map volume key -> explicit ``name:`` override, if any."""
    match = re.search(r"^volumes:\s*$", text, re.MULTILINE)
    assert match
    result: dict[str, str | None] = {}
    current: str | None = None
    for line in text[match.end():].splitlines():
        entry = re.match(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):\s*$", line)
        if entry:
            current = entry.group(1)
            result[current] = None
            continue
        override = re.match(r"^\s+name:\s*(\S+)\s*$", line)
        if override and current:
            result[current] = override.group(1).strip().strip('"')
    return result


def _dockerfile_env(dockerfile: Path, key: str) -> str | None:
    text = dockerfile.read_text(encoding="utf-8")
    for match in re.finditer(r"\\\s*\n\s*([A-Z_]+)=", text):
        if match.group(1) == key:
            tail = text[match.end():].split("\\", 1)[0].strip()
            return tail.strip('"')
    for match in re.finditer(r"^ENV\s+([A-Z_]+)=(\S+)", text, re.MULTILINE):
        if match.group(1) == key:
            return match.group(2).strip().strip('"')
    return None


def _gateway_block() -> str:
    return _service_block(COMPOSE.read_text(encoding="utf-8"), "gateway")


def _runner_block() -> str:
    return _service_block(
        RUNNER_COMPOSE.read_text(encoding="utf-8"), "executor-runner"
    )


# --------------------------------------------------------------------------
# B1 — the gateway can actually create a run in the deployed layout
# --------------------------------------------------------------------------


def test_compose_gives_the_gateway_a_mounted_run_workspaces_volume() -> None:
    """B1: the run-workspaces root must be a real, writable volume."""
    block = _gateway_block()
    configured = _env_value(block, RUN_WORKSPACES_ENV_VAR)
    assert configured == RUN_WORKSPACES_MOUNT, (
        f"the gateway must set {RUN_WORKSPACES_ENV_VAR}={RUN_WORKSPACES_MOUNT}, "
        f"found {configured!r}. The gateway is read_only with volumes for /data, "
        "/workspace and /runs only, so any other path can neither exist nor be "
        "created: the entrypoint then aborts on first start."
    )
    targets = _volume_targets(block)
    assert RUN_WORKSPACES_MOUNT in targets, (
        f"nothing is mounted at {RUN_WORKSPACES_MOUNT}; the gateway service has "
        f"{sorted(targets)}"
    )


def test_compose_does_not_nest_run_workspaces_inside_the_fixture() -> None:
    """B1 follow-on: the fixture is the copy SOURCE.

    Run workspaces under ``/workspace`` would be copied into every later run's
    workspace, reopening exactly the cross-run visibility #117 closes.
    """
    configured = _env_value(_gateway_block(), RUN_WORKSPACES_ENV_VAR)
    assert configured is not None
    assert configured != GATEWAY_FIXTURE_ROOT
    assert not configured.startswith(GATEWAY_FIXTURE_ROOT + "/"), (
        f"{RUN_WORKSPACES_ENV_VAR}={configured} is inside the fixture volume; "
        "run workspaces nested in the copy source leak between runs"
    )


def test_gateway_image_declares_and_owns_the_run_workspaces_directory() -> None:
    """The mount point must exist in the image and be writable by appuser.

    Two things make this load-bearing rather than tidiness: the service is
    ``read_only: true``, so the path cannot be created at runtime; and Docker
    seeds an empty named volume from the image's directory, so without the
    ``chown`` the volume is root-owned and appuser cannot create a workspace.
    """
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert _dockerfile_env(DEPLOY / "Dockerfile", RUN_WORKSPACES_ENV_VAR) == (
        RUN_WORKSPACES_MOUNT
    )
    mkdir = re.search(r"mkdir -p ([^\n&|]+)", text)
    assert mkdir, f"the image must create {RUN_WORKSPACES_MOUNT} so the mount point exists"
    assert RUN_WORKSPACES_MOUNT in mkdir.group(1).split(), (
        f"the image must create {RUN_WORKSPACES_MOUNT} so the mount point exists; "
        f"it creates {mkdir.group(1).split()}"
    )
    chown = re.search(r"chown -R appuser:appuser ([^\n&|]+)", text)
    assert chown, "the image must chown its writable directories to appuser"
    assert RUN_WORKSPACES_MOUNT in chown.group(1).split(), (
        f"appuser must own {RUN_WORKSPACES_MOUNT}; without it the named volume is "
        "root-owned and every create_run 503s"
    )


def test_b1_create_run_works_in_a_read_only_container_root(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1, end to end: only the declared volumes are writable, and a run starts.

    Nothing is mocked here — the real ``RunWorkspaceManager`` runs against a real
    filesystem whose parent is read-only, which is the condition the container
    imposes. This is the check that was missing when the ``-runs`` sibling
    default shipped.
    """
    fixture = _host_path(writable_container, GATEWAY_FIXTURE_ROOT)
    runs = _host_path(writable_container, RUN_WORKSPACES_MOUNT)

    # The shipped configuration: the env var points at the declared volume.
    monkeypatch.setenv(RUN_WORKSPACES_ENV_VAR, str(runs))
    manager = RunWorkspaceManager(fixture)
    workspace = manager.initialize(RUN_ID)

    assert workspace.parent == runs, (
        f"run workspace landed at {workspace.parent}, not the declared volume"
    )
    # Baseline content copied; the managed root never absorbs the fixture.
    assert (workspace / "invoices" / "approved" / "vendor-a.txt").is_file()
    assert (workspace / "outputs" / "archive.txt").is_file()
    assert not (workspace / RUN_WORKSPACES_DIRNAME).exists()


def test_b1_default_root_stays_inside_a_writable_root(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The *default* (no env var) must also land somewhere writable.

    This is the guard on the default itself. A deployment that sets nothing must
    still work, and the value that ships must never be a path outside the
    fixture volume.
    """
    monkeypatch.delenv(RUN_WORKSPACES_ENV_VAR, raising=False)
    fixture = _host_path(writable_container, GATEWAY_FIXTURE_ROOT)

    default_root = default_run_workspaces_root(fixture)
    # Not a sibling of the fixture: siblings of /workspace do not exist in the
    # container, which is exactly how B1 shipped.
    assert default_root.parent == fixture, (
        f"the default root {default_root} is a sibling of the fixture; in the "
        "container that path can neither exist nor be created"
    )

    workspace = RunWorkspaceManager(fixture).initialize(RUN_ID)
    assert workspace.is_dir()
    assert fixture in workspace.parents


def test_b1_read_only_root_rejects_the_old_sibling_default(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION CHECK for the two tests above: the old ``-runs`` default fails.

    Reproduces the shipped bug rather than the fix: point the manager at the
    ``-runs`` sibling the first revision defaulted to and assert it cannot be
    created. If this test ever passes, the read-only simulation stopped being
    faithful and the two tests above would be vacuous.
    """
    fixture = _host_path(writable_container, GATEWAY_FIXTURE_ROOT)
    sibling = fixture.parent / f"{fixture.name}-runs"

    manager = RunWorkspaceManager(fixture, root=sibling)
    with pytest.raises(RunWorkspaceRootError):
        manager.initialize(RUN_ID)

    assert not sibling.exists(), (
        "the read-only parent was writable; the simulation is not faithful and "
        "test_b1_default_root_stays_inside_a_writable_root proves nothing"
    )


def test_unwritable_root_is_reported_as_configuration_not_a_lost_workspace(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad mount must not read like one run losing its workspace.

    The two need different fixes (a mount versus a lost directory), and the
    reviewer must be able to tell them apart from the code alone.
    """
    fixture = _host_path(writable_container, GATEWAY_FIXTURE_ROOT)
    monkeypatch.setenv(
        RUN_WORKSPACES_ENV_VAR, str(fixture.parent / "not-a-volume" / "runs")
    )
    with pytest.raises(RunWorkspaceError) as caught:
        RunWorkspaceManager(fixture).initialize(RUN_ID)

    assert caught.value.code == RUN_WORKSPACES_ROOT_UNAVAILABLE, (
        "an unwritable root must not be reported as a single run losing its "
        f"workspace; got {caught.value.code!r}"
    )
    # A RunWorkspaceRootError is also a RunWorkspaceError, so the service can
    # catch the specific case first without reordering the hierarchy.
    assert isinstance(caught.value, RunWorkspaceRootError)


def test_unwritable_root_message_names_the_env_var(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator-facing 503 must say which variable to set.

    The exception carries a code; the API message is what a reader actually
    sees in a failed ``create_run``. Without the variable named, the only clue
    is a code that means nothing on first encounter.
    """
    from scopewatch.errors import ScopewatchAPIError

    fixture = _host_path(writable_container, GATEWAY_FIXTURE_ROOT)
    monkeypatch.setenv(
        RUN_WORKSPACES_ENV_VAR, str(fixture.parent / "not-a-volume" / "runs")
    )
    with pytest.raises(ScopewatchAPIError) as caught:
        asyncio.run(
            _service(fixture).create_run(name="probe", task_scope=_scope())
        )
    assert caught.value.code == RUN_WORKSPACES_ROOT_UNAVAILABLE
    assert RUN_WORKSPACES_ENV_VAR in str(caught.value)


# --------------------------------------------------------------------------
# B2 — the remote runner can reach the gateway-announced workspace
# --------------------------------------------------------------------------


def test_b2_runner_root_equals_the_gateway_announced_root() -> None:
    """B2: one path, two containers, shared by a shared mount.

    The gateway announces ``run_workspace`` as a bare directory name; the runner
    resolves it under its own root. If those roots differ, no key can ever
    resolve and the remote backend is 100% unavailable — failing closed, which is
    correct, and still an outage.
    """
    gateway_root = _env_value(_gateway_block(), RUN_WORKSPACES_ENV_VAR)
    runner_root = _env_value(_runner_block(), "EXECUTOR_RUNNER_WORKSPACE")
    assert gateway_root == runner_root, (
        f"the gateway announces run workspaces under {gateway_root!r} but the "
        f"runner resolves them under {runner_root!r}; every remote dispatch "
        "fails closed with 500"
    )


def test_b2_runner_mounts_the_shared_run_workspaces_volume() -> None:
    """A path is only shared across containers through a shared mount."""
    gateway_targets = _volume_targets(_gateway_block())
    runner_targets = _volume_targets(_runner_block())
    configured = _env_value(_runner_block(), "EXECUTOR_RUNNER_WORKSPACE")
    assert configured in runner_targets, (
        f"the runner resolves run workspaces under {configured} but mounts "
        f"{sorted(runner_targets)}; the announced key cannot resolve"
    )
    shared = gateway_targets & runner_targets
    assert configured in shared, (
        f"{configured} is not shared between the gateway and the runner "
        f"(gateway mounts {sorted(gateway_targets)}, runner mounts "
        f"{sorted(runner_targets)})"
    )


def test_b2_both_compose_files_name_the_same_host_volume() -> None:
    """Two files declaring the same volume key must agree on its host name.

    Otherwise they resolve to two different host volumes and the shared mount is
    a fiction that still parses.
    """
    gateway_volumes = _declared_volume_names(COMPOSE.read_text(encoding="utf-8"))
    runner_volumes = _declared_volume_names(
        RUNNER_COMPOSE.read_text(encoding="utf-8")
    )
    shared = set(gateway_volumes) & set(runner_volumes)
    assert shared, (
        "the gateway and the runner declare no volume in common, so they cannot "
        "share run workspaces"
    )
    for key in sorted(shared):
        assert gateway_volumes[key] == runner_volumes[key], (
            f"volume {key!r} resolves to {gateway_volumes[key]!r} for the gateway "
            f"and {runner_volumes[key]!r} for the runner"
        )


def test_b2_runner_does_not_mount_the_pristine_fixture() -> None:
    """The socket-holding, root-running sidecar gets no write access to the fixture.

    It resolves per-run workspaces only, so the fixture mount buys nothing while
    risking exactly what #117 exists to prevent.
    """
    runner_targets = _volume_targets(_runner_block())
    assert GATEWAY_FIXTURE_ROOT not in runner_targets, (
        "the runner must not mount the scenario fixture: it holds the Docker "
        "socket and runs as root"
    )
    assert _env_value(_runner_block(), "EXECUTOR_RUNNER_WORKSPACE") != (
        GATEWAY_FIXTURE_ROOT
    )


def test_b2_announced_key_resolves_inside_the_runner_root(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2, end to end: the key the gateway sends resolves where the runner looks.

    No Docker and no HTTP: the two halves of the contract are each real, and the
    check is that they agree on one directory. The runner's own resolver is
    imported and used, so a change to its refusal rules shows up here.
    """
    resolve_run_workspace = _load_runner().resolve_run_workspace

    runs = _host_path(writable_container, RUN_WORKSPACES_MOUNT)
    fixture = _host_path(writable_container, GATEWAY_FIXTURE_ROOT)

    monkeypatch.setenv(RUN_WORKSPACES_ENV_VAR, str(runs))
    gateway_workspace = RunWorkspaceManager(fixture).initialize(RUN_ID)
    announced_key = RunWorkspaceManager(fixture).workspace_key(RUN_ID)

    # The runner's configured root, read from the shipped compose file.
    runner_root = Path(_env_value(_runner_block(), "EXECUTOR_RUNNER_WORKSPACE") or "")
    simulated_runner_root = runs

    resolved, reason = resolve_run_workspace(simulated_runner_root, announced_key)
    assert reason is None, (
        f"the runner refused the key the gateway announced: {reason}"
    )
    assert resolved == gateway_workspace, (
        f"the gateway wrote {gateway_workspace} but the runner resolved {resolved}"
    )
    assert runner_root.name == simulated_runner_root.name


def test_runner_still_refuses_the_mounted_root_and_siblings(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2 fix must not become a fallback: no key resolves to the shared root.

    Guards the fail-closed property the whole #117 design rests on. Reads the
    real sidecar module.
    """
    resolve_run_workspace = _load_runner().resolve_run_workspace

    runs = _host_path(writable_container, RUN_WORKSPACES_MOUNT)
    fixture = _host_path(writable_container, GATEWAY_FIXTURE_ROOT)
    monkeypatch.setenv(RUN_WORKSPACES_ENV_VAR, str(runs))
    manager = RunWorkspaceManager(fixture)
    manager.initialize(RUN_ID)
    manager.initialize(SIBLING_RUN_ID)

    for bad in (
        None,                       # absent key
        "",                         # empty
        "..",                       # traversal
        ".",                        # the root itself
        RUN_ID + "/../" + SIBLING_RUN_ID,
        "run\n",                    # trailing newline (#117 D4)
        SIBLING_RUN_ID + "/",       # a sibling, with a trailing slash
    ):
        resolved, reason = resolve_run_workspace(runs, bad)
        assert resolved is None and reason is not None, (
            f"the runner accepted {bad!r} -> {resolved}"
        )


# --------------------------------------------------------------------------
# Documentation of the contract
# --------------------------------------------------------------------------


def test_env_example_documents_the_two_sided_path_contract() -> None:
    """The value is not an operator knob; the reason has to be written down."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert RUN_WORKSPACES_ENV_VAR in text
    assert "EXECUTOR_RUNNER_WORKSPACE" in text
    assert RUN_WORKSPACES_MOUNT in text
    # It must not be presented as a setting to fill in.
    for line in text.splitlines():
        if line.startswith(f"{RUN_WORKSPACES_ENV_VAR}="):
            raise AssertionError(
                f"{RUN_WORKSPACES_ENV_VAR} is a gateway/runner path contract, "
                "not an operator knob; do not offer it as a setting"
            )


def test_runbook_records_the_requirement_and_its_symptoms() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert RUN_WORKSPACES_ENV_VAR in text
    assert "EXECUTOR_RUNNER_WORKSPACE" in text
    assert "RUN_WORKSPACES_ROOT_UNAVAILABLE" in text, (
        "the runbook must name the code an operator will actually see"
    )


# --------------------------------------------------------------------------
# The nesting consequence, which the default introduces
# --------------------------------------------------------------------------


def test_nested_default_root_is_excluded_from_every_fixture_copy(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nesting the root in the fixture must not leak run A into run B.

    The copy source is the fixture, so without an explicit exclusion each run
    would absorb every earlier run's workspace — silently restoring the
    cross-run visibility #117 removes.
    """
    monkeypatch.delenv(RUN_WORKSPACES_ENV_VAR, raising=False)
    fixture = _host_path(writable_container, GATEWAY_FIXTURE_ROOT)
    manager = RunWorkspaceManager(fixture)

    first = manager.initialize(RUN_ID)
    (first / "outputs" / "only-in-run-a.txt").write_text("run A", encoding="utf-8")
    second = manager.initialize(SIBLING_RUN_ID)

    assert not (second / RUN_WORKSPACES_DIRNAME).exists()
    assert not (second / RUN_ID).exists()
    assert not (second / "outputs" / "only-in-run-a.txt").exists()


def test_nested_exclusion_does_not_drop_a_same_named_fixture_directory(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blanket name-pattern ignore would silently drop real fixture content.

    ``shutil.ignore_patterns(name)`` matches at every depth, so a scenario with
    its own nested directory of the same name would vanish from every copy. The
    hook must ignore exactly the one top-level entry.
    """
    monkeypatch.delenv(RUN_WORKSPACES_ENV_VAR, raising=False)
    fixture = _host_path(writable_container, GATEWAY_FIXTURE_ROOT)
    nested = fixture / "scenarios" / RUN_WORKSPACES_DIRNAME
    nested.mkdir(parents=True)
    (nested / "scenario.json").write_text("{}", encoding="utf-8")

    workspace = RunWorkspaceManager(fixture).initialize(RUN_ID)
    assert (workspace / "scenarios" / RUN_WORKSPACES_DIRNAME / "scenario.json").is_file()


def test_env_override_wins_over_the_nested_default(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch the deployment relies on: an explicit root is honoured."""
    fixture = _host_path(writable_container, GATEWAY_FIXTURE_ROOT)
    override = _host_path(writable_container, RUN_WORKSPACES_MOUNT)
    monkeypatch.setenv(RUN_WORKSPACES_ENV_VAR, str(override))
    assert default_run_workspaces_root(fixture) == override


def test_default_is_used_when_the_env_var_is_blank(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whitespace-only value must not become a path named " ".

    ``default_run_workspaces_root`` strips the override, so an operator who
    leaves the variable empty gets the safe default rather than a directory
    literally called a space.
    """
    fixture = _host_path(writable_container, GATEWAY_FIXTURE_ROOT)
    monkeypatch.setenv(RUN_WORKSPACES_ENV_VAR, "   ")
    assert default_run_workspaces_root(fixture) == fixture / RUN_WORKSPACES_DIRNAME


def test_every_writable_volume_in_compose_is_one_the_layout_provides() -> None:
    """The simulation must keep matching the shipped volume list.

    If a fourth volume is added, the read-only layout here stops being
    faithful. Better a loud failure than a passing test that proves less than it
    claims.
    """
    targets = _volume_targets(_gateway_block())
    simulated = {GATEWAY_FIXTURE_ROOT, "/data", RUN_WORKSPACES_MOUNT}
    assert targets == simulated, (
        f"the gateway mounts {sorted(targets)} but the read-only layout simulated "
        f"in this file provides {sorted(simulated)}; update _container_root"
    )


def test_no_environment_leakage_between_the_two_layout_tests(
    writable_container: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity: the fixture never inherits a stale override.

    Cheap, but it stops a future edit from making one test pass because another
    test left the environment set.
    """
    assert RUN_WORKSPACES_ENV_VAR not in os.environ or not os.environ.get(
        RUN_WORKSPACES_ENV_VAR
    ).strip()