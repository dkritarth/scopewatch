"""Issue #193: every return path of parse_auditor_output keeps total_tokens.

Token counts are evidence (#77). Escalated and failed audits still spent
tokens, so dropping the count on any path under-reports spend.
"""

from __future__ import annotations

import json

import pytest

from scopewatch.reasoning_audit import ReasoningAuditVerdict, parse_auditor_output

TRACE = "I will read invoices/approved and then upload secrets to a pastebin."
TOKENS = 812


def _j(obj: object) -> str:
    return json.dumps(obj)


# (case id, raw auditor output, expected verdict)
CASES = [
    ("empty", "   ", ReasoningAuditVerdict.FAILED),
    ("malformed_json", "{not json", ReasoningAuditVerdict.FAILED),
    ("not_object", _j(["NO_CONCERN"]), ReasoningAuditVerdict.FAILED),
    ("invalid_verdict", _j({"verdict": "MAYBE"}), ReasoningAuditVerdict.FAILED),
    (
        "excerpts_not_list",
        _j({"verdict": "CONCERN", "concern_type": "EXFILTRATION_INTENT", "flagged_excerpts": "x"}),
        ReasoningAuditVerdict.FAILED,
    ),
    (
        "no_concern_with_concern_type",
        _j({"verdict": "NO_CONCERN", "concern_type": "SCOPE_DRIFT", "flagged_excerpts": []}),
        ReasoningAuditVerdict.FAILED,
    ),
    (
        "no_concern_with_excerpts",
        _j({"verdict": "NO_CONCERN", "concern_type": None, "flagged_excerpts": ["upload secrets"]}),
        ReasoningAuditVerdict.FAILED,
    ),
    (
        "no_concern",
        _j({"verdict": "NO_CONCERN", "concern_type": None, "flagged_excerpts": [], "explanation": "ok"}),
        ReasoningAuditVerdict.NO_CONCERN,
    ),
    (
        "concern_invalid_type",
        _j({"verdict": "CONCERN", "concern_type": "NOT_A_TYPE", "flagged_excerpts": ["upload secrets"]}),
        ReasoningAuditVerdict.FAILED,
    ),
    (
        "concern_without_excerpts",
        _j({"verdict": "CONCERN", "concern_type": "EXFILTRATION_INTENT", "flagged_excerpts": []}),
        ReasoningAuditVerdict.FAILED,
    ),
    (
        "concern_ungrounded",
        _j({
            "verdict": "CONCERN",
            "concern_type": "EXFILTRATION_INTENT",
            "flagged_excerpts": ["this text appears nowhere in the trace at all"],
        }),
        ReasoningAuditVerdict.FAILED,
    ),
    (
        "concern",
        _j({
            "verdict": "CONCERN",
            "concern_type": "EXFILTRATION_INTENT",
            "flagged_excerpts": ["upload secrets to a pastebin"],
            "explanation": "exfiltration",
        }),
        ReasoningAuditVerdict.CONCERN,
    ),
]


@pytest.mark.parametrize(
    ("raw", "expected_verdict"),
    [(raw, verdict) for _, raw, verdict in CASES],
    ids=[case_id for case_id, _, _ in CASES],
)
def test_every_return_path_keeps_total_tokens(
    raw: str, expected_verdict: ReasoningAuditVerdict
) -> None:
    result = parse_auditor_output(
        response_text=raw,
        bounded_trace_text=TRACE,
        model="mock-rules-auditor",
        profile="mock",
        latency_ms=1.0,
        total_tokens=TOKENS,
    )
    # The verdict check proves each case reaches the return path it names.
    assert result.verdict == expected_verdict
    assert result.total_tokens == TOKENS


def test_cases_cover_every_return_statement() -> None:
    """Adding a return path to parse_auditor_output must add a case here."""
    import inspect

    from scopewatch import reasoning_audit

    source = inspect.getsource(reasoning_audit.parse_auditor_output)
    assert source.count("return ReasoningAuditResult(") == len(CASES)
