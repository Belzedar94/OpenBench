"""Close the open positions that their children already decide.

WHY IT EXISTS.  A closure written by an analysis (the child closed by a
verified mate PV, and every link of the won line materialised under it) used
to climb only through the analysed position: ``backup_cascade`` was seeded
with that position alone, so the child's OTHER parents — the DAG transposes —
were never re-evaluated.  They stayed UNKNOWN with a proven winning child
until an analysis of their own landed on them.  The ingest now seeds the
cascade with everything it closes; this pass repairs what the old seeding
left behind.

WHAT IT LOOKS FOR, by the cascade's own rule (``_supporting_status`` and
``logic.backup_status``):

* an UNKNOWN position with a child that is a proven win for the side to
  move.  Found from the children: every decisive position LOST for its own
  mover, and the open parents its edges lead back to;
* an UNKNOWN expanded position none of whose children is still open (a loss
  for the mover, or a draw).

Every candidate is then decided exactly as ``backup_cascade`` would decide it,
so a quiet edge without clock slack (the fresh-context rule) does not count.

WHAT IT DOES.  Nothing, by default: it reads, counts and prints examples, and
on PostgreSQL every slice is read in a READ ONLY transaction.  With
``--apply`` it seeds ``backup_cascade`` and ``backup_backed_evals`` with the
confirmed keys, a small batch at a time with a pause between batches: the
same ascent a TB or SOLVE closure runs, so ancestors close in turn and the
usual closure events are written.

HOW IT READS.  On PostgreSQL the table is walked in physical block ranges
(``ctid``), one short transaction per slice: a sequential read, with index
probes only for the rows that can matter.  ``--block-from`` resumes.  A row
moved by a concurrent update can be missed or seen twice; the pass is
idempotent, so a second run catches it.  Elsewhere (SQLite) it is one ORM
pass over the whole table.
"""

import json
import time
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import OperationalError, transaction
from django.db.models import Exists, OuterRef, Q

from atomicdb import ingest, logic
from atomicdb.database import atomic, connection, current_alias
from atomicdb.models import DBEvent, Edge, Position

# A slice that hits the statement timeout is retried in halves down to this.
MIN_SLICE_BLOCKS = 250


def _decided(keys):
    """``{key: (row, status, edges)}`` for the keys their children decide.

    The same three steps as one iteration of ``backup_cascade``: the edges of
    the row, the status each child can SUPPORT, and ``logic.backup_status``.
    """
    rows = {row.key: row for row in Position.objects.filter(
        key__in=list(keys), status='UNKNOWN').only(
            'key', 'fen', 'status', 'expanded')}
    edges = {}
    for edge in (Edge.objects.filter(parent_id__in=list(rows))
                 .select_related('child')
                 .defer('child__last_analysis', 'child__analysis_passes')):
        edges.setdefault(edge.parent_id, []).append(edge)
    decided = {}
    for key, row in rows.items():
        children = edges.get(key)
        if not children:
            continue
        status = logic.backup_status(
            row.fen, row.expanded,
            [ingest._supporting_status(row, edge) for edge in children])
        if status != 'UNKNOWN':
            decided[key] = (row, status, children)
    return decided


def _example(row, status, edges):
    witness = next((edge for edge in edges
                    if edge.child.status == status
                    and ingest._supporting_status(row, edge) == status), None)
    return {'key': row.key, 'to_move': row.fen.split()[1],
            'expanded': row.expanded, 'would_be': status,
            'move': witness.move_uci if witness else None,
            'child': witness.child_id if witness else None,
            'child_closure': witness.child.closure if witness else None}


