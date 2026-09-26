"""Deterministic policy engine for Scopewatch baseline."""

import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
import uuid

from scopewatch.executor import get_executor_backend
from scopewatch.models import (
    PolicyOutcome,
    ReasonCode,
    RunStatus,
    SUPPORTED_OPERATIONS,
    SUPPORTED_TOOLS,
)
from scopewatch.schemas import ActionRequest, PolicyDecision, Run, TaskScope


def _normalize_relative_path(path_str: str) -> Optional[Path]:
    """Normalize a relative path string without filesystem resolution."""
    raw = path_str.strip().replace("\\", "/")
    if not raw:
        return Path(".")
    parts = [p for p in raw.split("/") if p and p != "."]
    if not parts:
        return Path(".")
    return Path(*parts)


def _is_descendant_or_equal(child: Path, parent: Path) -> bool:
    """Return True if child is identical to or within parent directory."""
    if parent == Path(".") or str(parent) in ("", "."):
        return True
    child_parts = child.parts
    parent_parts = parent.parts
    if len(child_parts) < len(parent_parts):
        return False
    return child_parts[: len(parent_parts)] == parent_parts


def evaluate_policy(
    action: ActionRequest,
    run: Run,
    workspace_root: Path,
) -> PolicyDecision:
    """Evaluate an ActionRequest deterministically against the Run's TaskScope.

    Follows the 15-step evaluation order specified in baseline requirements.
    """
    decision_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    scope = run.task_scope

    # Step 1: Validate schema and required fields (performed by Pydantic; check non-empty operation/tool/resource)
    if not action.tool or not action.operation:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.MALFORMED_REQUEST,
            explanation="Action request missing tool or operation.",
            matched_rule="RULE_MALFORMED_REQUEST",
            decided_at=now_iso,
            deterministic=True,
        )

    # Step 2: Confirm the run exists and can accept tool calls.
    # WAITING_FOR_APPROVAL still accepts submissions (mirrors the service-layer
    # gate): a turn with two tool calls where the first holds must not deny
    # the second for run-state reasons.
    if run.status != RunStatus.ACTIVE and run.status != RunStatus.WAITING_FOR_APPROVAL:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.OPERATION_NOT_ALLOWED,
            explanation=(
                f"Run '{run.id}' is in state {run.status.value}, "
                "not ACTIVE or WAITING_FOR_APPROVAL."
            ),
            matched_rule="RULE_RUN_NOT_ACTIVE",
            decided_at=now_iso,
            deterministic=True,
        )

    # Step 3: Confirm tool is allowlisted
    if action.tool not in scope.allowed_tools or action.tool not in SUPPORTED_TOOLS:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.TOOL_NOT_ALLOWED,
            explanation=f"Tool '{action.tool}' is not allowlisted in the task scope.",
            matched_rule="RULE_TOOL_NOT_ALLOWED",
            decided_at=now_iso,
            deterministic=True,
        )

    # Step 4: Confirm operation is recognized
    if action.operation not in SUPPORTED_OPERATIONS:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.UNSUPPORTED_OPERATION,
            explanation=f"Operation '{action.operation}' is not supported.",
            matched_rule="RULE_UNSUPPORTED_OPERATION",
            decided_at=now_iso,
            deterministic=True,
        )

    # Step 5: Reject all network operations in the baseline
    if action.operation == "network_request" or bool(scope.allowed_network_destinations):
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.NETWORK_DISABLED,
            explanation="Network access is disabled in the synthetic baseline.",
            matched_rule="RULE_NETWORK_DISABLED",
            decided_at=now_iso,
            deterministic=True,
        )

    # run_command: allowlisted argv execution (issue #36). The agent submits a
    # raw string (arguments["command"], parsed here with shlex) or a pre-split
    # list (arguments["argv"]); execution always uses the argv list form with
    # no shell. This branch runs before the resource-path steps because the
    # resource field carries the command cwd, not a target file.
    if action.operation == "run_command":
        return _evaluate_run_command(action, scope, workspace_root, decision_id, now_iso)

    # Step 6: Normalize the requested resource
    resource_raw = action.resource or ""

    # Step 7: Reject null bytes
    if "\x00" in resource_raw:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.MALFORMED_REQUEST,
            explanation="Resource path contains null bytes.",
            matched_rule="RULE_NULL_BYTE_REJECTED",
            decided_at=now_iso,
            deterministic=True,
        )

    # Step 8: Reject absolute paths
    if resource_raw.startswith("/") or resource_raw.startswith("\\") or (len(resource_raw) > 1 and resource_raw[1] == ":"):
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.PATH_OUTSIDE_WORKSPACE,
            explanation="Absolute paths are prohibited.",
            matched_rule="RULE_ABSOLUTE_PATH_REJECTED",
            decided_at=now_iso,
            deterministic=True,
        )

    # Step 9: Reject '..' traversal
    path_obj = Path(resource_raw)
    if ".." in path_obj.parts:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.PATH_TRAVERSAL,
            explanation="Directory traversal using '..' is prohibited.",
            matched_rule="RULE_PATH_TRAVERSAL_REJECTED",
            decided_at=now_iso,
            deterministic=True,
        )

    normalized_rel = _normalize_relative_path(resource_raw)
    if normalized_rel is None:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.MALFORMED_REQUEST,
            explanation="Invalid resource path format.",
            matched_rule="RULE_MALFORMED_PATH",
            decided_at=now_iso,
            deterministic=True,
        )

    # Step 10: Resolve path against the synthetic workspace
    resolved_workspace = workspace_root.resolve()
    candidate_path = (resolved_workspace / normalized_rel).resolve()

    # Step 11: Reject symlink escapes
    try:
        candidate_path.relative_to(resolved_workspace)
    except ValueError:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.SYMLINK_ESCAPE,
            explanation="Resolved path escapes the synthetic workspace boundary.",
            matched_rule="RULE_SYMLINK_ESCAPE_REJECTED",
            decided_at=now_iso,
            deterministic=True,
        )

    # Also check existing symlinks in path
    test_path = resolved_workspace / normalized_rel
    if test_path.is_symlink():
        target = test_path.resolve()
        try:
            target.relative_to(resolved_workspace)
        except ValueError:
            return PolicyDecision(
                id=decision_id,
                action_request_id=action.id,
                outcome=PolicyOutcome.DENY,
                reason_code=ReasonCode.SYMLINK_ESCAPE,
                explanation="Symlink targets a location outside the synthetic workspace.",
                matched_rule="RULE_SYMLINK_ESCAPE_REJECTED",
                decided_at=now_iso,
                deterministic=True,
            )

    # Step 12: Check blocked paths before allowed paths
    for blocked in scope.blocked_paths:
        blocked_norm = _normalize_relative_path(blocked)
        if blocked_norm and _is_descendant_or_equal(normalized_rel, blocked_norm):
            return PolicyDecision(
                id=decision_id,
                action_request_id=action.id,
                outcome=PolicyOutcome.DENY,
                reason_code=ReasonCode.BLOCKED_PATH,
                explanation=f"Resource '{resource_raw}' is under blocked path '{blocked}'.",
                matched_rule="RULE_BLOCKED_PATH_MATCHED",
                decided_at=now_iso,
                deterministic=True,
            )

    # Check allowed paths
    path_is_allowed = False
    for allowed in scope.allowed_paths:
        allowed_norm = _normalize_relative_path(allowed)
        if allowed_norm and _is_descendant_or_equal(normalized_rel, allowed_norm):
            path_is_allowed = True
            break

    if not path_is_allowed:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.PATH_NOT_ALLOWED,
            explanation=f"Resource '{resource_raw}' is not within any allowed path.",
            matched_rule="RULE_PATH_NOT_ALLOWED",
            decided_at=now_iso,
            deterministic=True,
        )

    # Step 13: Check whether the operation is recognized for the scope
    operation_allowed = action.operation in scope.allowed_operations
    operation_requires_approval = action.operation in scope.requires_approval

    if not operation_allowed and not operation_requires_approval:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.OPERATION_NOT_ALLOWED,
            explanation=f"Operation '{action.operation}' is not permitted by the task scope.",
            matched_rule="RULE_OPERATION_NOT_ALLOWED",
            decided_at=now_iso,
            deterministic=True,
        )

    # Step 14: If the operation requires approval, return HOLD
    if operation_requires_approval:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.HOLD,
            reason_code=ReasonCode.APPROVAL_REQUIRED,
            explanation=f"Operation '{action.operation}' requires explicit reviewer approval.",
            matched_rule="RULE_APPROVAL_REQUIRED",
            decided_at=now_iso,
            deterministic=True,
        )

    # Step 15: Otherwise return ALLOW
    return PolicyDecision(
        id=decision_id,
        action_request_id=action.id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation=f"Operation '{action.operation}' on '{resource_raw}' is permitted.",
        matched_rule="RULE_ALLOWED_TOOL_AND_RESOURCE",
        decided_at=now_iso,
        deterministic=True,
    )


