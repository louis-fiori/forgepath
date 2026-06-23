"""Tests for the poller's run_cycle path. Key concern: masking parity with the
single-log path — run_cycle returns its raw-sourced candidate verbatim in /analyze,
so PII must be scrubbed before it leaves the service."""

from __future__ import annotations

import asyncio

from app.config import settings
from app.detector import DedupCache
from app.models import LogSample, PodSignal


def _stub_gather(monkeypatch, *, samples, pods, error_count=50):
    """Patch the four backend calls so run_cycle builds a candidate from the given samples."""
    import app.poller as poller

    async def fake_error_count(*_a, **_k):
        return error_count

    async def fake_samples(*_a, **_k):
        return samples

    async def fake_pods(*_a, **_k):
        return pods

    async def fake_snapshot(*_a, **_k):
        return {}

    monkeypatch.setattr(poller.loki, "error_count", fake_error_count)
    monkeypatch.setattr(poller.loki, "recent_error_samples", fake_samples)
    monkeypatch.setattr(poller.k8s, "pod_signals", fake_pods)
    monkeypatch.setattr(poller.prometheus, "snapshot", fake_snapshot)
    return poller


def test_run_cycle_masks_candidate_in_response(monkeypatch):
    # Regression: the candidate is returned verbatim, so IBAN/email in a Loki
    # line or pod event must be scrubbed before it leaves the service.
    monkeypatch.setattr(settings, "masking_enabled", True)
    monkeypatch.setattr(type(settings), "llm_enabled", property(lambda self: False))

    samples = [
        LogSample(
            error_type="payout-failed",
            count=42,
            component="payout-worker",
            samples=["payout to IBAN FR7630006000011234567890189 failed for bob.456@example.com"],
        )
    ]
    pods = [PodSignal(pod="payout-worker-0", recent_events=["restart paging alice.123@example.com"])]
    poller = _stub_gather(monkeypatch, samples=samples, pods=pods)

    result = asyncio.run(poller.run_cycle(None, DedupCache(60), "payments"))

    assert result.detected is True
    sample = result.candidate.log_samples[0].samples[0]
    assert "FR7630006000011234567890189" not in sample
    assert "bob.456@example.com" not in sample
    assert "[REDACTED_IBAN]" in sample and "[REDACTED_EMAIL]" in sample

    event = result.candidate.pod_signals[0].recent_events[0]
    assert "alice.123@example.com" not in event
    assert "[REDACTED_EMAIL]" in event


def test_run_cycle_leaves_pii_when_masking_disabled(monkeypatch):
    # Masking off: the raw line passes through unchanged.
    monkeypatch.setattr(settings, "masking_enabled", False)
    monkeypatch.setattr(type(settings), "llm_enabled", property(lambda self: False))

    raw = "payout failed for bob.456@example.com"
    samples = [LogSample(error_type="payout-failed", count=42, samples=[raw])]
    poller = _stub_gather(monkeypatch, samples=samples, pods=[])

    result = asyncio.run(poller.run_cycle(None, DedupCache(60), "payments"))
    assert result.candidate.log_samples[0].samples[0] == raw


def test_concurrent_cycles_same_fingerprint_file_one_issue(monkeypatch):
    # Regression (TOCTOU): two concurrent cycles for the same incident (poller vs
    # on-demand /analyze) must not both pass dedup and file twice.
    monkeypatch.setattr(settings, "masking_enabled", False)
    monkeypatch.setattr(type(settings), "llm_enabled", property(lambda self: True))

    from app.models import Diagnosis

    samples = [LogSample(error_type="payout-failed", count=42, samples=["boom"])]
    poller = _stub_gather(monkeypatch, samples=samples, pods=[])

    filed: list[int] = []

    async def fake_fetch(*_a, **_k):
        return "# runbook"

    async def fake_analyze(*_a, **_k):
        await asyncio.sleep(0)  # yield so both cycles interleave at the lock
        return Diagnosis(
            summary="s", severity="high", affected_component="payout-worker",
            probable_root_cause="r", recommended_remediation="m", confidence=0.7,
        )

    async def fake_open_issue(*_a, **_k):
        filed.append(1)
        return "https://example.test/issues/1"

    async def fake_notify(*_a, **_k):
        return False

    monkeypatch.setattr(poller, "analyze", fake_analyze)
    monkeypatch.setattr(poller.runbook, "fetch", fake_fetch)
    monkeypatch.setattr(poller.github_issue, "open_issue", fake_open_issue)
    monkeypatch.setattr(poller.backstage, "notify", fake_notify)

    async def drive():
        cache = DedupCache(3600)
        return await asyncio.gather(
            poller.run_cycle(None, cache, "payments"),
            poller.run_cycle(None, cache, "payments"),
        )

    results = asyncio.run(drive())
    assert len(filed) == 1, f"expected exactly one issue filed, got {len(filed)}"
    assert sum(1 for r in results if r.deduped) == 1, "exactly one cycle should be deduped"
