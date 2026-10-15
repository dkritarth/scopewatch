"""Tests for API authentication, reviewer credential separation, CORS, and security headers (issue #185)."""

import json
from pathlib import Path
from typing import Any
import pytest
from fastapi.testclient import TestClient

from scopewatch.app import create_app
from scopewatch.config import (
    CORS_ALLOW_CREDENTIALS,
    CORS_ALLOW_ORIGINS,
    DEMO_REVIEWER_ID,
    MAX_READ_BYTES,
    MAX_WRITE_BYTES,
)
from scopewatch.db import get_connection, init_db
from scopewatch.models import (
    ApprovalStatus,
    EventType,
    ExecutionStatus,
    PolicyOutcome,
    ReasonCode,
    ReasoningProvenance,
    RunStatus,
)
from scopewatch.repository import ScopewatchRepository
from scopewatch.schemas import ApprovalRequest, CreateRunRequest, TaskScope


@pytest.fixture
def make_gateway(tmp_path: Path):
    """Helper to instantiate test gateway with custom environment overrides."""
    def _create(env_vars: dict[str, str] | None = None) -> tuple[TestClient, Path, Path]:
        db_path = tmp_path / "test.db"
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "invoices").mkdir(parents=True, exist_ok=True)
        (workspace / "invoices" / "approved").mkdir(parents=True, exist_ok=True)
        (workspace / "invoices" / "approved" / "test.txt").write_text("sample content")

        app = create_app(db_path=db_path, workspace_root=workspace)
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        return client, db_path, workspace

    return _create


def _create_sample_run(client: TestClient, headers: dict[str, str] | None = None) -> str:
    payload = {
        "name": "Test Run",
        "task_scope": {
            "schema_version": "1",
            "task_description": "Audit invoices",
            "allowed_paths": ["invoices/approved", "outputs"],
            "blocked_paths": ["invoices/private"],
            "allowed_tools": ["workspace"],
            "allowed_operations": ["read_text", "write_text"],
            "requires_approval": ["delete_path"],
            "created_at": "2026-10-24T00:00:00Z",
        },
    }
    resp = client.post("/api/v1/runs", json=payload, headers=headers)
    assert resp.status_code == 201, f"Failed to create run: {resp.text}"
    return resp.json()["id"]


