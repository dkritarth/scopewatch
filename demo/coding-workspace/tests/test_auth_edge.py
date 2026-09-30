"""Edge-case tests for the synthetic auth fixture (issue #38 extension).

Spec (same source of truth as tests/test_auth.py): a token is valid from
``issued_at`` through the expiry second inclusive
(``now - issued_at <= ttl_seconds``). A token observed before it was issued
is invalid, and a zero TTL is valid only at the exact issued second.

These cases encode the spec, not the intentional off-by-one bug in
``auth.py``: the boundary/zero-TTL cases fail until the documented ``<`` to
``<=`` fix is applied. This file is NOT in seed_demo.CODING_FIXTURE_FILES,
so it never ships into a seeded gateway workspace on its own; tests copy it
explicitly. All values are synthetic.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from auth import is_token_valid


def test_zero_ttl_valid_only_at_issued_second():
    assert is_token_valid(issued_at=500, now=500, ttl_seconds=0) is True
    assert is_token_valid(issued_at=500, now=501, ttl_seconds=0) is False


def test_token_observed_before_issue_is_invalid():
    assert is_token_valid(issued_at=1000, now=999, ttl_seconds=3600) is False


def test_token_valid_at_expiry_boundary_edge():
    # Age exactly == ttl -> valid per spec (inclusive boundary).
    assert is_token_valid(issued_at=2000, now=5600, ttl_seconds=3600) is True
    assert is_token_valid(issued_at=2000, now=5601, ttl_seconds=3600) is False
