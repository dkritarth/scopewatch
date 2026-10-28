"""Regressions for #305: case-variant aliases of blocked paths.

On a case-insensitive volume (macOS default, Windows), ``outputs/PRIVATE/x``
and ``outputs/private/x`` name the same file, so an upper-case alias of a
blocked path must be refused. On a case-sensitive volume (typical Linux) the
two are genuinely distinct and the upper-case spelling must keep its normal,
non-blocked meaning.

The volume probe answers three states, and the matcher uses them differently:
``True`` folds case for both the blocked and the allowed comparison,
``False`` folds neither, and ``None`` (probe failed) folds the blocked
comparison only. That asymmetry is the point of the design: an undetectable
volume may tighten the scope, never widen it (``allowed=["outputs"]`` must not
accept ``OUTPUTS/x`` when we cannot prove the volume folds case).

Coverage: the matcher units, the probe's cache, and integration tests showing
that policy, the executor's pre-I/O check, and the runner sidecar all pass the
same verdict and reach the same answer. The probe is monkeypatched in the
integration tests, so they behave identically on every host and never touch a
network or a real model provider. All fixtures are synthetic.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

import pytest

import scopewatch.scope_paths as scope_paths
from scopewatch import docker_job
from scopewatch.executor import ExecutionSecurityError, _verify_scoped_target
from scopewatch.models import PolicyOutcome, ReasonCode
from scopewatch.policy import evaluate_policy, revalidate_run_command_in_workspace
from scopewatch.schemas import ActionRequest, Run, TaskScope
from scopewatch.scope_paths import (
    BLOCKED_PATH,
    PATH_NOT_ALLOWED,
    clear_case_probe_cache,
    normalize_relative_segments as N,
    scoped_path_denial,
    segments_within,
    volume_is_case_insensitive,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "deploy" / "executor-runner" / "runner.py"

ALLOWED = ("outputs",)
BLOCKED = ("outputs/private",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _empty_probe_cache() -> None:
    """No cached volume verdict may leak between tests."""
    clear_case_probe_cache()
    yield
    clear_case_probe_cache()


def _denial(alias: str, resolved: str, *, case_insensitive: bool | None):
    return scoped_path_denial(
        N(alias), N(resolved), ALLOWED, BLOCKED, case_insensitive=case_insensitive
    )


# ---------------------------------------------------------------------------
# The matcher: the bypass itself
# ---------------------------------------------------------------------------


def test_lowercase_blocked_is_denied_in_every_volume_state():
    for ci in (False, True, None):
        code, prefix = _denial(
            "outputs/private/synthetic.txt",
            "outputs/private/synthetic.txt",
            case_insensitive=ci,
        )
        assert code == BLOCKED_PATH and prefix == "outputs/private"


@pytest.mark.parametrize("alias", [
    "outputs/PRIVATE/synthetic.txt",
    "outputs/Private/synthetic.txt",
    "outputs/pRiVaTe/synthetic.txt",
    "OUTPUTS/PRIVATE/synthetic.txt",
])
def test_case_variant_alias_blocked_on_insensitive_volume(alias):
    # The #305 bug: these slipped through as ordinary allowed `outputs` paths.
    assert _denial(alias, alias, case_insensitive=True) == (
        BLOCKED_PATH,
        "outputs/private",
    )


@pytest.mark.parametrize("alias", [
    "outputs/PRIVATE/synthetic.txt",
    "outputs/Private/synthetic.txt",
])
def test_case_variant_alias_is_distinct_on_sensitive_volume(alias):
    # Linux semantics preserved: PRIVATE is a different directory, allowed.
    assert _denial(alias, alias, case_insensitive=False) is None


# ---------------------------------------------------------------------------
# The matcher: the three states must not widen the allowed set
# ---------------------------------------------------------------------------


def test_unknown_volume_folds_blocks_but_not_allows():
    """A failed probe tightens blocked matching and leaves allowlists exact."""
    # Blocked side folds on unknown, so the alias is refused.
    assert _denial(
        "outputs/PRIVATE/x.txt", "outputs/PRIVATE/x.txt", case_insensitive=None
    ) == (BLOCKED_PATH, "outputs/private")
    # Allowed side does not fold on unknown: OUTPUTS is not the allowed dir.
    assert _denial(
        "OUTPUTS/x.txt", "OUTPUTS/x.txt", case_insensitive=None
    ) == (PATH_NOT_ALLOWED, None)


def test_unknown_volume_default_is_the_safe_one():
    """Omitting the verdict is the same as not knowing it."""
    assert scoped_path_denial(
        N("outputs/PRIVATE/x.txt"), N("outputs/PRIVATE/x.txt"), ALLOWED, BLOCKED
    ) == (BLOCKED_PATH, "outputs/private")
    assert scoped_path_denial(
        N("OUTPUTS/x.txt"), N("OUTPUTS/x.txt"), ALLOWED, BLOCKED
    ) == (PATH_NOT_ALLOWED, None)


def test_confirmed_insensitive_volume_does_not_widen_the_sensitive_case():
    """Only a confirmed True folds the allowlist (the intended macOS behaviour)."""
    assert _denial("OUTPUTS/x.txt", "OUTPUTS/x.txt", case_insensitive=True) is None
    assert _denial(
        "OUTPUTS/x.txt", "OUTPUTS/x.txt", case_insensitive=False
    ) == (PATH_NOT_ALLOWED, None)


# ---------------------------------------------------------------------------
# The matcher: the acceptance criteria's edge cases
# ---------------------------------------------------------------------------


def test_allowed_case_variant_not_falsely_rejected_on_insensitive_volume():
    # A case-variant of an *allowed* path must stay allowed, not become
    # PATH_NOT_ALLOWED, or folding would break the allow side as it fixes block.
    assert _denial("OUTPUTS/report.txt", "OUTPUTS/report.txt", case_insensitive=True) is None


def test_allowed_alias_cannot_authorize_case_variant_blocked_target():
    # Allowed requested name, resolved target is a case-variant of a blocked
    # path (the "symlink swapped after the decision" shape, #184) -> blocked.
    assert _denial("outputs/ok.txt", "outputs/PRIVATE/secret.txt", case_insensitive=True) == (
        BLOCKED_PATH,
        "outputs/private",
    )


def test_nonexistent_write_target_case_variant_blocked_on_insensitive_volume():
    # resolve() cannot canonicalise a path that does not exist yet, so a
    # nonexistent upper-case write target keeps its casing; it must still be
    # caught when the volume folds case.
    assert _denial(
        "outputs/PRIVATE/new-file.txt", "outputs/PRIVATE/new-file.txt", case_insensitive=True
    ) == (BLOCKED_PATH, "outputs/private")


def test_segments_within_folds_only_when_asked():
    up = N("outputs/PRIVATE/x")
    assert segments_within(up, "outputs/private") is False
    assert segments_within(up, "outputs/private", case_insensitive=True) is True


# ---------------------------------------------------------------------------
# The live volume probe and its cache
# ---------------------------------------------------------------------------


def test_volume_probe_matches_this_host():
    # Empirical, not a platform guess. Create two spellings of one name and
    # see whether the volume treats them as the same file; the probe must
    # agree with that observation on whatever volume the tests run on.
    with tempfile.TemporaryDirectory() as d:
        marker = Path(d) / "ProbeCaseCheck"
        marker.write_text("synthetic", encoding="utf-8")
        observed_ci = (Path(d) / "probecasecheck").exists()
        assert volume_is_case_insensitive(d) == observed_ci


def test_probe_returns_unknown_for_an_unusable_dir():
    # An undetectable volume is "unknown", never a guess in either direction.
    assert volume_is_case_insensitive("/nonexistent-scopewatch-probe-dir-xyz") is None


def test_probe_leaves_no_file_behind():
    with tempfile.TemporaryDirectory() as d:
        volume_is_case_insensitive(d)
        assert [p.name for p in Path(d).iterdir()] == []


def test_confirmed_probe_result_is_cached_per_root(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    real_probe = scope_paths._probe_case_insensitivity

    def counting_probe(root: str):
        calls.append(root)
        return real_probe(root)

    monkeypatch.setattr(scope_paths, "_probe_case_insensitivity", counting_probe)
    with tempfile.TemporaryDirectory() as d:
        first = volume_is_case_insensitive(d)
        second = volume_is_case_insensitive(d)
        third = volume_is_case_insensitive(d + "/.")
    assert first is not None
    assert second == first == third
    # One probe for the same root, even when spelled slightly differently.
    assert len(calls) == 1


def test_unknown_probe_result_is_not_cached(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    answers: list[bool | None] = [None, None, False]

    def fake_probe(root: str):
        calls.append(root)
        return answers.pop(0)

    monkeypatch.setattr(scope_paths, "_probe_case_insensitivity", fake_probe)
    with tempfile.TemporaryDirectory() as d:
        assert volume_is_case_insensitive(d) is None
        assert volume_is_case_insensitive(d) is None
        # A volume that becomes usable later is re-probed and then cached.
        assert volume_is_case_insensitive(d) is False
        assert volume_is_case_insensitive(d) is False
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Integration: policy
# ---------------------------------------------------------------------------


@pytest.fixture
def case_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "outputs").mkdir(parents=True)
    (ws / "outputs" / "old.txt").write_text("synthetic output", encoding="utf-8")
    return ws


@pytest.fixture
def case_run() -> Run:
    scope = TaskScope(
        schema_version="1",
        task_description="Write only inside outputs.",
        allowed_paths=["outputs"],
        blocked_paths=["outputs/private"],
        allowed_tools=["workspace"],
        allowed_operations=["write_text", "read_text"],
        allowed_network_destinations=[],
        requires_approval=[],
        created_at=_now(),
    )
    return Run(
        id="run-305",
        name="Case variant run",
        task_scope=scope,
        created_at=_now(),
        updated_at=_now(),
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


def _write_action(run: Run, resource: str) -> ActionRequest:
    return ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation="write_text",
        resource=resource,
        arguments={"content": "synthetic"},
        requested_at=_now(),
    )


def _patch_policy_probe(monkeypatch: pytest.MonkeyPatch, verdict: bool | None) -> list[str]:
    seen: list[str] = []

    def probe(root: str):
        seen.append(root)
        return verdict

    monkeypatch.setattr("scopewatch.policy.volume_is_case_insensitive", probe)
    return seen


def test_policy_denies_case_variant_alias_when_volume_folds_case(
    monkeypatch: pytest.MonkeyPatch, case_workspace: Path, case_run: Run
) -> None:
    seen = _patch_policy_probe(monkeypatch, True)
    decision = evaluate_policy(
        _write_action(case_run, "outputs/PRIVATE/x.txt"), case_run, case_workspace
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH
    assert decision.matched_rule == "RULE_BLOCKED_PATH_MATCHED"
    assert "outputs/private" in decision.explanation
    # Policy asked the probe about the workspace it matched against.
    assert seen and seen[0].endswith("workspace")
    assert not (case_workspace / "outputs" / "PRIVATE").exists()


def test_policy_helper_asserts_allow_side_still_works_when_volume_folds_case(
    monkeypatch: pytest.MonkeyPatch, case_workspace: Path, case_run: Run
) -> None:
    _patch_policy_probe(monkeypatch, True)
    decision = evaluate_policy(
        _write_action(case_run, "OUTPUTS/x.txt"), case_run, case_workspace
    )
    assert decision.outcome == PolicyOutcome.ALLOW, decision
    assert decision.reason_code == ReasonCode.ALLOWED_TOOL_AND_RESOURCE


def test_policy_allows_the_same_path_when_volume_is_confirmed_sensitive(
    monkeypatch: pytest.MonkeyPatch, case_workspace: Path, case_run: Run
) -> None:
    _patch_policy_probe(monkeypatch, False)
    decision = evaluate_policy(
        _write_action(case_run, "outputs/PRIVATE/x.txt"), case_run, case_workspace
    )
    assert decision.outcome == PolicyOutcome.ALLOW, decision
    assert decision.reason_code == ReasonCode.ALLOWED_TOOL_AND_RESOURCE


def test_policy_unknown_volume_blocks_alias_and_refuses_to_widen_scope(
    monkeypatch: pytest.MonkeyPatch, case_workspace: Path, case_run: Run
) -> None:
    _patch_policy_probe(monkeypatch, None)

    blocked = evaluate_policy(
        _write_action(case_run, "outputs/PRIVATE/x.txt"), case_run, case_workspace
    )
    assert blocked.outcome == PolicyOutcome.DENY
    assert blocked.reason_code == ReasonCode.BLOCKED_PATH

    # The security bug this test exists for: with an unknown volume, an
    # upper-case spelling of an allowed directory is a *different* directory
    # on a case-sensitive disk and must not inherit the allowlist entry.
    widened = evaluate_policy(
        _write_action(case_run, "OUTPUTS/x.txt"), case_run, case_workspace
    )
    assert widened.outcome == PolicyOutcome.DENY
    assert widened.reason_code == ReasonCode.PATH_NOT_ALLOWED


# ---------------------------------------------------------------------------
# Integration: executor pre-I/O check
# ---------------------------------------------------------------------------


def test_executor_scope_check_denies_case_variant_alias(
    monkeypatch: pytest.MonkeyPatch, case_workspace: Path, case_run: Run
) -> None:
    monkeypatch.setattr("scopewatch.executor.volume_is_case_insensitive", lambda root: True)
    with pytest.raises(ExecutionSecurityError) as excinfo:
        _verify_scoped_target(case_workspace, "outputs/PRIVATE/x.txt", case_run.task_scope)
    assert excinfo.value.code == "SCOPE_VIOLATION"
    assert not (case_workspace / "outputs" / "PRIVATE").exists()


def test_executor_scope_check_allows_same_path_on_sensitive_volume(
    monkeypatch: pytest.MonkeyPatch, case_workspace: Path, case_run: Run
) -> None:
    monkeypatch.setattr("scopewatch.executor.volume_is_case_insensitive", lambda root: False)
    target = _verify_scoped_target(
        case_workspace, "outputs/PRIVATE/x.txt", case_run.task_scope
    )
    assert target == (case_workspace / "outputs" / "PRIVATE" / "x.txt").resolve()


def test_executor_scope_check_unknown_volume_matches_policy(
    monkeypatch: pytest.MonkeyPatch, case_workspace: Path, case_run: Run
) -> None:
    monkeypatch.setattr("scopewatch.executor.volume_is_case_insensitive", lambda root: None)
    with pytest.raises(ExecutionSecurityError) as blocked:
        _verify_scoped_target(case_workspace, "outputs/PRIVATE/x.txt", case_run.task_scope)
    assert blocked.value.code == "SCOPE_VIOLATION"
    with pytest.raises(ExecutionSecurityError) as widened:
        _verify_scoped_target(case_workspace, "OUTPUTS/x.txt", case_run.task_scope)
    assert widened.value.code == "SCOPE_VIOLATION"


# ---------------------------------------------------------------------------
# Integration: runner sidecar dispatch
# ---------------------------------------------------------------------------


def _load_runner():
    spec = importlib.util.spec_from_file_location("executor_runner_305", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runner_config(runner_mod, tmp_path: Path, run_workspace: str):
    config = runner_mod.RunnerConfig()
    config.token = "synthetic"
    config.signing_key = "synthetic-independent-signing-key"
    config.workspace = tmp_path / "volume"
    ws = config.workspace / run_workspace
    (ws / "outputs").mkdir(parents=True)
    (ws / "outputs" / "old.txt").write_text("synthetic output", encoding="utf-8")
    return config


def _runner_body(
    runner_mod, run_workspace: str, task_scope: dict, key: str, resource: str,
    *, operation: str = "write_text", arguments: dict | None = None,
) -> dict:
    from scopewatch.executor_remote import sign_dispatch_payload

    action = {
        "id": "action-305",
        "run_id": "run-305",
        "operation": operation,
        "resource": resource,
        "arguments": arguments or {"content": "synthetic"},
    }
    decision = {
        "id": "d-305",
        "action_request_id": "action-305",
        "outcome": "ALLOW",
    }
    body = {
        "issued_at": time.time(),
        "dispatch_token": f"token-{uuid.uuid4().hex}",
        "run_workspace": run_workspace,
        "action_digest": runner_mod.canonical_action_digest(
            action, decision, run_workspace, task_scope
        ),
        "action": action,
        "policy_decision": decision,
        "approval": None,
        "task_scope": task_scope,
    }
    body["dispatch_signature"] = sign_dispatch_payload(body, key)
    return body


def _patch_runner_for_dispatch(runner_mod, monkeypatch: pytest.MonkeyPatch, verdict) -> list[str]:
    """Fake the volume probe and a successful container, never a real one."""
    seen: list[str] = []

    def probe(root: str):
        seen.append(root)
        return verdict

    def fake_run(cmd, **kwargs):
        payload = {
            "status": "EXECUTED",
            "result": {"operation": "write_text", "bytes_written": 9},
            "error_code": None,
        }
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(payload).encode(), stderr=b""
        )

    monkeypatch.setattr(runner_mod, "volume_is_case_insensitive", probe)
    monkeypatch.setattr(runner_mod, "run_docker_process", fake_run)
    monkeypatch.setattr(runner_mod, "best_effort_remove", lambda *a, **k: None)
    return seen


def _runner_scope() -> dict:
    return {
        "allowed_paths": ["outputs"],
        "blocked_paths": ["outputs/private"],
        "allowed_commands": [],
    }


def test_runner_refuses_case_variant_alias_when_volume_folds_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner_mod = _load_runner()
    run_workspace = "run-305-ws"
    config = _runner_config(runner_mod, tmp_path, run_workspace)
    seen = _patch_runner_for_dispatch(runner_mod, monkeypatch, True)
    body = _runner_body(
        runner_mod, run_workspace, _runner_scope(), config.signing_key,
        "outputs/PRIVATE/x.txt",
    )

    status, payload = runner_mod.handle_execute(body, config, runner_mod.DispatchTokenStore())

    assert status == 403, payload
    assert payload == {"error": "resource violates task scope"}
    assert seen, "runner must probe the staged workspace volume"
    assert not (config.workspace / run_workspace / "outputs" / "PRIVATE").exists()


def test_runner_allows_the_same_path_when_volume_is_confirmed_sensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner_mod = _load_runner()
    run_workspace = "run-305-ws"
    config = _runner_config(runner_mod, tmp_path, run_workspace)
    _patch_runner_for_dispatch(runner_mod, monkeypatch, False)
    body = _runner_body(
        runner_mod, run_workspace, _runner_scope(), config.signing_key,
        "outputs/PRIVATE/x.txt",
    )

    status, payload = runner_mod.handle_execute(body, config, runner_mod.DispatchTokenStore())

    assert status == 200, payload
    assert payload["status"] == "EXECUTED", payload


def test_runner_unknown_volume_matches_policy_on_both_sides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner_mod = _load_runner()
    run_workspace = "run-305-ws"
    config = _runner_config(runner_mod, tmp_path, run_workspace)
    _patch_runner_for_dispatch(runner_mod, monkeypatch, None)

    for resource in ("outputs/PRIVATE/x.txt", "OUTPUTS/x.txt"):
        body = _runner_body(
            runner_mod, run_workspace, _runner_scope(), config.signing_key, resource
        )
        status, payload = runner_mod.handle_execute(
            body, config, runner_mod.DispatchTokenStore()
        )
        assert status == 403, (resource, status, payload)
        assert payload == {"error": "resource violates task scope"}


# ---------------------------------------------------------------------------
# Round 2 of the #305 fix: no blocked-path comparison may stay case-sensitive
# ---------------------------------------------------------------------------


def _run_command_run() -> Run:
    """A run whose scope actually permits a synthetic ``cat`` command."""
    scope = TaskScope(
        schema_version="1",
        task_description="Run only synthetic cat commands inside outputs.",
        allowed_paths=["outputs"],
        blocked_paths=["outputs/private"],
        allowed_tools=["workspace"],
        allowed_operations=["run_command"],
        allowed_network_destinations=[],
        requires_approval=[],
        allowed_commands=[["cat"]],
        created_at=_now(),
    )
    return Run(
        id="run-305-rc",
        name="Case variant run command",
        task_scope=scope,
        created_at=_now(),
        updated_at=_now(),
        synthetic=True,
        interception_coverage="Synthetic test gateway",
        reasoning_availability="Unavailable",
    )


def _run_command_action(
    run: Run, argv: list[str], resource: str = "."
) -> ActionRequest:
    return ActionRequest(
        id=str(uuid.uuid4()),
        run_id=run.id,
        tool="workspace",
        operation="run_command",
        resource=resource,
        arguments={"argv": argv, "timeout_s": 5},
        requested_at=_now(),
    )


# --- Policy: run_command argv blocked checks now fold too ------------------


def test_run_command_argv_case_variant_blocked_when_volume_folds_case(
    monkeypatch: pytest.MonkeyPatch, case_workspace: Path, docker_backend: None
) -> None:
    """`cat outputs/PRIVATE/secret.txt` must be DENIED on a folding volume."""
    seen = _patch_policy_probe(monkeypatch, True)
    run = _run_command_run()
    decision = evaluate_policy(
        _run_command_action(run, ["cat", "outputs/PRIVATE/secret.txt"]),
        run,
        case_workspace,
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH
    assert decision.matched_rule == "RULE_RUN_COMMAND_BLOCKED_PATH_MATCHED"
    assert "outputs/private" in decision.explanation
    assert seen and seen[0].endswith("workspace")


def test_run_command_argv_case_variant_blocked_when_volume_unknown(
    monkeypatch: pytest.MonkeyPatch, case_workspace: Path, docker_backend: None
) -> None:
    """A failed probe still refuses the alias (fail closed on the blocked side)."""
    _patch_policy_probe(monkeypatch, None)
    run = _run_command_run()
    decision = evaluate_policy(
        _run_command_action(run, ["cat", "outputs/PRIVATE/secret.txt"]),
        run,
        case_workspace,
    )
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH
    assert decision.matched_rule == "RULE_RUN_COMMAND_BLOCKED_PATH_MATCHED"


def test_run_command_argv_case_variant_allowed_when_volume_is_sensitive(
    monkeypatch: pytest.MonkeyPatch, case_workspace: Path, docker_backend: None
) -> None:
    """Linux semantics preserved: PRIVATE is a different path, so it is allowed."""
    _patch_policy_probe(monkeypatch, False)
    run = _run_command_run()
    decision = evaluate_policy(
        _run_command_action(run, ["cat", "outputs/PRIVATE/secret.txt"]),
        run,
        case_workspace,
    )
    assert decision.outcome == PolicyOutcome.ALLOW, decision
    assert decision.reason_code == ReasonCode.ALLOWED_TOOL_AND_RESOURCE


# --- Policy: the staged resolved-target check now folds too ----------------


def _staged_alias_workspace(tmp_path: Path) -> Path:
    staged = tmp_path / "staged"
    (staged / "outputs" / "PRIVATE").mkdir(parents=True)
    (staged / "outputs" / "PRIVATE" / "secret.txt").write_text("synthetic", encoding="utf-8")
    (staged / "outputs" / "alias.txt").symlink_to("PRIVATE/secret.txt")
    return staged


def test_staged_resolved_target_case_variant_blocked_when_volume_folds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A resolved alias into a blocked dir is refused when the volume folds."""
    monkeypatch.setattr("scopewatch.policy.volume_is_case_insensitive", lambda root: True)
    staged = _staged_alias_workspace(tmp_path)
    denial = revalidate_run_command_in_workspace(
        _run_command_action(_run_command_run(), ["cat", "outputs/alias.txt"]),
        _run_command_run().task_scope,
        staged,
    )
    assert denial is not None
    assert denial[0] == ReasonCode.BLOCKED_PATH
    assert denial[2] == "RULE_RUN_COMMAND_BLOCKED_PATH_MATCHED"


