"""The start position and its first moves over time (``roots``, 0050).

Community request: keep the best and the worst value the start position and
each first move have ever had, from White's side and with the time, catching
every change ("the value should be logged every time it changes"), and log
the main line from the start position so its evolution can be followed.

On PostgreSQL a row trigger keeps the extremes, and the tests marked so run
against it in the PostgreSQL CI job.  Everywhere else the hourly capture
folds the values in, and the fold is tested on both engines (on PostgreSQL
with the trigger switched off for the test).
"""

import importlib
import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import StringIO
from types import SimpleNamespace
from unittest import mock, skipUnless

from django.apps import apps as django_apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase

from . import ingest, logic, roots, views
from .database import atomic, connection
from .models import Edge, MainLineSnapshot, Position, RootExtreme
from .testing import TestCase

UTC = datetime_timezone.utc
MIGRATION = importlib.import_module('atomicdb.migrations.0050_root_extremes')
POSTGRES = connection.vendor == 'postgresql'
T0 = datetime(2026, 10, 26, 10, 7, tzinfo=UTC)


def _trigger_off():
    """Switch the trigger off for this test (rolled back with it).

    Called before the test writes anything: PostgreSQL refuses to alter a
    table that still has trigger events pending in the transaction.
    """
    if POSTGRES:
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE atomicdb_position '
                           f'DISABLE TRIGGER {roots.TRIGGER}')


def _set(pos, **fields):
    Position.objects.filter(key=pos.key).update(**fields)


def _child(parent, uci):
    return Edge.objects.get(parent=parent, move_uci=uci).child


def _row(pos):
    return RootExtreme.objects.get(key=pos.key)


def _summary(row):
    return (row.best_value, row.best_status, row.worst_value,
            row.worst_status, row.last_value, row.changes)


class WatchedKeysTests(SimpleTestCase):

    def test_the_trigger_watches_the_keys_the_tree_computes(self):
        """The migration spells the keys out; the move generator derives
        them.  If the canonical identity ever moves, this is what says so."""
        self.assertEqual(list(MIGRATION.WATCHED_KEYS), roots.watched_keys())

    def test_the_start_position_and_its_twenty_first_moves(self):
        watched = roots.watched_positions()
        self.assertEqual(len(watched), 21)
        self.assertEqual(watched[0], ('', '', ingest.root_key()))
        self.assertEqual(MIGRATION.START_KEY, ingest.root_key())
        self.assertEqual(
            sorted(san for _uci, san, _key in watched[1:]),
            sorted(['a3', 'a4', 'Na3', 'Nc3', 'b3', 'b4', 'c3', 'c4', 'd3',
                    'd4', 'e3', 'e4', 'f3', 'f4', 'Nf3', 'Nh3', 'g3', 'g4',
                    'h3', 'h4']))

    def test_names_shared_with_the_migration_and_the_explorer(self):
        self.assertEqual(roots.TRIGGER, MIGRATION.TRIGGER)
        self.assertEqual(roots.PLAY_LINK_MAX_PLIES,
                         views.PLAY_ROUTE_MAX_PLIES)


class ValueTests(SimpleTestCase):

    CASES = [('WHITE_WIN', -50, 30), ('BLACK_WIN', 50, 30), ('DRAW', 50, 30),
             ('UNKNOWN', 1243, 545), ('UNKNOWN', None, 545),
             ('UNKNOWN', None, None), ('UNKNOWN', 0, 545)]

    def test_the_value_is_the_one_the_explorer_headlines(self):
        self.assertEqual(
            [roots.value_of(*case) for case in self.CASES],
            [10_000, -10_000, 0, 1243, 545, None, 0])

    def test_the_migration_keeps_the_same_precedence(self):
        for case in self.CASES:
            self.assertEqual(MIGRATION._value(*case), roots.value_of(*case))

    def test_values_read_in_pawns_from_whites_side(self):
        self.assertEqual(roots.value_text(1243), '+12.43')
        self.assertEqual(roots.value_text(-5), '−0.05')
        self.assertEqual(roots.value_text(0), '0.00')
        self.assertEqual(roots.value_text(None), '—')
        self.assertEqual(roots.value_text(10_000, 'WHITE_WIN'), 'White win')
        self.assertEqual(roots.value_text(-10_000, 'BLACK_WIN'), 'Black win')
        self.assertEqual(roots.value_text(0, 'DRAW'), 'Draw')

    def test_the_line_is_numbered_from_the_start_position(self):
        self.assertEqual(roots.numbered(['Nf3', 'f6', 'Nc3']),
                         '1. Nf3 f6 2. Nc3')


