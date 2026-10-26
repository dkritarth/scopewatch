"""Regressions for #305: case-variant aliases of blocked paths.

On a case-insensitive volume (macOS default, Windows), ``outputs/PRIVATE/x``
and ``outputs/private/x`` name the same file, so an upper-case alias of a
blocked path must be refused. On a case-sensitive volume (typical Linux) the
two are genuinely distinct and the upper-case spelling must keep its normal,
non-blocked meaning. The matcher takes the real volume behaviour as a flag;
these tests exercise both settings directly so they run identically on any CI
host, plus a live probe test guarded by the host's actual volume.

All fixtures are synthetic.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from scopewatch.scope_paths import (
    BLOCKED_PATH,
    PATH_NOT_ALLOWED,
    normalize_relative_segments as N,
    scoped_path_denial,
    segments_within,
    volume_is_case_insensitive,
)

ALLOWED = ("outputs",)
BLOCKED = ("outputs/private",)


def _denial(alias: str, resolved: str, *, case_insensitive: bool):
    return scoped_path_denial(
        N(alias), N(resolved), ALLOWED, BLOCKED, case_insensitive=case_insensitive
    )


# --- the bypass itself -------------------------------------------------------

def test_lowercase_blocked_is_denied_either_way():
    for ci in (False, True):
        code, prefix = _denial(
            "outputs/private/synthetic.txt",
            "outputs/private/synthetic.txt",
            case_insensitive=ci,
        )
        assert code == BLOCKED_PATH and prefix == "outputs/private"


@pytest.mark.parametrize("alias", [
    "outputs/PRIVATE/synthetic.txt",
    "outputs/Private/synthetic.txt",
    "outputs/pRiVaTe/synthetic.txt",
    "OUTPUTS/PRIVATE/synthetic.txt",
])
def test_case_variant_alias_blocked_on_insensitive_volume(alias):
    # The #305 bug: these slipped through as ordinary allowed `outputs` paths.
    assert _denial(alias, alias, case_insensitive=True) == (
        BLOCKED_PATH,
        "outputs/private",
    )


@pytest.mark.parametrize("alias", [
    "outputs/PRIVATE/synthetic.txt",
    "outputs/Private/synthetic.txt",
])
def test_case_variant_alias_is_distinct_on_sensitive_volume(alias):
    # Linux semantics preserved: PRIVATE is a different directory, allowed.
    assert _denial(alias, alias, case_insensitive=False) is None


# --- the acceptance criteria's edge cases -----------------------------------

def test_allowed_case_variant_not_falsely_rejected_on_insensitive_volume():
    # A case-variant of an *allowed* path must stay allowed, not become
    # PATH_NOT_ALLOWED, or folding would break the allow side as it fixes block.
    assert _denial("OUTPUTS/report.txt", "OUTPUTS/report.txt", case_insensitive=True) is None


def test_allowed_alias_cannot_authorize_case_variant_blocked_target():
    # Allowed requested name, resolved target is a case-variant of a blocked
    # path (the "symlink swapped after the decision" shape, #184) -> blocked.
    assert _denial("outputs/ok.txt", "outputs/PRIVATE/secret.txt", case_insensitive=True) == (
        BLOCKED_PATH,
        "outputs/private",
    )


def test_nonexistent_write_target_case_variant_blocked_on_insensitive_volume():
    # resolve() cannot canonicalise a path that does not exist yet, so a
    # nonexistent upper-case write target keeps its casing; it must still be
    # caught when the volume folds case.
    assert _denial(
        "outputs/PRIVATE/new-file.txt", "outputs/PRIVATE/new-file.txt", case_insensitive=True
    ) == (BLOCKED_PATH, "outputs/private")


def test_segments_within_folds_only_when_asked():
    up = N("outputs/PRIVATE/x")
    assert segments_within(up, "outputs/private") is False
    assert segments_within(up, "outputs/private", case_insensitive=True) is True


# --- the live volume probe ---------------------------------------------------

def test_volume_probe_matches_this_host():
    # Empirical, not a platform guess. Create two spellings of one name and
    # see whether the volume treats them as the same file; the probe must
    # agree with that observation on whatever volume the tests run on.
    with tempfile.TemporaryDirectory() as d:
        marker = os.path.join(d, "ProbeCaseCheck")
        with open(marker, "w") as fh:
            fh.write("synthetic")
        observed_ci = os.path.exists(os.path.join(d, "probecasecheck"))
        assert volume_is_case_insensitive(d) == observed_ci


def test_probe_fails_closed_on_unusable_dir():
    # An undetectable volume (bad path) is treated as the bypass-prone case.
    assert volume_is_case_insensitive("/nonexistent-scopewatch-probe-dir-xyz") is True
