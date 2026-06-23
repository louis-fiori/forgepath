"""Pydantic models shared across the detection → analysis → surfacing pipeline."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LogSample(BaseModel):
    """A small representative bucket of error lines for one error_type."""

    error_type: str
    count: int
    component: str | None = None
    samples: list[str] = Field(default_factory=list)  # raw msg lines


class PodSignal(BaseModel):
    """High-confidence incident signals lifted from the Kubernetes API."""

    pod: str
    phase: str | None = None
    restart_count: int = 0
    waiting_reason: str | None = None  # e.g. CrashLoopBackOff
    last_terminated_reason: str | None = None  # e.g. OOMKilled
    recent_events: list[str] = Field(default_factory=list)


class IncidentCandidate(BaseModel):
    """A detected incident before LLM analysis. `signal_class` + `dominant_error_type`
    form the dedup fingerprint."""

    namespace: str
    signal_class: str  # log-volume | oom | crashloop | panic
    dominant_error_type: str
    error_count: int = 0
    window: str = "10m"
    log_samples: list[LogSample] = Field(default_factory=list)
    pod_signals: list[PodSignal] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)

    def fingerprint(self) -> str:
        # Excludes counts/timestamps so an ongoing incident collapses to one fingerprint.
        import hashlib

        key = f"{self.namespace}:{self.dominant_error_type}:{self.signal_class}"
        # usedforsecurity=False: this is a dedup fingerprint, not a security hash.
        return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]


class Diagnosis(BaseModel):
    """Structured output produced by Claude (the record_diagnosis tool schema).

    Validated, not trusted: values are surfaced verbatim to GitHub / Backstage (severity
    becomes an issue label, confidence a percentage), so `confidence=5.0` or a free-form
    severity must fail closed (ValidationError → caught in poller as an analysis failure)
    rather than produce a "500%" label or an arbitrary GitHub label."""

    summary: str
    severity: Literal["low", "medium", "high", "critical"]
    affected_component: str
    probable_root_cause: str
    recommended_remediation: str
    confidence: float = Field(ge=0.0, le=1.0)


class AnalyzeResult(BaseModel):
    """What the /analyze endpoint returns and what the poller logs."""

    detected: bool
    deduped: bool = False
    fingerprint: str | None = None
    candidate: IncidentCandidate | None = None
    diagnosis: Diagnosis | None = None
    issue_url: str | None = None
    notified: bool = False
    note: str | None = None
