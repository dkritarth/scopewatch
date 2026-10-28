"""Synthetic filesystem semantics for #319; no native APFS assumption."""
from pathlib import Path

import pytest

from scopewatch.scope_paths import BLOCKED_PATH, PATH_NOT_ALLOWED, scoped_path_denial

# Use the existing signed-runner fixture builders; no real Docker/model calls.
from test_issue_305_case_variant_blocked_paths import (
    case_run, case_workspace, _write_action, _load_runner, _runner_config,
    _runner_body, _patch_runner_for_dispatch,
)
from scopewatch.executor import ExecutionSecurityError, _verify_scoped_target
from scopewatch.models import PolicyOutcome, ReasonCode
from scopewatch.policy import evaluate_policy, revalidate_run_command_in_workspace


def check(root, path, *, allowed=(".",), blocked=(), probe=lambda directory: False):
    parts = tuple(Path(path).parts)
    return scoped_path_denial(
        parts, parts, allowed, blocked, workspace_root=str(root), case_probe=probe
    )


def test_existing_hardlink_to_blocked_file_is_denied(tmp_path):
    (tmp_path / "secret").write_text("synthetic")
    (tmp_path / "alias").hardlink_to(tmp_path / "secret")
    assert check(tmp_path, "alias", blocked=("secret",)) == (BLOCKED_PATH, "secret")


def test_directory_semantics_apply_to_children_not_directory_name(tmp_path):
    (tmp_path / "outputs").mkdir()
    probe = lambda directory: Path(directory) == tmp_path / "outputs"
    assert check(tmp_path, "outputs/PRIVATE/new", blocked=("outputs/private",), probe=probe) == (
        BLOCKED_PATH, "outputs/private"
    )
    assert check(tmp_path, "OUTPUTS/new", allowed=("outputs",), probe=probe) == (PATH_NOT_ALLOWED, None)


@pytest.mark.parametrize("verdict", [True, None])
def test_existing_distinct_names_override_blocked_overfold(tmp_path, verdict):
    for name in ("straße", "strasse"):
        (tmp_path / name).mkdir()
    assert check(tmp_path, "strasse/new", blocked=("straße",), probe=lambda _: verdict) is None


def test_unknown_child_never_widens_allowed_paths(tmp_path):
    (tmp_path / "outputs").mkdir()
    probe = lambda directory: True if Path(directory) == tmp_path else None
    assert check(tmp_path, "outputs/REPORT/new", allowed=("outputs/report",), probe=probe) == (
        PATH_NOT_ALLOWED, None
    )
    assert check(tmp_path, "outputs/PRIVATE/new", blocked=("outputs/private",), probe=probe) == (
        BLOCKED_PATH, "outputs/private"
    )


def test_nonexistent_allowlist_does_not_expand_sharp_s(tmp_path):
    assert check(tmp_path, "strasse/new", allowed=("straße",), probe=lambda _: True) == (
        PATH_NOT_ALLOWED, None
    )


def test_directory_flag_change_does_not_reuse_sensitive_cache(tmp_path, monkeypatch):
    import scopewatch.scope_paths as paths

    paths.clear_case_probe_cache()
    flags = [0]
    calls = []
    monkeypatch.setattr(paths, "_linux_directory_flags", lambda _: flags[0])
    monkeypatch.setattr(paths, "_probe_case_insensitivity", lambda directory: calls.append(directory) or False)
    assert paths.volume_is_case_insensitive(str(tmp_path)) is False
    flags[0] = 0x40000000  # synthetic FS_CASEFOLD_FL, no native +F claim
    assert paths.volume_is_case_insensitive(str(tmp_path)) is True
    assert len(calls) == 1
    paths.clear_case_probe_cache()


def test_replaced_directory_gets_a_new_probe(tmp_path, monkeypatch):
    import scopewatch.scope_paths as paths

    directory = tmp_path / "workspace"
    directory.mkdir()
    answers = iter([False, True])
    monkeypatch.setattr(paths, "_linux_directory_flags", lambda _: None)
    monkeypatch.setattr(paths, "_probe_case_insensitivity", lambda _: next(answers))
    assert paths.volume_is_case_insensitive(str(directory)) is False
    directory.rename(tmp_path / "old")
    directory.mkdir()
    assert paths.volume_is_case_insensitive(str(directory)) is True
    paths.clear_case_probe_cache()


