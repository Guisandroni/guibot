"""API tests for the FastAPI landing (requires `import bot` on first request to /api/*)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import landing_server


@pytest.fixture
def secret() -> str:
    return "pytest-landing-secret"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, secret: str) -> TestClient:
    monkeypatch.setenv("LANDING_API_SECRET", secret)
    return TestClient(landing_server.app)


def test_api_public_ok(client: TestClient) -> None:
    r = client.get("/api/public")
    assert r.status_code == 200
    data = r.json()
    assert "channel_slug" in data
    assert "commands_help" in data
    assert "chat_activity_enabled" in data
    assert "recent_winners" not in data


def test_api_config_unauthorized_no_header(client: TestClient) -> None:
    r = client.get("/api/config")
    assert r.status_code == 401


def test_api_config_forbidden_wrong_token(client: TestClient) -> None:
    r = client.get(
        "/api/config",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 403


def test_api_config_ok(client: TestClient, secret: str) -> None:
    r = client.get(
        "/api/config",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "channel_slugs" in data
    assert "chat_activity" in data
    assert "kick" in data
    assert "bot" in data
    assert "agent" in data


def test_post_sorteio_unauthorized(client: TestClient) -> None:
    r = client.post("/api/sorteio", json={})
    assert r.status_code == 401


def test_post_topchat_unauthorized(client: TestClient) -> None:
    r = client.post("/api/topchat", json={})
    assert r.status_code == 401


def test_post_clear_unauthorized(client: TestClient) -> None:
    r = client.post("/api/clear", json={})
    assert r.status_code == 401


def test_spa_deep_link_returns_html(client: TestClient) -> None:
    """Client-side routes must receive index.html, not FastAPI JSON 404."""
    if landing_server._static_root is None:
        pytest.skip("SPA dist not built (cd web && npm run build)")
    r = client.get("/docs")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "text/html" in ct.lower()


def test_bearer_routes_503_when_secret_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANDING_API_SECRET", raising=False)
    monkeypatch.setenv("LANDING_API_SECRET", "")
    c = TestClient(landing_server.app)
    r = c.get(
        "/api/config",
        headers={"Authorization": "Bearer anything"},
    )
    assert r.status_code == 503
    r2 = c.post("/api/sorteio", json={})
    assert r2.status_code == 503
