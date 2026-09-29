"""Graded severity prototype for Scopewatch flags (issue #50).

Display + triage ONLY. This module never changes ALLOW / HOLD / DENY
decisions: it maps an already-made decision (policy reason code) plus the
auditor signal (verdict + concern type) to a severity badge that only
reorders / annotates the reviewer queue.

Status: idea-stage prototype for discussion. Not an accepted design.
See ``README.md`` in this directory and the decision memo at
``docs/ideas/muse-spark-2026-09-28-severity-levels.md``.

Grounding (read from ``origin/main``, never modified by this prototype):

- ``backend/scopewatch/models.py``: ``ReasonCode`` (17 codes),
  ``PolicyOutcome`` (ALLOW / HOLD / DENY).
- ``backend/scopewatch/policy.py``: ``evaluate_policy`` 15-step order and
  ``_evaluate_run_command`` R1..R9; deterministic DENY codes and the two
  HOLD codes (``APPROVAL_REQUIRED``, run_command approval gate).
- ``backend/scopewatch/service.py``: escalate-only merge -- auditor
  ``CONCERN`` -> HOLD as ``REASONING_SCOPE_CONCERN``, auditor ``FAILED``
  (or unbuildable auditor) -> HOLD as ``REASONING_AUDIT_FAILED``;
  policy DENY is final and never relaxed by the auditor.
- ``backend/scopewatch/reasoning_audit.py``: ``ReasoningAuditVerdict``
  (``NO_CONCERN`` / ``CONCERN`` / ``FAILED``) and ``AuditConcernType``
  (``SCOPE_DRIFT`` / ``INJECTION_FOLLOWING`` / ``EXFILTRATION_INTENT`` /
  ``POLICY_EVASION``).

Rules enforced here:

1. Severity NEVER changes decisions (there is no decision output at all).
2. Auditor overlays only ever raise the displayed level, never lower it.
3. Ambiguous inputs resolve HIGHER, never lower.
4. Fail closed: unknown policy codes, verdicts, or concern types yield
   ``Ungraded`` with ``escalate=True`` (needs human classification),
   never a downgrade and never ``Low``.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

# Severity levels, low to high. UNGRADED is not a rank in the normal scale:
# it means "the rubric does not understand this input; classify by hand".
LOW = "Low"
MEDIUM = "Medium"
HIGH = "High"
EXTREME = "Extreme"
UNGRADED = "Ungraded"

_RANK = {LOW: 0, MEDIUM: 1, HIGH: 2, EXTREME: 3}

# Triage queue order, highest priority first. Ungraded sorts above High:
# an unknown signal needs human eyes promptly, but a confirmed
# exfiltration-shaped block still jumps the queue first. (Open question
# for maintainers -- see the decision memo.)
TRIAGE_ORDER = (EXTREME, UNGRADED, HIGH, MEDIUM, LOW)

# ---------------------------------------------------------------------------
# Base table: every ReasonCode on origin/main mapped to a default level.
# Rationale per group is in README.md. HOLD codes never map below Medium;
# ALLOW maps to Low; technique-shaped DENY codes map to High; routine
# scope-mismatch DENY codes map to Medium.
# ---------------------------------------------------------------------------
_BASE_SEVERITY: dict[str, str] = {
    # ALLOW -- benign baseline.
    "ALLOWED_TOOL_AND_RESOURCE": LOW,
    # HOLD -- routine reviewer gate / auditor escalations. Never Low.
    "APPROVAL_REQUIRED": MEDIUM,
    "REASONING_SCOPE_CONCERN": HIGH,
    "REASONING_AUDIT_FAILED": HIGH,
    # DENY, routine scope mismatch -- likely benign misconfiguration.
    "TOOL_NOT_ALLOWED": MEDIUM,
    "OPERATION_NOT_ALLOWED": MEDIUM,
    "UNSUPPORTED_OPERATION": MEDIUM,
    "PATH_NOT_ALLOWED": MEDIUM,
    "COMMAND_NOT_ALLOWED": MEDIUM,
    "MALFORMED_REQUEST": MEDIUM,
    # DENY, technique-shaped -- evasion / escape / forbidden-target shape.
    "BLOCKED_PATH": HIGH,
    "PATH_TRAVERSAL": HIGH,
    "SYMLINK_ESCAPE": HIGH,
    "PATH_OUTSIDE_WORKSPACE": HIGH,
    "NETWORK_DISABLED": HIGH,
    "SHELL_METACHARACTER": HIGH,
    # DENY, gateway-internal error -- unknown state, escalate.
    "POLICY_ERROR": HIGH,
}

# Decision outcome mirrored per code (from policy.py / service.py on
# origin/main). Used ONLY for the exfil-on-DENY -> Extreme rule and for
# property tests. This table never decides anything.
_OUTCOME: dict[str, str] = {
    "ALLOWED_TOOL_AND_RESOURCE": "ALLOW",
    "APPROVAL_REQUIRED": "HOLD",
    "REASONING_SCOPE_CONCERN": "HOLD",
    "REASONING_AUDIT_FAILED": "HOLD",
    "TOOL_NOT_ALLOWED": "DENY",
    "OPERATION_NOT_ALLOWED": "DENY",
    "UNSUPPORTED_OPERATION": "DENY",
    "PATH_NOT_ALLOWED": "DENY",
    "COMMAND_NOT_ALLOWED": "DENY",
    "MALFORMED_REQUEST": "DENY",
    "BLOCKED_PATH": "DENY",
    "PATH_TRAVERSAL": "DENY",
    "SYMLINK_ESCAPE": "DENY",
    "PATH_OUTSIDE_WORKSPACE": "DENY",
    "NETWORK_DISABLED": "DENY",
    "SHELL_METACHARACTER": "DENY",
    "POLICY_ERROR": "DENY",
}

# Auditor concern floors (display priority only). SCOPE_DRIFT is
# non-adversarial (unprompted extra work); the other three describe
# adversarial-shaped reasoning and floor at High.
_CONCERN_FLOOR: dict[str, str] = {
    "SCOPE_DRIFT": MEDIUM,
    "INJECTION_FOLLOWING": HIGH,
    "POLICY_EVASION": HIGH,
    "EXFILTRATION_INTENT": HIGH,
}

_VERDICTS = ("NO_CONCERN", "CONCERN", "FAILED")

# Badge spec: icon + text, never color-only. Icons are plain geometric
# shapes (no emoji); the text label is always rendered as real text so
# screen readers and monochrome displays get the full signal.
_BADGES: dict[str, tuple[str, str]] = {
    LOW: ("○", "LOW — routine"),
    MEDIUM: ("◎", "MEDIUM — review in queue"),
    HIGH: ("▲", "HIGH — review soon"),
    EXTREME: ("⬢", "EXTREME — review immediately"),
    UNGRADED: ("?", "UNGRADED — classify by hand"),
}

# Illustrative reviewer SLA hints. These are placeholders for maintainers
# to set; they carry no enforcement in this prototype.
_SLA_HINTS: dict[str, str] = {
    LOW: "batch; no action required",
    MEDIUM: "review within the normal review window (e.g. 1 business day)",
    HIGH: "review in the current review session",
    EXTREME: "review immediately; consider pausing the run pending review",
    UNGRADED: "triage first: classify the signal, then apply the SLA above",
}


class SeverityResult(NamedTuple):
    """Display-only severity for one decided action.

    Contains no decision field on purpose: there is deliberately no way
    to read an ALLOW / HOLD / DENY out of this object.
    """

    level: str
    badge_text: str
    badge_icon: str
    sla_hint: str
    escalate: bool
    explanation: str


def _result(level: str, explanation: str) -> SeverityResult:
    icon, text = _BADGES[level]
    return SeverityResult(
        level=level,
        badge_text=text,
        badge_icon=icon,
        sla_hint=_SLA_HINTS[level],
        escalate=level in (HIGH, EXTREME, UNGRADED),
        explanation=explanation,
    )


def severity_for(
    policy_code: Optional[str],
    audit_verdict: Optional[str] = None,
    concern_type: Optional[str] = None,
) -> SeverityResult:
    """Map (policy reason code, auditor verdict, concern type) to a severity.

    Args:
        policy_code: a ``ReasonCode`` value (e.g. ``"BLOCKED_PATH"``).
        audit_verdict: ``"NO_CONCERN"`` / ``"CONCERN"`` / ``"FAILED"``,
            or ``None`` when no audit ran for the action.
        concern_type: one of the four ``AuditConcernType`` values, or
            ``None`` when the auditor raised no concern.

    Matching is strict and exact: anything not in the known vocabularies
    (wrong case, whitespace, empty string, ``None`` policy code) yields
    ``Ungraded`` with ``escalate=True``. Unknown never downgrades.
    """
    if policy_code not in _BASE_SEVERITY:
        return _result(
            UNGRADED,
            f"Unknown policy_code {policy_code!r}: rubric has no mapping; "
            "classify by hand instead of guessing a lower level.",
        )
    if audit_verdict is not None and audit_verdict not in _VERDICTS:
        return _result(
            UNGRADED,
            f"Unknown audit_verdict {audit_verdict!r}: auditor signal is "
            "unreadable; classify by hand instead of guessing.",
        )
    if concern_type is not None and concern_type not in _CONCERN_FLOOR:
        return _result(
            UNGRADED,
            f"Unknown concern_type {concern_type!r}: auditor signal is "
            "unreadable; classify by hand instead of guessing.",
        )

    base = _BASE_SEVERITY[policy_code]
    assert policy_code is not None  # narrowed by the membership check above
    outcome = _OUTCOME[policy_code]

    if audit_verdict == "FAILED":
        # Audit blind spot: the gateway held the action fail-closed, but
        # the reviewer has no auditor evidence. Floor at High; never lower.
        level = base if _RANK[base] >= _RANK[HIGH] else HIGH
        return _result(
            level,
            f"{policy_code} with a failed auditor audit: no reasoning "
            f"evidence available, holding display priority at {level} "
            "(base was Higher-or-equal kept, otherwise raised to High).",
        )
    if audit_verdict == "CONCERN":
        if concern_type is None:
            # Inconsistent but possible: CONCERN with no typed concern.
            # Ambiguous -> higher, never lower.
            level = base if _RANK[base] >= _RANK[HIGH] else HIGH
            return _result(
                level,
                f"{policy_code} with an untyped auditor CONCERN: ambiguous "
                f"signal resolved upward to {level}, never downward.",
            )
        if concern_type == "EXFILTRATION_INTENT" and outcome == "DENY":
            # Blocked exfiltration-shaped attempt: highest triage priority.
            # The decision was already DENY; this only reorders review.
            return _result(
                EXTREME,
                f"{policy_code} paired with EXFILTRATION_INTENT on a denied "
                "action: blocked exfiltration shape, top of the review queue.",
            )
        floor = _CONCERN_FLOOR[concern_type]
        level = base if _RANK[base] >= _RANK[floor] else floor
        return _result(
            level,
            f"{policy_code} with auditor concern {concern_type}: display "
            f"priority is max(base {base}, concern floor {floor}) = {level}; "
            "concerns never lower the base level.",
        )
    # NO_CONCERN or no audit: the deterministic base stands as-is.
    return _result(
        base,
        f"{policy_code} with no auditor concern: base display level {base}.",
    )
