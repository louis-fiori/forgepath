"""Send a Backstage in-app notification for the diagnosis.

Authenticates with the s2s bearer token (BACKSTAGE_S2S_TOKEN). Best-effort.
"""

from __future__ import annotations

import httpx

from .config import settings
from .models import Diagnosis, IncidentCandidate

# Map our 4-level severity to the notifications payload severity enum.
_SEV = {"low": "low", "medium": "normal", "high": "high", "critical": "critical"}


def _description(candidate: IncidentCandidate, diag: Diagnosis) -> str:
    """A few lines of context so the notification is actionable on its own; the
    linked report carries the full breakdown."""
    return (
        f"{diag.summary}\n\n"
        f"Root cause: {diag.probable_root_cause}\n"
        f"Remediation: {diag.recommended_remediation}\n\n"
        f"Confidence {diag.confidence:.0%} · signal {candidate.signal_class}"
        f" · {candidate.error_count} errors / {candidate.window}"
    )


def _report_link(candidate: IncidentCandidate, issue_url: str | None) -> str:
    """Prefer the GitHub issue (the full report); fall back to the namespace's
    Grafana error explorer so the notification is never a dead end."""
    if issue_url:
        return issue_url
    return f"{settings.grafana_url}/d/error-explorer?var-namespace={candidate.namespace}"


async def notify(
    client: httpx.AsyncClient,
    candidate: IncidentCandidate,
    diag: Diagnosis,
    issue_url: str | None,
) -> bool:
    if not settings.backstage_enabled:
        return False
    payload = {
        "recipients": {"type": "broadcast"},
        "payload": {
            "title": f"Incident: {candidate.namespace}, {diag.affected_component}",
            "description": _description(candidate, diag),
            "severity": _SEV.get(diag.severity, "normal"),
            "topic": "incident-analyzer",
            "link": _report_link(candidate, issue_url),
        },
    }
    headers = {"Authorization": f"Bearer {settings.backstage_s2s_token}"}
    r = await client.post(
        f"{settings.backstage_url}/api/notifications",
        json=payload,
        headers=headers,
        timeout=15.0,
    )
    r.raise_for_status()
    return True
