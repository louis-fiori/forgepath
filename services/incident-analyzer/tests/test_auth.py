"""Tests for the s2s auth gate: gated endpoints require the bearer token when
configured; health, metrics and index stay open for probes and scraping."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models import AnalyzeResult

_TOKEN = "s3cr3t-s2s-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}

# (method, path, json body) for every gated endpoint.
_GATED = [
    ("GET", "/analyze", None),
    ("POST", "/analyze", {"namespace": "incident-generator"}),
    ("POST", "/analyze-log", {"namespace": "incident-generator", "log_line": "boom"}),
    ("GET", "/settings", None),
    ("POST", "/settings", {"issues_enabled": False}),
]
_OPEN = ["/healthz", "/readyz", "/metrics", "/"]


def _client(monkeypatch, token: str) -> TestClient:
    monkeypatch.setattr(settings, "poll_enabled", False)
    monkeypatch.setattr(settings, "backstage_s2s_token", token)

    async def stub_cycle(http, cache, **kwargs):
        return AnalyzeResult(detected=True, note="cycle")

    async def stub_single(http, cache, **kwargs):
        return AnalyzeResult(detected=True, note="single")

    import app.api

    monkeypatch.setattr(app.api, "run_cycle", stub_cycle)
    monkeypatch.setattr(app.api, "run_single_log", stub_single)
    from app.main import app as fastapi_app

    return TestClient(fastapi_app)


def _call(client: TestClient, method: str, path: str, body, headers=None):
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, json=body, headers=headers)


@pytest.mark.parametrize("method,path,body", _GATED)
def test_gated_endpoint_rejects_missing_token(monkeypatch, method, path, body):
    with _client(monkeypatch, _TOKEN) as client:
        r = _call(client, method, path, body)
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("method,path,body", _GATED)
def test_gated_endpoint_rejects_wrong_token(monkeypatch, method, path, body):
    with _client(monkeypatch, _TOKEN) as client:
        r = _call(client, method, path, body, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


@pytest.mark.parametrize("method,path,body", _GATED)
def test_gated_endpoint_accepts_correct_token(monkeypatch, method, path, body):
    with _client(monkeypatch, _TOKEN) as client:
        r = _call(client, method, path, body, headers=_AUTH)
    assert r.status_code == 200


@pytest.mark.parametrize("method,path,body", _GATED)
def test_gated_endpoint_open_when_no_token_configured(monkeypatch, method, path, body):
    # No token configured: gate is a no-op, no header needed.
    with _client(monkeypatch, "") as client:
        r = _call(client, method, path, body)
    assert r.status_code == 200


@pytest.mark.parametrize("path", _OPEN)
def test_unauthenticated_endpoints_stay_open(monkeypatch, path):
    # Probes and Prometheus must reach these even with auth enforced.
    with _client(monkeypatch, _TOKEN) as client:
        r = client.get(path)
    assert r.status_code == 200


def test_index_reports_auth_state(monkeypatch):
    with _client(monkeypatch, _TOKEN) as client:
        assert client.get("/").json()["auth_enabled"] is True
    with _client(monkeypatch, "") as client:
        assert client.get("/").json()["auth_enabled"] is False
