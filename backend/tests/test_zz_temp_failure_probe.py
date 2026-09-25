def test_deliberate_failure_for_branch_protection_check():
    assert False, "deliberate failure: proves required status checks block a merge"
