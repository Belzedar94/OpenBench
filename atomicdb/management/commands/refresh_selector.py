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

WHAT ELSE RIDES THIS TIMER.  Everything that is a bounded scheduling decision
rather than a request: the ENGINE debt top-up, coverage completion, and — when
``ATOMICDB_ADVERSARIAL`` is on — the two adversarial arms (dn repair and F0
solves on fragile mate claims).  They belong here and not in crons of their
own because they compete for the SAME queue allowances, and a decision about
how to spend one cap should be taken in one place.  The order inside a pass is
the priority order: debt, coverage, then the adversarial arms last, so a cap
that coverage just spent is no longer available to dn repair.
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
            '--pool-target', type=int, default=ingest.ANALYSIS_POOL_TARGET,
            help='Keep this many PENDING analysis tasks minted so the fleet '
                 'never waits on the lease-path refill (default: '
                 '%(default)s).')
        parser.add_argument(
            '--debt-cap', type=int, default=ingest.DEBT_QUEUE_CAP,
            help='Maximum PENDING SolveTasks before the ENGINE-debt top-up '
                 'stops adding more (default: %(default)s).')
        parser.add_argument(
            '--coverage-cap', type=int, default=ingest.COVERAGE_QUEUE_CAP,
            help='Maximum PENDING coverage-completion tasks (default: '
                 '%(default)s).')
        parser.add_argument(
            '--no-coverage', action='store_true',
            help='Do not top up the coverage-completion queue.')
        parser.add_argument(
            '--no-debt', action='store_true',
            help='Refresh priorities only; do not top up the debt queue.')
        parser.add_argument(
            '--adversarial', dest='adversarial', action='store_true',
            default=None,
            help='Run the dn-repair and fragile-mate arms this pass, whatever '
                 'ATOMICDB_ADVERSARIAL says.')
        parser.add_argument(
            '--no-adversarial', dest='adversarial', action='store_false',
            help='Skip both adversarial arms this pass.')
        parser.add_argument(
            '--dn-repair-cap', type=int, default=ingest.DN_REPAIR_QUEUE_CAP,
            help='Maximum PENDING dn-repair tasks before the arm stops '
                 'adding more; its own quota, not shared (default: '
                 '%(default)s).')
        parser.add_argument(
            '--fragile-cap', type=int, default=ingest.FRAGILE_QUEUE_CAP,
            help='Maximum PENDING fragile-mate SolveTasks (default: '
                 '%(default)s).')
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
            # Same process, same timer: the ENGINE debt queue is topped up to
            # its cap here rather than in a cron of its own.  It is bounded
            # work (one bulk_create of at most `cap - pending` rows) and it
            # belongs with the other scheduling decision, not beside it.
            enqueued = covered = repaired = fragile = 0
            if not options['no_debt']:
                enqueued = ingest.enqueue_engine_debt(cap=options['debt_cap'])
            if not options['no_coverage']:
                covered = ingest.enqueue_coverage_completion(
                    cap=options['coverage_cap'])
            # Los brazos adversariales, en el MISMO ciclo y detras de todo lo
            # demas: el cupo que la cobertura acabe de gastar es suyo y no el
            # de nadie mas, pero el orden de prioridad sigue siendo el correcto
            # (cerrar un nodo vale mas que engordar su dn).
            adversarial = options['adversarial']
            if adversarial is None:
                adversarial = ingest.adversarial_arms_enabled()
            if adversarial:
                repaired = ingest.enqueue_dn_repair(
                    cap=options['dn_repair_cap'])
                fragile = ingest.enqueue_fragile_mate_solves(
                    cap=options['fragile_cap'])
            # Colchon de analisis, DETRAS de los brazos con cupo: la flota
            # no debe esperar al minteo inline del lease (4 con cola vacia
            # = valles de utilizacion), pero el colchon tampoco debe
            # comerse el cupo de cobertura/dn/fragiles.
            pooled = ingest.top_up_analysis_pool(options['pool_target'])
            passes += 1
            elapsed = time.monotonic() - started
            self.stdout.write(json.dumps(
                {'pass': passes, 'seconds': round(elapsed, 3),
                 'pool_topped_up': pooled,
                 'debt_enqueued': enqueued, 'coverage_enqueued': covered,
                 'adversarial': bool(adversarial),
                 'dn_repair_enqueued': repaired,
                 'fragile_enqueued': fragile},
                sort_keys=True))
            if not options['loop']:
                break
            deadline = time.monotonic() + max(0.0, interval - elapsed)
            while not stopping['now'] and time.monotonic() < deadline:
                time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
