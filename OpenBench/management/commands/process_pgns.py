"""Archive uploaded gameplay PGNs in a dedicated long-running process."""

import json
import signal
import time
import traceback

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from OpenBench.models import PGN
from OpenBench.pgn_watcher import PGNWatcher


class Command(BaseCommand):
    help = 'Archive pending gameplay PGNs into one tar file per workload.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once', action='store_true',
            help='Process the current pending rows once and exit.')
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Process at most this many rows per pass.')
        parser.add_argument(
            '--idle-seconds', type=float, default=15.0,
            help='Sleep between polls when no PGN was archived.')

    def handle(self, *args, **options):
        limit = options['limit']
        if limit is not None and limit < 1:
            raise CommandError('--limit must be at least 1')

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
                    pass

        watcher = PGNWatcher()
        totals = {'processed': 0, 'failed': 0}

        while not stopping['now']:
            close_old_connections()
            pending = PGN.objects.filter(processed=False).order_by('id')
            if limit is not None:
                pending = pending[:limit]

            pass_processed = 0
            for pgn in pending.iterator():
                if stopping['now']:
                    break
                try:
                    watcher.process_pgn(pgn)
                except Exception:
                    totals['failed'] += 1
                    self.stderr.write('Failed to archive %s' % pgn.filename())
                    traceback.print_exc(file=self.stderr)
                else:
                    totals['processed'] += 1
                    pass_processed += 1

            if options['once']:
                break

            if pass_processed == 0:
                deadline = time.monotonic() + max(0.1, options['idle_seconds'])
                while not stopping['now'] and time.monotonic() < deadline:
                    time.sleep(0.1)

        close_old_connections()
        self.stdout.write(json.dumps(totals, sort_keys=True))
