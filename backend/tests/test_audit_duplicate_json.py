"""Ambiguous auditor objects must not discard earlier verdict evidence."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from scopewatch.db import init_db
from scopewatch.providers.client import ProviderClient
from scopewatch.providers.profile import ProviderProfile
from scopewatch.reasoning_audit import ReasoningAuditor, parse_auditor_output
from scopewatch.schemas import SubmitActionRequest, TaskScope
from scopewatch.service import ScopewatchService


DUPLICATES = [
    '"verdict":"CONCERN","verdict":"NO_CONCERN","concern_type":null,"flagged_excerpts":[]',
    '"verdict":"NO_CONCERN","concern_type":"SCOPE_DRIFT","concern_type":null,"flagged_excerpts":[]',
    '"verdict":"NO_CONCERN","concern_type":null,"flagged_excerpts":["synthetic trace"],"flagged_excerpts":[]',
    '"verdict":"NO_CONCERN","verdict":"NO_CONCERN","concern_type":null,"flagged_excerpts":[]',
    '"verdict":"CONCERN","ver\\u0064ict":"NO_CONCERN","concern_type":null,"flagged_excerpts":[]',
    '"verdict":"NO_CONCERN","concern_type":null,"flagged_excerpts":[],"explanation":"first","explanation":"second"',
]


@pytest.mark.parametrize("members", DUPLICATES,
                         ids=["verdict", "concern", "excerpts", "identical", "escaped-key", "explanation"])
@pytest.mark.parametrize("fenced", [False, True], ids=["plain", "fenced"])
def test_duplicate_members_fail_closed(members, fenced):
    response = "{" + members + "}"
    if fenced:
        response = "```json\n" + response + "\n```"
    result = parse_auditor_output(response, "synthetic trace", model="synthetic", profile="synthetic",
                                  total_tokens=17)
    assert result.verdict.value == "FAILED"
    assert result.error_code == "PARSE_ERROR"
    assert result.total_tokens == 17
    assert result.concern_type is None
    assert result.flagged_excerpts == []


@pytest.mark.parametrize("verdict,concern,excerpts", [
    ("NO_CONCERN", None, []), ("CONCERN", "SCOPE_DRIFT", ["synthetic trace"]),
])
def test_unique_member_objects_retain_verdicts(verdict, concern, excerpts):
    response = json.dumps({"verdict": verdict, "concern_type": concern,
                           "flagged_excerpts": excerpts, "explanation": "Synthetic result"})
    result = parse_auditor_output(response, "synthetic trace", model="synthetic", profile="synthetic")
    assert result.verdict.value == verdict
    assert result.error_code is None


@pytest.mark.parametrize("members", DUPLICATES[:3], ids=["verdict", "concern", "excerpts"])
def test_duplicate_provider_audit_holds_before_filesystem_effect(tmp_path, monkeypatch, members):
    monkeypatch.setenv("SCOPEWATCH_REASONING_AUDIT", "true")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_ROUTING", "single")
    monkeypatch.setenv("SCOPEWATCH_AGENT_PROFILE", "mock")
    monkeypatch.setenv("SCOPEWATCH_AUDITOR_PROFILE", "mock")
    monkeypatch.setenv("SCOPEWATCH_EXECUTOR", "local")
    database = tmp_path / "gateway.db"
    init_db(database)
    workspace = tmp_path / "workspace"
    (workspace / "outputs").mkdir(parents=True)
    marker = "synthetic-private-provider-detail"
    response = "{" + members + ',"unrecognized_detail":"' + marker + '"}'

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": response}}],
                                         "usage": {"total_tokens": 17}})

    profile = ProviderProfile(name="synthetic", base_url="https://synthetic.invalid",
                              model="synthetic", max_retries=0)
    with ProviderClient(profile=profile, api_key="synthetic", transport=httpx.MockTransport(handler)) as client:
        auditor = ReasoningAuditor(provider=client, profile="synthetic", model="synthetic")
        service = ScopewatchService(db_path=database, workspace_root=workspace, auditor=auditor)
        run, _ = service.create_run(name="Synthetic ambiguous audit", task_scope=TaskScope(
            task_description="Write synthetic output", allowed_paths=["outputs"],
            allowed_tools=["workspace"], allowed_operations=["write_text"],
            created_at="2026-11-06T00:00:00Z"))
        result = asyncio.run(service.submit_action(run.id, SubmitActionRequest(
            tool="workspace", operation="write_text", resource="outputs/result.txt",
            arguments={"content": "synthetic output"}, exposed_reasoning_trace="synthetic trace",
            reasoning_provenance="SYNTHETIC_FIXTURE")))
    output = Path(run.workspace_path) / "outputs/result.txt"
    assert result.reasoning_audit.verdict == "FAILED", (
        result.policy_decision.outcome, result.execution_receipt.status if result.execution_receipt else None,
        output.exists(),
    )
    assert result.reasoning_audit.error_code == "PARSE_ERROR"
    assert result.reasoning_audit.total_tokens == 17
    assert result.policy_decision.outcome == "HOLD"
    assert result.approval_request is not None
    assert result.execution_receipt is None
    assert not output.exists()
    assert marker not in result.model_dump_json()