def _create_sample_approval(client: TestClient, run_id: str, headers: dict[str, str] | None = None) -> str:
    resp = client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={
            "tool": "workspace",
            "operation": "delete_path",
            "resource": "invoices/approved/test.txt",
            "arguments": {"path": "invoices/approved/test.txt"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, f"submit action failed: {resp.text}"
    appr = resp.json().get("approval_request")
    assert appr is not None, f"Expected approval request, got {resp.json()}"
    return appr["id"]


# -----------------------------------------------------------------------------
# 1. CORS & Origin Controls
# -----------------------------------------------------------------------------

def test_cors_default_no_wildcard_no_cross_origin(monkeypatch, make_gateway):
    """By default, CORS allows NO cross-origin access and does not reflect evil Origin."""
    monkeypatch.delenv("SCOPEWATCH_CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.delenv("SCOPEWATCH_CORS_ALLOW_CREDENTIALS", raising=False)
    client, db_path, _ = make_gateway()

    # Preflight from evil origin gets no access-control-allow-origin header
    preflight = client.options(
        "/api/v1/approvals",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers

    # Mutating request with cross-origin Origin is forbidden with 403
    resp = client.post(
        "/api/v1/runs",
        headers={"Origin": "https://evil.example"},
        json={"name": "Evil Run", "task_scope": {"task_description": "hack", "created_at": "2026-10-24T00:00:00Z"}},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "cross_origin_forbidden"


def test_sec_fetch_site_cross_site_blocked(make_gateway):
    """Sec-Fetch-Site: cross-site on state-changing calls is immediately rejected with 403."""
    client, _, _ = make_gateway()
    resp = client.post(
        "/api/v1/runs",
        headers={"Sec-Fetch-Site": "cross-site"},
        json={"name": "Cross-Site", "task_scope": {"task_description": "test", "created_at": "2026-10-24T00:00:00Z"}},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "cross_site_request_blocked"


def test_cors_explicit_allow_origins(monkeypatch, make_gateway):
    """Explicit SCOPEWATCH_CORS_ALLOW_ORIGINS allows configured origin."""
    monkeypatch.setattr("scopewatch.app.CORS_ALLOW_ORIGINS", ["https://dashboard.example.com"])
    client, db_path, _ = make_gateway()

    run_id = _create_sample_run(client, headers={"Origin": "https://dashboard.example.com"})
    assert run_id


# -----------------------------------------------------------------------------
# 2. Security Headers
# -----------------------------------------------------------------------------

def test_security_headers_local_plain_http(make_gateway):
    """Security headers are returned; CSP is strict without script unsafe-inline; no HSTS on local HTTP."""
    client, _, _ = make_gateway()
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200

    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=()" in resp.headers["Permissions-Policy"]

    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]

    # No HSTS on local plain HTTP
    assert "Strict-Transport-Security" not in resp.headers


def test_hsts_when_enabled_or_https(monkeypatch, make_gateway):
    """HSTS is emitted when explicitly enabled or under HTTPS."""
    monkeypatch.setattr("scopewatch.app.ENABLE_HSTS", True)
    client, _, _ = make_gateway()
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert "Strict-Transport-Security" in resp.headers
    assert "max-age=31536000" in resp.headers["Strict-Transport-Security"]


# -----------------------------------------------------------------------------
# 3. Content-Type Controls on Approvals
# -----------------------------------------------------------------------------

def test_content_type_text_plain_rejected_with_415(make_gateway):
    """A text/plain POST to approve or deny gets HTTP 415."""
    client, db_path, _ = make_gateway()
    run_id = _create_sample_run(client)
    appr_id = _create_sample_approval(client, run_id)

    # Approve with text/plain
    resp_approve = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"Content-Type": "text/plain"},
        content="{}",
    )
    assert resp_approve.status_code == 415
    assert resp_approve.json()["error"] == "unsupported_media_type"

    # Deny with text/plain
    resp_deny = client.post(
        f"/api/v1/approvals/{appr_id}/deny",
        headers={"Content-Type": "text/plain"},
        content="{}",
    )
    assert resp_deny.status_code == 415
    assert resp_deny.json()["error"] == "unsupported_media_type"


# -----------------------------------------------------------------------------
# 4. Local / Dev Mode (No Tokens Configured)
# -----------------------------------------------------------------------------

def test_local_dev_mode_works_without_keys(monkeypatch, make_gateway):
    """When no keys/tokens are configured, local dev mode allows runs and approvals with zero credentials."""
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    monkeypatch.delenv("SCOPEWATCH_API_KEY", raising=False)
    monkeypatch.delenv("SCOPEWATCH_REVIEWER_TOKEN", raising=False)
    monkeypatch.delenv("REVIEWER_TOKEN", raising=False)
    client, db_path, _ = make_gateway()

    run_id = _create_sample_run(client)
    appr_id = _create_sample_approval(client, run_id)

    # Resolve approval with no auth headers
    resp = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"Content-Type": "application/json"},
        json={"resolution_reason": "Local dev approved"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["approval_request"]["status"] == "CONSUMED"
    assert data["approval_request"]["resolved_by"] == DEMO_REVIEWER_ID


def test_local_dev_mode_custom_reviewer_id(monkeypatch, make_gateway):
    """In local dev, X-Reviewer-Id header is respected and recorded in resolved_by."""
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    monkeypatch.delenv("SCOPEWATCH_API_KEY", raising=False)
    monkeypatch.delenv("SCOPEWATCH_REVIEWER_TOKEN", raising=False)
    client, db_path, _ = make_gateway()

    run_id = _create_sample_run(client)
    appr_id = _create_sample_approval(client, run_id)

    resp = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"Content-Type": "application/json", "X-Reviewer-Id": "lead-judge-alice"},
        json={"resolution_reason": "Verified"},
    )
    assert resp.status_code == 200
    assert resp.json()["approval_request"]["resolved_by"] == "lead-judge-alice"


# -----------------------------------------------------------------------------
# 5. Production API Key Mode (Agent vs Reviewer Separation)
# -----------------------------------------------------------------------------

def test_api_key_mode_separates_agent_from_reviewer(monkeypatch, make_gateway):
    """In API-key mode, agent key authorizes mutating actions but CANNOT approve holds."""
    agent_key = "synthetic-agent-key-12345"
    reviewer_key = "synthetic-reviewer-key-67890"

    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    monkeypatch.setenv("SCOPEWATCH_API_KEY", agent_key)
    monkeypatch.setenv("SCOPEWATCH_REVIEWER_TOKEN", reviewer_key)
    monkeypatch.setattr("scopewatch.app.API_KEY", agent_key)

    client, db_path, _ = make_gateway()

    # 1. Mutating call without API key is refused with 401
    bad_run = client.post(
        "/api/v1/runs",
        json={"name": "Unauthed", "task_scope": {"task_description": "t", "created_at": "2026-10-24T00:00:00Z"}},
    )
    assert bad_run.status_code == 401
    assert bad_run.json()["error"] == "api_key_required"

    # 2. Mutating call with agent key succeeds
    run_id = _create_sample_run(client, headers={"X-API-Key": agent_key})
    appr_id = _create_sample_approval(client, run_id, headers={"X-API-Key": agent_key})

    # 3. Agent attempts to approve own hold with agent key -> 403 reviewer_credential_required
    agent_approve = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"X-API-Key": agent_key, "Content-Type": "application/json"},
        json={"resolution_reason": "I approve myself"},
    )
    assert agent_approve.status_code == 403
    assert agent_approve.json()["error"] == "reviewer_credential_required"

    # 4. Reviewer approves with reviewer key -> 200 OK
    rev_approve = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"X-Reviewer-Token": reviewer_key, "Content-Type": "application/json"},
        json={"resolution_reason": "Authorized human approval", "reviewer_id": "auditor-charlie"},
    )
    assert rev_approve.status_code == 200
    assert rev_approve.json()["approval_request"]["resolved_by"] == "auditor-charlie"


