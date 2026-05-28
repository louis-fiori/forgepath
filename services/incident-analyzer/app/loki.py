"""Loki LogQL client. Loki runs in-cluster with auth disabled, so these are
plain GET requests against the query / query_range API."""

from __future__ import annotations

import json
import time
from collections import defaultdict

import httpx

from .config import settings
from .models import LogSample

# Severities that count as an error line. Shared by the count and sample queries
# so the two can't drift.
_SEVERITY = "error|fatal"


def _range_seconds(window: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        return int(window[:-1]) * units[window[-1]]
    except (ValueError, KeyError, IndexError):
        return 600


async def error_count(client: httpx.AsyncClient, namespace: str, window: str) -> int:
    """Total error/fatal lines in the window (instant query over count_over_time)."""
    q = (
        f'sum(count_over_time({{namespace="{namespace}"}} '
        f'| json | severity=~"{_SEVERITY}" [{window}]))'
    )
    r = await client.get(
        f"{settings.loki_url}/loki/api/v1/query",
        params={"query": q},
        timeout=15.0,
    )
    r.raise_for_status()
    result = r.json().get("data", {}).get("result", [])
    if not result:
        return 0
    # vector result: [{"metric": {...}, "value": [ts, "N"]}]
    try:
        return int(float(result[0]["value"][1]))
    except (KeyError, IndexError, ValueError):
        return 0


async def recent_error_samples(
    client: httpx.AsyncClient, namespace: str, window: str, limit: int = 200
) -> list[LogSample]:
    """Pull recent error lines and bucket them by error_type for the LLM context.

    Uses query_range to get actual lines, then groups in Python (keeps the LogQL
    portable across Loki versions).
    """
    end = int(time.time() * 1e9)
    start = end - _range_seconds(window) * 1_000_000_000
    q = f'{{namespace="{namespace}"}} | json | severity=~"{_SEVERITY}"'
    r = await client.get(
        f"{settings.loki_url}/loki/api/v1/query_range",
        params={"query": q, "start": str(start), "end": str(end), "limit": str(limit), "direction": "backward"},
        timeout=20.0,
    )
    r.raise_for_status()
    streams = r.json().get("data", {}).get("result", [])

    buckets: dict[str, dict] = defaultdict(lambda: {"count": 0, "component": None, "samples": []})
    for stream in streams:
        for _ts, line in stream.get("values", []):
            etype, component, msg = parse_line(line)
            b = buckets[etype]
            b["count"] += 1
            if b["component"] is None and component:
                b["component"] = component
            if len(b["samples"]) < 4 and msg:
                b["samples"].append(msg)

    out = [
        LogSample(error_type=etype, count=b["count"], component=b["component"], samples=b["samples"])
        for etype, b in buckets.items()
    ]
    out.sort(key=lambda s: s.count, reverse=True)
    return out


async def fetch_one_line(
    client: httpx.AsyncClient, namespace: str, filter_text: str, window: str
) -> str | None:
    """Most recent error/fatal log line in `namespace` containing `filter_text`.

    Decision: this Loki-search path is gated by the same `| json |
    severity=~"error|fatal"` filter the poller uses (see `_SEVERITY`), so the
    `query` mode of /analyze-log can only surface *incident* lines. The endpoint
    is a search primitive — without the gate, a token-holding caller could fish
    arbitrary non-error log content (`query=password`, `query=AKIA`, …) out of
    namespaces they otherwise can't read and have it sent to the LLM + filed as a
    GitHub issue. Masking is best-effort, so we narrow the searchable surface to
    error lines rather than rely on redaction alone.

    Operators who genuinely need to analyze a non-error line still can: the
    `log_line` (paste) path takes arbitrary content verbatim — but there the
    caller already holds the line, so it opens no new read/exfiltration path.
    """
    end = int(time.time() * 1e9)
    start = end - _range_seconds(window) * 1_000_000_000
    # json.dumps produces a valid LogQL quoted string (handles quotes/backslashes).
    # `|=` (line-contains) before parsing, then the shared severity gate.
    q = f'{{namespace="{namespace}"}} |= {json.dumps(filter_text)} | json | severity=~"{_SEVERITY}"'
    r = await client.get(
        f"{settings.loki_url}/loki/api/v1/query_range",
        params={"query": q, "start": str(start), "end": str(end), "limit": "1", "direction": "backward"},
        timeout=15.0,
    )
    r.raise_for_status()
    streams = r.json().get("data", {}).get("result", [])
    entries = [(int(ts), line) for s in streams for ts, line in s.get("values", [])]
    if not entries:
        return None
    return max(entries)[1]


def parse_line(line: str) -> tuple[str, str | None, str | None]:
    """Best-effort extraction of (error_type, component, msg) from a JSON log line.
    `line` is the raw structured JSON the incident-generator emitted."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return ("unknown", None, line[:300] if isinstance(line, str) else None)
    etype = d.get("error_type") or d.get("level") or "unknown"
    return (str(etype), d.get("component"), d.get("msg"))
