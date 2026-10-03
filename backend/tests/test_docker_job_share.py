"""Cross-process agreement on Docker job construction (issue #106).

The gateway (``backend/scopewatch/executor_docker.py``) and the runner
sidecar (``deploy/executor-runner/runner.py``) both build the ``docker run``
command, embed the same in-container helper, stage the workspace, and copy
results back. This module is the single source for all of that, and these
tests assert the two callers actually use it, over every operation and a
spread of argument shapes — not one happy-path ``read_text``.

Three properties are covered:

1. **Agreement.** For the same logical job, the gateway and the runner build
   byte-identical ``docker run`` argv apart from the container label, the
   container name, and the workspace mount source (each side stages its own
   copy). Parameterized over operation and argument shape.
2. **Single implementation.** Both modules resolve the flag list, helper
   code, and copy-back walk to the *same* objects, and neither file
   re-declares them in source.
3. **Stdlib-only import constraint.** The shared module imports nothing
   outside the standard library and nothing from ``scopewatch``, because the
   runner is stdlib-only by design.
"""

from datetime import datetime, timezone
import ast
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

from scopewatch import docker_job
from scopewatch.executor_docker import DockerExecutor
from scopewatch.models import ExecutionStatus, PolicyOutcome, ReasonCode
from scopewatch.schemas import ActionRequest, PolicyDecision

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "deploy" / "executor-runner" / "runner.py"
GATEWAY_SRC_PATH = REPO_ROOT / "backend" / "scopewatch" / "executor_docker.py"
SHARED_SRC_PATH = REPO_ROOT / "backend" / "scopewatch" / "docker_job.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("executor_runner_shared", RUNNER_PATH)
    assert spec is not None and spec.loader is not None, f"missing {RUNNER_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner_mod = _load_runner()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _action(operation: str, resource: str, **kwargs: object) -> ActionRequest:
    return ActionRequest(
        id=kwargs.get("action_id", "action-1"),  # type: ignore[arg-type]
        run_id="run-1",
        tool="workspace",
        operation=operation,
        resource=resource,
        arguments=kwargs.get("arguments", {}),  # type: ignore[arg-type]
        requested_at=_now(),
    )


def _allow(action_id: str = "action-1") -> PolicyDecision:
    return PolicyDecision(
        id=str(uuid.uuid4()),
        action_request_id=action_id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation="Permitted",
        matched_rule="RULE_ALLOWED",
        decided_at=_now(),
        deterministic=True,
    )


def _normalized_run_arguments(action: ActionRequest) -> dict[str, object]:
    """The argv-list form the gateway derives before dispatching."""
    from scopewatch.executor_docker import (
        RUN_COMMAND_DEFAULT_TIMEOUT_S,
        clamp_run_command_timeout,
        extract_run_command_argv,
    )

    argv = extract_run_command_argv(action)
    assert argv is not None
    return {
        "argv": argv,
        "timeout_s": clamp_run_command_timeout(
            (action.arguments or {}).get("timeout_s", RUN_COMMAND_DEFAULT_TIMEOUT_S)
        ),
    }


# Every operation the executor supports, crossed with argument shapes that
# exercise different argument-shape branches: absent arguments, unicode and
# multi-line content, a non-string content value, nested structures, extra
# keys the helper ignores, a single-element argv, and timeouts at / below /
# above the clamp bounds plus a non-numeric one.
OPERATION_CASES: list[tuple[str, str, dict[str, object]]] = [
    ("list_directory", "docs", {}),
    ("list_directory", ".", {"unexpected": "ignored by the helper"}),
    ("read_text", "docs/file_a.txt", {}),
    ("read_text", "docs/ünïcode ✓.txt", {}),
    ("write_text", "outputs/new.txt", {"content": "hello"}),
    ("write_text", "outputs/nested/dir/new.txt", {"content": "line one\nline two\n"}),
    ("write_text", "outputs/unicode.txt", {"content": "naïve ✓ 日本語"}),
    ("write_text", "outputs/coerced.txt", {"content": 12345}),
    ("write_text", "outputs/empty.txt", {"content": ""}),
    ("write_text", "outputs/absent-content.txt", {}),
    ("delete_path", "outputs/old.txt", {}),
    ("delete_path", "outputs/old.txt", {"force": True}),
    ("run_command", ".", {"command": "true"}),
    ("run_command", ".", {"argv": ["echo", "hi"]}),
    ("run_command", "outputs", {"argv": ["ls", "-la"]}),
    ("run_command", ".", {"argv": ["echo", "hi", "--flag=a b", ""]}),
    ("run_command", ".", {"argv": ["python3", "-c", "print('x')"]}),
    ("run_command", ".", {"argv": ["sleep", "0"], "timeout_s": 0.0}),
    ("run_command", ".", {"argv": ["sleep", "0"], "timeout_s": 1.0}),
    ("run_command", ".", {"argv": ["sleep", "0"], "timeout_s": 300.0}),
    ("run_command", ".", {"argv": ["sleep", "0"], "timeout_s": 10_000.0}),
    ("run_command", ".", {"argv": ["sleep", "0"], "timeout_s": "not-a-number"}),
    ("run_command", "outputs", {"command": "ls -la", "timeout_s": 45}),
    ("run_command", ".", {"argv": ["sleep", "0"], "timeout_s": None}),
]


