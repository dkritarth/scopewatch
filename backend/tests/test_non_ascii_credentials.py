"""Non-ASCII credential headers must be rejected cleanly, never crash (500).

hmac.compare_digest raises TypeError on str operands containing non-ASCII
characters, and Starlette decodes header bytes as latin-1, so any caller can
send such a value. All fixtures are synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scopewatch.app import create_app
from scopewatch.config import validate_auth_configuration
from scopewatch.demo_guards import token_matches
from scopewatch.provenance import capture_credential_verified

NON_ASCII = "café"


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("DEMO_TOKEN", "synthetic-demo-token")
    monkeypatch.setenv("SCOPEWATCH_REVIEWER_TOKEN", "synthetic-reviewer-token")
    app = create_app(db_path=tmp_path / "t.db", workspace_root=tmp_path / "ws")
    return TestClient(app, raise_server_exceptions=False)


def test_token_matches_non_ascii_is_false_not_error() -> None:
    assert token_matches(NON_ASCII, "synthetic-demo-token") is False
    assert token_matches(NON_ASCII, NON_ASCII) is True


def test_capture_credential_non_ascii_is_false_not_error() -> None:
    assert capture_credential_verified("synthetic-capture", NON_ASCII) is False


def test_validate_auth_configuration_accepts_non_ascii_tokens() -> None:
    validate_auth_configuration(
        {"DEMO_TOKEN": NON_ASCII, "SCOPEWATCH_REVIEWER_TOKEN": "other"}
    )
    with pytest.raises(ValueError):
        validate_auth_configuration(
            {"DEMO_TOKEN": NON_ASCII, "SCOPEWATCH_REVIEWER_TOKEN": NON_ASCII}
        )


@pytest.mark.parametrize(
    "header", ["X-Demo-Token", "X-Reviewer-Token", "X-Reviewer-Key"]
)
def test_non_ascii_credential_header_gets_401_or_403(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, header: str
) -> None:
    client = _client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/v1/runs",
        json={"name": "x", "task_scope": "y"},
        headers={header: NON_ASCII.encode("latin-1")},
    )
    assert resp.status_code in (401, 403), resp.text


def test_non_ascii_reviewer_header_on_approval_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(monkeypatch, tmp_path)
    resp = client.post(
        "/api/v1/approvals/none/approve",
        json={},
        headers={
            "X-Demo-Token": "synthetic-demo-token",
            "X-Reviewer-Token": NON_ASCII.encode("latin-1"),
        },
    )
    assert resp.status_code in (401, 403), resp.text
