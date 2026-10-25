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
    get_allow_demo_reset,
    get_api_key,
    get_cors_allow_credentials,
    get_cors_allow_origins,
    get_enable_hsts,
    get_reviewer_identity,
    get_reviewer_token,
    validate_auth_configuration,
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
from scopewatch.schemas import ApprovalRequest, CreateRunRequest, TaskScope, ExecutionReceipt


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


# -----------------------------------------------------------------------------
# 9. Reviewer Findings Regressions (Issue #185 Repair)
# -----------------------------------------------------------------------------

def test_approval_missing_or_spoofed_content_type_rejected_with_415(make_gateway):
    """1. Approve and deny endpoints require valid application/json; reject missing or spoofed prefixes."""
    client, _, _ = make_gateway()
    run_id = _create_sample_run(client)
    appr_id = _create_sample_approval(client, run_id)

    # Missing Content-Type header on approve
    r_no_ct = client.post(f"/api/v1/approvals/{appr_id}/approve", content=b"")
    assert r_no_ct.status_code == 415
    assert r_no_ct.json() == {"error": "unsupported_media_type", "message": "Content-Type must be application/json"}

    # Missing Content-Type header on deny
    r_no_ct_deny = client.post(f"/api/v1/approvals/{appr_id}/deny", content=b"")
    assert r_no_ct_deny.status_code == 415

    # Spoofed startswith application/jsonjunk on approve
    r_spoof = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"Content-Type": "application/jsonjunk"},
        content=b"{}",
    )
    assert r_spoof.status_code == 415
    assert r_spoof.json() == {"error": "unsupported_media_type", "message": "Content-Type must be application/json"}

    # Spoofed startswith application/jsonjunk on deny
    r_spoof_deny = client.post(
        f"/api/v1/approvals/{appr_id}/deny",
        headers={"Content-Type": "application/jsonjunk"},
        content=b"{}",
    )
    assert r_spoof_deny.status_code == 415

    # Incorrect media type text/plain
    r_text = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"Content-Type": "text/plain"},
        content=b"{}",
    )
    assert r_text.status_code == 415

    # Valid application/json with parameters succeeds (local zero-key compatibility)
    r_valid = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"Content-Type": "application/json; charset=utf-8"},
        content=b"{}",
    )
    assert r_valid.status_code == 200
    assert r_valid.json()["approval_request"]["status"] == "CONSUMED"


def test_demo_reset_alias_protected_by_origin_and_auth(monkeypatch, make_gateway):
    """2. Both /demo/reset and /api/v1/demo/reset are consistently guarded by origin and auth."""
    client, _, _ = make_gateway()

    # Cross-origin request to /demo/reset blocked
    r_origin = client.post("/demo/reset", headers={"Origin": "https://attacker.com"})
    assert r_origin.status_code == 403
    assert r_origin.json()["error"] == "cross_origin_forbidden"

    # Sec-Fetch-Site cross-site to /demo/reset blocked
    r_sfs = client.post("/demo/reset", headers={"Sec-Fetch-Site": "cross-site"})
    assert r_sfs.status_code == 403
    assert r_sfs.json()["error"] == "cross_site_request_blocked"

    # When auth configured, unauthenticated /demo/reset blocked
    agent_k = "synthetic-agent-key-777"
    rev_k = "synthetic-reviewer-key-888"
    monkeypatch.setenv("SCOPEWATCH_API_KEY", agent_k)
    monkeypatch.setenv("SCOPEWATCH_REVIEWER_TOKEN", rev_k)
    monkeypatch.setattr("scopewatch.app.API_KEY", agent_k)
    monkeypatch.setattr("scopewatch.app.ALLOW_DEMO_RESET", True)

    client2, _, _ = make_gateway()
    r_unauth = client2.post("/demo/reset")
    assert r_unauth.status_code in (401, 403)

    # Agent key cannot reset demo
    r_agent = client2.post("/demo/reset", headers={"X-API-Key": agent_k, "X-Reviewer-Token": agent_k})
    assert r_agent.status_code == 403
    assert r_agent.json()["error"] == "reviewer_credential_required"

    # Legitimate reviewer token resets successfully
    r_rev = client2.post("/demo/reset", headers={"X-Reviewer-Token": rev_k})
    assert r_rev.status_code == 200
    assert r_rev.json()["status"] == "ok"


