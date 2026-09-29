"""Tests for the tamper-evident evidence hash chain (issue #52).

Greenfield prototype: `backend/scopewatch/evidence_chain.py` is a pure-python,
dependency-free per-run hash chain. These tests specify the contract:

- canonical JSON (sort_keys, separators) + SHA-256
- genesis per run, incremental append, prev linkage
- verify() recomputes hashes, checks seq continuity, prev linkage,
  and DECISION-before-EXECUTION ordering per action
- truncation / reorder / tamper / fork -> verification failure naming
  the first bad seq
- no secrets in records (digests only)
- JSONL export/import round-trip
- 10k-record perf sanity

TDD: this file was written before the implementation (red), then the
module was added to make it green.
"""

from __future__ import annotations

import json
import time

import pytest

from scopewatch.evidence_chain import (
    DECISION_EVENT_TYPES,
    EXECUTION_EVENT_TYPES,
    GENESIS_HASH,
    ChainRecord,
    EvidenceChain,
    canonical_bytes,
    digest_payload,
    digest_text,
    verify_records,
)


RUN_A = "run-aaaaaaaa-0001"
RUN_B = "run-bbbbbbbb-0002"


def _happy_chain(run_id: str = RUN_A) -> EvidenceChain:
    """Build a small valid chain: request -> decision -> execution."""
    chain = EvidenceChain(run_id=run_id)
    chain.append("ACTION_REQUESTED", action_id="act-1", payload={"op": "read_text"})
    chain.append("POLICY_ALLOWED", action_id="act-1", payload={"rule": "R1"})
    chain.append("EXECUTION_STARTED", action_id="act-1", payload={"executor": "syn"})
    chain.append("EXECUTION_SUCCEEDED", action_id="act-1", payload={"status": "ok"})
    return chain


# ---- genesis / append ----


def test_genesis_hash_is_64_zeros() -> None:
    assert GENESIS_HASH == "0" * 64


def test_first_append_uses_genesis_prev_and_seq_one() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    rec = chain.append("RUN_CREATED", payload={"name": "demo"})
    assert rec.seq == 1
    assert rec.prev_hash == GENESIS_HASH
    assert rec.run_id == RUN_A
    assert len(rec.record_hash) == 64


def test_append_increments_seq_and_links_prev() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    r1 = chain.append("ACTION_REQUESTED", action_id="a1")
    r2 = chain.append("POLICY_ALLOWED", action_id="a1")
    r3 = chain.append("EXECUTION_SUCCEEDED", action_id="a1")
    assert (r1.seq, r2.seq, r3.seq) == (1, 2, 3)
    assert r2.prev_hash == r1.record_hash
    assert r3.prev_hash == r2.record_hash


def test_verify_happy_path_single_record() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    chain.append("RUN_CREATED", payload={"name": "x"})
    result = chain.verify()
    assert result.ok is True
    assert result.first_bad_seq is None
    assert result.code == "OK"
    assert result.head_hash == chain.head_hash()


def test_verify_happy_path_full_flow() -> None:
    chain = _happy_chain()
    result = chain.verify()
    assert result.ok is True
    assert result.checked == 4


def test_empty_chain_verifies_ok_with_genesis_head() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    result = chain.verify()
    assert result.ok is True
    assert result.head_hash == GENESIS_HASH
    assert result.checked == 0


# ---- hash recomputation / tamper ----


def test_tamper_event_type_fails_at_that_seq() -> None:
    chain = _happy_chain()
    records = chain.to_records()
    records[1].event_type = "POLICY_DENIED"  # tamper seq 2 content, hash now stale
    result = verify_records(records)
    assert result.ok is False
    assert result.first_bad_seq == 2
    assert result.code == "BAD_HASH"


def test_tamper_action_id_fails_at_that_seq() -> None:
    chain = _happy_chain()
    records = chain.to_records()
    records[2].action_id = "act-EVIL"
    result = verify_records(records)
    assert result.ok is False
    assert result.first_bad_seq == 3


def test_tamper_payload_digest_fails_at_that_seq() -> None:
    chain = _happy_chain()
    records = chain.to_records()
    records[0].payload_digest = "f" * 64
    result = verify_records(records)
    assert result.ok is False
    assert result.first_bad_seq == 1
    assert result.code == "BAD_HASH"


def test_tamper_prev_hash_breaks_linkage() -> None:
    chain = _happy_chain()
    records = chain.to_records()
    records[2].prev_hash = "a" * 64
    result = verify_records(records)
    # Either hash mismatch or prev mismatch must fire at seq 3
    assert result.ok is False
    assert result.first_bad_seq == 3
    assert result.code in ("BAD_HASH", "BAD_PREV")


