"""Small, honest compute snapshot for the public AtomicDB dashboard."""

import threading
import time
from datetime import timedelta

from django.utils import timezone

from .models import AnalysisTask, WorkerPing


LIVE_SECONDS = 180
RATE_MINUTES = 10
CACHE_SECONDS = 30

_lock = threading.Lock()
_cached_at = 0.0
_cached = None


def _compute(now):
    live_since = now - timedelta(seconds=LIVE_SECONDS)
    pings = list(WorkerPing.objects.filter(last_seen__gte=live_since)
                 .values('threads', 'current_task_id', 'last_nps',
                         'nps_updated'))
    cores = sum(max(0, row['threads'] or 0) for row in pings)
    # A fresh lease resets the rate, heartbeats then publish the current one.
    # Requiring a current task prevents an idle lease poll from reviving the
    # completed task's historical NPS.  NPS has its own freshness timestamp:
    # a worker heartbeat can remain live after engine progress has stopped.
    nps = sum(max(0, row['last_nps'] or 0) for row in pings
              if (row['current_task_id'] is not None
                  and row['nps_updated'] is not None
                  and row['nps_updated'] >= live_since))
    completed = AnalysisTask.objects.filter(
        state='COMPLETED',
        completed__gte=now - timedelta(minutes=RATE_MINUTES),
    ).count()
    return {
        'workers': len(pings),
        'cores': cores,
        'nps': nps,
        'positions_per_minute': completed / RATE_MINUTES,
        'live_seconds': LIVE_SECONDS,
        'rate_minutes': RATE_MINUTES,
    }


def worker_metrics(*, now=None, force=False):
    """Return a 30-second cached snapshot (or deterministic forced snapshot)."""
    global _cached_at, _cached
    if now is not None:
        return _compute(now)
    clock = time.monotonic()
    if not force and _cached is not None and clock - _cached_at < CACHE_SECONDS:
        return dict(_cached)
    with _lock:
        clock = time.monotonic()
        if (not force and _cached is not None
                and clock - _cached_at < CACHE_SECONDS):
            return dict(_cached)
        _cached = _compute(timezone.now())
        _cached_at = clock
        return dict(_cached)


def reset_metrics_cache():
    """Test/deploy helper; each server process otherwise refreshes naturally."""
    global _cached_at, _cached
    with _lock:
        _cached_at = 0.0
        _cached = None
