"""Prometheus instant-query client for corroborating metric context. No auth."""

from __future__ import annotations

import asyncio

import httpx

from .config import settings

# Metric -> PromQL. Optional LLM context; failures are swallowed so a missing
# metric never blocks analysis.
_QUERIES = {
    "panics_recovered": "sum(incidentgen_panics_recovered_total)",
    "leaked_bytes": "max(incidentgen_leaked_bytes)",
    "http_5xx_rate": 'sum(rate(incidentgen_http_requests_total{status=~"5.."}[5m]))',
}


async def _instant(client: httpx.AsyncClient, expr: str) -> float | None:
    try:
        r = await client.get(
            f"{settings.prometheus_url}/api/v1/query",
            params={"query": expr},
            timeout=10.0,
        )
        r.raise_for_status()
        result = r.json().get("data", {}).get("result", [])
        if not result:
            return None
        return float(result[0]["value"][1])
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None


async def snapshot(client: httpx.AsyncClient) -> dict[str, float]:
    # Fire the instant queries concurrently: they're independent, and serially the per-query
    # 10s timeouts could stack to ~30s and stall the poll cycle. _instant swallows its own
    # errors (returns None), so gather never raises.
    names = list(_QUERIES)
    values = await asyncio.gather(*(_instant(client, _QUERIES[n]) for n in names))
    return {n: v for n, v in zip(names, values, strict=True) if v is not None}