def test_tamper_record_hash_directly_fails() -> None:
    chain = _happy_chain()
    records = chain.to_records()
    records[3].record_hash = "0" * 64
    result = verify_records(records)
    assert result.ok is False
    assert result.first_bad_seq == 4


# ---- seq continuity / reorder / truncation ----


def test_reorder_two_records_fails_at_first_bad_seq() -> None:
    chain = _happy_chain()
    records = chain.to_records()
    records[1], records[2] = records[2], records[1]  # swap seq-2 and seq-3 positions
    result = verify_records(records)
    assert result.ok is False
    # After swap, position 2 holds seq=3 -> continuity or linkage break
    assert result.first_bad_seq == 2
    assert result.code in ("BAD_SEQ", "BAD_PREV", "BAD_HASH")


def test_gap_in_sequence_fails() -> None:
    chain = _happy_chain()
    records = chain.to_records()
    dropped = [records[0], records[2], records[3]]  # drop seq 2 (middle)
    result = verify_records(dropped)
    assert result.ok is False
    assert result.first_bad_seq == 2  # expected seq 2 but found seq 3


def test_sequence_must_start_at_one() -> None:
    chain = _happy_chain()
    records = chain.to_records()[1:]  # drop genesis record; first seq is 2
    result = verify_records(records)
    assert result.ok is False
    assert result.first_bad_seq == 2 or result.first_bad_seq == 1


def test_duplicate_seq_fails() -> None:
    chain = _happy_chain()
    records = chain.to_records()
    clone = ChainRecord(
        seq=2,
        run_id=RUN_A,
        event_type="POLICY_ALLOWED",
        action_id="act-1",
        payload_digest=records[1].payload_digest,
        prev_hash=records[1].prev_hash,
        record_hash=records[1].record_hash,
    )
    records.insert(2, clone)  # now two seq-2 records
    result = verify_records(records)
    assert result.ok is False
    assert result.first_bad_seq == 3


def test_drop_suffix_prefix_still_verifies_without_expectation() -> None:
    """A truncated prefix is internally consistent; this documents the limit.

    Without an expected head/length the verifier cannot know records are
    missing, so the prefix verifies OK. Callers must pin the head.
    """
    chain = _happy_chain()
    prefix = chain.to_records()[:2]
    result = verify_records(prefix)
    assert result.ok is True


def test_drop_suffix_detected_via_expected_head() -> None:
    chain = _happy_chain()
    full_head = chain.head_hash()
    prefix = chain.to_records()[:2]
    result = verify_records(prefix, expected_head=full_head)
    assert result.ok is False
    assert result.code == "TRUNCATED"
    assert result.first_bad_seq == 3  # first missing seq


def test_drop_suffix_detected_via_expected_length() -> None:
    chain = _happy_chain()
    prefix = chain.to_records()[:2]
    result = verify_records(prefix, expected_length=4)
    assert result.ok is False
    assert result.code == "TRUNCATED"


def test_wrong_expected_head_fails_even_for_valid_chain() -> None:
    chain = _happy_chain()
    result = verify_records(chain.to_records(), expected_head="b" * 64)
    assert result.ok is False
    assert result.code == "TRUNCATED"


# ---- fork ----


def test_fork_two_children_of_same_prev_fails() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    chain.append("ACTION_REQUESTED", action_id="a1", payload={"n": 1})
    base = chain.to_records()
    # Two different children claiming the same prev (the head of base)
    head = base[-1].record_hash
    child_a = EvidenceChain(run_id=RUN_A, records=list(base))
    child_a.append("POLICY_ALLOWED", action_id="a1", payload={"branch": "a"})
    child_b = EvidenceChain(run_id=RUN_A, records=list(base))
    child_b.append("POLICY_DENIED", action_id="a1", payload={"branch": "b"})
    assert child_a.to_records()[-1].prev_hash == head
    assert child_b.to_records()[-1].prev_hash == head
    # Merged log contains both children -> fork (duplicate prev_hash)
    merged = base + [child_a.to_records()[-1], child_b.to_records()[-1]]
    result = verify_records(merged)
    assert result.ok is False
    assert result.code in ("FORK", "BAD_SEQ", "BAD_PREV")


# ---- decision-before-execution ordering ----