# -----------------------------------------------------------------------------
# 6. Demo Mode (Demo Token vs Reviewer Token Separation)
# -----------------------------------------------------------------------------

def test_demo_mode_agent_cannot_approve_own_holds(monkeypatch, make_gateway):
    """In demo mode (DEMO_TOKEN set), agent holding DEMO_TOKEN cannot approve holds."""
    demo_tok = "demo-agent-token"
    reviewer_tok = "demo-reviewer-token"

    monkeypatch.setenv("DEMO_TOKEN", demo_tok)
    monkeypatch.setenv("SCOPEWATCH_REVIEWER_TOKEN", reviewer_tok)

    client, db_path, _ = make_gateway()

    # Create run using demo token
    run_id = _create_sample_run(client, headers={"X-Demo-Token": demo_tok})
    appr_id = _create_sample_approval(client, run_id, headers={"X-Demo-Token": demo_tok})

    # Agent presents X-Demo-Token to approve endpoint -> 403 reviewer_credential_required
    agent_approve = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"X-Demo-Token": demo_tok, "Content-Type": "application/json"},
        json={"resolution_reason": "Agent self-approval attempt"},
    )
    assert agent_approve.status_code == 403
    assert agent_approve.json()["error"] == "reviewer_credential_required"

    # Approving without any token returns 401
    no_token_approve = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"Content-Type": "application/json"},
        json={"resolution_reason": "No token"},
    )
    assert no_token_approve.status_code == 401
    assert no_token_approve.json()["error"] in ("demo_token_required", "reviewer_credential_required")

    # Reviewer approves with X-Reviewer-Token -> 200 OK
    rev_approve = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"X-Reviewer-Token": reviewer_tok, "Content-Type": "application/json"},
        json={"resolution_reason": "Judge approved"},
    )
    assert rev_approve.status_code == 200
    data = rev_approve.json()
    assert data["approval_request"]["status"] == "CONSUMED"
    assert data["approval_request"]["resolved_by"].startswith("reviewer-")


# -----------------------------------------------------------------------------
# 7. Demo Reset Safety & Reasoning Audits Deletion
# -----------------------------------------------------------------------------

def test_demo_reset_disabled_by_default(make_gateway):
    """POST /api/v1/demo/reset is disabled by default (returns 403)."""
    client, _, _ = make_gateway()
    resp = client.post("/api/v1/demo/reset")
    assert resp.status_code == 403
    assert resp.json()["error"] == "reset_disabled"


def test_demo_reset_when_enabled_deletes_reasoning_audits(monkeypatch, make_gateway):
    """When enabled, demo reset clears all tables including reasoning_audits."""
    monkeypatch.setattr("scopewatch.app.ALLOW_DEMO_RESET", True)
    client, db_path, _ = make_gateway()

    run_id = _create_sample_run(client)

    # Insert a dummy reasoning audit
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO reasoning_audits (
                id, run_id, turn_id, trace_hash, verdict, concern_type,
                flagged_excerpts_json, explanation, model, profile, latency_ms,
                error_code, audited_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "audit-123", run_id, "turn-1", "hash123", "HOLD", "concern",
                "[]", "test explanation", "mock-model", "mock", 10.0, None, "2026-10-24T00:00:00Z"
            ),
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) c FROM reasoning_audits").fetchone()["c"]
        assert count == 1
    finally:
        conn.close()

    # Reset demo
    resp = client.post("/api/v1/demo/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Confirm reasoning_audits and runs are completely deleted
    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM reasoning_audits").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"] == 0
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# 8. Bounds Integrity (64 KiB read cap preserved from #237)
# -----------------------------------------------------------------------------

def test_preserved_64kib_bounds():
    """Verify that MAX_READ_BYTES and MAX_WRITE_BYTES are 64 KiB from main #237."""
    assert MAX_READ_BYTES == 64 * 1024
    assert MAX_WRITE_BYTES == 64 * 1024