def test_staged_resolved_target_case_variant_allowed_when_volume_is_sensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("scopewatch.policy.volume_is_case_insensitive", lambda root: False)
    staged = _staged_alias_workspace(tmp_path)
    denial = revalidate_run_command_in_workspace(
        _run_command_action(_run_command_run(), ["cat", "outputs/alias.txt"]),
        _run_command_run().task_scope,
        staged,
    )
    assert denial is None


# --- Runner: _argv_scope_denial and handle_execute now fold ---------------


def test_argv_scope_denial_blocks_case_variant_blocked_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner_mod = _load_runner()
    config = _runner_config(runner_mod, tmp_path, "run-305-ws")
    task_scope = {
        "allowed_paths": ["outputs"],
        "blocked_paths": ["outputs/private"],
        "allowed_commands": [["cat"]],
    }
    root = config.workspace / "run-305-ws"
    # #319 probes each parent directory rather than inheriting one root verdict.
    monkeypatch.setattr(runner_mod, "volume_is_case_insensitive", lambda _: True)
    assert runner_mod._argv_scope_denial(
        ["cat", "outputs/PRIVATE/secret.txt"], root, task_scope, case_insensitive=True
    ) is not None
    monkeypatch.setattr(runner_mod, "volume_is_case_insensitive", lambda _: False)
    assert runner_mod._argv_scope_denial(
        ["cat", "outputs/PRIVATE/secret.txt"], root, task_scope, case_insensitive=False
    ) is None


