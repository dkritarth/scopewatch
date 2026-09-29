# Evidence log: export and nightly verification (operator guide)

This guide covers the **prototype** tamper-evident evidence chain from issue
#52 / ADR-0002. It is a detective control: it helps you notice post-hoc
editing, reordering, or deletion of exported evidence. It does **not**
prevent unsafe execution and does **not** prove the executor told the truth.

## What you need

- A Scopewatch checkout (Python 3.12, no extra dependencies).
- Exported evidence for a run (from the API or the DB), **or** a previously
  exported chain JSONL file.
- A place to store head-hash pins (any append-only file works for the
  prototype; see Limitations).

## 1. Build a chain from exported events

The chain builder is a sidecar: it reads exported events, never the live DB
schema. From Python:

```python
from scopewatch.evidence_chain import build_chain_from_events

# `events` in sequence order, e.g. from GET /runs/{id}/events or
# ScopewatchRepository.get_events(conn, run_id)
chain = build_chain_from_events(run_id, events)
chain.export_jsonl(f"/var/scopewatch/evidence/{run_id}.jsonl")
print("head:", chain.head_hash())
```

Only digests enter the chain: summaries, arguments, and reasoning traces are
SHA-256 hashed, never stored raw. The JSONL file itself therefore contains no
secrets beyond event types and linkage -- safe to archive.

## 2. Verify nightly (or on demand)

```bash
PYTHONPATH=backend python3 scripts/verify_evidence_chain.py /var/scopewatch/evidence/<run>.jsonl
```

Pin the head to detect truncation (dropped suffix). Without a pin, a bare
prefix verifies OK by construction:

```bash
# First night: record the head
PYTHONPATH=backend python3 scripts/verify_evidence_chain.py /var/scopewatch/evidence/<run>.jsonl --expected-length 128 | tee /var/scopewatch/pins/<run>.json

# Later nights: fail if anything was dropped or changed
PYTHONPATH=backend python3 scripts/verify_evidence_chain.py /var/scopewatch/evidence/<run>.jsonl \
  --expected-head <head-from-pin> \
  --expected-length 128
```

Exit codes: `0` = OK, `1` = verification failure (JSON names
`first_bad_seq`, `code`, `reason`), `2` = usage/IO error.

Suggested cron (example, adjust paths):

```cron
0 3 * * * cd /opt/scopewatch && PYTHONPATH=backend python3 scripts/verify_evidence_chain.py /var/scopewatch/evidence/$(date +\%F).jsonl --expected-head $(cat /var/scopewatch/pins/latest.head) >> /var/log/scopewatch-verify.log 2>&1
```

## 3. What an alert means

The verifier prints one JSON object. Key fields:

| `code` | Meaning | First action |
| --- | --- | --- |
| `BAD_HASH` | Record content at `first_bad_seq` was modified (hash recompute failed). | Treat the export as untrusted from that seq onward. Compare against an older pinned copy; investigate DB/write access. |
| `BAD_PREV` | Linkage break at `first_bad_seq` (reorder or splice). | Same as above; check for reordered or spliced segments. |
| `BAD_SEQ` | Sequence gap, duplicate, or reorder at `first_bad_seq`. | Check for deleted or duplicated rows in the export. |
| `FORK` | Two records claim the same `prev_hash` (branched history merged). | The export merged two histories; find which branch is canonical. |
| `RUN_MISMATCH` | Mixed `run_id` values. | Export bug or merged runs; re-export per run. |
| `ORDERING_VIOLATION` | `EXECUTION_*` for an action with no prior `POLICY_*` decision, or with no `action_id`. | **Serious**: evidence claims execution without a recorded decision. Check whether the decision row was deleted, or whether execution bypassed the gateway. Do not assume prevention worked. |
| `TRUNCATED` | Valid prefix but head/length pin mismatch (suffix dropped or diverged). | Compare against the pinned copy; the missing suffix starts at `first_bad_seq`. |
| `MALFORMED` / `FILE_NOT_FOUND` | Verifier could not read the file. | Operational issue, not tampering. Fix the pipeline. |

General rule: **quarantine the export**, keep the pinned copy, and
investigate write access to the DB and the export pipeline before trusting
the dashboard for that run.

## 4. Limitations (read before relying on this)

- **Detective, not preventive.** The chain never blocks execution. Never
  describe it as guaranteeing prevention or detection of live attacks.
- **No executor-truth guarantee.** A compromised executor can write a
  well-formed chain over false events. The chain proves the *record* was not
  altered afterwards, not that the *events* were true.
- **No wall-clock guarantee.** Timestamps are not hashed. Logical order
  (seq + prev linkage) is what is verified.
- **Truncation needs pins.** Without `--expected-head` / `--expected-length`,
  a dropped suffix is invisible. Pin heads every night and protect the pin
  store (a pin file on the same host as the DB is only marginally better
  than nothing).
- **Prototype scope.** The chain is a detached sidecar over *exported*
  events, not an in-DB guarantee. Drift between the DB and the JSONL export
  is possible. The planned migration (ADR-0002 rollout step 3) will bind
  hashes into the `evidence_events` transaction.

## 5. Synthetic-data reminder

Exports used for testing must be synthetic. Never commit real keys, private
traces, or runtime reports; `logs/` is gitignored. Only synthetic content
may be sent to external model providers.
