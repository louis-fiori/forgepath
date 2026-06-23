"""Incident detection + in-memory dedup.

Detection is any-of: log-volume threshold, CrashLoop/OOM pod condition, or a
recovered-panic spike. The fingerprint (error_type + signal class) ignores
counts/timestamps so an ongoing incident isn't re-filed every poll.
"""

from __future__ import annotations

import asyncio
import time

from .config import settings
from .models import IncidentCandidate, LogSample, PodSignal


def decide(
    namespace: str,
    error_count: int,
    log_samples: list[LogSample],
    pod_signals: list[PodSignal],
    metrics: dict[str, float],
    window: str,
) -> IncidentCandidate | None:
    """Return an IncidentCandidate if any signal fires, else None."""

    # 1. Hard pod conditions, highest confidence.
    for ps in pod_signals:
        if ps.last_terminated_reason == "OOMKilled":
            return _candidate(namespace, "oom", "oom-killed", error_count, log_samples, pod_signals, metrics, window)
        if ps.waiting_reason == "CrashLoopBackOff":
            return _candidate(namespace, "crashloop", "crash-loop", error_count, log_samples, pod_signals, metrics, window)

    # 2. Panic spike.
    if metrics.get("panics_recovered", 0) >= settings.panic_threshold and any(
        s.error_type == "nil-pointer-panic" for s in log_samples
    ):
        return _candidate(namespace, "panic", "nil-pointer-panic", error_count, log_samples, pod_signals, metrics, window)

    # 3. Log-volume threshold.
    if error_count >= settings.error_threshold:
        dominant = log_samples[0].error_type if log_samples else "unknown"
        return _candidate(namespace, "log-volume", dominant, error_count, log_samples, pod_signals, metrics, window)

    return None


def _candidate(namespace, signal_class, dominant, error_count, log_samples, pod_signals, metrics, window):
    return IncidentCandidate(
        namespace=namespace,
        signal_class=signal_class,
        dominant_error_type=dominant,
        error_count=error_count,
        window=window,
        log_samples=log_samples,
        pod_signals=pod_signals,
        metrics=metrics,
    )


def single_log_candidate(
    namespace: str, line: str, component: str | None = None, window: str = "manual"
) -> IncidentCandidate:
    """Wrap one operator-submitted log line into an IncidentCandidate.

    signal_class="single-log" keeps these fingerprints disjoint from the poller's,
    so a manual analysis never suppresses automated detection."""
    from .loki import parse_line

    line = line[:4000]
    etype, parsed_component, _msg = parse_line(line)
    return IncidentCandidate(
        namespace=namespace,
        signal_class="single-log",
        dominant_error_type=etype,
        error_count=1,
        window=window,
        log_samples=[
            LogSample(error_type=etype, count=1, component=component or parsed_component, samples=[line])
        ],
    )


class DedupCache:
    """In-memory fingerprint cache. Lost on restart, acceptable for a local-first
    demo (documented in the runbook)."""

    def __init__(self, cooldown_seconds: int) -> None:
        self._cooldown = cooldown_seconds
        self._entries: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, fingerprint: str) -> asyncio.Lock:
        """Per-fingerprint lock so concurrent cycles for the *same* incident (e.g. the
        poller racing an on-demand /analyze) serialize across the analyze/surface awaits,
        closing the should_escalate→record TOCTOU where both pass the dedup check and file
        duplicate issues. asyncio is single-threaded, so dict.setdefault here is atomic."""
        return self._locks.setdefault(fingerprint, asyncio.Lock())

    def should_escalate(self, fingerprint: str, force: bool = False) -> bool:
        now = time.time()
        entry = self._entries.get(fingerprint)
        if force or entry is None:
            return True
        return (now - entry.get("last_filed", 0)) > self._cooldown

    def record(self, fingerprint: str, issue_url: str | None) -> None:
        now = time.time()
        self._prune(now)
        entry = self._entries.setdefault(fingerprint, {"first_seen": now})
        entry["last_filed"] = now
        if issue_url:
            entry["issue_url"] = issue_url

    def _prune(self, now: float) -> None:
        """Drop entries past 2× the cooldown to bound memory in a long-lived pod (a stale
        entry re-escalates anyway). A held lock is kept so an in-flight cycle never has its
        lock swapped out, which would reopen the TOCTOU."""
        cutoff = now - 2 * self._cooldown
        stale = [fp for fp, e in self._entries.items() if e.get("last_filed", 0) < cutoff]
        for fp in stale:
            del self._entries[fp]
            lock = self._locks.get(fp)
            if lock is not None and not lock.locked():
                del self._locks[fp]

    def issue_url(self, fingerprint: str) -> str | None:
        return (self._entries.get(fingerprint) or {}).get("issue_url")
