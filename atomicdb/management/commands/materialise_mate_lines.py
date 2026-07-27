"""Turn the stored mate witnesses into navigable tree.

A MATE_PV closure lives only on its own node.  ``expand`` skips anything that
is not UNKNOWN, so the chain of the witness was never materialised and the
winning line stayed a string: the explorer shows "WHITE_WIN via MATE_PV, ≤M6"
in the header and "unexplored" on every single row underneath, the winning
move included.  Clicking it lands on a position the database does not have.

This walks the existing closures and materialises their chains: one edge per
ply of the witness, each suffix closed as the same win, one ply shorter, with
the same proof grade.  Nothing is invented — the witness was re-verified move
by move when it closed, and this re-verifies it again by default before
touching anything, because a historical row may predate a movegen fix.

RUNNABLE WITH THE TREE LIVE.  It only creates positions and edges and only
closes nodes that are still UNKNOWN; it never overwrites an existing closure,
never expands anything, and works one witness per transaction.

That was not enough on its own.  The first production run needed a full
quiesce because SQLite answered "database is locked" as soon as the ingest
processor wanted the writer.  So each witness now waits its turn
(``busy_timeout``) and, if it still loses the race, retries with backoff
instead of aborting the pass; ``--batch-size`` and ``--sleep`` slice the work
thin enough to live alongside a few hundred cores of workers.  It takes
longer, and nobody has to stop.

Resumable by key cursor, and idempotent: a chain already materialised re-walks
to the same rows and writes nothing.
"""

import json
import time

from django.core.management.base import BaseCommand
from django.db import OperationalError

from atomicdb import ingest
from atomicdb.database import atomic, connection
from atomicdb.models import DBEvent, Position

# Waiting for a busy writer is normal, not an error: the same allowance
# ``verify_mates`` gives itself.
BUSY_TIMEOUT_MS = 30_000
# Per-witness retries for when the timeout itself is exhausted.
RETRY_BACKOFF_SECONDS = (1, 3, 10, 30)


class Command(BaseCommand):
    help = 'Materialise the won_line chain of existing MATE_PV closures.'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=50,
                            help='Witnesses per resumable checkpoint; smaller '
                                 'batches are friendlier to a live tree.')
        parser.add_argument('--sleep', type=float, default=0.0,
                            help='Seconds to pause between batches, leaving '
                                 'the write lock to the workers.')
        parser.add_argument('--busy-timeout', type=int, default=BUSY_TIMEOUT_MS,
                            help='Milliseconds to wait for the write lock.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Stop after this many witnesses.')
        parser.add_argument('--proof', default=None,
                            help='Only this proof grade (ANDOR or ENGINE).')
        parser.add_argument('--no-verify', action='store_true',
                            help='Skip re-verifying each witness first.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report the work without writing.')
        parser.add_argument('--json', action='store_true',
                            help='Machine-readable summary.')

    def handle(self, *args, **options):
        connection.ensure_connection()
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute(
                    'PRAGMA busy_timeout = {}'.format(
                        int(options['busy_timeout'])))

        counts = {'witnesses': 0, 'edges_created': 0, 'nodes_closed': 0,
                  'plies_walked': 0, 'rejected': 0, 'skipped': 0,
                  'retried': 0, 'locked_out': 0}
        after_key, processed = '', 0
        seeds = []

        while options['limit'] is None or processed < options['limit']:
            take = options['batch_size']
            if options['limit'] is not None:
                take = min(take, options['limit'] - processed)
            if take <= 0:
                break
            rows = self._batch(after_key, take, options['proof'])
            if not rows:
                break
            for row in rows:
                after_key = row.key
                processed += 1
                counts['witnesses'] += 1
                if options['dry_run']:
                    counts['plies_walked'] += len(
                        (row.won_line or '').split())
                    continue
                result = self._materialise(row, options, counts)
                if result is None:
                    counts['locked_out'] += 1
                    self.stderr.write(
                        'LOCKED {}: still busy after {} retries; rerun '
                        'later'.format(row.key[:16],
                                       len(RETRY_BACKOFF_SECONDS)))
                    continue
                if result.get('rejected'):
                    counts['rejected'] += 1
                    self.stderr.write(
                        f"REJECTED {row.key[:16]}: {result['rejected']}")
                    continue
                counts['edges_created'] += result['created_edges']
                counts['nodes_closed'] += result['closed']
                counts['plies_walked'] += result['plies']
                if result['closed'] or result['created_edges']:
                    seeds.append(row.key)
                else:
                    counts['skipped'] += 1
            if options['sleep'] > 0:
                time.sleep(options['sleep'])

        if seeds and not options['dry_run']:
            # New exact children mean the ancestors may now back up further.
            # Sliced as thin as the main pass, for the same reason.
            step = max(1, options['batch_size'])
            for start in range(0, len(seeds), step):
                self._cascade(seeds[start:start + step], counts)
                if options['sleep'] > 0:
                    time.sleep(options['sleep'])
            DBEvent.objects.create(kind='WON_LINES_MATERIALISED',
                                   payload=dict(counts))

        if options['json']:
            self.stdout.write(json.dumps(counts, sort_keys=True))
            return
        self.stdout.write(
            'materialise_mate_lines: '
            + ' '.join(f'{name}={value}' for name, value in counts.items())
            + f' cursor={after_key[:16] or "-"}')

    def _materialise(self, row, options, counts):
        """One witness, retried while SQLite says the tree is busy.

        A lock is a queue, not a failure: the ingest processor holding the
        writer for a moment is exactly what is supposed to happen on a live
        tree.  Anything that is NOT a lock is re-raised — a real error must
        never be retried into silence.
        """
        for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
            try:
                with atomic():
                    return ingest.materialise_won_line(
                        row, verify=not options['no_verify'])
            except OperationalError as error:
                message = str(error).lower()
                if 'locked' not in message and 'busy' not in message:
                    raise
                if attempt >= len(RETRY_BACKOFF_SECONDS):
                    return None
                counts['retried'] += 1
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
        return None

    def _cascade(self, seeds, counts):
        """The ancestor backup, under the same patience as the witnesses."""
        for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
            try:
                ingest.backup_cascade(seeds)
                return
            except OperationalError as error:
                message = str(error).lower()
                if 'locked' not in message and 'busy' not in message:
                    raise
                if attempt >= len(RETRY_BACKOFF_SECONDS):
                    counts['locked_out'] += 1
                    return
                counts['retried'] += 1
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])

    def _batch(self, after_key, take, proof):
        rows = Position.objects.filter(
            closure='MATE_PV', status__in=('WHITE_WIN', 'BLACK_WIN'),
            won_line__isnull=False, key__gt=after_key,
        ).exclude(won_line='').exclude(proof='DISPUTED')
        if proof:
            rows = rows.filter(proof=proof)
        return list(rows.order_by('key')[:take])
