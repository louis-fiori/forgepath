"""Orchestration: one detect -> analyze -> dedup -> surface cycle, plus the
background loop that runs it per namespace on an interval."""

from __future__ import annotations

import asyncio
import logging

import httpx

from . import backstage, detector, github_issue, k8s, loki, metrics, prometheus, runbook
from .analyzer import analyze
from .config import settings
from .masking import redact, redact_candidate
from .models import AnalyzeResult

log = logging.getLogger("incident-analyzer")


async def _surface(
    http: httpx.AsyncClient,
    candidate,
    diagnosis,
    issues_enabled: bool,
) -> tuple[str | None, bool]:
    """Surface a diagnosis: GitHub issue first (for the URL), then Backstage
    notification. Both are best-effort and degrade independently."""
    issue_url = None
    if issues_enabled:
        try:
            issue_url = await github_issue.open_issue(http, candidate, diagnosis)
            if issue_url:
                metrics.inc("incidentanalyzer_issues_filed_total")
        except Exception as e:  # noqa: BLE001
            metrics.inc("incidentanalyzer_errors_total", 'stage="github"')
            log.warning("github issue failed: %s", e)
    else:
        log.info("issue creation disabled for this run (ns=%s)", candidate.namespace)

    notified = False
    try:
        notified = await backstage.notify(http, candidate, diagnosis, issue_url)
        if notified:
            metrics.inc("incidentanalyzer_notifications_sent_total")
    except Exception as e:  # noqa: BLE001
        metrics.inc("incidentanalyzer_errors_total", 'stage="backstage"')
        log.warning("backstage notification failed: %s", e)

    return issue_url, notified


async def run_cycle(
    http: httpx.AsyncClient,
    cache: detector.DedupCache,
    namespace: str,
    window: str | None = None,
    force: bool = False,
    open_issue: bool | None = None,
    default_issues_enabled: bool = True,
    llm=None,
) -> AnalyzeResult:
    """Run a single detection+analysis cycle for one namespace.

    `open_issue` overrides issue creation for this call (None uses the global
    default `default_issues_enabled`)."""
    issues_enabled = default_issues_enabled if open_issue is None else open_issue
    metrics.inc("incidentanalyzer_polls_total")
    window = window or settings.detect_window

    # 1. Gather signals concurrently.
    try:
        count, samples, pods, metric_snapshot = await asyncio.gather(
            loki.error_count(http, namespace, window),
            loki.recent_error_samples(http, namespace, window),
            k8s.pod_signals(namespace),
            prometheus.snapshot(http),
        )
    except Exception as e:  # noqa: BLE001, a backend hiccup shouldn't kill the loop
        metrics.inc("incidentanalyzer_errors_total", 'stage="gather"')
        log.warning("signal gather failed for %s: %s", namespace, e)
        return AnalyzeResult(detected=False, note=f"gather failed: {e}")

    # 2. Decide.
    candidate = detector.decide(namespace, count, samples, pods, metric_snapshot, window)
    if candidate is None:
        return AnalyzeResult(detected=False, note=f"no incident (errors={count})")

    # Scrub PII before the candidate leaves the service (returned verbatim and
    # surfaced to GitHub / Backstage).
    if settings.masking_enabled:
        redact_candidate(candidate)

    metrics.inc("incidentanalyzer_incidents_detected_total")
    fp = candidate.fingerprint()

    # Steps 3-5 run under a per-fingerprint lock so concurrent cycles for the
    # same incident (the poller racing an on-demand /analyze, or two /analyze
    # calls) can't both pass the dedup check and file duplicate issues. The
    # should_escalate→analyze(await)→record window was a TOCTOU; the lock makes
    # the check-analyze-record sequence atomic per fingerprint while different
    # fingerprints still run concurrently.
    async with cache.lock_for(fp):
        # 3. Dedup.
        if not cache.should_escalate(fp, force=force):
            metrics.inc("incidentanalyzer_incidents_deduped_total")
            return AnalyzeResult(
                detected=True, deduped=True, fingerprint=fp, candidate=candidate,
                issue_url=cache.issue_url(fp), note="suppressed by dedup cache",
            )

        # 4. Analyze with Claude.
        if not settings.llm_enabled:
            return AnalyzeResult(detected=True, fingerprint=fp, candidate=candidate,
                                 note="ANTHROPIC_API_KEY not set, detection only")
        try:
            rb = await runbook.fetch(http, namespace)
            diagnosis = await analyze(candidate, rb, llm)
            metrics.inc("incidentanalyzer_claude_calls_total")
        except Exception as e:  # noqa: BLE001
            # No record() on failure, so a later cycle is free to retry (the lock
            # is released here, not held for the cooldown).
            metrics.inc("incidentanalyzer_errors_total", 'stage="analyze"')
            log.exception("analysis failed for %s", namespace)
            return AnalyzeResult(detected=True, fingerprint=fp, candidate=candidate,
                                 note=f"analysis failed: {e}")

        # 5. Surface: GitHub issue first (for the URL), then Backstage notification.
        issue_url, notified = await _surface(http, candidate, diagnosis, issues_enabled)

        cache.record(fp, issue_url)
        log.info("incident filed: ns=%s fp=%s severity=%s issue=%s",
                 namespace, fp, diagnosis.severity, issue_url)
        return AnalyzeResult(
            detected=True, fingerprint=fp, candidate=candidate, diagnosis=diagnosis,
            issue_url=issue_url, notified=notified,
        )