def _canonical_argv(cmd: list[str]) -> list[str]:
    """Strip the two per-side differences so the rest must match exactly.

    The container name and the executor label legitimately differ (the runner
    mounts its own staged copy and tags itself), and the mount source is the
    staging directory each side created for itself. Everything else — every
    hardening flag, the image, the helper source, and the argv tail — is
    required to be byte-identical.
    """
    out: list[str] = []
    skip_next = False
    for index, token in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
        if token == "--name":
            out.append("--name")
            out.append("<container>")
            skip_next = True
            continue
        if token.startswith("scopewatch.executor="):
            out.append("scopewatch.executor=<executor>")
            continue
        if token.startswith("scopewatch.run="):
            out.append("scopewatch.run=<run>")
            continue
        if token.startswith("/tmp/") or token.startswith("/var/folders/"):
            # The staged workspace mount source: each side stages its own copy.
            out.append("<staged-workspace>")
            continue
        if token.startswith("/private/var/") and "scopewatch-run" in token:
            out.append("<staged-workspace>")
            continue
        _ = index
        out.append(token)
    return out


@pytest.mark.parametrize(
    ("operation", "resource", "arguments"),
    OPERATION_CASES,
    ids=[f"{op}-{res}-{sorted(args)}" for op, res, args in OPERATION_CASES],
)
def test_gateway_and_runner_build_the_same_docker_command(
    tmp_path: Path, operation: str, resource: str, arguments: dict[str, object]
) -> None:
    """Both sides dispatch byte-identical argv for the same logical job."""
    action = _action(operation, resource, arguments=arguments)

    # Gateway: normalize exactly as executor_docker does, then build.
    if operation == "run_command":
        job_arguments = _normalized_run_arguments(action)
        job_timeout = job_arguments["timeout_s"]
    else:
        job_arguments = dict(arguments)
        job_timeout = None
    gateway_job = docker_job.prepare_docker_job(
        operation=operation, resource=resource, arguments=job_arguments, timeout_s=job_timeout
    )
    gateway_cmd = docker_job.build_docker_command(
        image=docker_job.DOCKER_IMAGE,
        workspace_copy=tmp_path / "gateway-copy",
        job=gateway_job,
        container_name="scopewatch-gateway",
        run_label="run-1",
        executor_label="docker",
    )

    # Runner: same builder, its own label. It only ever receives the argv-list
    # form a conforming gateway already normalized (prepare_dispatch), so feed
    # it the gateway's normalized arguments and let its own normalize step
    # re-derive them.
    runner_arguments = dict(job_arguments)
    runner_timeout: float | None = None
    if operation == "run_command":
        argv, timeout, error = runner_mod.normalize_run_command(runner_arguments)
        assert error is None and argv is not None
        runner_arguments = {"argv": argv, "timeout_s": timeout}
        runner_timeout = timeout
    runner_job = runner_mod.prepare_docker_job(
        operation=operation,
        resource=resource,
        arguments=runner_arguments,
        timeout_s=runner_timeout,
    )
    runner_cmd = runner_mod.build_docker_command(
        image=runner_mod.RUNNER_DOCKER_IMAGE,
        workspace_copy=tmp_path / "runner-copy",
        job=runner_job,
        container_name="scopewatch-runner",
        run_label="run-1",
        executor_label="runner",
    )

    assert gateway_job.arguments_json == runner_job.arguments_json
    assert gateway_job == runner_job
    assert _canonical_argv(gateway_cmd) == _canonical_argv(runner_cmd)
    # The one thing they must NOT agree on is the executor label.
    assert "scopewatch.executor=docker" in gateway_cmd
    assert "scopewatch.executor=runner" in runner_cmd
    # Both embed the one shared helper source verbatim.
    helper = docker_job.helper_code()
    assert helper in gateway_cmd
    assert helper in runner_cmd


