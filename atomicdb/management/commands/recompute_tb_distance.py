"""Rewrite stored values to the tablebase-distance scale, from the raw lines.

WHY.  Until 24-Nov-2026 the worker clamped every centipawn score to +-9500,
so a tablebase win and a huge ordinary eval were stored as the same number
and tablebase wins could not be sorted by their distance to the tables.  New
results are stored on the new scale (``atomicdb.score_bands``); this command
moves the old ones.  Nothing is searched again: every line of a result keeps
the raw UCI ``info`` line the engine printed, and the new value is derived
from that line alone.

WHAT IT TOUCHES.  Open positions (``status = 'UNKNOWN'``) whose ``eval_cp``
is in the band ``9000 <= |eval_cp| <= 9500``: every value the old clamp
produced is there (exactly +-9500), and so is every plain score between 9000
and 9499, which the new scale compresses.  Closed positions are left alone:
their parents read their status, never their eval.

* A position with a search of its own: every line of ``last_analysis``
  (current and prior pass) is rewritten from its raw line, and ``eval_cp``
  (and ``best_move``, when it named the old best line) follows the new best
  line, but only when ``eval_cp`` was the old best line's value: a value
  written by something else (a restricted pass that improved it) is left.
* A position with no search of its own and no backed support, i.e. a seed
  of a parent's line: the parents' lines for that move are read, and the
  seed follows the raw line of the deepest one whose old value it carries.

The pass history (``analysis_passes``) is a record of what was printed then
and is not rewritten.  A line with no raw line (very old workers) keeps its
value: 9500 then still means "a capped engine win, distance unknown".

AFTERWARDS.  The parents of every rewritten position are climbed with the
same backed ascent a landing runs (``ingest.backup_backed_evals``, open
positions only, in small batches, retried after a lock conflict), seeded,
like a landing, with the position and its parents.  The status cascade is
not run: no status depends on an eval.

HOW IT READS THE TABLE.  The position table has no index on ``eval_cp``,
and a partial one would cost disk and every ingest's write for a one-off.
So the candidates are read by physical block ranges (PostgreSQL ``ctid``
ranges, a TID range scan, as ``sweep_stale_backed``'s rotation does; SQLite
rowids), ``--block-batch`` blocks per statement, each under
``statement_timeout``.  A whole pass reads the heap once.  The cursor of an
applied run is kept in a ``TB_DISTANCE_RECOMPUTE`` event, so a run cut by
``--max-seconds`` is carried on by the next one.

``--keys`` / ``--keys-file`` handle the given positions only (no scan).

DRY RUN BY DEFAULT.  Without ``--apply`` it reads, reports what it would
write and writes nothing at all, not even its cursor.
"""

import json
import time
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import OperationalError
from django.db.models import Q

from atomicdb import ingest, score_bands
from atomicdb.database import atomic, connection
from atomicdb.models import DBEvent, Edge, Position

EVENT = 'TB_DISTANCE_RECOMPUTE'
EXAMPLES = 8

# The rows the old clamp can have touched: the band, both signs.
BAND_Q = (Q(eval_cp__gte=score_bands.BIG_LOW,
            eval_cp__lte=score_bands.TB_TOP)
          | Q(eval_cp__gte=-score_bands.TB_TOP,
              eval_cp__lte=-score_bands.BIG_LOW))
BAND_SQL = ('((eval_cp >= %(low)d AND eval_cp <= %(top)d) OR '
            '(eval_cp >= -%(top)d AND eval_cp <= -%(low)d))'
            % {'low': score_bands.BIG_LOW, 'top': score_bands.TB_TOP})

_ROW_FIELDS = ('key', 'fen', 'status', 'eval_cp', 'best_move', 'last_analysis',
               'nodes_invested', 'backed_nodes')


def _chunks(items, size):
    size = max(1, size)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _stm_white(fen):
    return fen.split()[1] == 'w'


def _better(value, best, stm_white):
    return best is None or (value > best if stm_white else value < best)


