"""Kubernetes API reader: pod status + recent events for a namespace.

Uses in-cluster config + RBAC. The official client is sync, so calls are wrapped
in asyncio.to_thread to stay off the event loop.
"""

from __future__ import annotations

import asyncio

from .models import PodSignal

try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
        _AVAILABLE = True
    except Exception:  # not in-cluster (local dev), degrade gracefully
        _AVAILABLE = False
except Exception:  # kubernetes lib missing
    _AVAILABLE = False


def _collect(namespace: str) -> list[PodSignal]:
    v1 = k8s_client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace=namespace).items
    signals: list[PodSignal] = []
    for p in pods:
        name = p.metadata.name
        restarts = 0
        waiting_reason = None
        last_term = None
        for cs in (p.status.container_statuses or []):
            restarts += cs.restart_count or 0
            if cs.state and cs.state.waiting and cs.state.waiting.reason:
                waiting_reason = cs.state.waiting.reason
            if cs.last_state and cs.last_state.terminated and cs.last_state.terminated.reason:
                last_term = cs.last_state.terminated.reason
        signals.append(
            PodSignal(
                pod=name,
                phase=p.status.phase,
                restart_count=restarts,
                waiting_reason=waiting_reason,
                last_terminated_reason=last_term,
            )
        )

    # Recent warning events, attached to the first signal for context.
    try:
        events = v1.list_namespaced_event(namespace=namespace).items

        # The API doesn't guarantee order, so sort by event time before [-10:].
        def _event_time(e) -> float:
            t = e.last_timestamp or e.event_time or (e.metadata.creation_timestamp if e.metadata else None)
            return t.timestamp() if t else 0.0

        warnings = sorted(
            (e for e in events if e.type == "Warning" and e.message),
            key=_event_time,
        )
        recent = [f"{e.reason}: {e.message}" for e in warnings][-10:]
        if signals and recent:
            signals[0].recent_events = recent
    except Exception:  # noqa: S110 - event enrichment is best-effort; pod signals stand on their own
        pass

    return signals


async def pod_signals(namespace: str) -> list[PodSignal]:
    if not _AVAILABLE:
        return []
    try:
        return await asyncio.to_thread(_collect, namespace)
    except Exception:
        return []
