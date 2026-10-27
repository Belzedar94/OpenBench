"""Find the open positions whose backed value is stale, and repair them.

WHAT STALE MEANS.  One level of the backup, run now on what a position's
children hold, would write something other than what the position stores: a
value, a distance, a support or a move (``ingest.stale_backed_rows``, which
asks the very function every level of an ascent writes with).  The explorer
shows it as a header that its own table contradicts.

WHY THERE ARE ANY (26-Oct-2026).  Three things leave them, and none of them
heals on its own:

* capped ascents.  An ascent stops after ``BACKED_MAX_PLIES`` levels.  The
  main line is deeper than that now (77 plies of pointers below 7e11bbf6 that
  evening), so a change at its bottom never reaches its top in one ascent, and
  in a wide family under analysis every ingest is cut.  The parents of what
  the last level changed were left behind; the BACKED_GUARD event of the cut
  now carries them.  Since 27-Oct-2026 an ingest carries its own cut on as
  soon as it has committed (ingest.carry_cut_ascents), so this feed is the
  backstop: frontiers too wide for that, and carries that failed.
* concurrent writers.  The ingest writers run their ascents in their own
  transactions, at READ COMMITTED.  When two of them reach the same ancestor,
  each computes it without the other's uncommitted change: the one that
  commits last overwrites it (a lost update), or neither writes it because
  nothing changed in its own view (write skew).  Seen in the job records:
  5fc31111 kept 1216 via b7b5, a number from before b7b5 was searched, after
  ingests of b7b5 and of a grandchild 0.7 s apart (19-Oct); 409cf9f7 kept
  1217 via f7e8 after ingests of f7e8 and of a grandchild 0.1 s apart
  (24-Oct).  Nothing records that it happened.
* relics.  A change of rule (tie order, loops, echoes) reaches only what an
  ascent touches afterwards.  The nightly recascade that was to close them
  (invariant 4 of docs/value-semantics.md) has been stopped since 5-Oct-2026:
  a pass over the whole table no longer fits in a night.

THREE FEEDS, ONE CHECK.  Every run reads

1. ``guards``: the frontiers of the BACKED_GUARD events since the last run.
   They are not checked: they are the parents of rows that the last level of
   an ascent changed, and an ascent seeded with them continues the one that
   was cut.  ``--max-generation`` bounds how many times one frontier is
   carried on, so a family that never comes to rest is not chased for ever;
2. ``recent``: the positions that two ingest jobs overlapping in time both
   reach within ``--recent-depth`` plies up, among the jobs created since the
   last run: the merge points of the race above;
3. ``rotation``: a slice of the table in physical order (PostgreSQL block
   ranges, SQLite rowids), sized from the time since the last run so that
   the whole table is read every ``--rotation-days``.

What 2 and 3 find stale (and what a valve held back last time) is checked
again after ``--settle-seconds``.  Only a position still stale, with the very
numbers it stored the first time, and with no ingest of its own pending, is
repaired.  One that changed in between is being worked on; the next look
will see where it ended up.

REPAIR.  ``ingest.backup_backed_evals``, in short transactions, with proven
positions left out (``open_only``) and tried again after a lock conflict:
first the confirmed positions in small batches, then the continuations.

VALVES.  ``--max-changes`` rows written per run; ``--max-cut`` repair ascents
cut by a guard per run, the mark of a family that does not come to rest;
``--max-stale-fraction``, past which the rotation's findings are reported and
not repaired, because that many means a change of rule, which wants a
deliberate recascade; ``--max-seconds`` for the whole run.

COST (production, 26-Oct-2026, read only).  The block scan reads 2,000
blocks in 30 ms.  The check costs about 4 s per thousand open positions, and
the table holds about two million of them in 3.7 million blocks: a whole
rotation is some 2.2 hours of one connection, about 12 seconds of checks for
a 15-minute run at the default seven days.

STATE.  Each run writes one BACKED_SWEEP event: what it found, what it did,
and the cursors the next run starts from.  ``--dry-run`` writes nothing at
all.
"""

