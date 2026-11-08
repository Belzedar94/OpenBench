"""Climb the ancestors of the landings that left their climb in the queue.

Runs as its own service (docs/server/atomicdb-propagate.service) when
``ATOMICDB_DEFER_PROPAGATION`` is on: the ingest writers then commit each
result on its own position and leave the rest here (§
atomicdb/propagation_queue.py).  ONE instance is the intended shape: the
point of the queue is that a single climber merges what a flood lands under
the same ancestors and has nobody to deadlock with.  A second instance
corrupts nothing (each batch is claimed under row locks) but brings the
contention back.
"""

import json
import logging
import signal

from django.core.management.base import BaseCommand

from atomicdb import propagation_queue

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Climb the ancestors of landed AtomicDB searches (deferred stage).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once', action='store_true',
            help='Drain what is ready and exit instead of looping.')
        parser.add_argument(
            '--batch', type=int, default=propagation_queue.BATCH_SIZE,
            help='Landings merged into one climb (default %(default)s).')
        parser.add_argument(
            '--idle-seconds', type=float, default=1.0,
            help='Sleep between polls when the queue is empty.')
        parser.add_argument(
            '--status', action='store_true',
            help='Print the backlog (rows waiting, oldest age) and exit.')

    def handle(self, *args, **options):
        if options['status']:
            backlog = propagation_queue.backlog()
            backlog['oldest'] = (backlog['oldest'].isoformat()
                                 if backlog['oldest'] else None)
            self.stdout.write(json.dumps(backlog, sort_keys=True))
            return

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
                    pass   # no main thread: the loop leaves through --once

        batch = max(1, options['batch'])
        totals = {'settled': 0, 'failed_batches': 0, 'batches': 0}
        while not stopping['now']:
            # One batch per turn, so a SIGTERM waits for one climb at most.
            result = propagation_queue.drain(max_batches=1, limit=batch)
            for name in totals:
                totals[name] += result[name]
            if options['once']:
                if result['batches'] == 0 or (result['settled'] == 0
                                              and result['failed_batches']):
                    break
                continue
            if result['batches'] == 0 or result['settled'] == 0:
                propagation_queue.wait_idle(options['idle_seconds'],
                                            lambda: stopping['now'])
        self.stdout.write(json.dumps(totals, sort_keys=True))