@pytest.mark.parametrize(
    ("operation", "resource", "arguments"),
    OPERATION_CASES,
    ids=[f"{op}-{res}-{sorted(args)}" for op, res, args in OPERATION_CASES],
)
def test_gateway_job_argv_carries_normalized_arguments(
    operation: str, resource: str, arguments: dict[str, object]
) -> None:
    """The JSON the container parses is derived from the job, not restated."""
    action = _action(operation, resource, arguments=arguments)
    if operation == "run_command":
        expected = _normalized_run_arguments(action)
    else:
        expected = dict(arguments)
    job = docker_job.prepare_docker_job(
        operation=operation, resource=resource, arguments=expected
    )
    assert job.helper_argv == (operation, resource, json.dumps(expected))
    assert json.loads(job.arguments_json) == expected
    # Mismatched payload/serialization is rejected rather than dispatched.
    with pytest.raises(ValueError, match="disagree"):
        docker_job.DockerJob(
            operation=operation,
            resource=resource,
            arguments={"content": "a"},
            arguments_json='{"content": "b"}',
        )


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (0.0, 1.0),
        (0.5, 1.0),
        (1.0, 1.0),
        (12.5, 12.5),
        (300.0, 300.0),
        (10_000.0, 300.0),
        (None, 60.0),
        ("not-a-number", 60.0),
        (True, 1.0),
    ],
)
def test_run_command_timeout_clamp_is_shared_and_identical(
    requested: object, expected: float
) -> None:
    """Both sides clamp a requested timeout with the same bounds."""
    assert docker_job.clamp_run_command_timeout(requested) == expected
    assert runner_mod.clamp_run_command_timeout(requested) == expected


def test_run_command_host_timeout_derives_from_the_same_job() -> None:
    """The host wait is derived from the timeout the container receives."""
    job = docker_job.prepare_docker_job(
        operation="run_command",
        resource=".",
        arguments={"argv": ["echo", "hi"], "timeout_s": 12.5},
        timeout_s=12.5,
    )
    assert job.host_timeout_s(docker_job.DOCKER_TIMEOUT_S) == pytest.approx(
        12.5 + docker_job.DOCKER_RUN_TIMEOUT_BUFFER_S
    )
    # Non-command operations keep the caller's own default, unchanged.
    plain = docker_job.prepare_docker_job(operation="read_text", resource="a.txt")
    assert plain.host_timeout_s(7.5) == 7.5


def test_runner_normalizes_run_command_to_the_same_shape() -> None:
    """The runner's normalization matches the gateway's byte for byte."""
    for arguments in (
        {"argv": ["echo", "hi"]},
        {"argv": ["echo", "hi"], "timeout_s": 5.0},
        {"argv": ["echo", "hi"], "timeout_s": 10_000},
        {"argv": ["echo", "hi"], "timeout_s": "bogus"},
    ):
        action = _action("run_command", ".", arguments=arguments)
        gateway = _normalized_run_arguments(action)
        argv, timeout, error = runner_mod.normalize_run_command(arguments)
        assert error is None and argv is not None
        assert runner_mod.prepare_docker_job(
            operation="run_command",
            resource=".",
            arguments={"argv": argv, "timeout_s": timeout},
            timeout_s=timeout,
        ) == docker_job.prepare_docker_job(
            operation="run_command",
            resource=".",
            arguments=gateway,
            timeout_s=gateway["timeout_s"],
        )


# ---------------- Single-implementation guarantees ----------------


# Attribute name each caller binds the shared object to. The gateway keeps
# private aliases for its existing call sites and monkeypatch targets; the
# runner does the same for the two helpers it already called privately.
SHARED_BINDINGS: list[tuple[str, str, str]] = [
    # (shared name, gateway attribute, runner attribute)
    ("build_docker_command", "build_docker_command", "build_docker_command"),
    ("prepare_docker_job", "prepare_docker_job", "prepare_docker_job"),
    ("clamp_run_command_timeout", "clamp_run_command_timeout", "clamp_run_command_timeout"),
    ("sync_copy_back", "_sync_copy_back", "sync_copy_back"),
    ("replace_link", "_replace_link", "replace_link"),
    ("make_world_accessible", "_make_world_accessible", "make_world_accessible"),
    ("stage_workspace_copy", "_stage_workspace_copy", "stage_workspace_copy"),
    ("best_effort_remove", "_best_effort_remove", "best_effort_remove"),
]


