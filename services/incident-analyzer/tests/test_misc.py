"""Regression tests: LogQL window parsing, /settings non-dict-body guard,
/readyz reflecting a dead poller, and the single-log metric being described."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import settings
from app.loki import _range_seconds


def test_range_seconds_units():
    assert _range_seconds("30s") == 30
    assert _range_seconds("10m") == 600
    assert _range_seconds("2h") == 7200
    assert _range_seconds("1d") == 86400  # was silently falling back to 600
    assert _range_seconds("bogus") == 600  # unparseable → safe default


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "poll_enabled", False)
    monkeypatch.setattr(settings, "backstage_s2s_token", "")  # auth gate open
    # Keep startup from building a real LLM client regardless of the host's ~/.aws.
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    from app.main import app as fastapi_app

    return TestClient(fastapi_app)


def test_set_settings_non_dict_body_does_not_500(monkeypatch):
    # A bare-number JSON body must not 500, `"issues_enabled" in 42` would raise.
    with _client(monkeypatch) as client:
        r = client.post("/settings", json=42)
    assert r.status_code == 200
    assert "issues_enabled" in r.json()


def test_readyz_503_when_poller_dead(monkeypatch):
    with _client(monkeypatch) as client:
        # Simulate the background loop having died after a clean startup.
        monkeypatch.setattr(settings, "poll_enabled", True)
        client.app.state.poller_alive = False
        r = client.get("/readyz")
    assert r.status_code == 503


def test_readyz_ok_when_polling_disabled(monkeypatch):
    # Poll-only-off deployments don't have a loop to be unhealthy about.
    with _client(monkeypatch) as client:
        r = client.get("/readyz")
    assert r.status_code == 200


def test_single_log_metric_is_described():
    from app import metrics

    metrics.inc("incidentanalyzer_single_log_total")
    out = metrics.render()
    assert "# HELP incidentanalyzer_single_log_total" in out
    assert "# TYPE incidentanalyzer_single_log_total counter" in out


def test_diagnosis_rejects_out_of_range_and_freeform_values():
    # LLM structured output is validated, not trusted: a bad confidence (renders as
    # "500%") or free-form severity (arbitrary GitHub label) must fail closed.
    import pytest
    from pydantic import ValidationError

    from app.models import Diagnosis

    good = dict(
        summary="s", severity="high", affected_component="c",
        probable_root_cause="r", recommended_remediation="m", confidence=0.7,
    )
    Diagnosis(**good)  # baseline valid

    with pytest.raises(ValidationError):
        Diagnosis(**{**good, "confidence": 5.0})
    with pytest.raises(ValidationError):
        Diagnosis(**{**good, "confidence": -0.1})
    with pytest.raises(ValidationError):
        Diagnosis(**{**good, "severity": "catastrophic"})