# Characters rejected before parsing a run_command string. Checked against the
# raw submission (not post-split tokens) so quoting cannot smuggle shell
# syntax past the gateway; execution itself never uses a shell.
_RUN_COMMAND_METACHARACTERS = (";", "&", "|", ">", "<", "`", "$(", "\n", "\r", "\x00")


def _contains_shell_metacharacter(text: str) -> bool:
    """Return True if text contains any pre-parse shell metacharacter."""
    return any(marker in text for marker in _RUN_COMMAND_METACHARACTERS)


def _match_prefix_len(argv: list[str], prefixes: list[list[str]]) -> int:
    """Return the length of the first matching allowlist prefix, else 0."""
    for prefix in prefixes:
        if prefix and argv[: len(prefix)] == prefix:
            return len(prefix)
    return 0


def _argv_matches_prefix(argv: list[str], prefixes: list[list[str]]) -> bool:
    """Return True if argv starts with any non-empty allowlist prefix."""
    return _match_prefix_len(argv, prefixes) > 0


def _check_run_command_path(
    value: str,
    *,
    describe: str,
    scope: TaskScope,
    workspace_root: Path,
) -> Union[tuple[ReasonCode, str, str], None]:
    """Apply the existing path rules to one run_command path value.

    Mirrors the resource checks (absolute, null byte, traversal, workspace
    escape, blocked paths) but deliberately omits the allowed_paths
    membership requirement: command arguments and the command cwd only need
    traversal/blocked/containment validation.
    """
    if "\x00" in value:
        return (
            ReasonCode.MALFORMED_REQUEST,
            f"{describe} contains null bytes.",
            "RULE_RUN_COMMAND_NULL_BYTE_REJECTED",
        )
    if (
        value.startswith("/")
        or value.startswith("\\")
        or (len(value) > 1 and value[1] == ":")
    ):
        return (
            ReasonCode.PATH_OUTSIDE_WORKSPACE,
            f"{describe} uses an absolute path outside the workspace.",
            "RULE_RUN_COMMAND_ABSOLUTE_PATH_REJECTED",
        )
    if ".." in Path(value).parts:
        return (
            ReasonCode.PATH_TRAVERSAL,
            f"{describe} uses '..' traversal.",
            "RULE_RUN_COMMAND_PATH_TRAVERSAL_REJECTED",
        )
    normalized = _normalize_relative_path(value)
    if normalized is None:
        return (
            ReasonCode.MALFORMED_REQUEST,
            f"{describe} has an invalid path format.",
            "RULE_RUN_COMMAND_MALFORMED_PATH",
        )
    resolved_workspace = workspace_root.resolve()
    candidate = (resolved_workspace / normalized).resolve()
    try:
        candidate.relative_to(resolved_workspace)
    except ValueError:
        return (
            ReasonCode.SYMLINK_ESCAPE,
            f"{describe} escapes the workspace boundary.",
            "RULE_RUN_COMMAND_SYMLINK_ESCAPE_REJECTED",
        )
    test_path = resolved_workspace / normalized
    if test_path.is_symlink():
        try:
            test_path.resolve().relative_to(resolved_workspace)
        except ValueError:
            return (
                ReasonCode.SYMLINK_ESCAPE,
                f"{describe} is a symlink escaping the workspace.",
                "RULE_RUN_COMMAND_SYMLINK_ESCAPE_REJECTED",
            )
    for blocked in scope.blocked_paths:
        blocked_norm = _normalize_relative_path(blocked)
        if blocked_norm and _is_descendant_or_equal(normalized, blocked_norm):
            return (
                ReasonCode.BLOCKED_PATH,
                f"{describe} is under blocked path '{blocked}'.",
                "RULE_RUN_COMMAND_BLOCKED_PATH_MATCHED",
            )
    return None