@pytest.mark.parametrize(
    ("shared_name", "gateway_attr", "runner_attr"),
    SHARED_BINDINGS,
    ids=[row[0] for row in SHARED_BINDINGS],
)
def test_both_callers_use_the_same_shared_objects(
    shared_name: str, gateway_attr: str, runner_attr: str
) -> None:
    """Both modules resolve job construction to the shared implementation.

    Identity, not equality: a re-declared copy would pass an equality check and
    drift again, which is the whole point of #106.
    """
    import scopewatch.executor_docker as gateway_mod

    shared = getattr(docker_job, shared_name)
    assert getattr(gateway_mod, gateway_attr) is shared, (
        f"executor_docker.{gateway_attr} is not the shared {shared_name}"
    )
    assert getattr(runner_mod, runner_attr) is shared, (
        f"runner.{runner_attr} is not the shared {shared_name}"
    )


@pytest.mark.parametrize(
    "marker",
    [
        '"--cap-drop"',
        '"--network"',
        '"none"',
        '"--read-only"',
        '"--pids-limit"',
        '"--security-opt"',
        "SCOPEWATCH_WORKSPACE",
        "shell=False",
        "def sync_copy_back",
        "def replace_link",
        "def make_world_accessible",
        "def build_docker_command",
    ],
)
def test_flag_list_and_helper_exist_in_exactly_one_source_file(marker: str) -> None:
    """Neither caller re-declares the flag list, helper, or copy-back walk."""
    shared_src = SHARED_SRC_PATH.read_text(encoding="utf-8")
    gateway_src = GATEWAY_SRC_PATH.read_text(encoding="utf-8")
    runner_src = RUNNER_PATH.read_text(encoding="utf-8")
    assert marker in shared_src, f"{marker} missing from the shared module"
    assert marker not in gateway_src, f"{marker} re-declared in executor_docker.py"
    assert marker not in runner_src, f"{marker} re-declared in runner.py"


def test_shared_module_imports_only_the_standard_library() -> None:
    """The runner is stdlib-only, so the shared module must be too.

    Parsed from source rather than guessed from ``dir()`` so the check holds
    even for imports used only inside a docstring example or a type comment.
    """
    tree = ast.parse(SHARED_SRC_PATH.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pytest.fail("relative import in the shared module")
            if node.module:
                roots.add(node.module.split(".")[0])
    non_stdlib = sorted(r for r in roots if r not in sys.stdlib_module_names)
    assert non_stdlib == [], f"shared module must stay stdlib-only: {non_stdlib}"
    assert "scopewatch" not in roots


def test_container_output_caps_match_gateway_config() -> None:
    """The helper's caps are restated in the shared module; pin them together.

    The runner cannot import gateway config, so these values are duplicated on
    purpose. This test is what stops the duplication from drifting.
    """
    from scopewatch import config

    assert docker_job.MAX_READ_BYTES == config.MAX_READ_BYTES
    assert docker_job.MAX_WRITE_BYTES == config.MAX_WRITE_BYTES


def test_runner_only_imports_the_shared_modules_from_scopewatch() -> None:
    """The sidecar's gateway imports are the shared modules and nothing else.

    Two now: the Docker job module (#106) and the pre-dispatch gate (#105).
    Both are stdlib-only and both are COPYed in from their authored location,
    so an allowlist is the honest form of this check: a third import would be
    new gateway code reaching the socket-holding container.
    """
    tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
    scopewatch_roots: set[str] = set()
    for node in ast.walk(tree):
        # `from scopewatch import dispatch_gate` and
        # `from scopewatch.docker_job import x` are both gateway imports; the
        # alias resolves to the submodule it names, not to the package.
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] == "scopewatch":
                if node.module == "scopewatch":
                    # `from scopewatch import dispatch_gate`: the alias is
                    # the submodule being imported.
                    scopewatch_roots.update(
                        f"scopewatch.{alias.name}" for alias in node.names
                    )
                else:
                    # `from scopewatch.docker_job import x`: the names are
                    # members of that module.
                    scopewatch_roots.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "scopewatch":
                    scopewatch_roots.add(alias.name)
    assert scopewatch_roots == {"scopewatch.docker_job", "scopewatch.dispatch_gate"}


