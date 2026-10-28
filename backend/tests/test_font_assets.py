"""Public bundled-font serving and static-directory containment."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scopewatch.app import create_app


FONT_NAMES = (
    "dm-sans.woff2",
    "instrument-serif.woff2",
    "instrument-serif-italic.woff2",
)
FONTS_DIR = Path(__file__).resolve().parents[2] / "frontend" / "fonts"


@pytest.fixture(params=["local", "api", "reviewer", "api-and-reviewer", "demo-and-reviewer"])
def font_client(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in (
        "SCOPEWATCH_API_KEY", "API_KEY", "SCOPEWATCH_REVIEWER_TOKEN",
        "REVIEWER_TOKEN", "SCOPEWATCH_REVIEWER_KEY", "DEMO_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    if "api" in request.param:
        monkeypatch.setenv("SCOPEWATCH_API_KEY", "synthetic-agent-key")
    if "reviewer" in request.param:
        monkeypatch.setenv("SCOPEWATCH_REVIEWER_TOKEN", "synthetic-reviewer-key")
    if "demo" in request.param:
        monkeypatch.setenv("DEMO_TOKEN", "synthetic-demo-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with TestClient(create_app(db_path=tmp_path / "fonts.db", workspace_root=workspace)) as client:
        yield client


@pytest.mark.parametrize("name", FONT_NAMES)
def test_bundled_fonts_are_public_read_only_assets(font_client: TestClient, name: str) -> None:
    response = font_client.get(f"/fonts/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "font/woff2"
    assert response.content == (FONTS_DIR / name).read_bytes()
    assert response.content.startswith(b"wOF2")
    head = font_client.head(f"/fonts/{name}")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(response.content))
    assert font_client.post(f"/fonts/{name}", content=b"replacement").status_code == 405
    assert font_client.get(f"/fonts/{name}").content == response.content


@pytest.mark.parametrize("path", [
    "/fonts/missing.woff2",
    "/fonts/",
    "/fonts/%2e%2e/index.html",
    "/fonts/..%2findex.html",
    "/fonts/%2e%2e%2fstyles/celestial.css",
    "/fonts/%2e%2e%2f%2e%2e%2fAGENTS.md",
])
def test_font_paths_cannot_escape_directory(font_client: TestClient, path: str) -> None:
    assert font_client.get(path).status_code == 404


def test_font_mount_does_not_bypass_api_auth(
    font_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Asset availability must not make protected mutations public.
    monkeypatch.setenv("SCOPEWATCH_API_KEY", "synthetic-agent-key")
    assert font_client.post("/api/v1/runs", json={}).status_code == 401