import json
import math
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.db import DatabaseError, OperationalError, transaction
from django.utils import timezone

from atomicdb import ingest
from atomicdb.database import atomic, connection, current_alias
from atomicdb.models import DBEvent, Edge, IngestJob, Position

SWEEP_EVENT = 'BACKED_SWEEP'
GUARD_EVENT = 'BACKED_GUARD'
# Any constant: one sweep at a time per database (pg_try_advisory_lock).
ADVISORY_LOCK = 0x5BAC5EED
# Before the first run the slice is sized as if the last one ran this long
# ago, and never for more than a day: a sweep that was off for a week starts
# again with a day's slice, not a week's.
FIRST_INTERVAL_SECONDS = 900
MAX_INTERVAL_SECONDS = 86_400
# Keys a run hands to the next one (held back by a valve), at most.
PENDING_KEYS = 2_000
EXAMPLES = 5
# The stale-fraction valve only judges a rotation slice this large: a few
# positions say nothing about the table.
ROTATION_MIN_SAMPLE = 200


def _chunks(items, size):
    size = max(1, size)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _kind(stored, decided):
    """Which of the four numbers differs first: value, move, plies, support."""
    for name, old, new in zip(('value', 'move', 'plies', 'support'),
                              stored, decided):
        if old != new:
            return name
    return 'none'


def _merge_points(jobs, depth):
    """Positions that two jobs overlapping in time both reach going up.

    ``jobs`` is ``[(position_key, start, end)]``.  A position counts the jobs
    that reach it within ``depth`` plies up, its own job included (an
    ancestor analysed while a descendant's result lands is a merge point
    too).  One indexed query per ply and per thousand positions.
    """
    reached = defaultdict(set)
    frontier = defaultdict(set)
    for index, (key, _start, _end) in enumerate(jobs):
        frontier[key].add(index)
        reached[key].add(index)
    for _ply in range(depth):
        parents = defaultdict(set)
        keys = list(frontier)
        for chunk in _chunks(keys, 1_000):
            for parent, child in Edge.objects.filter(
                    child_id__in=chunk).values_list('parent_id', 'child_id'):
                parents[parent] |= frontier[child]
        for key, indices in parents.items():
            reached[key] |= indices
        frontier = parents
        if not frontier:
            break
    merged = []
    for key, indices in reached.items():
        if len(indices) < 2:
            continue
        windows = sorted((jobs[i][1], jobs[i][2]) for i in indices)
        latest_end = windows[0][1]
        for start, end in windows[1:]:
            if start < latest_end:
                merged.append(key)
                break
            latest_end = max(latest_end, end)
    return merged


