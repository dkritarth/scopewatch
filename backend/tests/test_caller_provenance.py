"""Tests for issue #116: caller-supplied reasoning provenance is never verified.

The gateway API cannot tell a reasoning trace captured in-process by the
provider client from a label typed into a JSON body. Only a submission that
presents the operator-issued capture credential may store a verified
provider-trace label; everything else is stored as a caller assertion with its
raw claim preserved for diagnostics.

Related: #120 (summary provenance through normalization) is covered here too
because it rides the same submission path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scopewatch.app import create_app
from scopewatch.db import init_db
from scopewatch.models import EventType, ReasoningProvenance
from scopewatch.provenance import (
    CAPTURE_TOKEN_ENV_VAR,
    CAPTURE_TOKEN_HEADER,
    capture_credential_verified,
    provenance_for_submission,
)
from scopewatch.schemas import TaskScope
from scopewatch.service import ScopewatchService

# Synthetic stand-in for the operator-issued credential. Not a real secret.
TEST_CAPTURE_TOKEN = "synthetic-test-capture-token"

# Reasoning audit is a live model call in some paths; keep these tests offline
# and deterministic. The escalation behaviour of reasoning is covered elsewhere.
AUDIT_OFF_ENV = "SCOPEWATCH_REASONING_AUDIT"


def _scope() -> TaskScope:
    return TaskScope(
        task_description="Audit approved invoices and write a summary.",
        allowed_paths=["invoices/approved", "outputs"],
        blocked_paths=["invoices/private"],
        allowed_tools=["workspace"],
        allowed_operations=["list_directory", "read_text", "write_text"],
        allowed_network_destinations=[],
        requires_approval=[],
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _client(tmp_path: Path, capture_token: str | None = None) -> tuple[TestClient, Path]:
    db_file = tmp_path / "caller_provenance.db"
    init_db(db_file)
    workspace = tmp_path / "workspace"
    (workspace / "invoices" / "approved").mkdir(parents=True)
    (workspace / "invoices" / "approved" / "vendor-a.txt").write_text(
        "Vendor A: $1,250.00\n", encoding="utf-8"
    )
    (workspace / "outputs").mkdir(parents=True)
    app = create_app(
        db_path=db_file, workspace_root=workspace, capture_token=capture_token
    )
    return TestClient(app), workspace


def _create_run(client: TestClient) -> str:
    resp = client.post("/api/v1/runs", json={"name": "caller-provenance", "task_scope": _scope().model_dump()})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _submit(
    client: TestClient,
    run_id: str,
    *,
    capture_token: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": "workspace",
        "operation": "list_directory",
        "resource": "invoices/approved",
        "arguments": {"path": "invoices/approved"},
    }
    payload.update(overrides)
    headers = {CAPTURE_TOKEN_HEADER: capture_token} if capture_token else {}
    resp = client.post(f"/api/v1/runs/{run_id}/actions", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture(autouse=True)
def _audit_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep provenance tests offline: the auditor would spend model calls."""
    monkeypatch.setenv(AUDIT_OFF_ENV, "off")


def _requested_events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    return [e for e in events if e["event_type"] == EventType.ACTION_REQUESTED.value]


# ---------------------------------------------------------------------------
# Trust rules
# ---------------------------------------------------------------------------


def test_capture_credential_fails_closed_when_not_configured() -> None:
    assert capture_credential_verified(None, TEST_CAPTURE_TOKEN) is False
    assert capture_credential_verified("", TEST_CAPTURE_TOKEN) is False
    assert capture_credential_verified("   ", TEST_CAPTURE_TOKEN) is False
    assert capture_credential_verified(TEST_CAPTURE_TOKEN, None) is False
    assert capture_credential_verified(TEST_CAPTURE_TOKEN, "wrong-token") is False
    assert capture_credential_verified(TEST_CAPTURE_TOKEN, TEST_CAPTURE_TOKEN) is True


def test_provenance_for_submission_only_downgrades_origin_claims() -> None:
    assert (
        provenance_for_submission(ReasoningProvenance.PROVIDER_EXPOSED_TRACE, True)
        == ReasoningProvenance.PROVIDER_EXPOSED_TRACE
    )
    assert (
        provenance_for_submission(ReasoningProvenance.PROVIDER_EXPOSED_TRACE, False)
        == ReasoningProvenance.CALLER_ASSERTED_PROVIDER_TRACE
    )
    assert (
        provenance_for_submission(ReasoningProvenance.AGENT_AUTHORED_SUMMARY, False)
        == ReasoningProvenance.CALLER_ASSERTED_SUMMARY
    )
    # Self-limiting claims never promise a provider origin, so they stand.
    assert (
        provenance_for_submission(ReasoningProvenance.SYNTHETIC_FIXTURE, False)
        == ReasoningProvenance.SYNTHETIC_FIXTURE
    )
    assert (
        provenance_for_submission(ReasoningProvenance.UNAVAILABLE, False)
        == ReasoningProvenance.UNAVAILABLE
    )