def _best(lines, value_of, stm_white):
    """``(value, move)`` of the best line, the first of equals, as ingest does."""
    best, move = None, None
    for line in lines:
        value = value_of(line)
        if value is not None and _better(value, best, stm_white):
            best, move = value, line.get('move')
    return best, move


def plan_own(row):
    """What a position with its own search would be rewritten to, or None.

    ``{'last_analysis': [...], 'eval_cp': int, 'best_move': str}`` with only
    the fields that change.
    """
    stored = row.last_analysis
    if not isinstance(stored, list) or not stored:
        return None
    stm_white = _stm_white(row.fen)
    lines = [score_bands.normalize_line(line, stm_white)
             if isinstance(line, dict) else line for line in stored]
    changes = {}
    if lines != stored:
        changes['last_analysis'] = lines
    current = [line for line in stored
               if isinstance(line, dict) and not line.get('prior_pass')]
    renewed = [line for line in lines
               if isinstance(line, dict) and not line.get('prior_pass')]

    def legacy(line):
        values = score_bands.stored_from_raw(line, stm_white)
        if values is not None and line.get('eval_cp') in values:
            return values[1]
        return line.get('eval_cp')

    old_best, old_move = _best(current, legacy, stm_white)
    new_best, new_move = _best(renewed, lambda ln: ln.get('eval_cp'),
                               stm_white)
    if (new_best is not None and row.eval_cp is not None
            and row.eval_cp in (old_best, new_best)
            and new_best != row.eval_cp):
        changes['eval_cp'] = new_best
        if new_move and row.best_move in (old_move, None):
            if new_move != row.best_move:
                changes['best_move'] = new_move
    return changes or None


def plan_seed(row, parents):
    """The new ``eval_cp`` of a seed, or None.

    ``parents`` is ``[(parent_fen, move, last_analysis)]``.  The deepest
    parent line for the move whose raw line accounts for the seed's value
    decides (current pass before prior pass, then the larger budget).
    """
    found = []
    for fen, move, stored in parents:
        if not isinstance(stored, list):
            continue
        budget = 0
        for line in stored:
            if (isinstance(line, dict) and not line.get('prior_pass')):
                budget = int(line.get('_budget') or 0)
                break
        stm_white = _stm_white(fen)
        for line in stored:
            if not isinstance(line, dict) or line.get('move') != move:
                continue
            values = score_bands.stored_from_raw(line, stm_white)
            if values is None or row.eval_cp not in values:
                continue
            found.append((not line.get('prior_pass'), budget, values[0]))
    if not found:
        return None
    new = max(found)[2]
    return None if new == row.eval_cp else new


