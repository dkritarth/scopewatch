# 0002: Tamper-evident evidence hash chain (sidecar wrapper)

**Status:** Proposed
**Date:** 2026-09-28
**Deciders:** scopewatch student team (prototype for issue #52)
**Prototype:** `backend/scopewatch/evidence_chain.py`, `scripts/verify_evidence_chain.py`

## Context

Issue #52 asks for tamper-evident evidence records: each evidence event
stores `prev_hash` and `hash = sha256(prev_hash || canonical_event_json)` so
deletion, reordering, or editing can be detected, with chain integrity shown
per run. It was raised in the PR #12 gap audit ("no tamper-evident evidence
records") and explicitly deferred as not needed for the hackathon demo.

The gateway already stores append-only `evidence_events` in SQLite with
monotonic per-run sequences (`ScopewatchRepository.append_event`), and the
service layer emits events in decision-before-execution order
(`ACTION_REQUESTED -> POLICY_* -> EXECUTION_*`). But nothing cryptographically
binds those rows together: a party with DB write access can edit, reorder, or
delete rows and the dashboard cannot tell. SQLite is still the right store;
this ADR decides how to add tamper evidence without a risky migration during
the hackathon timebox.

Constraints for this prototype:

1. **No existing behaviour may change.** `repository.py`, `db.py`,
   `service.py`, `app.py`, `executor*.py`, `policy.py`, providers, agent,
   frontend, and deploy code are frozen for this work.
2. **No new dependencies.** Pure-python stdlib only, so offline verification
   works anywhere.
3. **No secrets in the chain.** Arguments, traces, and summaries enter only
   as SHA-256 digests.
4. **Honest evidence.** Per project rules we never claim guaranteed detection
   or prevention outside the gateway.

## Decision

We adopt a **sidecar wrapper (option C below)** as the prototype, implemented
in the new module `backend/scopewatch/evidence_chain.py`:

- Per-run chain. Genesis `prev_hash` is 64 zeros. Each record is
  `{seq, run_id, event_type, action_id, payload_digest, prev_hash,
  record_hash}` where `record_hash = sha256(canonical_json(...))` and
  `payload_digest = sha256(canonical_json(sanitized payload))`.
- Canonical JSON is `json.dumps(sort_keys=True, separators=(",", ":"))`.
- `EvidenceChain` offers incremental `append()`, `verify()`, and
  JSONL `export_jsonl` / `from_jsonl` / `to_jsonl`.
- `verify_records()` recomputes every hash and checks, in order: seq
  continuity (start at 1, +1), fork (duplicate `prev_hash`), run_id
  consistency, prev linkage, hash recomputation, and DECISION-before-
  EXECUTION ordering per `action_id` (`POLICY_ALLOWED/DENIED/HELD` must
  precede `EXECUTION_STARTED/SUCCEEDED/FAILED` for the same action).
- First failure is reported as `{ok: false, code, reason, first_bad_seq,
  head_hash, checked}`. Truncation (dropped suffix) is detected only against
  a pinned `expected_head` / `expected_length`, because a bare prefix is
  internally consistent by construction.
- `build_chain_from_events(run_id, events)` adapts *exported* events (dicts
  or `EvidenceEvent` models from `get_events` / the API) into the chain by
  hashing summaries and details. No schema migration, no new tables.
- Offline CLI `scripts/verify_evidence_chain.py` exits 0 on OK, 1 on
  verification failure with the first-failure detail, 2 on usage/IO errors.

## Consequences

What becomes easier:

- Operators can export a run's events nightly, pin the head hash, and prove
  later that the export was not edited, reordered, or truncated.
- Reviewers get a precise first-failure pointer (`first_bad_seq` + code)
  instead of a vague "integrity error".
- The prototype is isolated: 40 tests in `backend/tests/test_evidence_chain.py`
  cover genesis, happy path, tamper/reorder/gap/fork, ordering, determinism,
  JSONL round-trip, adapter secrecy, and a 10k-record perf sanity check
  (< 1 s in practice, budget < 5 s), with zero changes to existing suites.

What becomes harder / what we accept:

- Two sources of truth temporarily: SQLite remains authoritative for serving;
  the chain is a detached attestation. Drift between them is possible until
  a later migration binds them (see rollout).
- Operators must manage head pins (where to store them, who signs them).
  Without pinning, suffix truncation is invisible.
- Semantic lies are out of scope: a compromised executor can record a
  well-formed but false chain. The chain proves *integrity of the record*,
  not *truth of the world*.

## Alternatives considered

### A. DB trigger / generated column (`prev_hash`, `hash` in SQLite)

Add `prev_hash` / `record_hash` columns to `evidence_events` with triggers or
repository-level computation on every `append_event`.

*Rejected for now.* It is the right long-term shape (single source of truth,
atomic with the insert transaction), but it requires a schema migration,
backfill of existing rows, trigger portability work, and careful handling of
the existing `MAX(sequence)+1` concurrency pattern -- too risky inside the
hackathon freeze, and it would touch the forbidden files for this prototype.
Recorded as future work below.

### B. In-app wrapper inside `service.py` / `repository.py`

Compute the chain inline on every `append_event` call in the request path.

*Rejected for now.* Same migration/coupling cost as A, plus it puts hashing
on the hot path and widens the blast radius of a prototype bug to every
gateway write. The sidecar keeps the prototype out of the request path until
the design is validated.

### C. Sidecar wrapper over exported events (chosen)

Chain builder reads exported events and writes a detached JSONL attestation;
verifier runs offline. Zero migration, zero hot-path changes, easy to delete
if the idea fails.

*Chosen* because it satisfies the prototype constraints, demonstrates the
cryptographic ordering property (`DECISION` before `EXECUTION`), and leaves a
clean migration path: move `compute_hash` into `append_event` later without
changing the record format.

## What this does NOT prove (explicit non-goals)

- **Executor compromise.** A compromised executor or DB writer can emit a
  valid chain over false events. The chain detects *post-hoc tampering with
  the record*, not *lying at record time*.
- **Wall-clock truth.** Timestamps are not covered by the hash (they are
  operator-controlled). Ordering is logical (seq + prev linkage), not temporal.
- **Completeness without pinning.** A dropped suffix with no pinned head
  verifies OK. Nightly pinning is mandatory (see operator doc).
- **Prevention.** This is detective, not preventive. It never blocks
  execution and must not be described as guaranteeing prevention.

## Rollout plan

1. **Prototype (this PR, advances #52, leaves it open).** New files only;
   no migration. Validate with new tests + `./scripts/validate.sh --quick`.
2. **Nightly operator flow.** Export per-run JSONL, verify, pin head hashes
   in an append-only log (see `docs/security/evidence-log.md`).
3. **Later migration (future issue, not this PR).** Add `prev_hash` /
   `record_hash` columns to `evidence_events`, compute in `append_event`
   inside the existing transaction, backfill with an offline script, and
   surface per-run integrity in the dashboard. Keep the record format and
   `verify_records` semantics unchanged so existing exports stay verifiable.
4. **Head-pin store.** Decide where pins live (signed file, separate host,
   or transparency-log style). Until then, pins in operator custody are
   better than none but are themselves tamperable -- state this honestly.

## Risks

- Pin store compromise makes truncation invisible again. Mitigation: separate
  host / signatures (future work).
- Hash covers digests of details, so a collision or second-preimage (SHA-256,
  currently infeasible) would break the guarantee. Algorithm agility
  (versioned hash id) is future work.
- Small-payload brute force: if a payload has tiny entropy (e.g. boolean),
  its digest is guessable. This leaks at most the *existence* of a known
  value, not the value itself from the chain alone -- but operators should
  not treat digests as encryption.
- Performance: hashing is O(n); 10k records verify in well under a second
  on dev hardware, but million-event runs will need incremental/parallel
  verification (future work).
