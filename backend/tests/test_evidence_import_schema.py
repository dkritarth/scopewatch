"""Every accepted evidence attribute must belong to the authenticated schema."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scopewatch.evidence_chain import EvidenceChain

ROOT = Path(__file__).resolve().parents[2]


def verify_cli(path, chain):
    return subprocess.run([
        sys.executable, str(ROOT / "scripts/verify_evidence_chain.py"), str(path),
        "--expected-head", chain.head_hash(), "--expected-length", "1", "--run-id", chain.run_id,
    ], env={**os.environ, "PYTHONPATH": str(ROOT / "backend")}, capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("field,value", [
    ("summary", "Synthetic fabricated approval"),
    ("details", {"decision": "ALLOW", "execution": "succeeded", "action_id": "fake-1"}),
])
def test_unknown_record_fields_cannot_pass_pinned_verification(tmp_path, field, value):
    chain = EvidenceChain("synthetic-import")
    chain.append("RUN_CREATED")
    record = json.loads(chain.to_jsonl())
    record[field] = value
    data = json.dumps(record)
    with pytest.raises(ValueError, match="Unknown"):
        EvidenceChain.from_jsonl_string(data)
    path = tmp_path / "chain.jsonl"
    path.write_text(data)
    result = verify_cli(path, chain)
    assert result.returncode == 2
    assert json.loads(result.stdout)["code"] == "MALFORMED"
    assert result.stderr == ""


@pytest.mark.parametrize("field", ["run_id", "event_type", "action_id", "payload_digest", "prev_hash", "record_hash"])
def test_unpaired_surrogates_are_malformed_before_hashing(tmp_path, field):
    chain = EvidenceChain("synthetic-import")
    chain.append("RUN_CREATED")
    record = json.loads(chain.to_jsonl())
    record[field] = "\ud800"
    data = json.dumps(record)
    with pytest.raises(ValueError, match=field):
        EvidenceChain.from_jsonl_string(data)
    path = tmp_path / "chain.jsonl"
    path.write_text(data)
    result = verify_cli(path, chain)
    assert result.returncode == 2
    assert json.loads(result.stdout)["code"] == "MALFORMED"
    assert result.stderr == ""


def test_excessive_json_nesting_returns_malformed_json_without_traceback(tmp_path):
    path = tmp_path / "chain.jsonl"
    path.write_text("[" * 10000 + "0" + "]" * 10000)
    chain = EvidenceChain("synthetic-import")
    chain.append("RUN_CREATED")
    result = verify_cli(path, chain)
    assert result.returncode == 2
    assert json.loads(result.stdout)["code"] == "MALFORMED"
    assert result.stderr == ""


def test_valid_unicode_and_optional_action_id_retain_pinned_round_trip(tmp_path):
    chain = EvidenceChain("synthetic-🧪")
    chain.append("RUN_CREATED")
    record = json.loads(chain.to_jsonl())
    del record["action_id"]
    path = tmp_path / "chain.jsonl"
    path.write_text(json.dumps(record))
    result = verify_cli(path, chain)
    assert result.returncode == 0
    assert json.loads(result.stdout)["code"] == "OK"
    assert result.stderr == ""


@pytest.mark.parametrize("field,earlier", [
    ("event_type", "POLICY_DENIED"),
    ("action_id", "synthetic-conflicting-action"),
    ("payload_digest", "synthetic-conflicting-digest"),
    ("seq", 99),
    ("run_id", "synthetic-conflicting-run"),
    ("prev_hash", "synthetic-conflicting-parent"),
    ("record_hash", "synthetic-conflicting-head"),
])
@pytest.mark.parametrize("escaped", [False, True], ids=["literal", "escaped"])
def test_duplicate_fields_rejected_before_pinned_verification(tmp_path, field, earlier, escaped):
    chain = EvidenceChain("synthetic-duplicate-import")
    chain.append("POLICY_ALLOWED", action_id="synthetic-action")
    name = ('"\\u%04x%s"' % (ord(field[0]), field[1:])) if escaped else json.dumps(field)
    ambiguous = "{" + name + ":" + json.dumps(earlier) + "," + chain.to_jsonl()[1:]
    path = tmp_path / "chain.jsonl"
    path.write_text(ambiguous, encoding="utf-8")
    result = verify_cli(path, chain)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "ok": False, "code": "MALFORMED",
        "reason": "malformed JSONL: Duplicate evidence record field",
    }
    assert result.stderr == ""
    with pytest.raises(ValueError, match="^Duplicate evidence record field$"):
        EvidenceChain.from_jsonl_string(ambiguous)
    with pytest.raises(ValueError, match="^Duplicate evidence record field$"):
        EvidenceChain.from_jsonl(path)


def test_identical_duplicate_fields_are_ambiguous(tmp_path):
    chain = EvidenceChain("synthetic-duplicate-import")
    chain.append("RUN_CREATED")
    ambiguous = '{"event_type":"RUN_CREATED",' + chain.to_jsonl()[1:]
    with pytest.raises(ValueError, match="^Duplicate evidence record field$"):
        EvidenceChain.from_jsonl_string(ambiguous)
    path = tmp_path / "chain.jsonl"
    path.write_text(ambiguous)
    assert verify_cli(path, chain).returncode == 2


def test_unique_escaped_members_and_unicode_values_preserve_pins(tmp_path):
    chain = EvidenceChain("synthetic-α")
    chain.append("POLICY_ALLOWED", action_id="synthetic-雪")
    record = json.loads(chain.to_jsonl())
    data = json.dumps(record, ensure_ascii=False).replace('"event_type":', '"\\u0065vent_type":')
    path = tmp_path / "chain.jsonl"
    path.write_text(data, encoding="utf-8")
    result = verify_cli(path, chain)
    assert result.returncode == 0
    assert json.loads(result.stdout)["code"] == "OK"
    imported = EvidenceChain.from_jsonl_string(data)
    assert imported.to_jsonl() == chain.to_jsonl()
    assert imported.run_id == "synthetic-α"
    assert imported.to_records()[0].action_id == "synthetic-雪"


def test_member_names_can_repeat_across_distinct_records():
    chain = EvidenceChain("synthetic-distinct-records")
    chain.append("RUN_CREATED")
    chain.append("POLICY_ALLOWED", action_id="synthetic-action")
    imported = EvidenceChain.from_jsonl_string(chain.to_jsonl())
    assert imported.verify(expected_head=chain.head_hash(), expected_length=2).ok
    assert imported.to_jsonl() == chain.to_jsonl()
