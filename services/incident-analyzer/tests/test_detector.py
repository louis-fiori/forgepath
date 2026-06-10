"""Tests for app.detector: the any-of incident decision and the in-memory
dedup cache (an ongoing incident collapses to one fingerprint per cooldown)."""

from __future__ import annotations

import asyncio

from app.detector import DedupCache, decide
from app.models import LogSample, PodSignal


def _samples(error_type: str = "db-connection-timeout", count: int = 30) -> list[LogSample]:
    return [LogSample(error_type=error_type, count=count, samples=["boom"])]


def test_no_signal_returns_none():
    assert decide("ns", error_count=0, log_samples=[], pod_signals=[], metrics={}, window="10m") is None


def test_below_threshold_returns_none():
    # 5 < default error_threshold (20), no pod/panic signal.
    out = decide("ns", error_count=5, log_samples=_samples(count=5), pod_signals=[], metrics={}, window="10m")
    assert out is None


def test_log_volume_over_threshold_fires():
    out = decide("ns", error_count=25, log_samples=_samples(), pod_signals=[], metrics={}, window="10m")
    assert out is not None
    assert out.signal_class == "log-volume"
    assert out.dominant_error_type == "db-connection-timeout"


def test_oom_fires_regardless_of_log_volume():
    pods = [PodSignal(pod="p1", last_terminated_reason="OOMKilled")]
    out = decide("ns", error_count=0, log_samples=[], pod_signals=pods, metrics={}, window="10m")
    assert out is not None
    assert out.signal_class == "oom"
    assert out.dominant_error_type == "oom-killed"


def test_crashloop_fires():
    pods = [PodSignal(pod="p1", waiting_reason="CrashLoopBackOff")]
    out = decide("ns", error_count=0, log_samples=[], pod_signals=pods, metrics={}, window="10m")
    assert out is not None
    assert out.signal_class == "crashloop"


def test_pod_condition_takes_priority_over_log_volume():
    pods = [PodSignal(pod="p1", last_terminated_reason="OOMKilled")]
    out = decide("ns", error_count=999, log_samples=_samples(), pod_signals=pods, metrics={}, window="10m")
    assert out.signal_class == "oom"  # not "log-volume"


def test_panic_spike_requires_both_metric_and_sample():
    panic_samples = [LogSample(error_type="nil-pointer-panic", count=1, samples=["recovered panic"])]
    # Metric + matching sample fires.
    out = decide("ns", error_count=0, log_samples=panic_samples, pod_signals=[], metrics={"panics_recovered": 1}, window="10m")
    assert out is not None and out.signal_class == "panic"
    # Metric without matching sample does not.
    out = decide("ns", error_count=0, log_samples=_samples(count=1), pod_signals=[], metrics={"panics_recovered": 1}, window="10m")
    assert out is None


def test_fingerprint_is_stable_and_ignores_counts():
    a = decide("ns", error_count=21, log_samples=_samples(count=21), pod_signals=[], metrics={}, window="10m")
    b = decide("ns", error_count=500, log_samples=_samples(count=500), pod_signals=[], metrics={}, window="1h")
    # Fingerprint keys on namespace + error_type + signal_class, not counts/windows.
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_differs_by_signal_class():
    log = decide("ns", error_count=21, log_samples=_samples(), pod_signals=[], metrics={}, window="10m")
    oom = decide("ns", error_count=0, log_samples=[], pod_signals=[PodSignal(pod="p", last_terminated_reason="OOMKilled")], metrics={}, window="10m")
    assert log.fingerprint() != oom.fingerprint()


# --- DedupCache ---

def test_dedup_first_sighting_escalates():
    cache = DedupCache(cooldown_seconds=3600)
    assert cache.should_escalate("fp1") is True


def test_dedup_suppresses_within_cooldown():
    cache = DedupCache(cooldown_seconds=3600)
    cache.record("fp1", issue_url="https://example/issues/1")
    assert cache.should_escalate("fp1") is False
    assert cache.issue_url("fp1") == "https://example/issues/1"


def test_dedup_force_bypasses_cooldown():
    cache = DedupCache(cooldown_seconds=3600)
    cache.record("fp1", issue_url=None)
    assert cache.should_escalate("fp1", force=True) is True


def test_dedup_escalates_again_after_cooldown():
    cache = DedupCache(cooldown_seconds=3600)
    cache.record("fp1", issue_url=None)
    # Backdate the last filing to just past the cooldown window.
    cache._entries["fp1"]["last_filed"] -= 3601
    assert cache.should_escalate("fp1") is True


def test_dedup_unknown_fingerprint_has_no_issue_url():
    cache = DedupCache(cooldown_seconds=3600)
    assert cache.issue_url("never-seen") is None


def test_dedup_prunes_entries_older_than_twice_cooldown():
    # A record() sweep drops fingerprints older than 2× the cooldown (plus their
    # unheld lock), bounding memory in a long-lived pod.
    cache = DedupCache(cooldown_seconds=3600)
    cache.record("old-fp", issue_url=None)
    cache.lock_for("old-fp")  # materialize the paired lock (unheld)
    # Backdate past 2× cooldown so the next record() sweeps it.
    cache._entries["old-fp"]["last_filed"] -= 2 * 3600 + 1

    cache.record("fresh-fp", issue_url=None)

    assert "old-fp" not in cache._entries
    assert "old-fp" not in cache._locks
    assert "fresh-fp" in cache._entries
    # A fingerprint still within 2× cooldown is retained.
    assert cache.issue_url("fresh-fp") is None and cache.should_escalate("fresh-fp") is False


def test_dedup_prune_keeps_held_lock():
    # A held lock must survive the sweep even with a stale entry, else we'd reopen
    # the TOCTOU.
    cache = DedupCache(cooldown_seconds=3600)
    cache.record("busy-fp", issue_url=None)
    cache._entries["busy-fp"]["last_filed"] -= 2 * 3600 + 1

    async def drive():
        async with cache.lock_for("busy-fp"):  # genuinely hold the lock across the sweep
            cache.record("other-fp", issue_url=None)  # triggers _prune while busy-fp is held
            assert "busy-fp" not in cache._entries  # entry pruned (harmless; record recreates)
            assert "busy-fp" in cache._locks  # held lock survives

    asyncio.run(drive())