class FoldTests(TestCase):
    """The capture's fold, for a database without the trigger."""

    def setUp(self):
        _trigger_off()
        self.root = ingest.get_or_create_position(logic.start_fen())

    def test_the_first_value_starts_the_row_and_is_not_a_change(self):
        _set(self.root, eval_cp=545)

        self.assertEqual(roots.fold_extremes(now=T0), (1, 0))

        row = _row(self.root)
        self.assertEqual(_summary(row),
                         (545, 'UNKNOWN', 545, 'UNKNOWN', 545, 0))
        self.assertEqual((row.since, row.best_at, row.worst_at), (T0,) * 3)

    def test_best_and_worst_keep_the_moment_they_were_first_reached(self):
        t = [T0 + timedelta(hours=hour) for hour in range(5)]
        _set(self.root, eval_cp=545)
        roots.fold_extremes(now=t[0])
        _set(self.root, backed_eval=1243)       # the backed value headlines
        roots.fold_extremes(now=t[1])
        _set(self.root, backed_eval=None, eval_cp=300)
        roots.fold_extremes(now=t[2])
        _set(self.root, backed_eval=1243)       # the best again, not a new one
        roots.fold_extremes(now=t[3])
        roots.fold_extremes(now=t[4])           # nothing moved

        row = _row(self.root)
        self.assertEqual(_summary(row),
                         (1243, 'UNKNOWN', 300, 'UNKNOWN', 1243, 3))
        self.assertEqual((row.best_at, row.worst_at, row.changed_at),
                         (t[1], t[2], t[3]))
        self.assertEqual(row.since, t[0])

    def test_a_position_with_no_value_has_no_row_yet(self):
        self.assertEqual(roots.fold_extremes(now=T0), (0, 0))
        self.assertFalse(RootExtreme.objects.exists())

    def test_a_proven_status_counts_as_its_value(self):
        _set(self.root, eval_cp=545)
        roots.fold_extremes(now=T0)
        _set(self.root, status='WHITE_WIN', closure='MINIMAX')
        roots.fold_extremes(now=T0 + timedelta(hours=1))

        row = _row(self.root)
        self.assertEqual((row.best_value, row.best_status), (10_000,
                                                             'WHITE_WIN'))
        self.assertEqual((row.worst_value, row.worst_status), (545,
                                                               'UNKNOWN'))

    def test_only_the_watched_positions_are_recorded(self):
        ingest.expand(self.root)
        e4 = _child(self.root, 'e2e4')
        ingest.expand(e4)
        _set(e4, eval_cp=185)
        _set(_child(e4, 'e7e5'), eval_cp=200)

        roots.fold_extremes(now=T0)

        self.assertEqual(list(RootExtreme.objects.values_list('key',
                                                              flat=True)),
                         [e4.key])


class MigrationSeedTests(TestCase):

    def setUp(self):
        _trigger_off()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)

    def _editor(self):
        return SimpleNamespace(connection=SimpleNamespace(
            alias=settings.ATOMICDB_DATABASE_ALIAS, vendor='sqlite'))

    def test_the_rows_start_at_todays_values(self):
        _set(self.root, eval_cp=545, backed_eval=1243)
        _set(_child(self.root, 'e2e4'), eval_cp=185)

        MIGRATION.seed(django_apps, self._editor())

        rows = {row.key: row for row in RootExtreme.objects.all()}
        self.assertEqual(set(rows), {self.root.key,
                                     _child(self.root, 'e2e4').key})
        root = rows[self.root.key]
        self.assertEqual(_summary(root),
                         (1243, 'UNKNOWN', 1243, 'UNKNOWN', 1243, 0))
        self.assertEqual(root.since, root.best_at)

    def test_without_postgresql_the_migration_only_seeds(self):
        editor = self._editor()
        editor.execute = self.fail     # no SQL of its own on SQLite
        _set(self.root, eval_cp=545)

        MIGRATION.install(django_apps, editor)
        MIGRATION.uninstall(django_apps, editor)

        self.assertTrue(RootExtreme.objects.filter(key=self.root.key)
                        .exists())


