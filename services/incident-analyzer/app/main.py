"""FastAPI app entrypoint. The lifespan wires up the shared httpx client, dedup
cache, and background poll task, and tears them down on shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx
from fastapi import FastAPI

from . import analyzer, poller
from .api import router
from .config import settings
from .detector import DedupCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("incident-analyzer")


def _on_poller_done(app: FastAPI, task: asyncio.Task) -> None:
    """If the poll loop ever exits (other than a clean shutdown cancel), flag the
    pod not-ready via /readyz and log loudly; otherwise detection would silently
    stop while /healthz stayed green."""
    app.state.poller_alive = False
    if task.cancelled():
        return  # normal shutdown
    exc = task.exception()
    if exc is not None:
        log.critical("background poll loop died, detection has stopped", exc_info=exc)
    else:
        log.error("background poll loop exited unexpectedly, detection has stopped")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient()
    app.state.dedup = DedupCache(settings.refile_cooldown_seconds)
    # Runtime-mutable issue-creation toggle (flip via POST /settings).
    app.state.issues_enabled = settings.issues_enabled
    # One Anthropic client for the whole process (the SDK pools httpx, so one per
    # analysis would leak a pool). None in detection-only mode.
    app.state.llm = analyzer.make_client() if settings.llm_enabled else None
    app.state.poller_alive = False
    task = None
    if settings.poll_enabled:
        app.state.poller_alive = True
        task = asyncio.create_task(poller.run_loop(app.state.http, app.state.dedup, app.state))
        task.add_done_callback(lambda t: _on_poller_done(app, t))
    log.info(
        "incident-analyzer up: provider=%s model=%s llm=%s github=%s backstage=%s poll=%s auth=%s",
        settings.llm_provider, settings.effective_model, settings.llm_enabled,
        settings.github_enabled, settings.backstage_enabled, settings.poll_enabled,
        settings.auth_enabled,
    )
    if not settings.poll_enabled:
        log.info("auto-detection disabled (POLL_ENABLED=false), analyze on demand via /analyze")
    if not settings.auth_enabled:
        log.warning(
            "no BACKSTAGE_S2S_TOKEN set, /analyze, /analyze-log and /settings are UNAUTHENTICATED; "
            "any in-cluster client can trigger LLM calls and open issues"
        )
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if app.state.llm is not None:
            await app.state.llm.close()
        await app.state.http.aclose()


app = FastAPI(title="incident-analyzer", lifespan=lifespan)
app.include_router(router)
