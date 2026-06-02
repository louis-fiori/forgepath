"""HTTP routes: health/readiness, Prometheus metrics, and the on-demand
/analyze endpoint (both GET and POST)."""

from __future__ import annotations

import hmac
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from . import metrics
from .config import settings
from .models import AnalyzeResult
from .poller import run_cycle, run_single_log

router = APIRouter()


def _require_s2s_token(authorization: str | None = Header(default=None)) -> None:
    """Gate the costly / side-effecting endpoints behind the shared s2s bearer
    token (settings.auth_enabled): they trigger LLM calls ($) and open issues, and
    ClusterIP doesn't limit access within the cluster. Open when no token is set
    (local / poll-only dev); health, metrics and index stay ungated for probes."""
    expected = settings.backstage_s2s_token
    if not expected:
        return
    prefix = "Bearer "
    presented = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
    # Constant-time compare so a 401 doesn't leak the token byte-by-byte.
    if not (presented and hmac.compare_digest(presented, expected)):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid service token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# DNS-1123 label. The namespace is interpolated into loki.py's LogQL selector,
# so an unvalidated value could read other namespaces' logs. fullmatch (not $)
# avoids the trailing-newline bypass.
_NAMESPACE_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def _validated_namespace(ns: str) -> str:
    if not _NAMESPACE_RE.fullmatch(ns):
        raise HTTPException(
            status_code=400,
            detail=r"invalid namespace (must be a DNS-1123 label: ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$)",
        )
    return ns


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict:
    # With polling on, "ready" means the detection loop is alive: if it died
    # (see main._on_poller_done) report not-ready so the pod shows 0/1.
    if settings.poll_enabled and not getattr(request.app.state, "poller_alive", True):
        raise HTTPException(status_code=503, detail="background poll loop is not running")
    return {"status": "ready"}


@router.get("/metrics")
async def get_metrics() -> Response:
    return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")


@router.get("/")
async def index(request: Request) -> dict:
    return {
        "service": "incident-analyzer",
        "watching": settings.namespaces,
        "poll_enabled": settings.poll_enabled,
        "poll_interval_seconds": settings.poll_interval_seconds,
        "llm_provider": settings.llm_provider,
        "model": settings.effective_model,
        "llm": settings.llm_enabled,
        "masking_enabled": settings.masking_enabled,
        "github": settings.github_enabled,
        "backstage": settings.backstage_enabled,
        "auth_enabled": settings.auth_enabled,
        "issues_enabled": request.app.state.issues_enabled,
        "endpoints": [
            "GET /analyze?namespace=&window=&force=&open_issue=",
            "POST /analyze",
            "POST /analyze-log",
            "GET/POST /settings",
        ],
    }


@router.get("/settings", dependencies=[Depends(_require_s2s_token)])
async def get_settings(request: Request) -> dict:
    return {"issues_enabled": request.app.state.issues_enabled}


@router.post("/settings", dependencies=[Depends(_require_s2s_token)])
async def set_settings(request: Request) -> dict:
    """Flip the global issue-creation toggle at runtime (no redeploy).
    Body: {"issues_enabled": true|false}."""
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: S110 - best-effort: a missing/invalid JSON body falls back to query params / defaults
        pass
    # A non-dict JSON body must not 500 (`"key" in 42` raises).
    if isinstance(body, dict) and "issues_enabled" in body:
        request.app.state.issues_enabled = bool(body["issues_enabled"])
    return {"issues_enabled": request.app.state.issues_enabled}


async def _analyze(
    request: Request, namespace: str, window: str | None, force: bool, open_issue: bool | None
) -> AnalyzeResult:
    return await run_cycle(
        request.app.state.http,
        request.app.state.dedup,
        namespace=namespace,
        window=window,
        force=force,
        open_issue=open_issue,
        default_issues_enabled=request.app.state.issues_enabled,
        llm=request.app.state.llm,
    )


@router.get("/analyze", dependencies=[Depends(_require_s2s_token)])
async def analyze_get(
    request: Request,
    namespace: str | None = None,
    window: str | None = None,
    force: bool = False,
    open_issue: bool | None = None,
) -> AnalyzeResult:
    ns = _validated_namespace(namespace or (settings.namespaces[0] if settings.namespaces else "default"))
    return await _analyze(request, ns, window, force, open_issue)


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


async def _request_params(request: Request):
    """Parse the JSON body (if any); return a picker where body wins over query string."""
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: S110 - best-effort: a missing/invalid JSON body falls back to query params / defaults
        pass
    q = request.query_params

    def pick(key):
        if isinstance(body, dict) and key in body:
            return body[key]
        return q.get(key)

    return pick


@router.post("/analyze", dependencies=[Depends(_require_s2s_token)])
async def analyze_post(request: Request) -> AnalyzeResult:
    pick = await _request_params(request)
    ns = _validated_namespace(pick("namespace") or (settings.namespaces[0] if settings.namespaces else "default"))
    force_raw = pick("force")
    open_issue_raw = pick("open_issue")
    return await _analyze(
        request,
        ns,
        pick("window"),
        _as_bool(force_raw) if force_raw is not None else False,
        None if open_issue_raw is None else _as_bool(open_issue_raw),
    )


@router.post("/analyze-log", dependencies=[Depends(_require_s2s_token)])
async def analyze_log_post(request: Request) -> AnalyzeResult:
    """Analyze a single log line: pasted raw (`log_line`) or fetched from Loki
    by a line-contains filter (`query`). Exactly one of the two is required."""
    pick = await _request_params(request)
    log_line = pick("log_line") or None
    query = pick("query") or None
    if bool(log_line) == bool(query):
        raise HTTPException(status_code=400, detail="provide exactly one of log_line or query")
    ns = _validated_namespace(pick("namespace") or (settings.namespaces[0] if settings.namespaces else "default"))
    open_issue_raw = pick("open_issue")
    return await run_single_log(
        request.app.state.http,
        request.app.state.dedup,
        namespace=ns,
        log_line=log_line,
        query=query,
        window=pick("window"),
        component=pick("component") or None,
        open_issue=None if open_issue_raw is None else _as_bool(open_issue_raw),
        default_issues_enabled=request.app.state.issues_enabled,
        llm=request.app.state.llm,
    )