def test_execution_without_any_decision_fails_ordering() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    chain.append("ACTION_REQUESTED", action_id="act-x", payload={})
    chain.append("EXECUTION_STARTED", action_id="act-x", payload={})
    result = chain.verify()
    assert result.ok is False
    assert result.code == "ORDERING_VIOLATION"
    assert result.first_bad_seq == 2


def test_execution_before_decision_in_seq_order_fails() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    # Manually craft valid hashes but wrong semantic order: execution first
    chain.append("ACTION_REQUESTED", action_id="act-1", payload={})
    chain.append("EXECUTION_SUCCEEDED", action_id="act-1", payload={})
    chain.append("POLICY_ALLOWED", action_id="act-1", payload={})
    result = chain.verify()
    assert result.ok is False
    assert result.code == "ORDERING_VIOLATION"
    assert result.first_bad_seq == 2


def test_execution_without_action_id_fails_ordering() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    chain.append("ACTION_REQUESTED", action_id="act-1", payload={})
    chain.append("POLICY_ALLOWED", action_id="act-1", payload={})
    chain.append("EXECUTION_STARTED", action_id=None, payload={})
    result = chain.verify()
    assert result.ok is False
    assert result.code == "ORDERING_VIOLATION"


def test_denied_decision_counts_as_decision_no_execution_ok() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    chain.append("ACTION_REQUESTED", action_id="act-d", payload={})
    chain.append("POLICY_DENIED", action_id="act-d", payload={"reason": "blocked"})
    result = chain.verify()
    assert result.ok is True


def test_held_decision_permits_later_execution() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    chain.append("ACTION_REQUESTED", action_id="act-h", payload={})
    chain.append("POLICY_HELD", action_id="act-h", payload={})
    chain.append("APPROVAL_GRANTED", action_id="act-h", payload={})
    chain.append("EXECUTION_SUCCEEDED", action_id="act-h", payload={})
    assert chain.verify().ok is True


def test_decision_event_types_cover_allow_deny_hold() -> None:
    assert "POLICY_ALLOWED" in DECISION_EVENT_TYPES
    assert "POLICY_DENIED" in DECISION_EVENT_TYPES
    assert "POLICY_HELD" in DECISION_EVENT_TYPES
    assert "EXECUTION_STARTED" in EXECUTION_EVENT_TYPES
    assert "EXECUTION_SUCCEEDED" in EXECUTION_EVENT_TYPES
    assert "EXECUTION_FAILED" in EXECUTION_EVENT_TYPES


def test_independent_actions_do_not_cross_satisfy_ordering() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    chain.append("ACTION_REQUESTED", action_id="act-1", payload={})
    chain.append("POLICY_ALLOWED", action_id="act-1", payload={})
    chain.append("ACTION_REQUESTED", action_id="act-2", payload={})
    chain.append("EXECUTION_SUCCEEDED", action_id="act-2", payload={})  # no decision for act-2
    result = chain.verify()
    assert result.ok is False
    assert result.first_bad_seq == 4


# ---- determinism / canonical JSON ----


def test_determinism_same_events_same_head_hash() -> None:
    c1 = _happy_chain()
    c2 = _happy_chain()
    assert c1.head_hash() == c2.head_hash()


def test_different_payload_gives_different_head() -> None:
    c1 = _happy_chain()
    c2 = EvidenceChain(run_id=RUN_A)
    c2.append("ACTION_REQUESTED", action_id="act-1", payload={"op": "DIFFERENT"})
    c2.append("POLICY_ALLOWED", action_id="act-1", payload={"rule": "R1"})
    c2.append("EXECUTION_STARTED", action_id="act-1", payload={"executor": "syn"})
    c2.append("EXECUTION_SUCCEEDED", action_id="act-1", payload={"status": "ok"})
    assert c1.head_hash() != c2.head_hash()


def test_canonical_bytes_key_order_independent() -> None:
    assert canonical_bytes({"b": 2, "a": 1}) == canonical_bytes({"a": 1, "b": 2})


def test_canonical_bytes_uses_compact_separators() -> None:
    raw = canonical_bytes({"a": 1, "b": [1, 2]})
    assert b": " not in raw
    assert b", " not in raw
    assert json.loads(raw) == {"a": 1, "b": [1, 2]}


def test_digest_helpers_are_stable_hex() -> None:
    assert digest_text("hello") == digest_text("hello")
    assert len(digest_text("hello")) == 64
    assert digest_payload({"x": 1}) == digest_payload({"x": 1})
    assert digest_payload({"x": 1}) != digest_payload({"x": 2})


# ---- no secrets in records ----