def _evaluate_run_command(
    action: ActionRequest,
    scope: TaskScope,
    workspace_root: Path,
    decision_id: str,
    now_iso: str,
) -> PolicyDecision:
    """Deterministically evaluate a run_command action (issue #36).

    Order: docker-only gate, argument extraction, scope operation gate,
    metacharacter rejection, shlex parsing, allowlist prefix match, argument
    path rules, cwd validation, approval hold, else allow.
    """

    def deny(reason_code: ReasonCode, explanation: str, matched_rule: str) -> PolicyDecision:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=reason_code,
            explanation=explanation,
            matched_rule=matched_rule,
            decided_at=now_iso,
            deterministic=True,
        )

    # R1: run_command only works with the Docker executor.
    if get_executor_backend() != "docker":
        return deny(
            ReasonCode.UNSUPPORTED_OPERATION,
            "Operation 'run_command' requires SCOPEWATCH_EXECUTOR=docker; "
            "the local executor cannot run commands.",
            "RULE_RUN_COMMAND_REQUIRES_DOCKER",
        )

    # R2: extract the submission. String form is parsed with shlex below;
    # list form is used directly.
    raw_command = action.arguments.get("command")
    argv_arg = action.arguments.get("argv")
    argv: Optional[list[str]] = None
    if raw_command is not None:
        if not isinstance(raw_command, str):
            return deny(
                ReasonCode.MALFORMED_REQUEST,
                "run_command arguments['command'] must be a string.",
                "RULE_RUN_COMMAND_MALFORMED",
            )
        if not raw_command.strip():
            return deny(
                ReasonCode.MALFORMED_REQUEST,
                "run_command requires a non-empty command string.",
                "RULE_RUN_COMMAND_MALFORMED",
            )
    elif isinstance(argv_arg, list):
        if not argv_arg or not all(isinstance(item, str) for item in argv_arg):
            return deny(
                ReasonCode.MALFORMED_REQUEST,
                "run_command arguments['argv'] must be a non-empty list of strings.",
                "RULE_RUN_COMMAND_MALFORMED",
            )
        argv = list(argv_arg)
    else:
        return deny(
            ReasonCode.MALFORMED_REQUEST,
            "run_command requires arguments['command'] (string) or "
            "arguments['argv'] (list of strings).",
            "RULE_RUN_COMMAND_MALFORMED",
        )

    # R3: the scope must permit the operation (allowed or approval-gated).
    if "run_command" not in scope.allowed_operations and "run_command" not in scope.requires_approval:
        return deny(
            ReasonCode.OPERATION_NOT_ALLOWED,
            "Operation 'run_command' is not permitted by the task scope.",
            "RULE_OPERATION_NOT_ALLOWED",
        )

    # R4: reject shell metacharacters before parsing.
    if raw_command is not None:
        if _contains_shell_metacharacter(raw_command):
            return deny(
                ReasonCode.SHELL_METACHARACTER,
                "Command string contains a shell metacharacter.",
                "RULE_SHELL_METACHARACTER_REJECTED",
            )
        # R5: gateway-side shlex parsing; execution uses the argv list form.
        try:
            argv = shlex.split(raw_command, posix=True)
        except ValueError:
            return deny(
                ReasonCode.MALFORMED_REQUEST,
                "Command string could not be parsed.",
                "RULE_RUN_COMMAND_MALFORMED",
            )
        if not argv:
            return deny(
                ReasonCode.MALFORMED_REQUEST,
                "run_command requires a non-empty command string.",
                "RULE_RUN_COMMAND_MALFORMED",
            )
    else:
        assert argv is not None
        for item in argv:
            if _contains_shell_metacharacter(item):
                return deny(
                    ReasonCode.SHELL_METACHARACTER,
                    "Command argument contains a shell metacharacter.",
                    "RULE_SHELL_METACHARACTER_REJECTED",
                )

    assert argv is not None and len(argv) > 0

    # R6: argv must start with an allowlisted per-scope prefix.
    matched_len = _match_prefix_len(argv, scope.allowed_commands)
    if matched_len == 0:
        return deny(
            ReasonCode.COMMAND_NOT_ALLOWED,
            f"Command '{argv[0]}' does not match any allowlisted command prefix.",
            "RULE_COMMAND_NOT_ALLOWED",
        )

    # R7: path-like arguments use the existing path rules. Only the
    # post-prefix arguments are checked; flag values after '=' are checked
    # as well so '--output=/abs/path' cannot smuggle an absolute path.
    for item in argv[matched_len:]:
        candidates = [item]
        if "=" in item:
            candidates.append(item.split("=", 1)[1])
        for candidate in candidates:
            denial = _check_run_command_path(
                candidate,
                describe=f"Command argument '{candidate}'",
                scope=scope,
                workspace_root=workspace_root,
            )
            if denial is not None:
                reason_code, explanation, matched_rule = denial
                return deny(reason_code, explanation, matched_rule)

    # R8: resource is the cwd: workspace root or a validated subdirectory.
    cwd_raw = action.resource or ""
    cwd_value = "." if not cwd_raw.strip() else cwd_raw
    cwd_denial = _check_run_command_path(
        cwd_value,
        describe="Working directory",
        scope=scope,
        workspace_root=workspace_root,
    )
    if cwd_denial is not None:
        reason_code, explanation, matched_rule = cwd_denial
        return deny(reason_code, explanation, matched_rule)

    # R9: approval-gated commands hold for review through existing approvals.
    if "run_command" in scope.requires_approval or _argv_matches_prefix(
        argv, scope.commands_requiring_approval
    ):
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.HOLD,
            reason_code=ReasonCode.APPROVAL_REQUIRED,
            explanation="Command requires explicit reviewer approval.",
            matched_rule="RULE_APPROVAL_REQUIRED",
            decided_at=now_iso,
            deterministic=True,
        )

    return PolicyDecision(
        id=decision_id,
        action_request_id=action.id,
        outcome=PolicyOutcome.ALLOW,
        reason_code=ReasonCode.ALLOWED_TOOL_AND_RESOURCE,
        explanation=f"Command '{argv[0]}' matches an allowlisted prefix and passed path checks.",
        matched_rule="RULE_RUN_COMMAND_ALLOWED",
        decided_at=now_iso,
        deterministic=True,
    )
