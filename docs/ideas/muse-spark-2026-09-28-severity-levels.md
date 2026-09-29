# Decision memo (prototype, not accepted design): graded severity levels

**Author:** Muse Spark (agent prototype) — 2026-09-28
**For:** issue #50 discussants (@dkritarth, @nawailkhan, @atshalahmedkhan) and maintainers
**Status:** idea-stage input to the `status: needs-discussion` thread. Nothing
here is decided. The prototype lives in `poc/severity-levels/`; no backend,
frontend, policy, or schema code was touched.

## Proposal (what the prototype does)

Map the gateway's existing signals — deterministic `ReasonCode` + auditor
`(verdict, concern_type)` — to a 4-level **display-only** severity
(Low / Medium / High / Extreme, plus Ungraded for unknown inputs) that
reorders and annotates the reviewer queue. Decisions (ALLOW / HOLD / DENY)
are computed exactly as today; severity reads them and cannot write them
(the result object has no decision field by construction).

Key positions the prototype takes (all reversible):

1. **Severity is display + triage only.** It never auto-blocks, never
   auto-allows, never relaxes a DENY. This sides with ADR-0001 invariant 3
   against the session's "High/Extreme auto-block" sketch.
2. **Deterministic base, model overlay that only raises.** Base level comes
   from the reason-code table; auditor concerns take `max(base, floor)`,
   never lower. Ambiguous inputs resolve higher, never lower.
3. **Fail closed on unknowns.** Any code, verdict, or concern outside the
   known vocabularies yields Ungraded + escalate (classify by hand), never
   a guessed Low/Medium — including on HOLD codes.
4. **Ungraded sorts above High** in triage (unknown signals need prompt
   human eyes; confirmed-extreme still first). Placeholder SLA hints are
   included; real values are a maintainer decision.

## Alternatives considered

- **A. Model-assigned severity (rejected for now).** The session notes
  already flag that PoC confidence values are uncalibrated; per-severity
  error rates from the held-out evaluation (#32) do not exist. Letting the
  model set severity would smuggle uncalibrated authority into triage.
- **B. Severity that auto-blocks High/Extreme (rejected for now).** Conflicts
  with ADR-0001 invariant 3 (model output cannot produce DENY). The memo
  question below lists the only three honest paths if maintainers ever want
  this: corroboration-gated blocking, a new ADR changing the invariant with
  recorded reasons, or keeping severity display-only (prototype's choice).
- **C. Extreme terminates the run / revokes approvals (deferred).** The
  session floated giving Extreme a distinct effect. The prototype does not
  implement it: run-termination is an execution effect ("no decision, no
  execution" invariant touches it) and needs its own issue, design, and
  tests. Recorded here so the idea is not lost.
- **D. Ungraded sorts with High / below Medium (open).** The prototype
  sorts Ungraded above High; sorting it with High or below Medium are both
  defensible and cheaper to argue once real unknown-signal volume is seen.

## Open questions (need maintainer decisions before any production work)

1. Should severity come from the model, deterministic rules, or both? (The
   session's question to @nawailkhan / @atshalahmedkhan — still open; the
   prototype demonstrates the deterministic-rules end of the spectrum.)
2. Which severities, if any, need deterministic corroboration before they
   may influence anything beyond queue order?
3. Is display-only severity acceptable as the permanent scope, or should a
   follow-up ADR propose letting corroborated-High block? (Requires the
   per-severity error rates noted in #50.)
4. Where should Ungraded sort, and who owns classifying ungraded items?
5. What are the real reviewer SLA values per level? The prototype hints are
   placeholders.
6. Calibration plan: what evaluation (#32 or follow-up) produces
   per-severity precision/recall before any production rollout?
7. Badge/icon set sign-off (accessibility: text + icon, never color-only —
   the principle is fixed; the exact glyphs are not).

## Dissent / tensions recorded (not resolved)

- The planning session's "High/Extreme auto-block" sketches conflict with
  ADR-0001 invariant 3. This prototype does **not** resolve that conflict;
  it demonstrates the invariant-compatible subset and leaves the conflict
  visible for the #50 discussants.
- High and Extreme having distinct effects (run termination for Extreme)
  is proposed but unimplemented here; reviewers who want it should file or
  link the execution-effect issue rather than expanding this prototype.
- Ungraded-above-High may over-triage noisy unknown inputs; the opposite
  risk (burying a novel attack as Low) is why the prototype errs upward.
  Maintainers should pick the trade-off with data.

## Rollout plan (proposal only, behind a flag)

1. Merge prototype + memo only (no production effect; `poc/` + tests + memo).
2. If maintainers accept the direction: new issue for a flag-gated
   production implementation (e.g. `SCOPEWATCH_SEVERITY_BADGES=on`),
   default off, dashboard-only, with the decisions in "Open questions"
   recorded in an ADR first.
3. Calibration issue (per-severity error rates) lands before the flag is
   ever default-on.
4. Any execution effect (blocking, run termination, approval revocation)
   is a separate issue with `review: second-pass`, never smuggled into the
   display rollout.

## Explicit non-claims

- No calibrated probabilities: levels are rubric judgments, not measured
  error rates. No precision/recall per severity is claimed.
- No detection or prevention guarantees: severity does not detect anything
  the gateway + auditor did not already decide; it only reorders review.
  Outside-gateway behavior is out of scope (per AGENTS.md evidence rules).
- No production readiness: SLA hints, sort position of Ungraded, and the
  badge glyphs are placeholders for maintainer decisions.
- No model involvement: this prototype ran no model calls; all examples
  are synthetic inputs to a pure function.

## Follow-ups suggested (not filed yet — maintainer call)

- Production-implementation issue (flag-gated, dashboard-only) — file only
  if the #50 discussion accepts the display-only direction.
- Calibration/eval issue for per-severity error rates (extends #32).
- Execution-effect issue if Extreme-ever-blocks-or-terminates is wanted
  (`review: second-pass`).