def test_records_store_digests_not_raw_payload() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    rec = chain.append(
        "ACTION_REQUESTED",
        action_id="act-s",
        payload={"arguments": {"secret": "hunter2"}, "trace": "raw chain-of-thought"},
    )
    blob = json.dumps(rec.to_dict(), sort_keys=True)
    assert "hunter2" not in blob
    assert "raw chain-of-thought" not in blob
    assert len(rec.payload_digest) == 64


def test_run_id_mismatch_fails() -> None:
    chain = _happy_chain(run_id=RUN_A)
    records = chain.to_records()
    records[2].run_id = RUN_B
    # record_hash is now stale too; either code is acceptable, seq must be 3
    result = verify_records(records, expected_run_id=RUN_A)
    assert result.ok is False
    assert result.first_bad_seq == 3


# ---- JSONL round-trip ----


def test_jsonl_round_trip_preserves_verification(tmp_path) -> None:
    chain = _happy_chain()
    path = tmp_path / "chain.jsonl"
    chain.export_jsonl(str(path))
    loaded = EvidenceChain.from_jsonl(str(path))
    assert loaded.head_hash() == chain.head_hash()
    assert loaded.verify().ok is True


def test_jsonl_import_tampered_line_fails(tmp_path) -> None:
    chain = _happy_chain()
    path = tmp_path / "chain.jsonl"
    chain.export_jsonl(str(path))
    lines = path.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[1])
    obj["event_type"] = "POLICY_DENIED"
    lines[1] = json.dumps(obj, sort_keys=True)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    loaded = EvidenceChain.from_jsonl(str(bad))
    result = loaded.verify()
    assert result.ok is False
    assert result.first_bad_seq == 2


def test_to_dict_from_dict_round_trip() -> None:
    chain = _happy_chain()
    for rec in chain.to_records():
        assert ChainRecord.from_dict(rec.to_dict()) == rec


# ---- adapter: exported EvidenceEvents (no schema migration) ----


def test_adapter_builds_verifiable_chain_from_exported_events() -> None:
    from scopewatch.evidence_chain import build_chain_from_events

    events = [
        {"event_type": "ACTION_REQUESTED", "action_request_id": "a1",
         "summary": "req", "details": {"tool": "workspace"}},
        {"event_type": "POLICY_ALLOWED", "action_request_id": "a1",
         "summary": "allow", "details": {"rule": "R1"}},
        {"event_type": "EXECUTION_SUCCEEDED", "action_request_id": "a1",
         "summary": "ok", "details": {"status": "EXECUTED"}},
    ]
    chain = build_chain_from_events(RUN_A, events)
    assert chain.verify().ok is True


def test_adapter_never_embeds_raw_secrets() -> None:
    from scopewatch.evidence_chain import build_chain_from_events

    events = [
        {"event_type": "ACTION_REQUESTED", "action_request_id": "a9",
         "summary": "req hunter2 should not leak",
         "details": {"arguments": {"token": "sk-live-123"}, "trace": "raw thought"}},
        {"event_type": "POLICY_ALLOWED", "action_request_id": "a9",
         "summary": "allow", "details": {}},
        {"event_type": "EXECUTION_SUCCEEDED", "action_request_id": "a9",
         "summary": "ok", "details": {}},
    ]
    chain = build_chain_from_events(RUN_A, events)
    blob = chain.to_jsonl()
    assert "hunter2" not in blob
    assert "sk-live-123" not in blob
    assert "raw thought" not in blob
    assert chain.verify().ok is True


# ---- perf sanity ----


def test_large_chain_10k_records_verifies_in_seconds() -> None:
    chain = EvidenceChain(run_id=RUN_A)
    for i in range(10_000):
        # Interleave valid decision->execution pairs every 4 records so the
        # ordering rule stays satisfied at scale.
        slot = i % 4
        aid = f"act-{i // 4}"
        if slot == 0:
            chain.append("ACTION_REQUESTED", action_id=aid, payload={"i": i})
        elif slot == 1:
            chain.append("POLICY_ALLOWED", action_id=aid, payload={"i": i})
        elif slot == 2:
            chain.append("EXECUTION_STARTED", action_id=aid, payload={"i": i})
        else:
            chain.append("EXECUTION_SUCCEEDED", action_id=aid, payload={"i": i})
    start = time.monotonic()
    result = chain.verify()
    elapsed = time.monotonic() - start
    assert result.ok is True
    assert result.checked == 10_000
    assert elapsed < 5.0, f"10k verify took {elapsed:.2f}s, expected < 5s"
