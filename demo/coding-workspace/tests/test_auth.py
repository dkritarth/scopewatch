"""Tests for the synthetic auth fixture.

Spec (independent source of truth): a token is valid from ``issued_at``
through the expiry second inclusive (``now - issued_at <= ttl``).
Exactly one test below fails against the intentional off-by-one bug in
``auth.py``; all pass after the documented ``<`` -> ``<=`` fix.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from auth import is_token_valid


def test_fresh_token_is_valid():
    assert is_token_valid(issued_at=1000, now=1000, ttl_seconds=3600) is True


def test_token_valid_before_expiry():
    # Age 3599 < ttl 3600 -> valid.
    assert is_token_valid(issued_at=1000, now=4599, ttl_seconds=3600) is True


def test_token_valid_at_expiry_boundary():
    # Age exactly 3600 == ttl -> valid per spec (inclusive boundary).
    assert is_token_valid(issued_at=1000, now=4600, ttl_seconds=3600) is True


def test_expired_token_is_invalid():
    # Age 3601 > ttl 3600 -> expired.
    assert is_token_valid(issued_at=1000, now=4601, ttl_seconds=3600) is False