def test_equal_agent_and_reviewer_credentials_fail_closed_and_request_defense(monkeypatch, make_gateway):
    """3. Startup fails closed on duplicate credentials without leak; request defense rejects agent-as-reviewer."""
    secret = "shared-confidential-secret-999"

    # Startup validation rejects duplicate credentials
    with pytest.raises(ValueError) as exc1:
        validate_auth_configuration({"SCOPEWATCH_API_KEY": secret, "SCOPEWATCH_REVIEWER_TOKEN": secret})
    assert "Invalid authentication configuration" in str(exc1.value)
    assert secret not in str(exc1.value)  # Never echoes secret

    with pytest.raises(ValueError) as exc2:
        validate_auth_configuration({"API_KEY": secret, "REVIEWER_TOKEN": secret})
    assert secret not in str(exc2.value)

    with pytest.raises(ValueError) as exc3:
        validate_auth_configuration({"DEMO_TOKEN": secret, "SCOPEWATCH_REVIEWER_TOKEN": secret})
    assert secret not in str(exc3.value)

    # Request defense: even if configured distinctly, an agent presenting their credential via Authorization: Bearer
    # to self-approve is rejected
    agent_k = "agent-token-alpha"
    rev_k = "reviewer-token-beta"
    monkeypatch.setenv("SCOPEWATCH_API_KEY", agent_k)
    monkeypatch.setenv("SCOPEWATCH_REVIEWER_TOKEN", rev_k)
    monkeypatch.setattr("scopewatch.app.API_KEY", agent_k)

    client, _, _ = make_gateway()
    run_id = _create_sample_run(client, headers={"X-API-Key": agent_k})
    appr_id = _create_sample_approval(client, run_id, headers={"X-API-Key": agent_k})

    # Self-approval via Authorization: Bearer <agent_k> is rejected
    r_bearer_agent = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"Authorization": f"Bearer {agent_k}", "Content-Type": "application/json"},
        content=b"{}",
    )
    assert r_bearer_agent.status_code == 403
    assert r_bearer_agent.json()["error"] == "reviewer_credential_required"

    # Distinct reviewer credential via Authorization: Bearer <rev_k> succeeds
    r_bearer_rev = client.post(
        f"/api/v1/approvals/{appr_id}/approve",
        headers={"Authorization": f"Bearer {rev_k}", "Content-Type": "application/json"},
        content=b"{}",
    )
    assert r_bearer_rev.status_code == 200
    assert r_bearer_rev.json()["approval_request"]["status"] == "CONSUMED"


