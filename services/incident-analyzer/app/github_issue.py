"""Open a GitHub issue with the diagnosis, using the platform's existing PAT.

Requires the PAT to have Issues: Read and write on the fork. Best-effort: a
failure returns None, never raised into the poller.
"""

from __future__ import annotations

import httpx

from .config import settings
from .models import Diagnosis, IncidentCandidate


def _body(candidate: IncidentCandidate, diag: Diagnosis) -> str:
    samples = "\n".join(
        f"- **{s.error_type}** (×{s.count})" + (f", {s.component}" if s.component else "")
        for s in candidate.log_samples[:8]
    )
    ns = candidate.namespace
    g = settings.grafana_url
    return f"""## AI Incident Analyzer

**Summary:** {diag.summary}

| Field | Value |
| --- | --- |
| Severity | `{diag.severity}` |
| Affected component | `{diag.affected_component}` |
| Confidence | `{diag.confidence:.2f}` |
| Namespace | `{ns}` |
| Detection signal | `{candidate.signal_class}` |
| Error lines ({candidate.window}) | `{candidate.error_count}` |

### Probable root cause
{diag.probable_root_cause}

### Recommended remediation
{diag.recommended_remediation}

### Error breakdown
{samples or "_n/a_"}

### Dashboards
- [Grafana, error explorer]({g}/d/error-explorer?var-namespace={ns})
- [Grafana, logs]({g}/d/service-logs?var-namespace={ns})
- [Grafana, pod metrics]({g}/d/cluster-pods?var-namespace={ns})

---
🤖 Filed automatically by the incident-analyzer.
"""


async def open_issue(
    client: httpx.AsyncClient, candidate: IncidentCandidate, diag: Diagnosis
) -> str | None:
    if not settings.github_enabled:
        return None
    url = f"https://api.github.com/repos/{settings.github_owner}/{settings.github_repo}/issues"
    payload = {
        "title": f"[Incident] {diag.affected_component}: {diag.summary}"[:240],
        "body": _body(candidate, diag),
        "labels": ["incident", "ai-analyzer", f"severity:{diag.severity}"],
    }
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    r = await client.post(url, json=payload, headers=headers, timeout=20.0)
    r.raise_for_status()
    return r.json().get("html_url")
