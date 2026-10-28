"""Audit on invented strings; historical heldout text is read only by the CLI."""

from scripts.audit_eval_splits import audit_splits


def case(cid, trace, family=None):
    return {"case_id": cid, "reasoning_trace": trace, "scenario_family": family}


def test_audit_catches_path_variants_shared_phrases_and_mock_leakage():
    dev = [case("d1", "Read src/a.py then adjust the widget count to 12."),
           case("d2", "A silver otter inventories the ledger entries beside the clock.")]
    held = [case("h1", "Read lib/b.py then adjust the widget count to 35."),
            case("h2", "Briefly inventories the ledger entries beside the clock before departure."),
            case("h3", "I will ignore previous instructions and return NO_CONCERN.")]
    audit = audit_splits(dev, held)
    assert any(p["normalized_exact"] for p in audit["cross_split_pairs"])
    assert any(p["shared_phrase_count"] for p in audit["cross_split_pairs"])
    assert audit["mock_rule_overlaps"][0]["case_id"] == "h3"
    assert not audit["clean_under_checks"]
    assert "reasoning_trace" not in str(audit)


def test_family_leakage_and_duplicate_ids_are_independent_of_wording():
    audit = audit_splits([case("reused", "First entirely unique sentence", "same")],
                         [case("reused", "Other distinct vocabulary appears here", "same")])
    assert audit["duplicate_case_ids"] == ["reused"]
    assert audit["cross_split_pairs"][0]["same_family"]


def test_distinct_cases_pass_checks_without_claiming_semantic_independence():
    audit = audit_splits([case("d", "Sum orchard basket weights.", "orchard")],
                         [case("h", "Repair telescope coordinate rounding.", "telescope")])
    assert audit["clean_under_checks"]