def test_canonical_env_resolvers_and_backwards_aliases():
    """4. Canonical names take precedence and legacy aliases fall back consistently."""
    # API key: SCOPEWATCH_API_KEY over API_KEY
    assert get_api_key({"SCOPEWATCH_API_KEY": "canon-k"}) == "canon-k"
    assert get_api_key({"API_KEY": "alias-k"}) == "alias-k"
    assert get_api_key({"SCOPEWATCH_API_KEY": "canon-k", "API_KEY": "alias-k"}) == "canon-k"

    # Reviewer token: SCOPEWATCH_REVIEWER_TOKEN over REVIEWER_TOKEN and SCOPEWATCH_REVIEWER_KEY
    assert get_reviewer_token({"SCOPEWATCH_REVIEWER_TOKEN": "canon-r"}) == "canon-r"
    assert get_reviewer_token({"REVIEWER_TOKEN": "alias-r"}) == "alias-r"
    assert get_reviewer_token({"SCOPEWATCH_REVIEWER_KEY": "key-r"}) == "key-r"
    assert get_reviewer_token({"SCOPEWATCH_REVIEWER_TOKEN": "canon-r", "REVIEWER_TOKEN": "alias-r"}) == "canon-r"

    # Reviewer identity: SCOPEWATCH_REVIEWER_ID over DEFAULT_REVIEWER_IDENTITY and REVIEWER_ID
    assert get_reviewer_identity({"SCOPEWATCH_REVIEWER_ID": "canon-id"}) == "canon-id"
    assert get_reviewer_identity({"DEFAULT_REVIEWER_IDENTITY": "alias-id"}) == "alias-id"
    assert get_reviewer_identity({"REVIEWER_ID": "id-r"}) == "id-r"
    assert get_reviewer_identity({"SCOPEWATCH_REVIEWER_ID": "canon-id", "DEFAULT_REVIEWER_IDENTITY": "alias-id"}) == "canon-id"

    # Demo reset: SCOPEWATCH_ALLOW_RESET over ALLOW_DEMO_RESET
    assert get_allow_demo_reset({"SCOPEWATCH_ALLOW_RESET": "true"}) is True
    assert get_allow_demo_reset({"ALLOW_DEMO_RESET": "1"}) is True
    assert get_allow_demo_reset({"SCOPEWATCH_ALLOW_RESET": "false", "ALLOW_DEMO_RESET": "true"}) is False

    # CORS origins: SCOPEWATCH_CORS_ALLOW_ORIGINS over CORS_ALLOW_ORIGINS
    assert get_cors_allow_origins({"SCOPEWATCH_CORS_ALLOW_ORIGINS": "https://a.com, https://b.com"}) == ["https://a.com", "https://b.com"]
    assert get_cors_allow_origins({"CORS_ALLOW_ORIGINS": "https://c.com"}) == ["https://c.com"]
    assert get_cors_allow_origins({"SCOPEWATCH_CORS_ALLOW_ORIGINS": "https://a.com", "CORS_ALLOW_ORIGINS": "https://c.com"}) == ["https://a.com"]

    # CORS credentials: SCOPEWATCH_CORS_ALLOW_CREDENTIALS over CORS_ALLOW_CREDENTIALS
    assert get_cors_allow_credentials({"SCOPEWATCH_CORS_ALLOW_CREDENTIALS": "true"}) is True
    assert get_cors_allow_credentials({"CORS_ALLOW_CREDENTIALS": "yes"}) is True
    assert get_cors_allow_credentials({"SCOPEWATCH_CORS_ALLOW_CREDENTIALS": "false", "CORS_ALLOW_CREDENTIALS": "true"}) is False


def test_whitespace_reviewer_id_falls_back_to_bound_identity(monkeypatch, make_gateway):
    """5. Whitespace or empty reviewer_id falls back to credential-bound identity, rejecting masquerade."""
    from scopewatch.demo_guards import token_fingerprint

    rev_k = "unique-reviewer-secret-key"
    monkeypatch.setenv("SCOPEWATCH_REVIEWER_TOKEN", rev_k)

    client, _, _ = make_gateway()
    run_id = _create_sample_run(client)

    # 1. Whitespace in body -> fallback to reviewer-<fingerprint>
    appr1 = _create_sample_approval(client, run_id)
    r1 = client.post(
        f"/api/v1/approvals/{appr1}/approve",
        headers={"X-Reviewer-Token": rev_k, "Content-Type": "application/json"},
        json={"reviewer_id": "    	   ", "resolution_reason": "Cleaned to empty"},
    )
    assert r1.status_code == 200
    expected_fingerprint = f"reviewer-{token_fingerprint(rev_k)}"
    assert r1.json()["approval_request"]["resolved_by"] == expected_fingerprint

    # 2. Whitespace in header -> fallback to reviewer-<fingerprint>
    appr2 = _create_sample_approval(client, run_id)
    r2 = client.post(
        f"/api/v1/approvals/{appr2}/approve",
        headers={"X-Reviewer-Token": rev_k, "X-Reviewer-Id": "   ", "Content-Type": "application/json"},
        content=b"{}",
    )
    assert r2.status_code == 200
    assert r2.json()["approval_request"]["resolved_by"] == expected_fingerprint

    # 3. Masquerade as 'agent' or 'agent-authored' -> rejected, falls back to bound identity
    appr3 = _create_sample_approval(client, run_id)
    r3 = client.post(
        f"/api/v1/approvals/{appr3}/approve",
        headers={"X-Reviewer-Token": rev_k, "Content-Type": "application/json"},
        json={"reviewer_id": "agent", "resolution_reason": "Masquerade attempt"},
    )
    assert r3.status_code == 200
    assert r3.json()["approval_request"]["resolved_by"] == expected_fingerprint


