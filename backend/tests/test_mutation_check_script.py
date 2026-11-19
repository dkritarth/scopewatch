"""scripts/mutation_check.py must not count an unapplied mutation as caught."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("mutation_check", REPO_ROOT / "scripts" / "mutation_check.py")
assert _spec and _spec.loader
mutation_check = importlib.util.module_from_spec(_spec)
sys.modules["mutation_check"] = mutation_check  # @dataclass resolves annotations via sys.modules
_spec.loader.exec_module(mutation_check)


def test_unapplied_mutation_is_not_reported_as_caught() -> None:
    stale = mutation_check.Mutation(
        name="stale", invariant="x", file="backend/scopewatch/policy.py",
        old="this pattern is not in policy.py", new="x", test="backend/tests/none.py::t",
    )
    assert mutation_check.run_one(stale) is False


def test_every_registered_mutation_pattern_still_exists() -> None:
    stale = [
        m.name for m in mutation_check.MUTATIONS
        if m.old not in (REPO_ROOT / m.file).read_text(encoding="utf-8")
    ]
    assert stale == []
