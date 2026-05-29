"""Fetch a service's runbook markdown so Claude can cite it.

Reads the TechDocs index.md via the GitHub raw endpoint and caches it in-process
with a TTL, keeping the cached Anthropic system block byte-stable across polls."""

from __future__ import annotations

import time

import httpx

from .config import settings

# Standing services use namespace == catalog dir name.
_CACHE: dict[str, tuple[float, str]] = {}
_TTL = 600.0  # 10 min


def _raw_url(service: str) -> str:
    return (
        f"https://raw.githubusercontent.com/{settings.github_owner}/{settings.github_repo}/"
        f"{settings.github_branch}/platform/backstage/catalog/{service}/docs/index.md"
    )


async def fetch(client: httpx.AsyncClient, service: str) -> str:
    now = time.time()
    cached = _CACHE.get(service)
    if cached and now - cached[0] < _TTL:
        return cached[1]

    headers = {}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        r = await client.get(_raw_url(service), headers=headers, timeout=15.0)
        if r.status_code == 200:
            text = r.text
        else:
            text = f"(No runbook found for {service} at {_raw_url(service)}, status {r.status_code}.)"
    except httpx.HTTPError as e:
        text = f"(Runbook fetch failed for {service}: {e})"

    _CACHE[service] = (now, text)
    return text
