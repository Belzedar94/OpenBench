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

SAFE WHILE THE TREE IS LIVE.  It only creates positions and edges and only
closes nodes that are still UNKNOWN; it never overwrites an existing closure,
never expands anything, and works one witness per transaction.  A worker
submitting at the same time competes for the SQLite write lock and nothing
else.  Resumable by key cursor, and idempotent: a chain already materialised
re-walks to the same rows and writes nothing.
"""

import json

from django.core.management.base import BaseCommand
from django.db.models import Q

from atomicdb import ingest, logic
from atomicdb.database import atomic, connection
from atomicdb.models import DBEvent, Position


class Command(BaseCommand):
    help = 'Materialise the won_line chain of existing MATE_PV closures.'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=200,
                            help='Witnesses read per resumable checkpoint.')
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
                cursor.execute('PRAGMA busy_timeout = 30000')

        counts = {'witnesses': 0, 'edges_created': 0, 'nodes_closed': 0,
                  'plies_walked': 0, 'rejected': 0, 'skipped': 0}
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
                with atomic():
                    result = ingest.materialise_won_line(
                        row, verify=not options['no_verify'])
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

        if seeds and not options['dry_run']:
            # New exact children mean the ancestors may now back up further.
            for start in range(0, len(seeds), 200):
                ingest.backup_cascade(seeds[start:start + 200])
            DBEvent.objects.create(kind='WON_LINES_MATERIALISED',
                                   payload=dict(counts))

        if options['json']:
            self.stdout.write(json.dumps(counts, sort_keys=True))
            return
        self.stdout.write(
            'materialise_mate_lines: '
            + ' '.join(f'{name}={value}' for name, value in counts.items())
            + f' cursor={after_key[:16] or "-"}')

    def _batch(self, after_key, take, proof):
        rows = Position.objects.filter(
            closure='MATE_PV', status__in=('WHITE_WIN', 'BLACK_WIN'),
            won_line__isnull=False, key__gt=after_key,
        ).exclude(won_line='').exclude(proof='DISPUTED')
        if proof:
            rows = rows.filter(proof=proof)
        return list(rows.order_by('key')[:take])
