"""Deterministic policy engine for Scopewatch baseline."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import uuid

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

    # Step 2: Confirm the run exists and is active
    if run.status != RunStatus.ACTIVE:
        return PolicyDecision(
            id=decision_id,
            action_request_id=action.id,
            outcome=PolicyOutcome.DENY,
            reason_code=ReasonCode.OPERATION_NOT_ALLOWED,
            explanation=f"Run '{run.id}' is in state {run.status.value}, not ACTIVE.",
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

    # Step 6: Normalize the requested resource
    resource_raw = action.resource or ""

    # Step 8: Reject null bytes
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

    # Step 7: Reject absolute paths
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
