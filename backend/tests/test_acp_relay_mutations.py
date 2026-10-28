"""Prove relay guard tests detect removal of the specific guard they cover.

Only one function is compiled and replaced per case. Exact source matching and
AST checks fail if a mutation drifts to another function or stops applying.
The existing public-ACP regression test must then fail an assertion, rather
than merely failing import, compilation, or fixture setup.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Callable

import pytest

import scopewatch.acp_adapter as acp
import test_acp_adapter as guards


# function, original expression, replacement, regression test, arguments
CASES = [
    ("_require_executed_receipt", '_bounded_optional(status, MAX_RELAYED_ERROR_CHARS)', 'status',
     guards.test_failed_receipt_status_and_error_code_are_bounded_and_marked, ()),
    ("_require_executed_receipt", '_bounded_optional(error_code, MAX_RELAYED_ERROR_CHARS)', 'error_code',
     guards.test_failed_receipt_status_and_error_code_are_bounded_and_marked, ()),
    ("_submit_action", '"http_status": resp.status_code,', '"http_status": resp.status_code, "raw": resp.json(),',
     guards.test_gateway_http_error_body_is_not_relayed, ({"detail": "synthetic raw body" * 5000},)),
    ("_submit_action", '_bounded_optional(\n                decision_info.get("reason_code"), MAX_RELAYED_ERROR_CHARS\n            )',
     'decision_info.get("reason_code")', guards.test_policy_deny_decision_text_is_bounded_and_marked, ()),
    ("_submit_action", '_bounded_optional(\n                        decision_info.get("explanation"), MAX_RELAYED_ERROR_CHARS\n                    )',
     'decision_info.get("explanation")', guards.test_policy_deny_decision_text_is_bounded_and_marked, ()),
    ("_submit_action", '_bounded_optional(\n                        decision_info.get("matched_rule"), MAX_RELAYED_ERROR_CHARS\n                    )',
     'decision_info.get("matched_rule")', guards.test_policy_deny_decision_text_is_bounded_and_marked, ()),
    ("_submit_action", '_bounded_optional(outcome, MAX_RELAYED_ERROR_CHARS)', 'outcome',
     guards.test_unknown_decision_outcome_is_bounded_and_marked, ()),
    *[("_handle_request_permission", f'_bounded_optional(decision.get("{field}"), MAX_RELAYED_ERROR_CHARS)',
       f'decision.get("{field}")', guards.test_preflight_decision_text_is_bounded_and_marked, ("ALLOW", "explanation"))
      for field in ("reason_code", "explanation", "matched_rule")],
    ("_handle_request_permission", '_bounded_text(\n                    explanation\n                    or f"Operation \'{operation}\' requires human approval before execution.",\n                    MAX_RELAYED_ERROR_CHARS,\n                )',
     'explanation or f"Operation \'{operation}\' requires human approval before execution."',
     guards.test_preflight_fallback_reason_is_bounded, ("HOLD",)),
    ("_handle_request_permission", '_bounded_text(\n                    explanation or f"Operation \'{operation}\' denied by policy.",\n                    MAX_RELAYED_ERROR_CHARS,\n                )',
     'explanation or f"Operation \'{operation}\' denied by policy."',
     guards.test_preflight_fallback_reason_is_bounded, ("DENY",)),
    ("_handle_request_permission", '"Gateway policy preflight returned an unknown outcome; failing closed."',
     'f"Unknown outcome: {outcome}"', guards.test_preflight_unknown_outcome_is_sanitized, ()),
    ("_bounded_entries", '_bounded_text(value, MAX_RELAYED_NAME_CHARS)', 'value',
     guards.test_list_directory_oversized_name_and_path_clips_are_marked, ()),
    ("_bounded_entries", '_bounded_text(entry, MAX_RELAYED_NAME_CHARS)', 'entry',
     guards.test_list_directory_legacy_name_clip_is_marked, ()),
    ("_bounded_entries", 'entries[:MAX_RELAYED_ENTRIES]', 'entries',
     guards.test_list_directory_count_clip_is_marked, ()),
    ("_bounded_entries", 'bounded.append(shown)', 'shown.update(entry)\n            bounded.append(shown)',
     guards.test_list_directory_extra_entry_keys_are_omitted_and_marked, ()),
    ("_bounded_entries", 'if not isinstance(value, str):', 'if False:',
     guards.test_list_directory_malformed_fields_are_refused, ([{"name": {"nested": "synthetic"}}],)),
    ("_bounded_entries", 'if key == "type" and value not in ("file", "directory"):', 'if False:',
     guards.test_list_directory_malformed_fields_are_refused, ([{"type": "file" * 5000}],)),
    ("_bounded_optional", 'if isinstance(value, str) else None', 'if True else None',
     guards.test_non_text_codes_are_not_coerced_into_payloads,
     ("terminal/run", {"policy_decision": {"outcome": "ALLOW"},
                       "execution_receipt": {"status": {"raw": "synthetic"}, "error_code": ["synthetic"]}})),
]


@pytest.mark.parametrize("name,before,after,regression,args", CASES,
                         ids=[f"{case[0]}-{i}" for i, case in enumerate(CASES)])
def test_guard_removal_is_detected(
    monkeypatch: pytest.MonkeyPatch, name: str, before: str, after: str,
    regression: Callable, args: tuple,
) -> None:
    owner = acp if name.startswith("_bounded") else acp.AcpClientAdapter
    source = inspect.getsource(getattr(owner, name))
    assert source.count(before) == 1, f"mutation must land exactly once in {name}"
    mutated = textwrap.dedent(source.replace(before, after, 1))
    tree = ast.parse(mutated)
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)
    assert tree.body[0].name == name, "mutation landed outside intended function"
    namespace = dict(vars(acp))
    exec(compile(tree, f"<relay mutation {name}>", "exec"), namespace)
    monkeypatch.setattr(owner, name, namespace[name])
    with pytest.raises(AssertionError):
        regression(*args)