# ---------------------------------------------------------------------------
# #116: a direct POST cannot acquire the verified provider-trace label
# ---------------------------------------------------------------------------


def test_direct_post_cannot_acquire_verified_provider_trace_label(tmp_path: Path) -> None:
    """Reproduction from #116: inventing text plus a JSON label must not verify.

    Deterministic policy is untouched: the action is still evaluated and still
    allowed on its own merits. Only the provenance label changes.
    """
    client, _ = _client(tmp_path)
    run_id = _create_run(client)

    body = _submit(
        client,
        run_id,
        exposed_reasoning_trace="caller invented this text",
        reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
        requested_by="invented model",
    )

    action = body["action_request"]
    assert action["reasoning_provenance"] == ReasoningProvenance.CALLER_ASSERTED_PROVIDER_TRACE.value
    assert action["caller_claimed_provenance"] == ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value
    # The text is preserved for review; only its asserted origin is downgraded.
    assert action["exposed_reasoning_trace"] == "caller invented this text"
    assert body["policy_decision"]["outcome"] == "ALLOW"

    events = _requested_events(client, run_id)
    assert len(events) == 1
    details = events[0]["details"]
    assert details["reasoning_provenance"] == ReasoningProvenance.CALLER_ASSERTED_PROVIDER_TRACE.value
    assert details["caller_claimed_provenance"] == ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value
    assert details["reasoning_provenance_verified"] is False

    # The stored record survives a re-read: the downgrade is not cosmetic.
    fetched = client.get(f"/api/v1/runs/{run_id}/actions/{action['id']}").json()["action_request"]
    assert fetched["reasoning_provenance"] == ReasoningProvenance.CALLER_ASSERTED_PROVIDER_TRACE.value
    assert fetched["caller_claimed_provenance"] == ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value


def test_summary_only_caller_claim_is_also_downgraded(tmp_path: Path) -> None:
    """The #116 comment case: a summary claim with no trace text at all."""
    client, _ = _client(tmp_path)
    run_id = _create_run(client)

    body = _submit(
        client,
        run_id,
        reasoning_summary="Synthetic summary",
        reasoning_provenance=ReasoningProvenance.AGENT_AUTHORED_SUMMARY.value,
    )

    action = body["action_request"]
    assert action["reasoning_provenance"] == ReasoningProvenance.CALLER_ASSERTED_SUMMARY.value
    assert action["caller_claimed_provenance"] == ReasoningProvenance.AGENT_AUTHORED_SUMMARY.value
    assert action["exposed_reasoning_trace"] is None


def test_trace_without_explicit_label_is_inferred_then_downgraded(tmp_path: Path) -> None:
    """A caller that omits the label still cannot reach the verified label."""
    client, _ = _client(tmp_path)
    run_id = _create_run(client)

    body = _submit(client, run_id, exposed_reasoning_trace="unlabelled caller text")

    action = body["action_request"]
    assert action["reasoning_provenance"] == ReasoningProvenance.CALLER_ASSERTED_PROVIDER_TRACE.value
    assert action["caller_claimed_provenance"] == ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value


def test_caller_cannot_relabel_existing_evidence_through_the_api(tmp_path: Path) -> None:
    """No JSON field can promote reasoning that a caller merely supplied."""
    client, _ = _client(tmp_path)
    run_id = _create_run(client)

    body = _submit(
        client,
        run_id,
        reasoning_summary="just a summary",
        reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
    )

    action = body["action_request"]
    assert action["reasoning_provenance"] == ReasoningProvenance.CALLER_ASSERTED_PROVIDER_TRACE.value
    assert action["exposed_reasoning_trace"] is None
    # A trace label on summary-only text is visible, not silently accepted.
    assert action["caller_claimed_provenance"] == ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value


