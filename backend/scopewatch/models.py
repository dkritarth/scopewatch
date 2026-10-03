"""Domain enumerations and constants for Scopewatch."""

import hashlib
import json
from enum import Enum
from typing import Optional


class RunStatus(str, Enum):
    ACTIVE = "ACTIVE"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PolicyOutcome(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    HOLD = "HOLD"


class ReasonCode(str, Enum):
    ALLOWED_TOOL_AND_RESOURCE = "ALLOWED_TOOL_AND_RESOURCE"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    OPERATION_NOT_ALLOWED = "OPERATION_NOT_ALLOWED"
    PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    BLOCKED_PATH = "BLOCKED_PATH"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    SYMLINK_ESCAPE = "SYMLINK_ESCAPE"
    NETWORK_DISABLED = "NETWORK_DISABLED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    SHELL_METACHARACTER = "SHELL_METACHARACTER"
    COMMAND_NOT_ALLOWED = "COMMAND_NOT_ALLOWED"
    POLICY_ERROR = "POLICY_ERROR"
    REASONING_SCOPE_CONCERN = "REASONING_SCOPE_CONCERN"
    REASONING_AUDIT_FAILED = "REASONING_AUDIT_FAILED"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class ExecutionStatus(str, Enum):
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    NOT_EXECUTED = "NOT_EXECUTED"


class EventType(str, Enum):
    RUN_CREATED = "RUN_CREATED"
    ACTION_REQUESTED = "ACTION_REQUESTED"
    POLICY_ALLOWED = "POLICY_ALLOWED"
    POLICY_DENIED = "POLICY_DENIED"
    POLICY_HELD = "POLICY_HELD"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    RUN_COMPLETED = "RUN_COMPLETED"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    REASONING_AUDIT_COMPLETED = "REASONING_AUDIT_COMPLETED"
    REASONING_AUDIT_FAILED = "REASONING_AUDIT_FAILED"


class ReasoningProvenance(str, Enum):
    UNAVAILABLE = "UNAVAILABLE"
    AGENT_AUTHORED_SUMMARY = "AGENT_AUTHORED_SUMMARY"
    PROVIDER_EXPOSED_TRACE = "PROVIDER_EXPOSED_TRACE"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    # #116: an API caller asserted this origin; the gateway did not verify it.
    # The raw claim is kept in ActionRequest.caller_claimed_provenance.
    CALLER_ASSERTED_PROVIDER_TRACE = "CALLER_ASSERTED_PROVIDER_TRACE"
    CALLER_ASSERTED_SUMMARY = "CALLER_ASSERTED_SUMMARY"


SUPPORTED_TOOLS = {"workspace"}
SUPPORTED_OPERATIONS = {
    "list_directory",
    "read_text",
    "write_text",
    "delete_path",
    "network_request",
    "run_command",
}
EXECUTABLE_OPERATIONS = {
    "list_directory",
    "read_text",
    "write_text",
    "run_command",
}

# Characters rejected before parsing a run_command string. Checked against the
# raw submission (not post-split tokens) so quoting cannot smuggle shell
# syntax past the gateway; execution itself never uses a shell. Declared here
# rather than in policy.py so the policy fingerprint below can cover it with
# the other declared rule-set facts.
RUN_COMMAND_METACHARACTERS = (";", "&", "|", ">", "<", "`", "$(", "\n", "\r", "\x00")


# ---------------------------------------------------------------------------
# Deterministic policy identity (issue #119)
# ---------------------------------------------------------------------------
# ``policy_version`` names *the deterministic policy implementation and rule
# set* that produced a decision. It is deliberately independent of
# ``schema_version`` ("1"), which describes the shape of the stored data: a
# persisted scope snapshot is useless for reproducing a result once the code
# that read it has changed, and the data format does not have to change for
# that to happen.
#
# The stored value is ``"{POLICY_RULES_REVISION}+{fingerprint}"``:
#
# * ``POLICY_RULES_REVISION`` is a human-readable label, ``YYYY-MM-DD.N`` for
#   the Nth revision on that date. It is bumped by hand.
# * ``policy_rules_fingerprint()`` is a short SHA-256 prefix over the declared
#   rule-set facts in this module. It is derived, so it cannot drift: editing
#   a reason code, the supported tool/operation sets, or the shell
#   metacharacter list changes it automatically.
#
# Update semantics:
#
# 1. Bump ``POLICY_RULES_REVISION`` whenever deterministic evaluation changes:
#    evaluation order, rule logic in ``policy.py`` / ``path_access.py``,
#    ``matched_rule`` strings, approval semantics, or dispatch-time gating. The
#    fingerprint cannot see evaluation order or helper logic, so the manual
#    bump is still required for those.
# 2. No manual action is needed for edits the fingerprint already covers; the
#    new fingerprint makes the revision distinguishable on its own.
# 3. There is deliberately no environment-variable override. An
#    operator-settable identity would let stored evidence name a policy
#    revision that never evaluated the action, which is exactly the failure
#    mode this field exists to prevent. Tests simulate a policy upgrade by
#    monkeypatching ``POLICY_RULES_REVISION``.
# 4. Decisions recorded before policy versions existed keep ``NULL`` and are
#    reported as ``LEGACY_POLICY_VERSION_LABEL``. They are never backfilled:
#    stamping today's revision onto a past decision would assert something
#    untrue about when it was evaluated.
POLICY_RULES_REVISION = "2026-10-03.1"

#: Display label for decisions recorded before policy versions were stored.
LEGACY_POLICY_VERSION_LABEL = "unknown (legacy)"

#: Hex characters of the SHA-256 rule-set fingerprint kept in the version.
POLICY_FINGERPRINT_LENGTH = 12


def policy_rules_fingerprint() -> str:
    """Return a fingerprint of the declared deterministic rule-set facts.

    Covers: reason codes, supported tools, supported and executable
    operations, and the ``run_command`` shell metacharacter set. Does *not*
    cover evaluation order or rule logic in :mod:`scopewatch.policy` and
    :mod:`scopewatch.path_access`; bump ``POLICY_RULES_REVISION`` for those.
    """
    payload = {
        "reason_codes": sorted(code.value for code in ReasonCode),
        "supported_tools": sorted(SUPPORTED_TOOLS),
        "supported_operations": sorted(SUPPORTED_OPERATIONS),
        "executable_operations": sorted(EXECUTABLE_OPERATIONS),
        "run_command_metacharacters": list(RUN_COMMAND_METACHARACTERS),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:POLICY_FINGERPRINT_LENGTH]


def get_policy_version() -> str:
    """Return the policy identity to stamp on decisions made right now."""
    return f"{POLICY_RULES_REVISION}+{policy_rules_fingerprint()}"


def format_policy_version(policy_version: Optional[str]) -> str:
    """Return the display label for a stored policy identity.

    ``None`` or empty means the decision predates policy-version tracking and
    stays visibly unknown rather than being assigned a version after the fact.
    """
    if policy_version and policy_version.strip():
        return policy_version
    return LEGACY_POLICY_VERSION_LABEL
