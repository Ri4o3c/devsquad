"""Loopback /health tests: token, Host, and Origin."""

from __future__ import annotations

import uvicorn
from fastapi.testclient import TestClient

from devsquad.server import BIND_HOST, app, serve


def _client(base_url: str) -> TestClient:
    return TestClient(app, base_url=base_url)


def test_health_ok_with_loopback_host_and_token(monkeypatch) -> None:
    monkeypatch.setenv("DEVSQUAD_TOKEN", "test-token")
    response = _client("http://127.0.0.1").get(
        "/health",
        headers={"X-DevSquad-Token": "test-token"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "test-token" not in response.text


def test_health_accepts_localhost_and_a_local_origin(monkeypatch) -> None:
    monkeypatch.setenv("DEVSQUAD_TOKEN", "test-token")
    response = _client("http://localhost:8765").get(
        "/health",
        headers={
            "X-DevSquad-Token": "test-token",
            "Origin": "http://127.0.0.1:8765",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_or_wrong_token_is_unauthorized(monkeypatch) -> None:
    monkeypatch.setenv("DEVSQUAD_TOKEN", "test-token")
    client = _client("http://127.0.0.1")
    missing = client.get("/health")
    wrong = client.get("/health", headers={"X-DevSquad-Token": "other-token"})
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json()["detail"] == "unauthorized"
    assert "test-token" not in missing.text
    assert "test-token" not in wrong.text


def test_empty_configured_token_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("DEVSQUAD_TOKEN", "")
    response = _client("http://127.0.0.1").get(
        "/health",
        headers={"X-DevSquad-Token": "anything"},
    )
    assert response.status_code == 401


def test_unexpected_host_is_forbidden(monkeypatch) -> None:
    monkeypatch.setenv("DEVSQUAD_TOKEN", "test-token")
    headers = {"X-DevSquad-Token": "test-token"}
    for base in (
        "http://evil.example",
        "http://127.0.0.1.attacker.tld",
        "http://localhost.attacker.tld",
    ):
        response = _client(base).get("/health", headers=headers)
        assert response.status_code == 403, base
        assert response.json()["detail"] == "forbidden"


def test_unexpected_origin_is_forbidden_even_with_the_token(monkeypatch) -> None:
    monkeypatch.setenv("DEVSQUAD_TOKEN", "test-token")
    client = _client("http://127.0.0.1")
    headers = {"X-DevSquad-Token": "test-token"}
    for origin in (
        "https://evil.example",
        "http://127.0.0.1.attacker.tld",
        "http://evil@127.0.0.1",
        "http://127.0.0.1/callback",
        "null",
    ):
        response = client.get("/health", headers={**headers, "Origin": origin})
        assert response.status_code == 403, origin


def test_docs_are_not_a_public_page(monkeypatch) -> None:
    monkeypatch.setenv("DEVSQUAD_TOKEN", "test-token")
    response = _client("http://127.0.0.1").get("/docs")
    assert response.status_code == 401
    assert response.status_code != 200


def test_serve_binds_loopback_only(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(target, host, port, log_level="info"):
        captured["app"] = target
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert BIND_HOST == "127.0.0.1"
    serve(9137)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9137
    assert captured["app"] is app