class Command(BaseCommand):
    help = ('Find the open positions whose backed value one level of the '
            'backup would rewrite, and repair those that stay stale.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--rotation-days', type=float, default=7.0,
            help='Read the whole table once every this many days; the slice '
                 'of a run follows from the time since the last one.')
        parser.add_argument(
            '--units', type=int, default=0,
            help='Blocks (PostgreSQL) or rowids (SQLite) to read this run, '
                 'instead of the --rotation-days share.')
        parser.add_argument(
            '--recent-depth', type=int, default=2,
            help='Plies up from an ingested position to look for merge '
                 'points of overlapping ingests.')
        parser.add_argument(
            '--settle-seconds', type=float, default=300.0,
            help='Look again after this long; repair only what is still '
                 'stale with the same stored numbers.')
        parser.add_argument('--check-batch', type=int, default=500,
                            help='Positions per check.')
        parser.add_argument('--repair-batch', type=int, default=50,
                            help='Confirmed positions per repair ascent.')
        parser.add_argument('--chunk', type=int, default=2_000,
                            help='Frontier keys per continuation ascent.')
        parser.add_argument(
            '--max-generation', type=int, default=2,
            help='Continue a BACKED_GUARD frontier only while its generation '
                 'is below this.')
        parser.add_argument('--max-changes', type=int, default=10_000,
                            help='Stop repairing past this many rows written.')
        parser.add_argument(
            '--max-cut', type=int, default=3,
            help='Stop repairing after this many repair ascents cut by a '
                 'guard.')
        parser.add_argument(
            '--max-stale-fraction', type=float, default=0.2,
            help='Report, and do not repair, the rotation when more of what '
                 'it checked is stale than this.')
        parser.add_argument('--max-seconds', type=float, default=600.0,
                            help='Time budget of the run.')
        parser.add_argument('--statement-timeout', type=int, default=60,
                            help='PostgreSQL: seconds per statement.')
        # Below PostgreSQL's deadlock_timeout (1 s on production): the sweep
        # gives a lock up before a deadlock check could choose an ingest as
        # its victim (pg_stat_database counted 225 deadlocks in the five
        # days after the move to the new server on 21-Oct-2026).
        parser.add_argument('--lock-timeout', type=float, default=0.2,
                            help='PostgreSQL: seconds a repair waits for a '
                                 'row an ingest holds.')
        parser.add_argument('--keys', default='',
                            help='Comma separated keys: check and repair '
                                 'these only.')
        parser.add_argument('--keys-file', default='',
                            help='File of keys, added to --keys.')
        parser.add_argument('--no-guards', action='store_true')
        parser.add_argument('--no-recent', action='store_true')
        parser.add_argument('--no-rotation', action='store_true')
        parser.add_argument('--dry-run', action='store_true',
                            help='Find and report; write nothing at all.')
        parser.add_argument('--json', action='store_true',
                            help='Print the summary as JSON.')

    # ------------------------------------------------------------ plumbing

    def _elapsed(self):
        return time.monotonic() - self.started

    def _postgres_session(self, opts):
        """Low priority settings for this session; returns what to restore."""
        settings = {
            'statement_timeout': '%ds' % max(1, opts['statement_timeout']),
            'lock_timeout': '%dms' % max(1, int(opts['lock_timeout'] * 1000)),
            'application_name': 'sweep_stale_backed',
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
    def _restore_session(previous):
        with connection.cursor() as cursor:
            for name, value in previous.items():
                cursor.execute('SELECT set_config(%s, %s, false)',
                               [name, value])

    @staticmethod
    def _advisory(function):
        with connection.cursor() as cursor:
            cursor.execute('SELECT %s(%%s)' % function, [ADVISORY_LOCK])
            return cursor.fetchone()[0]

    def _read_only(self, function, *args):
        """``function(*args)`` in a read only transaction that is discarded.

        On PostgreSQL a check that tried to write would fail here instead of
        writing; elsewhere it simply runs.
        """
        if connection.vendor != 'postgresql':
            return function(*args)
        with atomic(), connection.cursor() as cursor:
            cursor.execute('SET LOCAL transaction_read_only = on')
            result = function(*args)
            transaction.set_rollback(True, using=current_alias())
        return result

    # ------------------------------------------------------------ feeds

    @staticmethod
    def _last_run():
        return (DBEvent.objects.filter(kind=SWEEP_EVENT,
                                       payload__has_key='cursors')
                .order_by('-id').first())

    def _table_extent(self):
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

    def _rotation(self, opts, last, summary):
        unit, total = self._table_extent()
        cursors = (last.payload.get('cursors') or {}) if last else {}
        start = int(cursors.get('rotation') or 0)
        if cursors.get('rotation_unit') not in (None, unit) or start >= total:
            start = 0
        if opts['units'] > 0:
            units = opts['units']
        else:
            since = ((timezone.now() - last.ts).total_seconds() if last
                     else FIRST_INTERVAL_SECONDS)
            since = min(max(since, 60.0), MAX_INTERVAL_SECONDS)
            days = max(opts['rotation_days'], 1e-3)
            units = math.ceil(total * since / (days * 86_400))
        stop = min(total, start + max(1, units))
        table = Position._meta.db_table
        if unit == 'block':
            where = 'ctid >= %s::tid AND ctid < %s::tid'
            bounds = [f'({start},0)', f'({stop},0)']
        else:
            where = 'rowid >= %s AND rowid < %s'
            bounds = [start, stop]

        def read():
            with connection.cursor() as cursor:
                cursor.execute(
                    f'SELECT key FROM {table} WHERE {where}'
                    " AND status = 'UNKNOWN'"
                    ' AND (expanded OR backed_eval IS NOT NULL)', bounds)
                return [row[0] for row in cursor.fetchall()]

        keys = self._read_only(read) if total else []
        summary['rotation'] = {'unit': unit, 'from': start, 'to': stop,
                               'total': total, 'candidates': len(keys)}
        self.cursors.update(rotation=stop if stop < total else 0,
                            rotation_unit=unit)
        return keys

    def _recent(self, opts, last, summary):
        now = timezone.now()
        until = now - timedelta(seconds=max(0.0, opts['settle_seconds']))
        cursors = (last.payload.get('cursors') or {}) if last else {}
        since = None
        if cursors.get('recent_until'):
            since = datetime.fromisoformat(cursors['recent_until'])
        if since is None or since > until:
            since = until - timedelta(seconds=FIRST_INTERVAL_SECONDS)
        since = max(since, until - timedelta(seconds=MAX_INTERVAL_SECONDS))
        jobs = list(IngestJob.objects.filter(
            created__gt=since, created__lte=until,
            state=IngestJob.JState.DONE).values_list(
                'position_id', 'created', 'updated'))
        keys = _merge_points(jobs, max(0, opts['recent_depth']))
        summary['recent'] = {'jobs': len(jobs), 'merge_points': len(keys)}
        self.cursors['recent_until'] = until.isoformat()
        return keys

    def _guards(self, opts, last, summary):
        """``{generation: [keys]}`` of the BACKED_GUARD frontiers to carry on.

        Events younger than the settle time wait for the next run: an
        ingest's event is written before its transaction commits.
        """
        cursors = (last.payload.get('cursors') or {}) if last else {}
        settled = timezone.now() - timedelta(
            seconds=max(0.0, opts['settle_seconds']))
        events = DBEvent.objects.filter(kind=GUARD_EVENT, ts__lte=settled)
        if cursors.get('guard_event') is not None:
            events = events.filter(id__gt=int(cursors['guard_event']))
        else:
            events = events.filter(ts__gt=settled - timedelta(
                seconds=FIRST_INTERVAL_SECONDS))
        by_generation = defaultdict(dict)
        seen, last_id, held = 0, cursors.get('guard_event'), 0
        for event_id, payload in events.order_by('id').values_list(
                'id', 'payload'):
            seen += 1
            last_id = event_id
            generation = int(payload.get('generation') or 0)
            # Events written before the frontier was recorded carry no keys.
            keys = payload.get('keys') or []
            if not keys:
                continue
            if generation >= opts['max_generation']:
                held += len(keys)       # left, with its keys, to a person
                continue
            by_generation[generation].update(dict.fromkeys(keys))
        summary['guards'] = {
            'events': seen, 'held_back': held,
            'keys': {str(gen): len(keys)
                     for gen, keys in sorted(by_generation.items())}}
        self.cursors['guard_event'] = last_id
        return {gen: list(keys) for gen, keys in sorted(by_generation.items())}

    # ------------------------------------------------------------ check

    def _check(self, keys, opts):
        found = {}
        for batch in _chunks(sorted(set(keys)), opts['check_batch']):
            try:
                found.update(self._read_only(ingest.stale_backed_rows, batch))
            except OperationalError as error:
                # A statement timeout: this batch waits for the next look.
                self.counts['check_failed'] += len(batch)
                self.stderr.write('check of %d keys failed: %s' % (
                    len(batch), str(error).splitlines()[0]))
        return found

    # ------------------------------------------------------------ repair

    def _valve(self, opts):
        if self.counts['changed'] >= opts['max_changes']:
            return 'max-changes'
        if self.counts['cut'] >= opts['max_cut']:
            return 'max-cut'
        if self._elapsed() >= opts['max_seconds']:
            return 'max-seconds'
        return None

    def _repair(self, keys, generation):
        report = {}
        try:
            changed = ingest.backup_backed_evals_retrying(
                keys, open_only=True, generation=generation, report=report)
        except OperationalError as error:
            if not ingest.is_lock_conflict(error):
                raise
            # Its frontier is on record (BACKED_GUARD, "aborted").
            self.counts['busy'] += 1
            changed = report.get('changed', 0)
        self.counts['ascents'] += 1
        self.counts['changed'] += changed
        if report.get('guard') in ('ply-guard', 'node-budget'):
            self.counts['cut'] += 1
            self.unsettled.extend(keys[:3])

    # ------------------------------------------------------------ run

    def handle(self, *args, **opts):
        self.started = time.monotonic()
        self.counts = Counter(dict.fromkeys(
            ('checked', 'stale', 'confirmed', 'moved', 'resolved',
             'in_flight', 'held', 'check_failed', 'ascents', 'changed', 'cut',
             'busy'), 0))
        self.cursors = {}
        self.unsettled = []
        postgres = connection.vendor == 'postgresql'
        previous, owned = {}, False
        if postgres:
            previous = self._postgres_session(opts)
            try:
                owned = self._advisory('pg_try_advisory_lock')
            except DatabaseError:
                # A session that may not even take an advisory lock cannot
                # write either: only a dry run goes on without one.
                if not opts['dry_run']:
                    self._restore_session(previous)
                    raise
                owned = None
            if owned is False:
                self._restore_session(previous)
                self.stdout.write('another sweep_stale_backed holds the '
                                  'lock; nothing done')
                return
        try:
            summary = self._sweep(opts)
        finally:
            if owned:
                self._advisory('pg_advisory_unlock')
            if previous:
                self._restore_session(previous)
        if opts['json']:
            self.stdout.write(json.dumps(summary, sort_keys=True,
                                         default=str))
        else:
            self._print(summary)

    def _sweep(self, opts):
        summary = {'dry_run': bool(opts['dry_run'])}
        given = [key for key in opts['keys'].split(',') if key]
        if opts['keys_file']:
            with open(opts['keys_file']) as handle:
                given += handle.read().replace(',', ' ').split()
        guards, rotation = {}, []
        if given:
            summary['mode'] = 'keys'
            candidates = list(dict.fromkeys(given))
        else:
            summary['mode'] = 'scheduled'
            last = self._last_run()
            pending = list((last.payload.get('pending') or []) if last
                           else [])
            candidates = list(pending)
            if not opts['no_rotation']:
                rotation = self._rotation(opts, last, summary)
                candidates += rotation
            if not opts['no_recent']:
                candidates += self._recent(opts, last, summary)
            if not opts['no_guards']:
                guards = self._guards(opts, last, summary)
            summary['pending_in'] = len(pending)
        candidates = list(dict.fromkeys(candidates))

        first = self._check(candidates, opts)
        self.counts['checked'] = len(candidates)
        self.counts['stale'] = len(first)
        kinds = Counter(_kind(*pair) for pair in first.values())
        summary['kinds'] = dict(kinds)
        summary['examples'] = [
            {'key': key, 'stored': list(stored), 'decided': list(decided)}
            for key, (stored, decided) in sorted(first.items())[:EXAMPLES]]

        # The rotation is a sample of the whole table.  When too much of it is
        # stale that is a change of rule, not drift, and it wants a
        # deliberate recascade, not a sweep: reported, not repaired.
        valves = []
        rotation_stale = len(set(rotation) & set(first))
        summary['rotation_stale'] = rotation_stale
        held = set()
        if (len(rotation) >= ROTATION_MIN_SAMPLE and rotation_stale
                > opts['max_stale_fraction'] * len(rotation)):
            valves.append('stale-fraction')
            held = set(rotation)
            self.counts['held'] = rotation_stale

        confirmed, pending = [], []
        recheck = [key for key in first if key not in held]
        if recheck:
            settle = max(0.0, opts['settle_seconds'])
            if self._elapsed() + settle >= opts['max_seconds']:
                # No time left to look again: the next run will.
                valves.append('max-seconds')
                pending += recheck
            else:
                if settle:
                    time.sleep(settle)
                again = self._check(recheck, opts)
                for key, (stored, _decided) in again.items():
                    if stored == first[key][0]:
                        confirmed.append(key)
                    else:
                        self.counts['moved'] += 1
                self.counts['resolved'] = len(set(recheck) - set(again))
                # A position whose own result is landing is recomputed by
                # that ingest; repairing it now would only race with it.
                flight = set(IngestJob.objects.filter(
                    position_id__in=confirmed,
                    state__in=(IngestJob.JState.PENDING,
                               IngestJob.JState.PROCESSING))
                    .values_list('position_id', flat=True))
                self.counts['in_flight'] = len(flight)
                confirmed = sorted(key for key in confirmed
                                   if key not in flight)
        self.counts['confirmed'] = len(confirmed)

        if not opts['dry_run']:
            # Confirmed positions first, in small ascents; the continuations
            # of cut ascents after them, a chunk per ascent.
            work = [(1, batch)
                    for batch in _chunks(confirmed, opts['repair_batch'])]
            for generation, keys in guards.items():
                work += [(generation + 1, chunk)
                         for chunk in _chunks(keys, opts['chunk'])]
            for index, (generation, keys) in enumerate(work):
                stop = self._valve(opts)
                if stop:
                    # What is left is checked first by the next run.
                    valves.append(stop)
                    for _generation, rest in work[index:]:
                        pending += rest
                    break
                self._repair(keys, generation)
        summary['valve'] = ','.join(dict.fromkeys(valves)) or None
        summary['pending'] = pending[:PENDING_KEYS]
        summary['pending_dropped'] = max(0, len(pending) - PENDING_KEYS)
        summary['unsettled'] = list(dict.fromkeys(self.unsettled))[:20]
        summary.update(self.counts)
        summary['seconds'] = round(self._elapsed(), 1)
        if summary['mode'] == 'scheduled':
            summary['cursors'] = dict(self.cursors)
        if not opts['dry_run']:
            DBEvent.objects.create(kind=SWEEP_EVENT, payload={
                key: value for key, value in summary.items()
                if key != 'dry_run'})
        return summary

    def _print(self, summary):
        rotation = summary.get('rotation') or {}
        self.stdout.write(
            'sweep_stale_backed ({mode}{dry}): checked={checked} '
            'stale={stale} confirmed={confirmed} changed={changed} '
            'ascents={ascents} cut={cut} valve={valve} '
            'seconds={seconds}'.format(
                mode=summary.get('mode'),
                dry=', dry run' if summary.get('dry_run') else '',
                checked=summary.get('checked', 0),
                stale=summary.get('stale', 0),
                confirmed=summary.get('confirmed', 0),
                changed=summary.get('changed', 0),
                ascents=summary.get('ascents', 0),
                cut=summary.get('cut', 0),
                valve=summary.get('valve'),
                seconds=summary.get('seconds')))
        if rotation:
            self.stdout.write(
                '  rotation {unit} {from}-{to} of {total}: {candidates} '
                'candidates'.format(**rotation))
        for name in ('recent', 'guards', 'kinds'):
            if summary.get(name):
                self.stdout.write('  %s %s' % (
                    name, json.dumps(summary[name], sort_keys=True)))
        for example in summary.get('examples') or ():
            self.stdout.write('  ' + json.dumps(example, sort_keys=True))