def test_runner_image_ships_the_shared_module_from_the_same_source() -> None:
    """The Dockerfile COPYs the shared module, not a vendored duplicate."""
    dockerfile = (REPO_ROOT / "deploy" / "executor-runner" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "COPY backend/scopewatch/docker_job.py /app/scopewatch/docker_job.py" in dockerfile
    compose = (
        REPO_ROOT / "deploy" / "executor-runner" / "compose.executor-runner.yaml"
    ).read_text(encoding="utf-8")
    assert "context: .." in compose
    assert "dockerfile: deploy/executor-runner/Dockerfile" in compose


# ---------------- Behaviour: staging and copy-back on both sides ----------------


def _mock_docker_run(monkeypatch, module, container_write=None):
    """Patch a module's ``subprocess.run`` to simulate the Docker CLI."""
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if isinstance(cmd, list) and cmd[:2] == ["docker", "run"]:
            if container_write is not None:
                copy_path = Path(cmd[cmd.index("-v") + 1].split(":")[0])
                container_write(copy_path)
            payload = json.dumps(
                {"status": "EXECUTED", "result": {"operation": "read_text"}, "error_code": None}
            ).encode()
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr=b"")
        if isinstance(cmd, list) and cmd[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", fake_run)


@pytest.mark.parametrize("runner_side", [False, True])
def test_executed_write_syncs_back_through_a_preexisting_link(
    monkeypatch, tmp_path: Path, runner_side: bool
) -> None:
    """Both backends still copy results back over an existing destination link.

    Issue #80 made this symlink-tolerant. Sharing the walk means one fix now
    covers the gateway and the sidecar; this asserts both actually reach it.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("before", encoding="utf-8")
    (ws / "notes-link.txt").symlink_to("notes.txt")

    def container_write(copy: Path) -> None:
        (copy / "notes.txt").write_text("after", encoding="utf-8")

    if not runner_side:
        import scopewatch.executor_docker as gateway_mod

        _mock_docker_run(monkeypatch, gateway_mod, container_write)
        monkeypatch.setattr(gateway_mod, "is_docker_available", lambda *a, **k: True)
        receipt = DockerExecutor().execute(
            _action("write_text", "notes.txt", arguments={"content": "after"}),
            ws,
            policy_decision=_allow(),
        )
        assert receipt.status == ExecutionStatus.EXECUTED, receipt.sanitized_result
        assert receipt.executor == "docker-executor"
    else:
        _mock_docker_run(monkeypatch, runner_mod, container_write)
        import time

        from scopewatch.executor_remote import sign_dispatch_payload

        # Issue #117: the runner resolves a per-run directory inside its
        # volume, so the dispatch announces which one and the mounted root
        # holds it.
        run_workspace = "run-1-ws"
        config = runner_mod.RunnerConfig()
        config.token = "synthetic"
        config.signing_key = "synthetic-independent-signing-key"
        config.workspace = ws
        (config.workspace / run_workspace).mkdir()
        shutil.copy2(ws / "notes.txt", config.workspace / run_workspace / "notes.txt")
        (config.workspace / run_workspace / "notes-link.txt").symlink_to("notes.txt")
        action = {
            "id": "action-1",
            "run_id": "run-1",
            "operation": "write_text",
            "resource": "notes.txt",
            "arguments": {"content": "after"},
        }
        decision = {"id": "d1", "action_request_id": "action-1", "outcome": "ALLOW"}
        body = {
            "issued_at": time.time(),
            "dispatch_token": "cross-test-token",
            "run_workspace": run_workspace,
            "action_digest": runner_mod.canonical_action_digest(
                action, decision, run_workspace
            ),
            "action": action,
            "policy_decision": decision,
            "approval": None,
        }
        body["dispatch_signature"] = sign_dispatch_payload(body, config.signing_key)
        status, payload = runner_mod.handle_execute(
            body, config, runner_mod.DispatchTokenStore()
        )
        assert status == 200, payload
        assert payload["status"] == "EXECUTED", payload

    # The link survives as a link and the write landed through it.
    target = (
        ws
        if not runner_side
        else ws / "run-1-ws"
    )
    assert (target / "notes-link.txt").is_symlink()
    assert (target / "notes.txt").read_text(encoding="utf-8") == "after"


def test_staging_leaves_no_residue_on_failure(tmp_path: Path) -> None:
    """A failed staging cleans up after itself instead of leaking a copy."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "file.txt").write_text("x", encoding="utf-8")
    before = set(tmp_path.iterdir())
    with pytest.raises(OSError):
        docker_job.stage_workspace_copy(src / "missing")
    assert set(tmp_path.iterdir()) == before