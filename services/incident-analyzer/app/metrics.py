"""Minimal, concurrency-safe Prometheus text-format registry for the analyzer's
own counters, picked up by the kubernetes-pods scrape job via prometheus.io/scrape."""

from __future__ import annotations

import threading

_META: dict[str, tuple[str, str]] = {
    "incidentanalyzer_polls_total": ("counter", "Detection cycles run (background + on-demand)."),
    "incidentanalyzer_single_log_total": ("counter", "Single-log analyses run via /analyze-log."),
    "incidentanalyzer_incidents_detected_total": ("counter", "Incident candidates detected."),
    "incidentanalyzer_incidents_deduped_total": ("counter", "Candidates suppressed by the dedup cache."),
    "incidentanalyzer_claude_calls_total": ("counter", "Claude analysis calls made."),
    "incidentanalyzer_issues_filed_total": ("counter", "GitHub issues opened."),
    "incidentanalyzer_notifications_sent_total": ("counter", "Backstage notifications sent."),
    "incidentanalyzer_errors_total": ("counter", "Internal errors, by stage."),
}

_lock = threading.Lock()
_values: dict[str, float] = {}


def _key(name: str, labels: str = "") -> str:
    return name if not labels else f"{name}{{{labels}}}"


def inc(name: str, labels: str = "", by: float = 1.0) -> None:
    with _lock:
        _values[_key(name, labels)] = _values.get(_key(name, labels), 0.0) + by


def render() -> str:
    with _lock:
        keys = sorted(_values)
        out: list[str] = []
        emitted: set[str] = set()
        for k in keys:
            name = k.split("{", 1)[0]
            if name not in emitted and name in _META:
                typ, help_ = _META[name]
                out.append(f"# HELP {name} {help_}")
                out.append(f"# TYPE {name} {typ}")
                emitted.add(name)
            out.append(f"{k} {_values[k]:g}")
        return "\n".join(out) + "\n"
