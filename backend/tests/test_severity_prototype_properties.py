"""Property + unit tests for the severity-levels prototype (issue #50).

NEW file only: no backend modules are imported or modified. The prototype
lives in ``poc/severity-levels/severity.py`` and is loaded via importlib so
these tests also guard the "prototype is dependency-free" property.

What is pinned here (display-only, idea stage -- not accepted design):

- determinism (pure function),
- monotonicity (auditor overlays never lower a level; HOLD never Low;
  DENY + exfiltration-intent never below High),
- fail-closed unknowns (-> Ungraded + escalate, never a downgrade),
- severity carries no decision (ALLOW / HOLD / DENY untouched by design:
  the result object cannot even express one).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SEVERITY_PATH = (
    Path(__file__).resolve().parents[2] / "poc" / "severity-levels" / "severity.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("severity_prototype", _SEVERITY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sev = _load()

HOLD_CODES = [
    "APPROVAL_REQUIRED",
    "REASONING_SCOPE_CONCERN",
    "REASONING_AUDIT_FAILED",
]
DENY_CODES = [
    "TOOL_NOT_ALLOWED",
    "OPERATION_NOT_ALLOWED",
    "UNSUPPORTED_OPERATION",
    "PATH_NOT_ALLOWED",
    "COMMAND_NOT_ALLOWED",
    "MALFORMED_REQUEST",
    "BLOCKED_PATH",
    "PATH_TRAVERSAL",
    "SYMLINK_ESCAPE",
    "PATH_OUTSIDE_WORKSPACE",
    "NETWORK_DISABLED",
    "SHELL_METACHARACTER",
    "POLICY_ERROR",
]
ALL_CODES = ["ALLOWED_TOOL_AND_RESOURCE"] + HOLD_CODES + DENY_CODES
VERDICTS = [None, "NO_CONCERN", "CONCERN", "FAILED"]
CONCERNS = [None, "SCOPE_DRIFT", "INJECTION_FOLLOWING", "POLICY_EVASION", "EXFILTRATION_INTENT"]


def test_prototype_module_loads_standalone():
    assert hasattr(sev, "severity_for")
    assert sev.TRIAGE_ORDER == ("Extreme", "Ungraded", "High", "Medium", "Low")


def test_rubric_covers_every_known_reason_code():
    assert set(sev._BASE_SEVERITY) == set(ALL_CODES)
    assert len(sev._BASE_SEVERITY) == 17


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ALLOWED_TOOL_AND_RESOURCE", "Low"),
        ("APPROVAL_REQUIRED", "Medium"),
        ("TOOL_NOT_ALLOWED", "Medium"),
        ("MALFORMED_REQUEST", "Medium"),
        ("COMMAND_NOT_ALLOWED", "Medium"),
        ("BLOCKED_PATH", "High"),
        ("NETWORK_DISABLED", "High"),
        ("REASONING_SCOPE_CONCERN", "High"),
        ("REASONING_AUDIT_FAILED", "High"),
        ("POLICY_ERROR", "High"),
    ],
)
def test_base_table_spot_checks(code, expected):
    assert sev.severity_for(code, "NO_CONCERN", None).level == expected


def test_determinism_same_inputs_same_output():
    for code in ALL_CODES:
        for verdict in VERDICTS:
            for concern in CONCERNS:
                first = sev.severity_for(code, verdict, concern)
                second = sev.severity_for(code, verdict, concern)
                assert first == second, (code, verdict, concern)


@pytest.mark.parametrize("code", HOLD_CODES)
@pytest.mark.parametrize("verdict", VERDICTS)
@pytest.mark.parametrize("concern", CONCERNS)
def test_hold_outcomes_never_low(code, verdict, concern):
    assert sev.severity_for(code, verdict, concern).level != "Low"


@pytest.mark.parametrize("code", DENY_CODES)
def test_deny_with_exfiltration_intent_never_below_high(code):
    level = sev.severity_for(code, "CONCERN", "EXFILTRATION_INTENT").level
    assert level in ("High", "Extreme"), code


@pytest.mark.parametrize(
    "code", ["BOGUS_CODE", "", "blocked_path", " BLOCKED_PATH ", "ALLOW", "DENY"]
)
def test_unknown_policy_code_is_ungraded_and_escalates(code):
    result = sev.severity_for(code, "NO_CONCERN", None)
    assert result.level == "Ungraded"
    assert result.escalate is True


def test_none_policy_code_is_ungraded():
    result = sev.severity_for(None)
    assert result.level == "Ungraded"
    assert result.escalate is True


@pytest.mark.parametrize("verdict", ["MAYBE", "", "concern", "Concern", 123])
def test_unknown_verdict_is_ungraded(verdict):
    result = sev.severity_for("BLOCKED_PATH", verdict, None)
    assert result.level == "Ungraded"
    assert result.escalate is True


@pytest.mark.parametrize("concern", ["HACKING", "", "exfiltration_intent", 42])
def test_unknown_concern_is_ungraded(concern):
    result = sev.severity_for("BLOCKED_PATH", "CONCERN", concern)
    assert result.level == "Ungraded"
    assert result.escalate is True


def test_ungraded_never_downgrades_a_hold():
    # Even garbage auditor input on a HOLD code must not come back Low/Medium.
    result = sev.severity_for("APPROVAL_REQUIRED", "MAYBE", "HACKING")
    assert result.level == "Ungraded"
    assert result.level not in ("Low", "Medium")


def test_concern_overlay_never_lowers_base():
    base_rank = sev._RANK
    for code in ALL_CODES:
        base_level = sev.severity_for(code, "NO_CONCERN", None).level
        assert base_level != "Ungraded"
        for concern in CONCERNS[1:]:
            raised = sev.severity_for(code, "CONCERN", concern).level
            assert raised != "Ungraded"
            assert base_rank[raised] >= base_rank[base_level], (code, concern)


def test_failed_audit_never_lowers_base():
    base_rank = sev._RANK
    for code in ALL_CODES:
        base_level = sev.severity_for(code, "NO_CONCERN", None).level
        failed_level = sev.severity_for(code, "FAILED", None).level
        assert base_rank[failed_level] >= base_rank[base_level], code


def test_result_carries_no_decision():
    for code in ALL_CODES:
        for verdict in VERDICTS:
            for concern in CONCERNS:
                result = sev.severity_for(code, verdict, concern)
                assert not hasattr(result, "outcome")
                assert not hasattr(result, "decision")
                assert result.level not in ("ALLOW", "HOLD", "DENY")


def test_exfiltration_on_deny_reaches_extreme():
    assert (
        sev.severity_for("NETWORK_DISABLED", "CONCERN", "EXFILTRATION_INTENT").level
        == "Extreme"
    )
    assert (
        sev.severity_for("BLOCKED_PATH", "CONCERN", "EXFILTRATION_INTENT").level
        == "Extreme"
    )


def test_exfiltration_without_deny_stays_high_not_extreme():
    assert (
        sev.severity_for(
            "ALLOWED_TOOL_AND_RESOURCE", "CONCERN", "EXFILTRATION_INTENT"
        ).level
        == "High"
    )
    assert (
        sev.severity_for("APPROVAL_REQUIRED", "CONCERN", "EXFILTRATION_INTENT").level
        == "High"
    )


def test_untyped_concern_resolves_upward_not_downward():
    assert sev.severity_for("TOOL_NOT_ALLOWED", "CONCERN", None).level == "High"
    assert sev.severity_for("BLOCKED_PATH", "CONCERN", None).level == "High"


def test_scope_drift_does_not_over_escalate_routine_mismatch():
    assert (
        sev.severity_for("TOOL_NOT_ALLOWED", "CONCERN", "SCOPE_DRIFT").level
        == "Medium"
    )


@pytest.mark.parametrize("concern", ["INJECTION_FOLLOWING", "POLICY_EVASION"])
def test_adversarial_concerns_raise_medium_base_to_high(concern):
    assert sev.severity_for("TOOL_NOT_ALLOWED", "CONCERN", concern).level == "High"


def test_badge_spec_text_plus_icon_never_color_only():
    seen_icons = set()
    for level in ("Low", "Medium", "High", "Extreme", "Ungraded"):
        icon, text = sev._BADGES[level]
        assert icon and text
        assert icon != text
        assert level.lower() in text.lower()
        seen_icons.add(icon)
    assert len(seen_icons) == 5  # distinct icon per level


def test_escalate_flag_semantics():
    assert sev.severity_for("ALLOWED_TOOL_AND_RESOURCE").escalate is False
    assert sev.severity_for("APPROVAL_REQUIRED").escalate is False
    assert sev.severity_for("BLOCKED_PATH").escalate is True
    assert (
        sev.severity_for("NETWORK_DISABLED", "CONCERN", "EXFILTRATION_INTENT").escalate
        is True
    )
    assert sev.severity_for("BOGUS").escalate is True


def test_rank_ordering_low_to_extreme():
    rank = sev._RANK
    assert rank["Low"] < rank["Medium"] < rank["High"] < rank["Extreme"]


def test_every_result_has_actionable_explanation_and_sla():
    for code in ALL_CODES:
        result = sev.severity_for(code)
        assert result.explanation
        assert result.sla_hint
        assert result.badge_text and result.badge_icon