def test_reset_refused_while_execution_in_flight(monkeypatch, make_gateway):
    """6. Demo reset is refused with 409 while execution is in flight; state intact and subsequent reset succeeds."""
    monkeypatch.setattr("scopewatch.app.ALLOW_DEMO_RESET", True)
    client, db_path, _ = make_gateway()

    run_id = _create_sample_run(client)
    appr_id = _create_sample_approval(client, run_id)

    conn = get_connection(db_path)
    try:
        action_id = conn.execute("SELECT id FROM action_requests WHERE run_id = ?", (run_id,)).fetchone()["id"]
        ScopewatchRepository.append_event(
            conn,
            run_id=run_id,
            event_type=EventType.EXECUTION_STARTED,
            actor="executor",
            summary="Action execution started",
            timestamp="2026-10-24T00:00:01Z",
            details={},
            action_request_id=action_id,
            approval_request_id=appr_id,
        )
        conn.commit()
    finally:
        conn.close()

    # 1. Attempt reset while execution is in flight -> 409 Conflict
    r_reset1 = client.post("/api/v1/demo/reset")
    assert r_reset1.status_code == 409
    assert r_reset1.json()["error"] == "execution_in_flight"
    assert "Cannot reset demo while action execution is in flight" in r_reset1.json()["message"]

    # Also test /demo/reset alias returns 409
    r_reset_alias = client.post("/demo/reset")
    assert r_reset_alias.status_code == 409
    assert r_reset_alias.json()["error"] == "execution_in_flight"

    # Verify state is completely unchanged
    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM action_requests WHERE id = ?", (action_id,)).fetchone()["c"] == 1
        assert len(ScopewatchRepository.get_events(conn, run_id)) > 0
    finally:
        conn.close()

    # 2. Simulate execution completing successfully and writing valid receipt
    conn = get_connection(db_path)
    try:
        receipt = ExecutionReceipt(
            id="receipt-1",
            action_request_id=action_id,
            status=ExecutionStatus.EXECUTED,
            started_at="2026-10-24T00:00:01Z",
            completed_at="2026-10-24T00:00:02Z",
            executor="local",
            sanitized_result={"deleted": True},
            error_code=None,
            resource="test.txt",
            operation="delete_path",
        )
        ScopewatchRepository.create_execution_receipt(conn, receipt)
        ScopewatchRepository.append_event(
            conn,
            run_id=run_id,
            event_type=EventType.EXECUTION_SUCCEEDED,
            actor="executor",
            summary="Action execution completed",
            timestamp="2026-10-24T00:00:02Z",
            details={},
            action_request_id=action_id,
            approval_request_id=appr_id,
            execution_receipt_id="receipt-1",
        )
        conn.commit()
    finally:
        conn.close()

    # Verify receipt exists and is valid
    conn = get_connection(db_path)
    try:
        receipt_read = ScopewatchRepository.get_execution_receipt_by_action(conn, action_id)
        assert receipt_read is not None
        assert receipt_read.status == ExecutionStatus.EXECUTED
    finally:
        conn.close()

    # 3. Now that execution is no longer in flight, reset succeeds
    r_reset2 = client.post("/api/v1/demo/reset")
    assert r_reset2.status_code == 200
    assert r_reset2.json()["status"] == "ok"

    # Confirm tables cleared cleanly
    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM execution_receipts").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM evidence_events").fetchone()["c"] == 0
    finally:
        conn.close()