def test_no_reasoning_supplied_stays_unavailable_without_diagnostics_noise(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    run_id = _create_run(client)

    body = _submit(client, run_id)

    action = body["action_request"]
    assert action["reasoning_provenance"] == ReasoningProvenance.UNAVAILABLE.value
    assert action["caller_claimed_provenance"] is None
    details = _requested_events(client, run_id)[0]["details"]
    assert "caller_claimed_provenance" not in details
    assert details["reasoning_provenance_verified"] is False


# ---------------------------------------------------------------------------
# #116: the trusted in-process capture path still verifies
# ---------------------------------------------------------------------------


def test_authenticated_capture_keeps_provider_trace_label(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, capture_token=TEST_CAPTURE_TOKEN)
    run_id = _create_run(client)

    body = _submit(
        client,
        run_id,
        exposed_reasoning_trace="Step 1: list invoices. Step 2: read vendor totals.",
        reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
        capture_token=TEST_CAPTURE_TOKEN,
    )

    action = body["action_request"]
    assert action["reasoning_provenance"] == ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value
    assert action["caller_claimed_provenance"] is None
    details = _requested_events(client, run_id)[0]["details"]
    assert details["reasoning_provenance_verified"] is True
    assert "caller_claimed_provenance" not in details


def test_authenticated_capture_keeps_summary_label(tmp_path: Path) -> None:
    """#120: summary provenance survives an authenticated submission too."""
    client, _ = _client(tmp_path, capture_token=TEST_CAPTURE_TOKEN)
    run_id = _create_run(client)

    body = _submit(
        client,
        run_id,
        reasoning_summary="Summary: list the approved invoices first.",
        reasoning_provenance=ReasoningProvenance.AGENT_AUTHORED_SUMMARY.value,
        capture_token=TEST_CAPTURE_TOKEN,
    )

    action = body["action_request"]
    assert action["reasoning_provenance"] == ReasoningProvenance.AGENT_AUTHORED_SUMMARY.value
    assert action["caller_claimed_provenance"] is None


def test_wrong_capture_credential_does_not_verify(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, capture_token=TEST_CAPTURE_TOKEN)
    run_id = _create_run(client)

    body = _submit(
        client,
        run_id,
        exposed_reasoning_trace="text with a bad credential",
        reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
        capture_token="not-the-right-token",
    )

    action = body["action_request"]
    assert action["reasoning_provenance"] == ReasoningProvenance.CALLER_ASSERTED_PROVIDER_TRACE.value
    assert action["caller_claimed_provenance"] == ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value


def test_gateway_without_credential_authenticates_nothing(tmp_path: Path) -> None:
    """Failing closed: with no credential configured, no submission verifies."""
    client, _ = _client(tmp_path, capture_token=None)
    run_id = _create_run(client)

    body = _submit(
        client,
        run_id,
        exposed_reasoning_trace="text from a caller holding an offer",
        reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
        capture_token=TEST_CAPTURE_TOKEN,
    )

    assert (
        body["action_request"]["reasoning_provenance"]
        == ReasoningProvenance.CALLER_ASSERTED_PROVIDER_TRACE.value
    )


def test_service_reads_capture_token_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway takes its credential from the operator environment."""
    monkeypatch.setenv(CAPTURE_TOKEN_ENV_VAR, TEST_CAPTURE_TOKEN)
    db_file = tmp_path / "env.db"
    init_db(db_file)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    service = ScopewatchService(db_path=db_file, workspace_root=workspace)
    assert service.capture_token == TEST_CAPTURE_TOKEN


def test_dispatcher_sends_capture_header_only_when_configured(tmp_path: Path) -> None:
    """The agent dispatcher authenticates its in-process capture, and only then."""
    from scopewatch.agent.tools import GatewayDispatcher

    client, _ = _client(tmp_path, capture_token=TEST_CAPTURE_TOKEN)
    from scopewatch.schemas import SubmitActionRequest

    request = SubmitActionRequest(
        tool="workspace",
        operation="list_directory",
        resource="invoices/approved",
        arguments={"path": "invoices/approved"},
        exposed_reasoning_trace="Trace captured in-process.",
        reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE,
    )

    run_id = _create_run(client)
    authenticated = GatewayDispatcher(
        base_url="http://testserver", http_client=client, capture_token=TEST_CAPTURE_TOKEN
    )
    response = authenticated.submit_action(run_id, request)
    assert response.action_request.reasoning_provenance == ReasoningProvenance.PROVIDER_EXPOSED_TRACE

    run_id_2 = _create_run(client)
    unauthenticated = GatewayDispatcher(
        base_url="http://testserver", http_client=client, capture_token=None
    )
    response_2 = unauthenticated.submit_action(run_id_2, request)
    assert (
        response_2.action_request.reasoning_provenance
        == ReasoningProvenance.CALLER_ASSERTED_PROVIDER_TRACE
    )


# ---------------------------------------------------------------------------
# Invariants that must survive the provenance change
# ---------------------------------------------------------------------------


def test_provenance_downgrade_does_not_relax_deterministic_policy(tmp_path: Path) -> None:
    """A caller-asserted trace changes evidence labels only, never a DENY."""
    client, _ = _client(tmp_path)
    run_id = _create_run(client)

    body = _submit(
        client,
        run_id,
        operation="read_text",
        resource="invoices/private/executive-salaries.txt",
        arguments={"path": "invoices/private/executive-salaries.txt"},
        exposed_reasoning_trace="caller invented this text",
        reasoning_provenance=ReasoningProvenance.PROVIDER_EXPOSED_TRACE.value,
    )

    assert body["policy_decision"]["outcome"] == "DENY"
    assert body["execution_receipt"]["status"] == "NOT_EXECUTED"
    assert (
        body["action_request"]["reasoning_provenance"]
        == ReasoningProvenance.CALLER_ASSERTED_PROVIDER_TRACE.value
    )


def test_provenance_is_not_decoded_into_decision_outcomes(tmp_path: Path) -> None:
    """Garbage claim values are rejected by schema validation, not coerced."""
    client, _ = _client(tmp_path)
    run_id = _create_run(client)

    resp = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "list_directory",
            "resource": "invoices/approved",
            "arguments": {},
            "reasoning_provenance": "TOTALLY_MADE_UP",
        },
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "SCHEMA_VALIDATION_ERROR"
    assert "reasoning_provenance" in error["message"]