def test_runner_handle_execute_denies_case_variant_argv_when_volume_folds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner_mod = _load_runner()
    run_workspace = "run-305-ws"
    config = _runner_config(runner_mod, tmp_path, run_workspace)
    _patch_runner_for_dispatch(runner_mod, monkeypatch, True)
    body = _runner_body(
        runner_mod,
        run_workspace,
        {
            "allowed_paths": ["outputs"],
            "blocked_paths": ["outputs/private"],
            "allowed_commands": [["cat"]],
        },
        config.signing_key,
        ".",
        operation="run_command",
        arguments={"argv": ["cat", "outputs/PRIVATE/secret.txt"], "timeout_s": 5},
    )

    status, payload = runner_mod.handle_execute(body, config, runner_mod.DispatchTokenStore())

    assert status == 403
    assert payload == {
        "error": "command argument violates blocked path 'outputs/private'"
    }


# --- docker_job: staging and copy-back fold the blocked comparison --------


def test_docker_job_blocked_checks_fold_case_variants() -> None:
    blocked = (("outputs", "private"),)
    assert docker_job._is_blocked(("outputs", "PRIVATE"), blocked, case_insensitive=True)
    assert not docker_job._is_blocked(("outputs", "PRIVATE"), blocked, case_insensitive=False)
    assert docker_job._is_blocked(
        ("outputs", "PRIVATE", "secret.txt"), blocked, case_insensitive=True
    )
    assert docker_job._contains_blocked(
        ("outputs",), (("OUTPUTS", "Private"),), case_insensitive=True
    )
    assert not docker_job._contains_blocked(
        ("outputs",), (("OUTPUTS", "Private"),), case_insensitive=False
    )