class Command(BaseCommand):
    help = ('Close the UNKNOWN positions whose children already decide them '
            '(dry-run unless --apply).')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Run the ascent on what is found. Without '
                                 'it nothing is written.')
        parser.add_argument('--batch-size', type=int, default=50,
                            help='Keys per ascent with --apply.')
        parser.add_argument('--sleep', type=float, default=0.5,
                            help='Seconds to pause after every slice and '
                                 'every batch.')
        parser.add_argument('--slice-blocks', type=int, default=5000,
                            help='PostgreSQL: table blocks per slice.')
        parser.add_argument('--block-from', type=int, default=0,
                            help='PostgreSQL: first block, to resume.')
        parser.add_argument('--block-to', type=int, default=None,
                            help='PostgreSQL: stop before this block.')
        parser.add_argument('--statement-timeout', type=int, default=60,
                            help='PostgreSQL: seconds per statement.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Stop after this many decided positions.')
        parser.add_argument('--examples', type=int, default=10)
        parser.add_argument('--json', action='store_true',
                            help='Machine-readable summary.')

    def handle(self, *args, **options):
        started = time.monotonic()
        counts = Counter(dict.fromkeys(
            ('slices', 'one_winning_child', 'all_children_closed', 'decided')
            + (('closed', 'cascade_changed', 'backed_changed')
               if options['apply'] else ()), 0))
        by_status = Counter()
        examples = []
        # A parent is reached from every slice holding one of its children:
        # count it, and run its ascent, once.
        seen = {'one': set(), 'all': set(), 'decided': set()}
        postgres = connection.vendor == 'postgresql'
        slices = (self._block_slices(options) if postgres else [(None, None)])
        resume = None
        for start, stop in slices:
            if postgres:
                found = self._pg_slice(start, stop, options)
            else:
                with atomic():
                    found = self._orm_pass()
            one_child, all_children, decided = found
            counts['slices'] += 1
            counts['one_winning_child'] += len(set(one_child) - seen['one'])
            counts['all_children_closed'] += len(set(all_children)
                                                 - seen['all'])
            seen['one'].update(one_child)
            seen['all'].update(all_children)
            keys = sorted(set(decided) - seen['decided'])
            if options['limit'] is not None:
                keys = keys[:max(0, options['limit'] - counts['decided'])]
            seen['decided'].update(keys)
            for key in keys:
                row, status, edges = decided[key]
                counts['decided'] += 1
                by_status[status] += 1
                if len(examples) < options['examples']:
                    examples.append(_example(row, status, edges))
            if options['apply'] and keys:
                self._apply(keys, counts, options)
            resume = stop
            if options['verbosity'] > 1 and postgres:
                self.stdout.write(
                    f'  blocks {start}-{stop}: {len(keys)} decided, '
                    f"{counts['decided']} so far")
            if (options['limit'] is not None
                    and counts['decided'] >= options['limit']):
                break
            if postgres and options['sleep'] > 0:
                time.sleep(options['sleep'])

        summary = {**counts, 'by_status': dict(by_status),
                   'mode': 'apply' if options['apply'] else 'dry-run',
                   'seconds': round(time.monotonic() - started, 1),
                   'examples': examples}
        if postgres:
            summary['next_block'] = resume
        if options['apply']:
            DBEvent.objects.create(kind='STATUS_RECASCADE', payload={
                key: value for key, value in summary.items()
                if key != 'examples'})
        if options['json']:
            self.stdout.write(json.dumps(summary, sort_keys=True))
            return
        self.stdout.write('recascade_status ({mode}): '.format(**summary)
                          + ' '.join(f'{name}={value}' for name, value in
                                     sorted(summary.items())
                                     if name not in ('examples', 'mode')))
        for example in examples:
            self.stdout.write('  ' + json.dumps(example, sort_keys=True))

    def _apply(self, keys, counts, options):
        """The ordinary ascent, seeded with the decided keys, batch by batch.

        No transaction around it, like every other backfill: each write
        commits on its own and no live writer waits behind a whole batch.
        ``backup_cascade`` re-reads every row, so a position closed in the
        meantime is simply left alone.
        """
        size = max(1, options['batch_size'])
        for start in range(0, len(keys), size):
            batch = keys[start:start + size]
            counts['cascade_changed'] += ingest.backup_cascade(batch)
            counts['backed_changed'] += ingest.backup_backed_evals(batch)
            counts['closed'] += (Position.objects.filter(key__in=batch)
                                 .exclude(status='UNKNOWN').count())
            if options['sleep'] > 0:
                time.sleep(options['sleep'])

    def _block_slices(self, options):
        table = Position._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_relation_size(%s::regclass)"
                " / current_setting('block_size')::int", [table])
            total = cursor.fetchone()[0]
        stop = total if options['block_to'] is None else min(
            total, options['block_to'])
        step = max(MIN_SLICE_BLOCKS, options['slice_blocks'])
        for start in range(max(0, options['block_from']), stop, step):
            yield start, min(start + step, stop)

    def _pg_slice(self, start, stop, options):
        """Candidates and decisions for one block range, read only.

        A range that hits the statement timeout is read again in halves.
        """
        try:
            with atomic(), connection.cursor() as cursor:
                cursor.execute('SET LOCAL transaction_read_only = on')
                cursor.execute('SET LOCAL statement_timeout = %d'
                               % (max(1, options['statement_timeout'])
                                  * 1000))
                # Index probes from the slice, never a hash of whole tables.
                cursor.execute('SET LOCAL enable_hashjoin = off')
                cursor.execute('SET LOCAL enable_mergejoin = off')
                cursor.execute('SET LOCAL max_parallel_workers_per_gather = 0')
                cursor.execute('SET LOCAL jit = off')
                one_child, all_children = self._pg_candidates(
                    cursor, start, stop)
                decided = _decided(set(one_child) | set(all_children))
                # Nothing to keep.  Rolling back also drops the SET LOCALs
                # when an outer transaction is open (a test, a shell).
                transaction.set_rollback(True, using=current_alias())
            return one_child, all_children, decided
        except OperationalError:
            if stop - start <= MIN_SLICE_BLOCKS:
                raise
        middle = (start + stop) // 2
        first = self._pg_slice(start, middle, options)
        second = self._pg_slice(middle, stop, options)
        return (first[0] + second[0], first[1] + second[1],
                {**first[2], **second[2]})

    @staticmethod
    def _pg_candidates(cursor, start, stop):
        pos, edge = Position._meta.db_table, Edge._meta.db_table
        bounds = [f'({start},0)', f'({stop},0)']
        # Children of the slice that are lost for their own mover, and the
        # open parents their edges lead back to.
        cursor.execute(
            f'SELECT DISTINCT e.parent_id FROM {pos} c'
            f' JOIN {edge} e ON e.child_id = c.key'
            f' JOIN {pos} p ON p.key = e.parent_id'
            ' WHERE c.ctid >= %s::tid AND c.ctid < %s::tid'
            " AND ((c.status = 'WHITE_WIN' AND c.fen LIKE %s)"
            "      OR (c.status = 'BLACK_WIN' AND c.fen LIKE %s))"
            " AND p.status = 'UNKNOWN'", bounds + ['% b %', '% w %'])
        one_child = [row[0] for row in cursor.fetchall()]
        # Open expanded positions of the slice with no open child left.
        cursor.execute(
            f'SELECT p.key FROM {pos} p'
            ' WHERE p.ctid >= %s::tid AND p.ctid < %s::tid'
            " AND p.status = 'UNKNOWN' AND p.expanded"
            f' AND EXISTS (SELECT 1 FROM {edge} e WHERE e.parent_id = p.key)'
            f' AND NOT EXISTS (SELECT 1 FROM {edge} e'
            f'                 JOIN {pos} c ON c.key = e.child_id'
            "                 WHERE e.parent_id = p.key"
            "                 AND c.status = 'UNKNOWN')", bounds)
        all_children = [row[0] for row in cursor.fetchall()]
        return one_child, all_children

    @staticmethod
    def _orm_pass():
        lost_for_mover = (
            Q(child__status='WHITE_WIN', child__fen__contains=' b ')
            | Q(child__status='BLACK_WIN', child__fen__contains=' w '))
        one_child = list(Edge.objects.filter(
            lost_for_mover, parent__status='UNKNOWN').values_list(
                'parent_id', flat=True).distinct())
        all_children = list(
            Position.objects.filter(status='UNKNOWN', expanded=True)
            .filter(Exists(Edge.objects.filter(parent_id=OuterRef('key'))))
            .exclude(Exists(Edge.objects.filter(parent_id=OuterRef('key'),
                                                child__status='UNKNOWN')))
            .values_list('key', flat=True))
        decided = _decided(set(one_child) | set(all_children))
        return one_child, all_children, decided
