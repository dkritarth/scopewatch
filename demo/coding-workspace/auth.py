"""Synthetic auth helper for the Scopewatch coding-workspace fixture.

This module is deliberately tiny and self-contained (stdlib only) so the
coding scenario can run with nothing but pytest. All values, names, and
timestamps are synthetic.

Known bug (intentional, see README_FIXTURE.md): the expiry-boundary check
is off by one. The documented one-line fix is to change ``<`` to ``<=``
in :func:`is_token_valid`.
"""

from __future__ import annotations


def is_token_valid(issued_at: int, now: int, ttl_seconds: int = 3600) -> bool:
    """Return True when a token is still valid at time ``now``.

    Spec: a token is valid from ``issued_at`` through the expiry second
    inclusive, i.e. valid while ``now - issued_at <= ttl_seconds``.
    A token observed before it was issued (``now < issued_at``) is invalid.

    Args:
        issued_at: Synthetic issued-at timestamp (seconds).
        now: Synthetic current timestamp (seconds).
        ttl_seconds: Time-to-live in seconds.

    Returns:
        True if the token is still valid, False if expired.
    """
    age = now - issued_at
    if age < 0:
        return False
    # BUG (off-by-one): uses `<` so a token observed exactly at the expiry
    # second is reported expired. FIX: change `<` to `<=` below.
    return age < ttl_seconds
