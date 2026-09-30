"""Tamper-evident evidence hash chain (issue #52 prototype).

Greenfield, pure-python, dependency-free (stdlib only).

What this is
------------
A per-run append-only hash chain that lets an operator prove that a list of
exported evidence events still has the same order and content it had when it
was recorded, and that every EXECUTION event for an action was preceded by a
policy DECISION event for that same action (``evidence before effect``).

Each record contains **digests only** -- never raw arguments, traces, or
summaries -- so the chain itself leaks no secrets::

    record = {seq, run_id, event_type, action_id, payload_digest, prev_hash}
    record_hash = sha256(canonical_json(record without record_hash))
    payload_digest = sha256(canonical_json(sanitized payload))

``canonical_json`` is ``json.dumps(sort_keys=True, separators=(",", ":"))``.

What this is NOT (honest limits, see ADR-0002)
----------------------------------------------
- It does not prove the executor was uncompromised; a compromised executor
  could record a well-formed but false chain.
- It does not prove wall-clock truth; timestamps are not covered by the hash.
- It cannot detect truncation without a pinned expected head/length; a bare
  prefix verifies OK by construction. Operators must pin the head nightly.
- It is a sidecar wrapper: it reads *exported* events and builds a chain.
  No schema migration, no changes to ``repository.py`` / ``service.py``.

Integration (wrapper/adapter, no migration)
-------------------------------------------
Use :func:`build_chain_from_events` to chain events exported from the
existing store (``ScopewatchRepository.get_events``), without touching the
DB schema::

    from scopewatch.evidence_chain import build_chain_from_events
    chain = build_chain_from_events(run_id, exported_events)
    chain.export_jsonl("/tmp/run.jsonl")

Then verify offline with ``scripts/verify_evidence_chain.py``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union


GENESIS_HASH = "0" * 64

DECISION_EVENT_TYPES = frozenset(
    {
        "POLICY_ALLOWED",
        "POLICY_DENIED",
        "POLICY_HELD",
    }
)

EXECUTION_EVENT_TYPES = frozenset(
    {
        "EXECUTION_STARTED",
        "EXECUTION_SUCCEEDED",
        "EXECUTION_FAILED",
    }
)


# ---------------------------------------------------------------------------
# canonical JSON + digests
# ---------------------------------------------------------------------------


def canonical_bytes(obj: Any) -> bytes:
    """Canonical JSON bytes: sorted keys, compact separators, UTF-8."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    """SHA-256 hex of a UTF-8 string (for summaries / traces)."""
    return sha256_hex(text.encode("utf-8"))


def digest_payload(payload: Mapping[str, Any]) -> str:
    """SHA-256 hex of the canonical JSON of a (sanitized) payload dict."""
    return sha256_hex(canonical_bytes(dict(payload)))


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


@dataclass(frozen=False)
class ChainRecord:
    """One link in the per-run chain. Only digests, never raw secrets."""

    seq: int
    run_id: str
    event_type: str
    action_id: Optional[str]
    payload_digest: str
    prev_hash: str
    record_hash: str

    def _hashable_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "event_type": self.event_type,
            "payload_digest": self.payload_digest,
            "prev_hash": self.prev_hash,
            "run_id": self.run_id,
            "seq": self.seq,
        }

    def compute_hash(self) -> str:
        """Recompute the record hash from the record's content fields."""
        return sha256_hex(canonical_bytes(self._hashable_dict()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "action_id": self.action_id,
            "payload_digest": self.payload_digest,
            "prev_hash": self.prev_hash,
            "record_hash": self.record_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChainRecord":
        return cls(
            seq=int(data["seq"]),
            run_id=str(data["run_id"]),
            event_type=str(data["event_type"]),
            action_id=data.get("action_id"),
            payload_digest=str(data["payload_digest"]),
            prev_hash=str(data["prev_hash"]),
            record_hash=str(data["record_hash"]),
        )


@dataclass
class VerificationResult:
    ok: bool
    first_bad_seq: Optional[int]
    reason: str
    code: str  # OK | BAD_SEQ | BAD_PREV | BAD_HASH | RUN_MISMATCH | FORK | ORDERING_VIOLATION | TRUNCATED
    head_hash: str
    checked: int


def _coerce_record(item: Union[ChainRecord, Mapping[str, Any]]) -> ChainRecord:
    if isinstance(item, ChainRecord):
        return item
    return ChainRecord.from_dict(item)


def verify_records(
    records: Iterable[Union[ChainRecord, Mapping[str, Any]]],
    expected_run_id: Optional[str] = None,
    expected_head: Optional[str] = None,
    expected_length: Optional[int] = None,
) -> VerificationResult:
    """Verify a linear chain. Returns the first failure, if any.

    Checks, in order per record:
    1. seq continuity (must start at 1, increment by 1)
    2. fork (two records claiming the same prev_hash)
    3. run_id consistency
    4. prev linkage
    5. record_hash recomputation (tamper)
    6. DECISION-before-EXECUTION ordering per action_id

    Then, if all records pass, compares against ``expected_head`` /
    ``expected_length`` for truncation detection. A bare prefix verifies OK
    without an expectation -- callers must pin the head to detect suffix
    drops.
    """
    recs = [_coerce_record(r) for r in records]

    if not recs:
        head = GENESIS_HASH
        if expected_head is not None and expected_head != GENESIS_HASH:
            return VerificationResult(
                ok=False,
                first_bad_seq=1,
                reason=f"empty chain but expected head {expected_head[:12]}...",
                code="TRUNCATED",
                head_hash=head,
                checked=0,
            )
        if expected_length is not None and expected_length != 0:
            return VerificationResult(
                ok=False,
                first_bad_seq=1,
                reason=f"empty chain but expected length {expected_length}",
                code="TRUNCATED",
                head_hash=head,
                checked=0,
            )
        if expected_run_id is not None:
            # Empty chain for a known run is still internally consistent.
            pass
        return VerificationResult(
            ok=True,
            first_bad_seq=None,
            reason="empty chain",
            code="OK",
            head_hash=head,
            checked=0,
        )

    run_ref = expected_run_id if expected_run_id is not None else recs[0].run_id
    seen_prev: dict[str, int] = {}
    decided: set[str] = set()
    prev_hash = GENESIS_HASH

    for idx, rec in enumerate(recs):
        expected_seq = idx + 1

        # 1. seq continuity
        if rec.seq != expected_seq:
            return VerificationResult(
                ok=False,
                first_bad_seq=expected_seq,
                reason=(
                    f"seq break at position {expected_seq}: "
                    f"found seq={rec.seq}, expected seq={expected_seq}"
                ),
                code="BAD_SEQ",
                head_hash=recs[-1].record_hash,
                checked=idx,
            )

        # 2. fork: two records claiming the same prev_hash
        if rec.prev_hash in seen_prev:
            return VerificationResult(
                ok=False,
                first_bad_seq=rec.seq,
                reason=(
                    f"fork detected at seq={rec.seq}: prev_hash "
                    f"{rec.prev_hash[:12]}... already claimed by seq={seen_prev[rec.prev_hash]}"
                ),
                code="FORK",
                head_hash=recs[-1].record_hash,
                checked=idx,
            )
        seen_prev[rec.prev_hash] = rec.seq

        # 3. run consistency
        if rec.run_id != run_ref:
            return VerificationResult(
                ok=False,
                first_bad_seq=rec.seq,
                reason=(
                    f"run_id mismatch at seq={rec.seq}: "
                    f"found {rec.run_id!r}, expected {run_ref!r}"
                ),
                code="RUN_MISMATCH",
                head_hash=recs[-1].record_hash,
                checked=idx,
            )

        # 4. prev linkage
        if idx == 0:
            if rec.prev_hash != GENESIS_HASH:
                return VerificationResult(
                    ok=False,
                    first_bad_seq=rec.seq,
                    reason=(
                        f"genesis prev mismatch at seq={rec.seq}: "
                        f"found {rec.prev_hash[:12]}..., expected genesis"
                    ),
                    code="BAD_PREV",
                    head_hash=recs[-1].record_hash,
                    checked=idx,
                )
        elif rec.prev_hash != prev_hash:
            return VerificationResult(
                ok=False,
                first_bad_seq=rec.seq,
                reason=(
                    f"prev linkage break at seq={rec.seq}: "
                    f"found {rec.prev_hash[:12]}..., expected {prev_hash[:12]}..."
                ),
                code="BAD_PREV",
                head_hash=recs[-1].record_hash,
                checked=idx,
            )

        # 5. hash recomputation (tamper)
        recomputed = rec.compute_hash()
        if recomputed != rec.record_hash:
            return VerificationResult(
                ok=False,
                first_bad_seq=rec.seq,
                reason=(
                    f"record hash mismatch at seq={rec.seq} "
                    f"(event_type={rec.event_type}): content was modified"
                ),
                code="BAD_HASH",
                head_hash=recs[-1].record_hash,
                checked=idx,
            )

        # 6. decision-before-execution ordering per action
        if rec.event_type in DECISION_EVENT_TYPES and rec.action_id is not None:
            decided.add(rec.action_id)
        if rec.event_type in EXECUTION_EVENT_TYPES:
            if rec.action_id is None:
                return VerificationResult(
                    ok=False,
                    first_bad_seq=rec.seq,
                    reason=(
                        f"ordering violation at seq={rec.seq}: "
                        f"EXECUTION event {rec.event_type} has no action_id"
                    ),
                    code="ORDERING_VIOLATION",
                    head_hash=recs[-1].record_hash,
                    checked=idx,
                )
            if rec.action_id not in decided:
                return VerificationResult(
                    ok=False,
                    first_bad_seq=rec.seq,
                    reason=(
                        f"ordering violation at seq={rec.seq}: "
                        f"EXECUTION for action {rec.action_id!r} "
                        f"without a prior DECISION event"
                    ),
                    code="ORDERING_VIOLATION",
                    head_hash=recs[-1].record_hash,
                    checked=idx,
                )

        prev_hash = rec.record_hash

    head = recs[-1].record_hash

    # Truncation / head pinning (only after the chain itself is valid).
    if expected_length is not None and len(recs) != expected_length:
        missing = len(recs) + 1
        return VerificationResult(
            ok=False,
            first_bad_seq=missing,
            reason=(
                f"truncation detected: chain has {len(recs)} records, "
                f"expected {expected_length}"
            ),
            code="TRUNCATED",
            head_hash=head,
            checked=len(recs),
        )
    if expected_head is not None and head != expected_head:
        return VerificationResult(
            ok=False,
            first_bad_seq=len(recs) + 1,
            reason=(
                f"head mismatch: found {head[:12]}..., "
                f"expected {expected_head[:12]}... (truncated or diverged)"
            ),
            code="TRUNCATED",
            head_hash=head,
            checked=len(recs),
        )

    return VerificationResult(
        ok=True,
        first_bad_seq=None,
        reason=f"verified {len(recs)} records",
        code="OK",
        head_hash=head,
        checked=len(recs),
    )


# ---------------------------------------------------------------------------
# incremental chain
# ---------------------------------------------------------------------------


class EvidenceChain:
    """Incremental per-run chain builder and verifier (wrapper, not storage)."""

    def __init__(
        self,
        run_id: str,
        records: Optional[Iterable[Union[ChainRecord, Mapping[str, Any]]]] = None,
    ) -> None:
        self._run_id = run_id
        self._records: list[ChainRecord] = []
        if records:
            for item in records:
                rec = _coerce_record(item)
                # Store independent copies so callers cannot mutate internals
                # through the list they passed in.
                self._records.append(
                    ChainRecord(
                        seq=rec.seq,
                        run_id=rec.run_id,
                        event_type=rec.event_type,
                        action_id=rec.action_id,
                        payload_digest=rec.payload_digest,
                        prev_hash=rec.prev_hash,
                        record_hash=rec.record_hash,
                    )
                )

    @property
    def run_id(self) -> str:
        return self._run_id

    def __len__(self) -> int:
        return len(self._records)

    def head_hash(self) -> str:
        if not self._records:
            return GENESIS_HASH
        return self._records[-1].record_hash

    def append(
        self,
        event_type: str,
        action_id: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> ChainRecord:
        """Append one event. Only the digest of ``payload`` is stored."""
        payload_digest = digest_payload(dict(payload) if payload else {})
        seq = len(self._records) + 1
        prev_hash = self.head_hash()
        rec = ChainRecord(
            seq=seq,
            run_id=self._run_id,
            event_type=str(event_type),
            action_id=action_id,
            payload_digest=payload_digest,
            prev_hash=prev_hash,
            record_hash="",  # filled below
        )
        rec.record_hash = rec.compute_hash()
        self._records.append(rec)
        return rec

    def verify(
        self,
        expected_head: Optional[str] = None,
        expected_length: Optional[int] = None,
        expected_run_id: Optional[str] = None,
    ) -> VerificationResult:
        return verify_records(
            self._records,
            expected_run_id=expected_run_id or self._run_id,
            expected_head=expected_head,
            expected_length=expected_length,
        )

    def to_records(self) -> list[ChainRecord]:
        """Return independent copies of the records."""
        return [
            ChainRecord(
                seq=r.seq,
                run_id=r.run_id,
                event_type=r.event_type,
                action_id=r.action_id,
                payload_digest=r.payload_digest,
                prev_hash=r.prev_hash,
                record_hash=r.record_hash,
            )
            for r in self._records
        ]

    def to_jsonl(self) -> str:
        lines = [
            json.dumps(r.to_dict(), sort_keys=True, separators=(",", ":"))
            for r in self._records
        ]
        return ("\n".join(lines) + "\n") if lines else ""

    def export_jsonl(self, path: Union[str, Path]) -> Path:
        p = Path(path)
        p.write_text(self.to_jsonl(), encoding="utf-8")
        return p

    @classmethod
    def from_jsonl_string(
        cls, data: str, run_id: Optional[str] = None
    ) -> "EvidenceChain":
        records: list[ChainRecord] = []
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            records.append(ChainRecord.from_dict(json.loads(line)))
        inferred = run_id or (records[0].run_id if records else "")
        return cls(run_id=inferred, records=records)

    @classmethod
    def from_jsonl(
        cls, path: Union[str, Path], run_id: Optional[str] = None
    ) -> "EvidenceChain":
        p = Path(path)
        return cls.from_jsonl_string(p.read_text(encoding="utf-8"), run_id=run_id)


# ---------------------------------------------------------------------------
# adapter: exported EvidenceEvents -> chain (no schema migration)
# ---------------------------------------------------------------------------


def _enum_or_str(value: Any) -> str:
    """Unwrap Enum / pydantic-serialized values to plain strings."""
    if value is None:
        return ""
    enum_val = getattr(value, "value", None)
    if isinstance(enum_val, str):
        return enum_val
    return str(value)


def payload_for_event(event: Union[Mapping[str, Any], Any]) -> dict[str, Any]:
    """Build a secrets-free payload summary for one exported event.

    Accepts a plain dict (as exported from the API / DB) or a pydantic
    ``EvidenceEvent``. Only hashes of free-text fields (summary, details,
    arguments, traces) are kept -- raw values never enter the chain.
    """
    if hasattr(event, "model_dump"):
        data: dict[str, Any] = dict(event.model_dump())
    elif isinstance(event, Mapping):
        data = dict(event)
    else:
        raise TypeError(f"unsupported event type: {type(event)!r}")

    raw_type = data.get("event_type", "")
    event_type = _enum_or_str(raw_type)
    summary = str(data.get("summary", "") or "")
    details = data.get("details", {}) or {}
    if not isinstance(details, dict):
        details = {"value": details}

    linked = {
        "action_request_id": data.get("action_request_id"),
        "policy_decision_id": data.get("policy_decision_id"),
        "approval_request_id": data.get("approval_request_id"),
        "execution_receipt_id": data.get("execution_receipt_id"),
        "turn_id": data.get("turn_id"),
        "actor": data.get("actor"),
    }
    return {
        "event_type": event_type,
        "summary_sha256": digest_text(summary),
        "details_sha256": digest_payload(
            {str(k): v for k, v in details.items()}
        ),
        "linked": {k: v for k, v in linked.items() if v is not None},
    }


def _action_id_for_event(event: Union[Mapping[str, Any], Any]) -> Optional[str]:
    if hasattr(event, "model_dump"):
        data: dict[str, Any] = dict(event.model_dump())
    elif isinstance(event, Mapping):
        data = dict(event)
    else:
        raise TypeError(f"unsupported event type: {type(event)!r}")
    for key in ("action_request_id", "action_id"):
        value = data.get(key)
        if value:
            return str(value)
    return None


def _event_type_for_event(event: Union[Mapping[str, Any], Any]) -> str:
    if hasattr(event, "model_dump"):
        data: dict[str, Any] = dict(event.model_dump())
    elif isinstance(event, Mapping):
        data = dict(event)
    else:
        raise TypeError(f"unsupported event type: {type(event)!r}")
    return _enum_or_str(data.get("event_type", ""))


def build_chain_from_events(
    run_id: str,
    events: Iterable[Union[Mapping[str, Any], Any]],
) -> EvidenceChain:
    """Build a verifiable chain from exported evidence events.

    The caller passes events in sequence order (e.g. from
    ``ScopewatchRepository.get_events`` or ``GET /runs/{id}/events``).
    Each event contributes ``(event_type, action_id, digested payload)``;
    raw summaries, arguments, and traces are hashed, never stored.
    """
    chain = EvidenceChain(run_id=run_id)
    for event in events:
        chain.append(
            _event_type_for_event(event),
            action_id=_action_id_for_event(event),
            payload=payload_for_event(event),
        )
    return chain


__all__ = [
    "GENESIS_HASH",
    "DECISION_EVENT_TYPES",
    "EXECUTION_EVENT_TYPES",
    "ChainRecord",
    "VerificationResult",
    "EvidenceChain",
    "canonical_bytes",
    "sha256_hex",
    "digest_text",
    "digest_payload",
    "payload_for_event",
    "build_chain_from_events",
    "verify_records",
]
