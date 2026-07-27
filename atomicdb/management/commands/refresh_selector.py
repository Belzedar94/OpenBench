"""Keep the AtomicDB selector priorities fresh, outside the HTTP path.

WHY.  ``refresh_priorities`` is a Dijkstra over the whole DAG plus two Python
dictionaries holding every position and edge.  It used to run inline in
``/atomicdb/api/lease``, so every worker poll could pay it — in five gunicorn
processes at once, each with its own copy of the graph.  At 450k positions that
is seconds of CPU; ten times bigger it is the first thing that breaks.

WHAT CHANGES.  Exactly one process (this one) recomputes priorities on a timer.
``next_tasks`` reads the column as it stands.  A priority that is a minute old
still orders the queue perfectly well: it is a heuristic about where to look,
never a source of truth.

FALLBACK.  ``ATOMICDB_INLINE_SELECTOR = True`` in the web process restores the
old inline behaviour through the same code path, so this service can be stopped
without stopping the project.  See Documentation/atomicdb-selector.service.
"""

import json
import signal
import time

from django.core.management.base import BaseCommand

from atomicdb import ingest
from atomicdb.database import connection
from atomicdb.models import Position


class Command(BaseCommand):
    help = 'Recompute AtomicDB selector priorities outside the request path.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--loop', action='store_true',
            help='Keep refreshing on --interval instead of running once.')
        parser.add_argument(
            '--interval', type=float, default=60.0,
            help='Seconds between refreshes when looping (default: 60).')
        parser.add_argument(
            '--status', action='store_true',
            help='Print how many live positions carry a priority and exit.')

    def handle(self, *args, **options):
        if options['status']:
            live = Position.objects.filter(status='UNKNOWN')
            self.stdout.write(json.dumps({
                'live': live.count(),
                'tombstoned': live.filter(
                    priority__lte=ingest.DEAD / 2).count(),
                'top_priority': live.order_by('-priority').values_list(
                    'priority', flat=True).first(),
            }, sort_keys=True))
            return

        connection.ensure_connection()
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA busy_timeout = 30000')

        interval = max(1.0, float(options['interval']))
        stopping = {'now': False}

        def stop(signum, frame):
            del signum, frame
            stopping['now'] = True

        for name in ('SIGINT', 'SIGTERM'):
            handler = getattr(signal, name, None)
            if handler is not None:
                try:
                    signal.signal(handler, stop)
                except (ValueError, OSError):
                    pass   # sin hilo principal: el bucle sale por --loop off

        passes = 0
        while not stopping['now']:
            started = time.monotonic()
            # ``force``: the service's own interval is the only clock that
            # matters here, not the process-local 30s cache the old inline
            # caller needed.
            ingest.refresh_priorities(force=True)
            passes += 1
            elapsed = time.monotonic() - started
            self.stdout.write(json.dumps(
                {'pass': passes, 'seconds': round(elapsed, 3)},
                sort_keys=True))
            if not options['loop']:
                break
            deadline = time.monotonic() + max(0.0, interval - elapsed)
            while not stopping['now'] and time.monotonic() < deadline:
                time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