class Command(BaseCommand):
    help = ('Rewrite open positions stored with the old +-9500 clamp to the '
            'tablebase-distance scale, from their raw UCI lines, and climb '
            'their parents.  Dry run unless --apply.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write.  Without it, report only.')
        parser.add_argument('--keys', default='',
                            help='Comma separated keys: these only, no scan.')
        parser.add_argument('--keys-file', default='',
                            help='File of keys, added to --keys.')
        parser.add_argument('--from-block', type=int, default=None,
                            help='Start the scan here (blocks on PostgreSQL, '
                                 'rowids on SQLite) instead of the cursor of '
                                 'the last applied run.')
        parser.add_argument('--blocks', type=int, default=0,
                            help='Scan at most this many blocks/rowids this '
                                 'run (0: to the end of the table).')
        parser.add_argument('--block-batch', type=int, default=2_000,
                            help='Blocks/rowids read per statement.')
        parser.add_argument('--write-batch', type=int, default=200,
                            help='Positions rewritten per transaction.')
        parser.add_argument('--repair-batch', type=int, default=50,
                            help='Rewritten positions per backed ascent.')
        parser.add_argument('--no-propagate', action='store_true',
                            help='Rewrite only; leave the parents to the '
                                 'sweep (sweep_stale_backed).')
        parser.add_argument('--max-changes', type=int, default=0,
                            help='Stop after rewriting this many positions '
                                 '(0: no limit).')
        parser.add_argument('--max-seconds', type=float, default=3_000.0,
                            help='Time budget of the run.')
        parser.add_argument('--statement-timeout', type=int, default=30,
                            help='PostgreSQL: seconds per statement.')
        parser.add_argument('--lock-timeout', type=float, default=0.2,
                            help='PostgreSQL: seconds a write waits for a '
                                 'row an ingest holds.')
        parser.add_argument('--json', action='store_true',
                            help='Print the summary as JSON.')

    # ------------------------------------------------------------ plumbing

    def _elapsed(self):
        return time.monotonic() - self.started

    def _session(self, opts):
        if connection.vendor != 'postgresql':
            return {}
        settings = {
            'statement_timeout': '%ds' % max(1, opts['statement_timeout']),
            'lock_timeout': '%dms' % max(1, int(opts['lock_timeout'] * 1000)),
            'application_name': 'recompute_tb_distance',
        }
        previous = {}
        with connection.cursor() as cursor:
            for name, value in settings.items():
                cursor.execute('SELECT current_setting(%s)', [name])
                previous[name] = cursor.fetchone()[0]
                cursor.execute('SELECT set_config(%s, %s, false)',
                               [name, value])
        return previous

    @staticmethod
    def _restore(previous):
        if not previous:
            return
        with connection.cursor() as cursor:
            for name, value in previous.items():
                cursor.execute('SELECT set_config(%s, %s, false)',
                               [name, value])

    @staticmethod
    def _extent():
        """``(unit, total)``: blocks on PostgreSQL, rowids elsewhere."""
        table = Position._meta.db_table
        with connection.cursor() as cursor:
            if connection.vendor == 'postgresql':
                cursor.execute(
                    "SELECT pg_relation_size(%s::regclass)"
                    " / current_setting('block_size')::int", [table])
                return 'block', int(cursor.fetchone()[0] or 0)
            cursor.execute(
                f'SELECT COALESCE(MAX(rowid), 0) + 1 FROM {table}')
            return 'rowid', int(cursor.fetchone()[0] or 0)

    @staticmethod
    def _scan(unit, start, stop):
        """Keys of the open band rows in ``[start, stop)``: one statement."""
        table = Position._meta.db_table
        if unit == 'block':
            where = 'ctid >= %s::tid AND ctid < %s::tid'
            bounds = [f'({start},0)', f'({stop},0)']
        else:
            where = 'rowid >= %s AND rowid < %s'
            bounds = [start, stop]
        with connection.cursor() as cursor:
            cursor.execute(
                f'SELECT key FROM {table} WHERE {where}'
                f" AND status = 'UNKNOWN' AND {BAND_SQL}", bounds)
            return [row[0] for row in cursor.fetchall()]

    @staticmethod
    def _last_cursor():
        event = (DBEvent.objects.filter(kind=EVENT,
                                        payload__has_key='cursor')
                 .order_by('-id').first())
        return (event.payload or {}) if event else {}

    # ------------------------------------------------------------ plan

    def _plan(self, keys):
        """``{key: (old_eval, changes)}`` for the band rows among ``keys``."""
        rows = list(Position.objects.filter(key__in=keys, status='UNKNOWN')
                    .filter(BAND_Q).only(*_ROW_FIELDS))
        plans = {}
        seeds = []
        for row in rows:
            own = bool(row.nodes_invested) or bool(
                isinstance(row.last_analysis, list) and row.last_analysis)
            if own:
                self.counts['own'] += 1
                changes = plan_own(row)
                if changes:
                    plans[row.key] = (row.eval_cp, changes, 'own')
                elif not any(isinstance(line, dict) and line.get('raw')
                             for line in (row.last_analysis or [])):
                    self.counts['no_raw'] += 1
            elif not row.backed_nodes:
                seeds.append(row)
            else:
                self.counts['backed_only'] += 1
        if seeds:
            self.counts['seed'] += len(seeds)
            edges = list(Edge.objects.filter(
                child_id__in=[row.key for row in seeds])
                .values_list('child_id', 'parent_id', 'move_uci'))
            parents = (Position.objects.only('key', 'fen', 'last_analysis')
                       .in_bulk({parent for _child, parent, _move in edges}))
            by_child = {}
            for child, parent, move in edges:
                prow = parents.get(parent)
                if prow is not None:
                    by_child.setdefault(child, []).append(
                        (prow.fen, move, prow.last_analysis))
            for row in seeds:
                new = plan_seed(row, by_child.get(row.key, ()))
                if new is not None:
                    plans[row.key] = (row.eval_cp, {'eval_cp': new}, 'seed')
        return plans

    # ------------------------------------------------------------ write

    def _write(self, plans):
        """Apply ``plans`` in one transaction; returns the keys written.

        Each row is re-read under lock and written only while its
        ``eval_cp`` is still the value the plan was made from (and, for a
        seed, while it is still a seed): anything an ingest wrote in between
        wins, and the next run looks at it again.
        """
        written = []
        with atomic():
            locked = {row.key: row for row in Position.objects
                      .select_for_update().filter(key__in=list(plans))
                      .only(*_ROW_FIELDS)}
            for key, (old_eval, changes, kind) in plans.items():
                row = locked.get(key)
                if row is None or row.status != 'UNKNOWN' \
                        or row.eval_cp != old_eval:
                    self.counts['moved'] += 1
                    continue
                if kind == 'seed':
                    if row.nodes_invested or row.backed_nodes:
                        self.counts['moved'] += 1
                        continue
                else:
                    # Re-plan on the locked row: its lines may be newer.
                    changes = plan_own(row)
                    if not changes:
                        self.counts['moved'] += 1
                        continue
                for name, value in changes.items():
                    setattr(row, name, value)
                row.save(update_fields=[*changes, 'updated'])
                written.append(key)
        return written

    def _climb(self, keys, opts):
        if not keys or opts['no_propagate']:
            return
        parents = list(Edge.objects.filter(child_id__in=keys)
                       .values_list('parent_id', flat=True).distinct())
        seeds = list(dict.fromkeys([*keys, *parents]))
        for batch in _chunks(seeds, opts['repair_batch']):
            report = {}
            try:
                self.counts['backed_changed'] += \
                    ingest.backup_backed_evals_retrying(
                        batch, open_only=True, report=report)
            except OperationalError as error:
                if not ingest.is_lock_conflict(error):
                    raise
                # Its frontier is on record (BACKED_GUARD), and the sweep
                # carries it on.
                self.counts['climb_busy'] += 1
            self.counts['ascents'] += 1
            if report.get('guard') in ('ply-guard', 'node-budget'):
                self.counts['climb_cut'] += 1

    # ------------------------------------------------------------ run

    def handle(self, *args, **opts):
        self.started = time.monotonic()
        self.counts = Counter(dict.fromkeys(
            ('candidates', 'own', 'seed', 'backed_only', 'no_raw', 'planned',
             'written', 'moved', 'busy', 'ascents', 'backed_changed',
             'climb_busy', 'climb_cut'), 0))
        self.bands = Counter()
        self.examples = []
        previous = self._session(opts)
        try:
            summary = self._run(opts)
        finally:
            self._restore(previous)
        if opts['json']:
            self.stdout.write(json.dumps(summary, sort_keys=True))
        else:
            self._print(summary)
        return None

    def _given_keys(self, opts):
        keys = [key for key in opts['keys'].split(',') if key]
        if opts['keys_file']:
            with open(opts['keys_file']) as handle:
                keys += handle.read().replace(',', ' ').split()
        return list(dict.fromkeys(keys))

    def _batches(self, opts, summary):
        """Yields lists of candidate keys, and records the scan cursor."""
        given = self._given_keys(opts)
        if given:
            summary['mode'] = 'keys'
            for chunk in _chunks(given, opts['write_batch']):
                yield chunk
            return
        summary['mode'] = 'scan'
        unit, total = self._extent()
        start = opts['from_block']
        if start is None:
            # A finished pass leaves cursor 0, so the next run starts over
            # (harmless: a rewritten row plans nothing).
            last = self._last_cursor()
            start = (int(last.get('cursor') or 0)
                     if last.get('unit') in (None, unit) else 0)
        start = max(0, min(start, total))
        stop_at = total if opts['blocks'] <= 0 else min(
            total, start + opts['blocks'])
        summary['scan'] = {'unit': unit, 'from': start, 'total': total}
        position = start
        while position < stop_at:
            if self._stop(opts):
                break
            upper = min(stop_at, position + max(1, opts['block_batch']))
            keys = self._scan(unit, position, upper)
            position = upper
            summary['scan']['to'] = position
            if keys:
                yield keys
        summary['scan']['to'] = position
        summary['scan']['done'] = position >= total

    def _stop(self, opts):
        if self._elapsed() >= opts['max_seconds']:
            self.valve = 'max-seconds'
            return True
        if opts['max_changes'] and self.counts['written'] >= opts['max_changes']:
            self.valve = 'max-changes'
            return True
        return False

    def _run(self, opts):
        apply = bool(opts['apply'])
        summary = {'apply': apply}
        self.valve = None
        for keys in self._batches(opts, summary):
            self.counts['candidates'] += len(keys)
            plans = self._plan(keys)
            self.counts['planned'] += len(plans)
            for key, (old, changes, kind) in plans.items():
                new = changes.get('eval_cp')
                if new is not None:
                    self.bands['%s->%s' % (score_bands.band(old),
                                           score_bands.band(new))] += 1
                else:
                    self.bands['lines-only'] += 1
                if len(self.examples) < EXAMPLES:
                    self.examples.append({'key': key, 'kind': kind,
                                          'eval_cp': [old, new],
                                          'fields': sorted(changes)})
            if not apply or not plans:
                continue
            for chunk in _chunks(list(plans), opts['write_batch']):
                part = {key: plans[key] for key in chunk}
                try:
                    written = self._write(part)
                except OperationalError as error:
                    if not ingest.is_lock_conflict(error):
                        raise
                    self.counts['busy'] += len(part)
                    continue
                self.counts['written'] += len(written)
                self._climb(written, opts)
            if self._stop(opts):
                break
        summary.update(self.counts)
        summary['bands'] = dict(self.bands)
        summary['examples'] = self.examples
        summary['valve'] = self.valve
        summary['seconds'] = round(self._elapsed(), 1)
        if apply and summary.get('mode') == 'scan':
            scan = summary['scan']
            DBEvent.objects.create(kind=EVENT, payload={
                **{key: value for key, value in summary.items()
                   if key not in ('apply', 'scan', 'examples')},
                'cursor': 0 if scan.get('done') else scan.get('to', 0),
                'unit': scan['unit'], 'done': bool(scan.get('done'))})
        elif apply:
            DBEvent.objects.create(kind=EVENT, payload={
                key: value for key, value in summary.items()
                if key not in ('apply', 'examples')})
        return summary

    def _print(self, summary):
        self.stdout.write(
            'recompute_tb_distance ({mode}{dry}): candidates={candidates} '
            'planned={planned} written={written} moved={moved} '
            'busy={busy} ascents={ascents} backed_changed={backed_changed} '
            'valve={valve} seconds={seconds}'.format(
                mode=summary.get('mode'),
                dry='' if summary.get('apply') else ', dry run',
                **{name: summary.get(name) for name in (
                    'candidates', 'planned', 'written', 'moved', 'busy',
                    'ascents', 'backed_changed', 'valve', 'seconds')}))
        if summary.get('scan'):
            self.stdout.write('  scan %s' % json.dumps(summary['scan'],
                                                       sort_keys=True))
        self.stdout.write('  rows own=%s seed=%s backed_only=%s no_raw=%s' % (
            summary.get('own'), summary.get('seed'),
            summary.get('backed_only'), summary.get('no_raw')))
        if summary.get('bands'):
            self.stdout.write('  bands %s' % json.dumps(summary['bands'],
                                                        sort_keys=True))
        for example in summary.get('examples') or []:
            self.stdout.write('  ' + json.dumps(example, sort_keys=True))
