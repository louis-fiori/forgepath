"""Coverage for the surfacing + analysis paths: Claude tool-block parsing in
analyzer.analyze, backstage.notify, github_issue.open_issue, runbook.fetch's TTL
cache, and the poller's run_loop iteration."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app import backstage, github_issue, runbook
from app.analyzer import analyze
from app.config import settings
from app.detector import DedupCache, single_log_candidate
from app.models import AnalyzeResult, Diagnosis


def _diag(severity: str = "high", confidence: float = 0.8) -> Diagnosis:
    return Diagnosis(
        summary="db pool exhausted",
        severity=severity,
        affected_component="payments-api",
        probable_root_cause="connection leak",
        recommended_remediation="bump pool size per runbook",
        confidence=confidence,
    )


def _candidate():
    return single_log_candidate("payments", '{"error_type":"db-timeout","msg":"boom"}')


def _mock_client(handler, seen: list | None = None) -> httpx.AsyncClient:
    def _h(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(_h))


# --- analyzer.analyze: tool-block parsing + guards ---

class _Block:
    def __init__(self, type: str, name: str | None = None, input: dict | None = None):
        self.type = type
        self.name = name
        self.input = input


class _Resp:
    def __init__(self, content: list, stop_reason: str = "tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, resp: _Resp):
        self._resp = resp
        self.kwargs: dict | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self._resp


class _FakeClient:
    def __init__(self, resp: _Resp):
        self.messages = _FakeMessages(resp)


def test_analyze_parses_tool_block():
    payload = dict(
        summary="s", severity="high", affected_component="c",
        probable_root_cause="r", recommended_remediation="m", confidence=0.8,
    )
    client = _FakeClient(_Resp([_Block("text"), _Block("tool_use", "record_diagnosis", payload)]))
    diag = asyncio.run(analyze(_candidate(), "# runbook", client))
    assert isinstance(diag, Diagnosis)
    assert diag.severity == "high" and diag.confidence == 0.8
    # forced single tool call + cached runbook prefix
    assert client.messages.kwargs["tool_choice"] == {"type": "tool", "name": "record_diagnosis"}
    assert client.messages.kwargs["system"][1]["cache_control"] == {"type": "ephemeral"}


def test_analyze_raises_on_max_tokens_truncation():
    payload = dict(
        summary="s", severity="high", affected_component="c",
        probable_root_cause="r", recommended_remediation="m", confidence=0.8,
    )
    client = _FakeClient(_Resp([_Block("tool_use", "record_diagnosis", payload)], stop_reason="max_tokens"))
    with pytest.raises(RuntimeError, match="truncated"):
        asyncio.run(analyze(_candidate(), "rb", client))


def test_analyze_raises_when_no_tool_call():
    client = _FakeClient(_Resp([_Block("text")]))
    with pytest.raises(RuntimeError, match="did not return"):
        asyncio.run(analyze(_candidate(), "rb", client))


def test_analyze_rejects_out_of_range_confidence():
    # A model returning confidence=5.0 must fail validation, not surface "500%".
    bad = dict(
        summary="s", severity="high", affected_component="c",
        probable_root_cause="r", recommended_remediation="m", confidence=5.0,
    )
    client = _FakeClient(_Resp([_Block("tool_use", "record_diagnosis", bad)]))
    with pytest.raises(ValidationError):
        asyncio.run(analyze(_candidate(), "rb", client))


# --- backstage.notify ---

def test_backstage_notify_posts_payload(monkeypatch):
    monkeypatch.setattr(settings, "backstage_s2s_token", "s2s-tok")
    seen: list[httpx.Request] = []
    client = _mock_client(lambda r: httpx.Response(200, json={}), seen)
    ok = asyncio.run(backstage.notify(client, _candidate(), _diag(severity="medium"), "https://issue/1"))
    assert ok is True
    req = seen[0]
    assert req.url.path == "/api/notifications"
    assert req.headers["Authorization"] == "Bearer s2s-tok"
    body = json.loads(req.content)
    assert body["payload"]["severity"] == "normal"  # medium → normal mapping
    assert body["payload"]["link"] == "https://issue/1"


def test_backstage_notify_falls_back_to_grafana_link(monkeypatch):
    monkeypatch.setattr(settings, "backstage_s2s_token", "s2s-tok")
    seen: list[httpx.Request] = []
    client = _mock_client(lambda r: httpx.Response(200, json={}), seen)
    asyncio.run(backstage.notify(client, _candidate(), _diag(), None))
    body = json.loads(seen[0].content)
    assert "/d/error-explorer" in body["payload"]["link"]
    assert "var-namespace=payments" in body["payload"]["link"]


def test_backstage_notify_disabled_returns_false(monkeypatch):
    monkeypatch.setattr(settings, "backstage_s2s_token", "")
    assert asyncio.run(backstage.notify(None, _candidate(), _diag(), None)) is False


# --- github_issue.open_issue ---

def test_open_issue_posts_and_returns_url(monkeypatch):
    monkeypatch.setattr(settings, "github_token", "ghp_token")
    monkeypatch.setattr(settings, "github_owner", "o")
    monkeypatch.setattr(settings, "github_repo", "r")
    seen: list[httpx.Request] = []
    client = _mock_client(lambda r: httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/7"}), seen)
    url = asyncio.run(github_issue.open_issue(client, _candidate(), _diag()))
    assert url == "https://github.com/o/r/issues/7"
    req = seen[0]
    assert req.url.path == "/repos/o/r/issues"
    assert req.headers["Authorization"] == "Bearer ghp_token"
    body = json.loads(req.content)
    assert "severity:high" in body["labels"]
    assert "incident" in body["labels"]


def test_open_issue_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "github_token", "")
    assert asyncio.run(github_issue.open_issue(None, _candidate(), _diag())) is None


# --- runbook.fetch TTL cache ---

def test_runbook_fetch_caches_within_ttl():
    runbook._CACHE.clear()
    seen: list[httpx.Request] = []
    client = _mock_client(lambda r: httpx.Response(200, text="# Runbook body"), seen)
    first = asyncio.run(runbook.fetch(client, "svc-a"))
    second = asyncio.run(runbook.fetch(client, "svc-a"))
    assert first == second == "# Runbook body"
    assert len(seen) == 1, "second fetch within TTL must be served from cache"


def test_runbook_fetch_non_200_returns_placeholder():
    runbook._CACHE.clear()
    client = _mock_client(lambda r: httpx.Response(404, text="nope"))
    out = asyncio.run(runbook.fetch(client, "ghost"))
    assert "No runbook found" in out and "status 404" in out


# --- poller.run_loop ---

def test_run_loop_polls_each_namespace_then_sleeps(monkeypatch):
    import app.poller as poller

    monkeypatch.setattr(settings, "watch_namespaces", "a,b")

    seen: list[str] = []

    async def fake_cycle(http, cache, ns, **kwargs):
        seen.append(ns)
        return AnalyzeResult(detected=False, note="noop")

    async def fake_sleep(_seconds):
        raise asyncio.CancelledError  # break the infinite loop after one pass

    monkeypatch.setattr(poller, "run_cycle", fake_cycle)
    monkeypatch.setattr(poller.asyncio, "sleep", fake_sleep)

    class _State:
        issues_enabled = True
        llm = None

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(poller.run_loop(None, DedupCache(60), _State()))
    assert seen == ["a", "b"]  # every namespace polled before the sleep