async def run_single_log(
    http: httpx.AsyncClient,
    cache: detector.DedupCache,
    namespace: str,
    log_line: str | None = None,
    query: str | None = None,
    window: str | None = None,
    component: str | None = None,
    open_issue: bool | None = None,
    default_issues_enabled: bool = True,
    llm=None,
) -> AnalyzeResult:
    """Analyze one log line, pasted raw (`log_line`) or fetched from Loki by a
    line-contains filter (`query`, newest match wins). Same flow as run_cycle,
    minus detection."""
    issues_enabled = default_issues_enabled if open_issue is None else open_issue
    metrics.inc("incidentanalyzer_single_log_total")

    # 1. Resolve the line.
    if log_line is not None:
        line, window = log_line, "manual"
    else:
        window = window or settings.detect_window
        try:
            line = await loki.fetch_one_line(http, namespace, query, window)
        except Exception as e:  # noqa: BLE001
            metrics.inc("incidentanalyzer_errors_total", 'stage="gather"')
            log.warning("loki fetch failed for %s: %s", namespace, e)
            return AnalyzeResult(detected=False, note=f"loki fetch failed: {e}")
        if line is None:
            return AnalyzeResult(detected=False, note=f"no log line matched {query!r} in {window}")

    # Scrub before the line enters the candidate (returned verbatim, not just
    # sent to the LLM).
    if settings.masking_enabled:
        line = redact(line)

    candidate = detector.single_log_candidate(namespace, line, component=component, window=window)
    fp = candidate.fingerprint()
    # 2. No dedup check: a user-initiated analysis must never be suppressed.

    # 3. Analyze with Claude.
    if not settings.llm_enabled:
        return AnalyzeResult(detected=True, fingerprint=fp, candidate=candidate,
                             note="ANTHROPIC_API_KEY not set, detection only")
    try:
        rb = await runbook.fetch(http, namespace)
        diagnosis = await analyze(candidate, rb, llm)
        metrics.inc("incidentanalyzer_claude_calls_total")
    except Exception as e:  # noqa: BLE001
        metrics.inc("incidentanalyzer_errors_total", 'stage="analyze"')
        log.exception("single-log analysis failed for %s", namespace)
        return AnalyzeResult(detected=True, fingerprint=fp, candidate=candidate,
                             note=f"analysis failed: {e}")

    # 4. Surface like a regular incident.
    issue_url, notified = await _surface(http, candidate, diagnosis, issues_enabled)

    cache.record(fp, issue_url)
    log.info("single-log analyzed: ns=%s fp=%s severity=%s issue=%s",
             namespace, fp, diagnosis.severity, issue_url)
    return AnalyzeResult(
        detected=True, fingerprint=fp, candidate=candidate, diagnosis=diagnosis,
        issue_url=issue_url, notified=notified,
    )


async def run_loop(http: httpx.AsyncClient, cache: detector.DedupCache, state) -> None:
    """Background loop: poll every namespace on the configured interval.
    `state` is the app's mutable state holder (carries the runtime issues toggle)."""
    log.info("poller started: namespaces=%s interval=%ss",
             settings.namespaces, settings.poll_interval_seconds)
    while True:
        for ns in settings.namespaces:
            try:
                result = await run_cycle(http, cache, ns,
                                         default_issues_enabled=state.issues_enabled,
                                         llm=state.llm)
                if result.detected and not result.deduped:
                    log.info("cycle %s → %s", ns, result.note or "incident handled")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("cycle crashed for %s", ns)
        await asyncio.sleep(settings.poll_interval_seconds)