@skipUnless(POSTGRES, 'the trigger exists on PostgreSQL only')
class TriggerTests(TestCase):
    """Every change, through every write path, as it happens."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.e4 = _child(self.root, 'e2e4')

    def test_the_trigger_is_installed_and_names_the_watch_list(self):
        self.assertTrue(roots.trigger_installed())
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_get_triggerdef(oid) FROM pg_trigger '
                           'WHERE tgname = %s', [roots.TRIGGER])
            definition = cursor.fetchone()[0]
        self.assertIn('AFTER UPDATE OF', definition)
        for column in ('status', 'backed_eval', 'eval_cp'):
            self.assertIn(column, definition)
        for key in roots.watched_keys():
            self.assertIn(key, definition)

    def test_every_change_is_caught_not_a_sample(self):
        _set(self.e4, eval_cp=185)          # the first value starts the row
        _set(self.e4, backed_eval=401)
        _set(self.e4, backed_eval=120)
        _set(self.e4, backed_eval=300)

        self.assertEqual(_summary(_row(self.e4)),
                         (401, 'UNKNOWN', 120, 'UNKNOWN', 300, 3))

    def test_the_cascades_bulk_update_is_seen(self):
        """No signal fires for these writes; the reason it is a trigger."""
        rows = [Position(key=self.root.key, backed_eval=1243,
                         backed_move='g1f3', backed_plies=20, backed_nodes=1),
                Position(key=self.e4.key, backed_eval=401,
                         backed_move='d7d5', backed_plies=3, backed_nodes=1)]
        Position.objects.bulk_update(rows, ingest._BACKED_FIELDS)

        self.assertEqual(_row(self.root).last_value, 1243)
        self.assertEqual(_row(self.e4).last_value, 401)

    def test_a_write_that_leaves_the_headline_alone_is_not_a_change(self):
        _set(self.e4, eval_cp=30)
        pos = Position.objects.get(key=self.e4.key)
        pos.visits += 1
        pos.save()                          # rewrites eval_cp=30 as it was
        _set(self.e4, backed_eval=50)       # 30 -> 50: a change
        _set(self.e4, eval_cp=99)           # the headline is still 50

        row = _row(self.e4)
        self.assertEqual((row.last_value, row.changes), (50, 1))

    def test_a_proven_status_counts_as_its_value(self):
        _set(self.e4, eval_cp=185)
        _set(self.e4, status='WHITE_WIN', closure='MINIMAX')

        row = _row(self.e4)
        self.assertEqual((row.best_value, row.best_status, row.last_status),
                         (10_000, 'WHITE_WIN', 'WHITE_WIN'))

    def test_unwatched_positions_are_left_alone(self):
        ingest.expand(self.e4)
        _set(_child(self.e4, 'e7e5'), eval_cp=200, backed_eval=210)

        self.assertFalse(RootExtreme.objects.exists())

    def test_a_rolled_back_write_leaves_no_trace(self):
        try:
            with atomic():
                _set(self.e4, eval_cp=777)
                raise RuntimeError('abandon')
        except RuntimeError:
            pass

        self.assertFalse(RootExtreme.objects.exists())

    def test_the_trigger_and_the_fold_agree(self):
        """The same sequence through both keepers ends in the same row."""
        steps = [dict(eval_cp=10), dict(backed_eval=60), dict(eval_cp=5),
                 dict(backed_eval=-40), dict(backed_eval=-40),
                 dict(backed_eval=None), dict(status='DRAW'),
                 dict(status='UNKNOWN', backed_eval=75)]
        mirror = None
        at = T0
        for step in steps:
            _set(self.e4, **step)
            status, backed_eval, eval_cp = Position.objects.filter(
                key=self.e4.key).values_list(
                    'status', 'backed_eval', 'eval_cp').get()
            value = roots.value_of(status, backed_eval, eval_cp)
            if mirror is None:
                mirror = roots.start_row(self.e4.key, value, status, at)
            else:
                roots.observe(mirror, value, status, at)

        self.assertEqual(_summary(_row(self.e4)), _summary(mirror))


@skipUnless(POSTGRES, 'the trigger exists on PostgreSQL only')
class TriggerMigrationTests(TestCase):

    def test_the_migration_reverses_and_replays(self):
        with connection.schema_editor() as editor:
            MIGRATION.uninstall(django_apps, editor)
        self.assertFalse(roots.trigger_installed())

        with connection.schema_editor() as editor:
            MIGRATION.install(django_apps, editor)
            MIGRATION.install(django_apps, editor)   # and again: no error
        self.assertTrue(roots.trigger_installed())

    def test_with_the_trigger_the_capture_leaves_the_extremes_to_it(self):
        _snapshot, _created, folded = roots.capture(now=T0)
        self.assertIsNone(folded)


class MainLineTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.nf3 = _child(self.root, 'g1f3')
        ingest.expand(self.nf3)
        self.f6 = _child(self.nf3, 'f7f6')

    def test_the_line_follows_the_backed_move_then_the_best_move(self):
        _set(self.root, eval_cp=545, backed_eval=1243, backed_move='g1f3',
             best_move='e2e4')
        _set(self.nf3, eval_cp=541, best_move='f7f6')
        _set(self.f6, eval_cp=486)

        line = roots.walk_main_line()

        self.assertEqual(line['moves'], 'g1f3 f7f6')
        self.assertEqual(line['san'], 'Nf3 f6')
        self.assertEqual((line['plies'], line['value'], line['status']),
                         (2, 1243, 'UNKNOWN'))
        self.assertEqual((line['end_key'], line['end_value'],
                          line['end_reason']),
                         (self.f6.key, 486, MainLineSnapshot.End.NO_MOVE))

    def test_a_proven_position_ends_the_line_and_belongs_to_it(self):
        _set(self.root, best_move='g1f3')
        _set(self.nf3, status='WHITE_WIN', closure='MINIMAX',
             best_move='f7f6')

        line = roots.walk_main_line()

        self.assertEqual((line['moves'], line['end_key'], line['end_status'],
                          line['end_value'], line['end_reason']),
                         ('g1f3', self.nf3.key, 'WHITE_WIN', 10_000,
                          MainLineSnapshot.End.PROVEN))

    def test_a_move_the_tree_has_no_edge_for_ends_the_line(self):
        _set(self.root, best_move='g1f3')
        _set(self.nf3, best_move='f7f6')
        _set(self.f6, best_move='b1c3')     # f6 was never expanded

        line = roots.walk_main_line()

        self.assertEqual((line['plies'], line['end_reason']),
                         (2, MainLineSnapshot.End.NO_EDGE))

    def test_the_ply_limit_ends_the_line(self):
        _set(self.root, best_move='g1f3')
        _set(self.nf3, best_move='f7f6')

        line = roots.walk_main_line(max_plies=1)

        self.assertEqual((line['moves'], line['end_reason']),
                         ('g1f3', MainLineSnapshot.End.LIMIT))

    def test_the_line_stops_before_it_repeats_a_position(self):
        nf6 = _child(self.nf3, 'g8f6')
        ingest.expand(nf6)
        back = _child(nf6, 'f3g1')
        ingest.expand(back)
        _set(self.root, best_move='g1f3')
        _set(self.nf3, best_move='g8f6')
        _set(nf6, best_move='f3g1')
        _set(back, best_move='f6g8')        # would be the start position

        line = roots.walk_main_line()

        self.assertEqual((line['moves'], line['end_key'],
                          line['end_reason']),
                         ('g1f3 g8f6 f3g1', back.key,
                          MainLineSnapshot.End.REPEAT))

    def test_one_recording_per_utc_hour_and_never_rewritten(self):
        _set(self.root, eval_cp=545, best_move='g1f3')

        first, created = roots.capture_main_line(now=T0)
        self.assertTrue(created)
        self.assertEqual(first.bucket_start,
                         T0.replace(minute=0, second=0, microsecond=0))
        _set(self.root, eval_cp=900)
        again, created = roots.capture_main_line(now=T0 + timedelta(
            minutes=40))
        self.assertFalse(created)
        self.assertEqual((again.pk, again.value), (first.pk, 545))
        later, created = roots.capture_main_line(now=T0 + timedelta(hours=1))
        self.assertTrue(created)
        self.assertEqual(later.value, 900)

        first.plies = 99
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            first.save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            first.delete()


class CaptureCommandTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        _set(self.root, eval_cp=545, backed_eval=1243, backed_move='g1f3')

    def _run(self, *args):
        output = StringIO()
        call_command('capture_atomicdb_progress', *args, stdout=output)
        return output.getvalue()

    @mock.patch(
        'atomicdb.management.commands.capture_atomicdb_progress.timezone.now')
    def test_the_hourly_run_records_the_main_line_beside_the_counters(
            self, now):
        now.return_value = T0
        first = json.loads(self._run('--json'))
        second = json.loads(self._run('--json'))

        self.assertTrue(first['main_line']['created'])
        self.assertFalse(second['main_line']['created'])
        self.assertEqual(first['main_line']['moves'], 'g1f3')
        self.assertEqual(first['main_line']['value'], 1243)
        self.assertEqual(MainLineSnapshot.objects.count(), 1)
        kept_by = 'trigger' if POSTGRES else 'capture'
        self.assertEqual(first['extremes']['kept_by'], kept_by)
        # Kept one way or the other, the row is there.
        self.assertEqual(_row(self.root).last_value, 1243)

    def test_the_summary_line_mentions_the_main_line(self):
        self.assertIn('main line: created', self._run())

    def test_the_preview_writes_nothing(self):
        output = self._run('--preview')

        self.assertIn('"main_line"', output)
        self.assertFalse(MainLineSnapshot.objects.exists())
        if not POSTGRES:
            self.assertFalse(RootExtreme.objects.exists())


class RootsPageTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.nf3 = _child(self.root, 'g1f3')
        ingest.expand(self.nf3)
        self.f6 = _child(self.nf3, 'f7f6')

    def _page(self):
        response = self.client.get('/atomicdb/roots/')
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_an_empty_record_says_so(self):
        body = self._page()

        self.assertIn('Start position', body)
        self.assertIn('1. Nf3', body)
        self.assertEqual(body.count('Nothing recorded yet.'), 2)

    def test_the_table_quotes_now_best_and_worst_from_whites_side(self):
        best_at = datetime(2026, 10, 20, 8, 14, tzinfo=UTC)
        worst_at = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)
        _set(self.root, eval_cp=545, backed_eval=1243)
        _set(self.f6, eval_cp=486)
        RootExtreme.objects.update_or_create(key=self.root.key, defaults=dict(
            best_value=1310, best_status='UNKNOWN', best_at=best_at,
            worst_value=310, worst_status='UNKNOWN', worst_at=worst_at,
            last_value=1243, last_status='UNKNOWN', changed_at=best_at,
            changes=7, since=worst_at))

        body = self._page()

        self.assertIn('+12.43', body)        # now: the backed value
        self.assertIn('+13.10', body)
        self.assertIn('2026-10-20 08:14', body)
        self.assertIn('+3.10', body)
        self.assertIn('2026-07-28 09:00', body)
        self.assertIn('since 2026-07-28', body)

    def test_the_main_line_and_its_history_by_day(self):
        _set(self.root, eval_cp=545, backed_eval=1243, backed_move='g1f3')
        _set(self.nf3, best_move='f7f6')
        day_one = datetime(2026, 10, 25, 10, 5, tzinfo=UTC)
        roots.capture_main_line(now=day_one)
        _set(self.root, backed_eval=1100)
        roots.capture_main_line(now=day_one + timedelta(hours=5))
        _set(self.root, backed_eval=1219)
        latest, _created = roots.capture_main_line(
            now=day_one + timedelta(hours=23))

        context = roots.page_context(now=latest.bucket_start)
        self.assertEqual([(row['day'].isoformat(), row['value_h'])
                          for row in context['history']],
                         [('2026-10-26', '+12.19'), ('2026-10-25', '+11.00')])

        body = self._page()
        self.assertIn('1. Nf3 f6', body)
        self.assertIn('2 plies', body)
        self.assertIn('ends at a position not analysed yet', body)
        self.assertIn(f'/atomicdb/explore/{self.f6.key}/?play=g1f3,f7f6',
                      body)

    def test_the_page_costs_a_fixed_handful_of_queries(self):
        _set(self.root, eval_cp=545, backed_move='g1f3')
        roots.capture(now=T0)
        roots.capture(now=T0 + timedelta(days=1))
        self.assertTrue(RootExtreme.objects.exists())

        with self.assertNumQueries(
                5, using=settings.ATOMICDB_DATABASE_ALIAS):
            roots.page_context(now=T0 + timedelta(days=1))

    def test_the_front_page_links_here(self):
        body = self.client.get('/atomicdb/').content.decode()
        self.assertIn('href="/atomicdb/roots/"', body)
