# Graded severity levels: prototype (issue #50)

**Status:** Active prototype for discussion. Not an accepted design, not
production code. Decision memo:
`docs/ideas/muse-spark-2026-09-28-severity-levels.md`.

> ** Gian non-claim:** severity here is display + triage only. It never
> changes ALLOW / HOLD / DENY. All examples below use synthetic inputs.

## Question

Can we map the gateway's existing deterministic reason codes plus the
auditor's verdict/concern type to a 4-level display severity
(Low / Medium / High / Extreme) that helps reviewers triage, without
touching decisions and without violating the escalate-only invariant
(ADR-0001)?

An answer that changes the plan: if any mapping requires the model to
block on its own authority, or relaxes a deterministic DENY, the design
is wrong and must be reworked before production (see memo, open
questions).

## How to run

No dependencies beyond the standard library. From the repo root:

```bash
python3 -m pytest backend/tests/test_severity_prototype_properties.py -q
python3 - <<'EOF'
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location(
    "severity", Path("poc/severity-levels/severity.py"))
sev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sev)
print(sev.severity_for("NETWORK_DISABLED", "CONCERN", "EXFILTRATION_INTENT"))
print(sev.severity_for("APPROVAL_REQUIRED", "NO_CONCERN", None))
print(sev.severity_for("BOGUS_CODE"))
EOF
```

## Mapping function

`severity.py::severity_for(policy_code, audit_verdict=None, concern_type=None)`
is a pure, deterministic function (same inputs -> same output, no I/O,
no randomness, no model calls). It returns a `SeverityResult`
`(level, badge_text, badge_icon, sla_hint, escalate, explanation)` with
deliberately **no decision field**: there is no way to read an
ALLOW / HOLD / DENY out of it.

Rubric (grounded in `ReasonCode` on `origin/main`):

| Policy code | Outcome mirror | Default level | Why |
| --- | --- | --- | --- |
| `ALLOWED_TOOL_AND_RESOURCE` | ALLOW | Low | Benign baseline; batch display. |
| `APPROVAL_REQUIRED` | HOLD | Medium | Routine reviewer gate, no adversarial signal. HOLD never Low. |
| `TOOL_NOT_ALLOWED`, `OPERATION_NOT_ALLOWED`, `UNSUPPORTED_OPERATION`, `PATH_NOT_ALLOWED`, `COMMAND_NOT_ALLOWED`, `MALFORMED_REQUEST` | DENY | Medium | Scope mismatch; usually benign misconfiguration. HOLD/DENY floor respected. |
| `REASONING_SCOPE_CONCERN` | HOLD | High | Auditor already flagged reasoning; reviewer needs context soon. |
| `REASONING_AUDIT_FAILED` | HOLD | High | Audit blind spot: fail-closed HOLD with no auditor evidence. |
| `BLOCKED_PATH`, `PATH_TRAVERSAL`, `SYMLINK_ESCAPE`, `PATH_OUTSIDE_WORKSPACE`, `NETWORK_DISABLED`, `SHELL_METACHARACTER` | DENY | High | Technique-shaped: forbidden target, escape, or injection syntax. |
| `POLICY_ERROR` | DENY | High | Gateway-internal error means unknown state; escalate. |

Auditor overlays (display priority only; `max(base, floor)`, never lower):

| Auditor signal | Floor / rule |
| --- | --- |
| `NO_CONCERN` or no audit | Base stands. |
| `CONCERN` + `SCOPE_DRIFT` | Medium (non-adversarial extra work). |
| `CONCERN` + `INJECTION_FOLLOWING` / `POLICY_EVASION` | High. |
| `CONCERN` + `EXFILTRATION_INTENT` on ALLOW/HOLD | High. |
| `CONCERN` + `EXFILTRATION_INTENT` on DENY | **Extreme** (blocked exfiltration shape; decision was already DENY). |
| `CONCERN` with untyped/`None` concern | High (ambiguous -> higher, never lower). |
| `FAILED` verdict | High floor (blind spot; the HOLD already happened fail-closed). |
| Unknown code / verdict / concern (wrong case, empty, `None` code) | **Ungraded** + `escalate=True`: classify by hand, never guess low. |

## Dashboard badge spec (proposal)

- Badge = **icon + text label**, never color-only. Color may be a redundant
  signal. Text is always rendered as real text (screen-reader safe).
- Icons (plain geometric shapes, no emoji):
  Low `○`, Medium `◎`, High `▲`, Extreme `⬢`, Ungraded `?`.
- Labels: `LOW — routine`, `MEDIUM — review in queue`,
  `HIGH — review soon`, `EXTREME — review immediately`,
  `UNGRADED — classify by hand`.
- Sort/filter semantics: triage queue sorts
  Extreme > Ungraded > High > Medium > Low (ungraded sorts above High so
  unknown signals get prompt human eyes; confirmed-extreme still first).
  Filters expose each level plus "ungraded only" and "needs classification".
  Decisions (ALLOW/HOLD/DENY) remain a separate, independent filter axis:
  severity must never be confused with the decision.
- Reviewer SLA hints (illustrative; maintainers must set real ones):
  Low = batch/no action; Medium = normal review window;
  High = current review session; Extreme = immediately, consider pausing
  the run pending review; Ungraded = triage/classify first.

## Worked examples (synthetic)

1. **Traversal probe with evasion language (stays DENY).**
   Input: `severity_for("PATH_TRAVERSAL", "CONCERN", "POLICY_EVASION")`.
   Output: High (`▲ HIGH — review soon`). The deterministic gateway
   already denied the `../` read; the auditor's evasion flag agrees.
   Severity only puts it above the routine Medium queue. Decision
   untouched: DENY before, DENY after.
2. **Routine approval gate (stays HOLD).**
   Input: `severity_for("APPROVAL_REQUIRED", "NO_CONCERN", None)`.
   Output: Medium (`◎ MEDIUM — review in queue`). An approval-gated
   delete with clean reasoning waits its normal turn. No escalation,
   no auto-allow. Decision untouched: HOLD before, HOLD after.
3. **Blocked network attempt with exfil intent (stays DENY, top of queue).**
   Input: `severity_for("NETWORK_DISABLED", "CONCERN", "EXFILTRATION_INTENT")`.
   Output: Extreme (`⬢ EXTREME — review immediately`). The baseline
   disables network, so the attempt was already denied; the auditor's
   exfil flag moves it to the top of triage. Severity adds no blocking
   power it did not already have. Decision untouched: DENY before,
   DENY after.

## Result (2026-09-28, prototype, no model involved)

- `severity_for` implemented as specified above; 40 property/unit checks
  in `backend/tests/test_severity_prototype_properties.py` pass.
- Verified properties: determinism; HOLD outcomes never Low; DENY paired
  with `EXFILTRATION_INTENT` never below High; unknown inputs ->
  Ungraded + escalate; auditor overlays monotone non-decreasing;
  result object carries no decision field.
- Negative / open results: levels are uncalibrated (no per-severity error
  rates exist; evaluation #32 would need them first); UNGRADED sort
  position and real SLA values are maintainer decisions, not findings.

## Status

Active prototype for discussion on #50. Promotion to production requires
the decisions listed in the memo (calibration, UNGRADED handling, SLA
values, flag-gated rollout); none of those are claimed here.