def test_staging_and_copyback_protect_existing_hardlinks(tmp_path):
    import shutil
    from scopewatch.docker_job import stage_workspace_copy, sync_copy_back
    from scopewatch.scope_paths import blocked_path_matcher

    live = tmp_path / "live"
    live.mkdir()
    (live / "secret").write_text("synthetic secret")
    (live / "alias").hardlink_to(live / "secret")
    matcher = blocked_path_matcher(str(live), lambda _: False)
    staging, copy = stage_workspace_copy(live, ["secret"], path_matcher=matcher)
    try:
        assert not (copy / "alias").exists()
        assert not (copy / "secret").exists()
        (copy / "alias").write_text("synthetic overwrite")
        assert sync_copy_back(copy, live, ["secret"], path_matcher=matcher) == ["alias"]
        assert (live / "secret").read_text() == "synthetic secret"
    finally:
        shutil.rmtree(staging)


@pytest.mark.parametrize("child_verdict", [True, None])
def test_policy_local_and_remote_dispatch_agree_on_child_block(
    case_run, case_workspace, tmp_path, monkeypatch, child_verdict,
):
    resource = "outputs/PRIVATE/new.txt"
    probe = lambda directory: child_verdict if Path(directory).name == "outputs" else False
    monkeypatch.setattr("scopewatch.policy.volume_is_case_insensitive", probe)
    monkeypatch.setattr("scopewatch.executor.volume_is_case_insensitive", probe)
    decision = evaluate_policy(_write_action(case_run, resource), case_run, case_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH
    assert decision.deterministic is True
    with pytest.raises(ExecutionSecurityError, match="scope"):
        _verify_scoped_target(case_workspace, resource, case_run.task_scope)

    runner = _load_runner()
    config = _runner_config(runner, tmp_path, "run-319")
    _patch_runner_for_dispatch(runner, monkeypatch, False)
    monkeypatch.setattr(runner, "volume_is_case_insensitive", probe)
    body = _runner_body(
        runner, "run-319", case_run.task_scope.model_dump(), config.signing_key, resource,
    )
    status, payload = runner.handle_execute(body, config, runner.DispatchTokenStore())
    assert status == 403
    assert payload == {"error": "resource violates task scope"}
    assert not (case_workspace / resource).exists()
    assert not (config.workspace / "run-319" / resource).exists()


def test_hardlink_denial_is_deterministic_and_rechecked_at_dispatch(case_run, case_workspace):
    (case_workspace / "outputs/private").mkdir()
    secret = case_workspace / "outputs/private/secret"
    secret.write_text("synthetic secret")
    (case_workspace / "outputs/alias").hardlink_to(secret)
    case_run.task_scope.blocked_paths = ["outputs/private/secret"]
    action = _write_action(case_run, "outputs/alias")
    decision = evaluate_policy(action, case_run, case_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH
    assert decision.deterministic is True
    with pytest.raises(ExecutionSecurityError):
        _verify_scoped_target(case_workspace, action.resource, case_run.task_scope)
    assert secret.read_text() == "synthetic secret"


def test_command_policy_and_dispatch_use_child_semantics(case_run, case_workspace, monkeypatch, docker_backend):
    probe = lambda directory: Path(directory).name == "outputs"
    monkeypatch.setattr("scopewatch.policy.volume_is_case_insensitive", probe)
    case_run.task_scope.allowed_operations = ["run_command"]
    case_run.task_scope.allowed_commands = [["cat"]]
    action = _write_action(case_run, ".")
    action.operation = "run_command"
    action.arguments = {"argv": ["cat", "outputs/PRIVATE/new.txt"]}
    decision = evaluate_policy(action, case_run, case_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.BLOCKED_PATH
    denial = revalidate_run_command_in_workspace(action, case_run.task_scope, case_workspace)
    assert denial is not None
    assert denial[0] == ReasonCode.BLOCKED_PATH
    runner = _load_runner()
    monkeypatch.setattr(runner, "volume_is_case_insensitive", probe)
    assert runner._argv_scope_denial(
        action.arguments["argv"], case_workspace, case_run.task_scope.model_dump(),
    ) is not None


def test_legacy_allowlist_folding_does_not_expand_sharp_s():
    assert scoped_path_denial(
        ("strasse", "new"), ("strasse", "new"), ["straße"], [], case_insensitive=True,
    ) == (PATH_NOT_ALLOWED, None)


def test_missing_identity_retains_conservative_blocked_overfold(tmp_path):
    assert check(tmp_path, "strasse/new", blocked=("straße",), probe=lambda _: True) == (
        BLOCKED_PATH, "straße"
    )


def test_samefile_evidence_never_widens_an_unknown_allowlist(tmp_path):
    (tmp_path / "outputs").mkdir()
    if not (tmp_path / "OUTPUTS").exists():
        (tmp_path / "OUTPUTS").symlink_to("outputs", target_is_directory=True)
    assert check(tmp_path, "OUTPUTS/new", allowed=("outputs",), probe=lambda _: None) == (
        PATH_NOT_ALLOWED, None
    )


def test_remote_dispatch_refuses_existing_hardlink(tmp_path, monkeypatch, case_run):
    runner = _load_runner()
    config = _runner_config(runner, tmp_path, "run-319-hardlink")
    root = config.workspace / "run-319-hardlink"
    (root / "outputs/secret").write_text("synthetic protected")
    (root / "outputs/alias").hardlink_to(root / "outputs/secret")
    _patch_runner_for_dispatch(runner, monkeypatch, False)
    scope = case_run.task_scope.model_dump()
    scope["blocked_paths"] = ["outputs/secret"]
    body = _runner_body(runner, "run-319-hardlink", scope, config.signing_key, "outputs/alias")
    status, payload = runner.handle_execute(body, config, runner.DispatchTokenStore())
    assert status == 403
    assert payload == {"error": "resource violates task scope"}
    assert (root / "outputs/secret").read_text() == "synthetic protected"


def test_distinct_existing_unicode_paths_stay_allowed_across_dispatch(
    case_run, case_workspace, tmp_path, monkeypatch,
):
    case_run.task_scope.blocked_paths = ["outputs/straße"]
    for name in ("straße", "strasse"):
        (case_workspace / "outputs" / name).mkdir()
    monkeypatch.setattr("scopewatch.policy.volume_is_case_insensitive", lambda _: True)
    monkeypatch.setattr("scopewatch.executor.volume_is_case_insensitive", lambda _: True)
    resource = "outputs/strasse/new"
    decision = evaluate_policy(_write_action(case_run, resource), case_run, case_workspace)
    assert decision.outcome == PolicyOutcome.ALLOW
    assert _verify_scoped_target(case_workspace, resource, case_run.task_scope) == case_workspace / resource
    runner = _load_runner()
    config = _runner_config(runner, tmp_path, "run-319-unicode")
    for name in ("straße", "strasse"):
        (config.workspace / "run-319-unicode/outputs" / name).mkdir()
    _patch_runner_for_dispatch(runner, monkeypatch, True)
    body = _runner_body(
        runner, "run-319-unicode", case_run.task_scope.model_dump(), config.signing_key, resource,
    )
    status, payload = runner.handle_execute(body, config, runner.DispatchTokenStore())
    assert status == 200
    assert payload["status"] == "EXECUTED"


def test_unknown_child_refuses_allowed_alias_across_dispatch(
    case_run, case_workspace, tmp_path, monkeypatch,
):
    resource = "outputs/REPORT/new"
    case_run.task_scope.allowed_paths = ["outputs/report"]
    probe = lambda directory: None if Path(directory).name == "outputs" else False
    monkeypatch.setattr("scopewatch.policy.volume_is_case_insensitive", probe)
    monkeypatch.setattr("scopewatch.executor.volume_is_case_insensitive", probe)
    decision = evaluate_policy(_write_action(case_run, resource), case_run, case_workspace)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == ReasonCode.PATH_NOT_ALLOWED
    with pytest.raises(ExecutionSecurityError):
        _verify_scoped_target(case_workspace, resource, case_run.task_scope)
    runner = _load_runner()
    config = _runner_config(runner, tmp_path, "run-319-unknown")
    _patch_runner_for_dispatch(runner, monkeypatch, False)
    monkeypatch.setattr(runner, "volume_is_case_insensitive", probe)
    body = _runner_body(
        runner, "run-319-unknown", case_run.task_scope.model_dump(), config.signing_key, resource,
    )
    status, _ = runner.handle_execute(body, config, runner.DispatchTokenStore())
    assert status == 403