def test_staging_excludes_case_variant_blocked_dir_when_volume_folds(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    (ws / "outputs" / "PRIVATE").mkdir(parents=True)
    (ws / "outputs" / "PRIVATE" / "secret.txt").write_text("SYNTHETIC", encoding="utf-8")
    (ws / "outputs" / "ok.txt").write_text("synthetic", encoding="utf-8")

    staging_root, copy = docker_job.stage_workspace_copy(
        ws, ["outputs/private"], case_insensitive=True
    )
    try:
        assert not (copy / "outputs" / "PRIVATE").exists()
        assert not (copy / "outputs" / "PRIVATE" / "secret.txt").exists()
        assert (copy / "outputs" / "ok.txt").is_file()
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def test_copy_back_refuses_case_variant_blocked_dir_when_volume_folds(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    (ws / "outputs" / "private").mkdir(parents=True)
    (ws / "outputs" / "private" / "secret.txt").write_text("host-original", encoding="utf-8")
    staged = tmp_path / "staged"
    (staged / "outputs" / "PRIVATE").mkdir(parents=True)
    (staged / "outputs" / "PRIVATE" / "secret.txt").write_text(
        "OVERWRITTEN_BY_CONTAINER", encoding="utf-8"
    )

    refused = docker_job.sync_copy_back(staged, ws, ["outputs/private"], case_insensitive=True)

    assert "outputs/PRIVATE" in refused
    assert (ws / "outputs" / "private" / "secret.txt").read_text(encoding="utf-8") == (
        "host-original"
    )


# --- The fold itself: NFD vs NFC when folding is on -----------------------


def test_nfd_vs_nfc_blocked_alias_is_caught_when_folding_is_on() -> None:
    nfc_blocked = "outputs/privé"
    nfd_blocked = "outputs/priv" + "e\u0301"
    nfc_alias = nfc_blocked + "/secret.txt"
    nfd_alias = nfd_blocked + "/secret.txt"
    for blocked, alias in ((nfc_blocked, nfd_alias), (nfd_blocked, nfc_alias)):
        assert scoped_path_denial(
            N(alias), N(alias), ALLOWED, (blocked,), case_insensitive=True
        ) == (BLOCKED_PATH, blocked)
