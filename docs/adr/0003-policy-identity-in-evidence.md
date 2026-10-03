# 0003: Persisted policy identity in action evidence

**Status:** Proposed
**Date:** 2026-10-03
**Deciders:** scopewatch student team (prototype for issue #119)
**Prototype:** `backend/scopewatch/models.py` (`policy_rules_fingerprint`, `get_policy_version`, `format_policy_version`), `backend/scopewatch/schemas.py`, `backend/scopewatch/policy.py`, `backend/scopewatch/repository.py`

## Context

The accepted architecture requires every evidence record to carry the
deterministic policy result *and policy version*, defines `policy_version` on
the canonical `ActionRequest` as "the policy evaluated for the request", and
binds approval to "the exact run, operation, arguments digest, and policy
version". ADR-0001 does not remove that requirement.

At `4519197` the gateway had no such field. `TaskScope` and `ActionRequest`
carried `schema_version` ("1"), which describes the *shape of the stored data*,
not the code that read it. `PolicyDecision` recorded outcome, reason, matched
rule, and timestamp; `policy_decisions` had no identity column; and
`submit_action` emitted reason and matched-rule details only. A persisted scope
snapshot is only useful for reproduction if the evaluator is pinned too, and a
mutable checkout is not a durable identity.

Two constraints shaped the decision:

1. **Never assign an identity we cannot prove.** A pre-existing decision row
   was evaluated by some code revision we no longer know. Naming today's
   revision for it would be a fabricated claim.
2. **An identity nobody can influence is evidence.** Anything an operator can
   set at runtime becomes something a misconfiguration — or an attacker — can
   make lie.

## Decision

### Format

The stored `policy_version` is `"{POLICY_RULES_REVISION}+{fingerprint}"`, for
example `2026-10-03.1+3f2a1b9c8d0e`:

- `POLICY_RULES_REVISION` is a human-readable label, `YYYY-MM-DD.N` for the Nth
  revision on that date. Bumped by hand.
- `fingerprint` is the first 12 hex characters of a SHA-256 over the *declared*
  rule-set facts in `scopewatch.models`: the `ReasonCode` members,
  `SUPPORTED_TOOLS`, `SUPPORTED_OPERATIONS`, `EXECUTABLE_OPERATIONS`, and
  `RUN_COMMAND_METACHARACTERS`. It is derived, so it cannot drift.

The two halves cover different failure modes. The fingerprint catches the
edits most likely to be forgotten — a new reason code, an extra operation, a
dropped metacharacter — with no manual step. The revision label covers what
the fingerprint provably cannot see: evaluation order, rule logic in
`policy.py` and `path_access.py`, `matched_rule` strings, approval semantics,
and dispatch-time gating. `policy_rules_fingerprint()` states its coverage in
its own docstring so nobody has to guess whether it is a full code digest. It
is not one, and nothing here claims it is.

### Update semantics

1. Bump `POLICY_RULES_REVISION` on any change to deterministic evaluation.
2. No manual step for edits the fingerprint already covers.
3. **No environment-variable override.** `get_policy_version()` reads only the
   shipped code and constants. An operator-settable identity would let a
   deployment claim any revision it liked, which is the exact ambiguity this
   field exists to remove. Tests simulate a revision change by monkeypatching
   `POLICY_RULES_REVISION`.
4. Pre-existing rows keep `NULL` and read as `unknown (legacy)`. They are never
   backfilled.

### Where it is stamped, and where it is not

`evaluate_policy` is the single public entry point of the engine and stamps the
identity on its result; the rule logic lives in a private
`_evaluate_policy_rules`. So no rule path can omit the identity.

`PolicyDecision.policy_version` defaults to `None`, not to the current version.
That default fails closed: a decision built without going through the engine —
above all a read of a row written before versioning existed — ends up visibly
unknown rather than silently claiming today's revision. Every repository read
path passes the stored value through verbatim, including `None`.

### Single source of truth for approvals

`ApprovalRequest.policy_version` is resolved from the bound
`policy_decisions` row on every read (a `LEFT JOIN`, so listing approvals is
one query rather than one per row). It is deliberately *not* a second stored
column: an approval authorizes exactly one stored decision, so keeping the
identity on the decision makes it impossible for the two to disagree.

### Approval drift

An approval issued under one revision and resolved after the policy has moved
on is **honoured on its evaluated decision**, with the drift recorded
explicitly on the `APPROVAL_GRANTED` / `APPROVAL_DENIED` event:

| Key | Meaning |
| --- | --- |
| `policy_version` | the revision that evaluated the action, read from the stored decision (`null` if pre-versioning) |
| `policy_version_current` | the revision running now — present **only** when it differs |
| `policy_version_changed` | `true` only when it differs, so drift can never be silent |
| `policy_decision_id` | the exact decision the approval resolves |

Re-evaluating the action under the live revision at resolution time would
silently reinterpret what the reviewer saw, and could flip a granted approval
to a denial (or the reverse) with no human in the loop. Decisions are
append-only, so the outcome the reviewer approved is still exactly on the
record.

This is a statement about evidence, not a relaxation. The approval stays bound
to one stored decision for one exact action, cannot be replayed, and still
cannot override a deterministic `DENY` — all three are covered by tests under
revision drift.

## Consequences

- A decision can now be reproduced after a rule change, and a reviewer can see
  in the dashboard and the event log which revision decided or held each action.
- Changing the allowlists or the reason-code set changes the stored identity
  automatically, so a forgotten bump degrades into two distinguishable
  revisions rather than one silently wrong label.
- Anything that reads `policy_decisions` must select or join the new column, or
  the identity is silently missing from that view.
- The identity adds ~25 bytes to every `POLICY_*` and `APPROVAL_*` event's
  `details`, which the evidence chain already hashes.
- `policy_decisions` gains a nullable column; `init_db` adds it idempotently
  and deliberately does not backfill.
- Deployment configuration gained nothing: there is no flag to set, and none to
  get wrong.

## Alternatives considered

- **Reuse `schema_version`.** Rejected: it describes the data format. Nothing
  forces the policy engine and the wire format to change together, so it cannot
  distinguish two evaluations of identical data.
- **A content hash of the policy engine's source files.** Rejected: it depends
  on source being available at runtime, which breaks under frozen or zipped
  deployments, and it changes on pure reformatting — churn without meaning.
- **An environment-variable override** (the first draft of this work used one,
  `SCOPEWATCH_POLICY_VERSION`). Rejected: it makes the identity operator-settable,
  so stored evidence can name a revision that never evaluated the action. Tests
  monkeypatch the constant instead.
- **Stamping `PolicyDecision` with a `default_factory` returning the current
  version.** Rejected: it protects the write path but endangers the read path,
  which is where the honesty requirement lives. A future read path that forgot
  the column would stamp every legacy row with today's revision. `None` plus a
  single stamping point inverts that, so the failure mode is "unknown".
- **Storing the identity a second time on `approval_requests`.** Rejected: two
  writable copies of one fact can drift. It is derived from the decision.
- **Re-evaluate a held action under the live policy when the reviewer responds.**
  Rejected: it makes the outcome depend on when the human clicked, which is the
  silent reinterpretation the issue asks us to avoid.
- **Invalidating approvals across a revision change.** Deferred: it would force
  reviewers to redo work on every deploy and would not be a safety win, since
  the decision that was approved is immutable and still exact. Recorded drift
  is the honest middle ground for V1.