"""The start position, first moves and named openings over time (``roots``).

Community requests: keep the best and the worst value the start position and
each first move have ever had, from White's side and with the time, catching
every change ("the value should be logged every time it changes"), and log
the main line from the start position so its evolution can be followed
(migration 0050); then follow the three main replies to 1.Nf3 and every
position the opening catalogue names the same way (migration 0051); then
every position a community name is approved for, from its approval on
(migration 0052: the watch list becomes a table).

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

from OpenBench.models import Profile

from . import community_names, ingest, logic, openings, roots, views
from .database import atomic, connection
from .models import (Edge, MainLineSnapshot, OpeningNameSuggestion, Position,
                     RootExtreme, WatchedKey)
from .testing import TestCase, worker_account

UTC = datetime_timezone.utc
MIGRATION = importlib.import_module('atomicdb.migrations.0050_root_extremes')
OPENINGS = importlib.import_module('atomicdb.migrations.0051_watch_openings')
WATCHED = importlib.import_module('atomicdb.migrations.0052_watched_keys')
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


def _unwatched_child(parent):
    """A child of ``parent`` that nothing watches (expanding it first)."""
    ingest.expand(parent)
    watched = set(roots.watched_keys())
    for edge in (Edge.objects.filter(parent=parent).select_related('child')
                 .order_by('move_uci')):
        if edge.child.key not in watched:
            return edge.child
    raise AssertionError('every child is watched')


def _catalogue_positions(count=1):
    """Put ``count`` catalogue-only openings in the tree; ``[(pos, row)]``."""
    first = {row.key for row in roots.first_table()}
    catalogue = openings.load_catalog()
    made = []
    for row in roots.named_openings():
        if row.key in first:
            continue
        made.append((ingest.get_or_create_position(catalogue[row.key].fen),
                     row))
        if len(made) == count:
            return made
    raise AssertionError('not enough catalogue openings')


def _row(pos):
    return RootExtreme.objects.get(key=pos.key)


def _summary(row):
    return (row.best_value, row.best_status, row.worst_value,
            row.worst_status, row.last_value, row.changes)


def _trigger_definition():
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_get_triggerdef(oid) FROM pg_trigger '
                       'WHERE tgname = %s', [roots.TRIGGER])
        return cursor.fetchone()[0]


def _watch_function_exists():
    with connection.cursor() as cursor:
        cursor.execute('SELECT to_regprocedure(%s) IS NOT NULL',
                       [f'{roots.WATCH_FUNCTION}(text)'])
        return cursor.fetchone()[0]


def _watch_function_source():
    with connection.cursor() as cursor:
        cursor.execute('SELECT prosrc, provolatile FROM pg_proc '
                       'WHERE oid = to_regprocedure(%s)',
                       [f'{roots.WATCH_FUNCTION}(text)'])
        return cursor.fetchone()


def _is_watched(key):
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT {roots.WATCH_FUNCTION}(%s)', [key])
        return cursor.fetchone()[0]


def _approver(name='belzedar'):
    user = worker_account(name)
    Profile.objects.filter(user=user).update(approver=True)
    return user


def _suggest(pos, name, kind=OpeningNameSuggestion.SKind.NEW, **fields):
    return OpeningNameSuggestion.objects.create(
        position=pos, proposed_name=name, kind=kind, ip='127.0.0.1',
        **fields)


class WatchedKeysTests(SimpleTestCase):

    def test_the_trigger_watches_the_keys_the_tree_computes(self):
        """The migrations spell the keys out; the move generator and the
        catalogue derive them.  If either moves, this is what says so."""
        self.assertEqual(list(OPENINGS.WATCHED_KEYS), roots.standing_keys())
        self.assertEqual(
            list(MIGRATION.WATCHED_KEYS),
            [row.key for row in roots.first_table()
             if row.kind in ('start', 'move')])

    def test_0052_gives_every_standing_key_the_source_the_code_does(self):
        self.assertEqual(WATCHED._standing_sources(),
                         {key: str(source) for key, source
                          in roots.standing_sources().items()})
        counts = {}
        for source in roots.standing_sources().values():
            counts[source] = counts.get(source, 0) + 1
        self.assertEqual(counts, {'root': 21, 'reply': 3, 'catalog': 341})

    def test_the_first_table_is_the_start_moves_and_three_replies(self):
        table = roots.first_table()
        self.assertEqual(len(table), 24)
        self.assertEqual((table[0].key, table[0].label),
                         (ingest.root_key(), 'Start position'))
        self.assertEqual(MIGRATION.START_KEY, ingest.root_key())
        self.assertEqual(
            sorted(row.label for row in table if row.kind == 'move'),
            sorted(f'1. {san}' for san in (
                'a3', 'a4', 'Na3', 'Nc3', 'b3', 'b4', 'c3', 'c4', 'd3', 'd4',
                'e3', 'e4', 'f3', 'f4', 'Nf3', 'Nh3', 'g3', 'g4', 'h3', 'h4')))
        nf3 = next(index for index, row in enumerate(table)
                   if row.label == '1. Nf3')
        replies = table[nf3 + 1:nf3 + 4]
        self.assertEqual([(row.kind, row.label, row.line, row.parent)
                          for row in replies],
                         [('reply', '1... f6', ('g1f3', 'f7f6'), 'g1f3'),
                          ('reply', '1... e5', ('g1f3', 'e7e5'), 'g1f3'),
                          ('reply', '1... d6', ('g1f3', 'd7d6'), 'g1f3')])
        for row in replies:
            self.assertEqual(
                row.key,
                logic.key_of(logic.apply_move(
                    logic.apply_move(logic.start_fen(), 'g1f3'),
                    row.line[1])))

    def test_every_catalogue_opening_is_watched_once(self):
        catalogue = openings.load_catalog()
        keys = roots.standing_keys()
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(roots.named_openings()), len(catalogue))
        self.assertTrue(set(catalogue) <= set(keys))
        first = {row.key for row in roots.first_table()}
        self.assertEqual(len(keys), len(first | set(catalogue)))

    def test_names_shared_with_the_migrations_and_the_explorer(self):
        self.assertEqual(roots.TRIGGER, MIGRATION.TRIGGER)
        self.assertEqual(roots.TRIGGER, OPENINGS.TRIGGER)
        self.assertEqual(roots.WATCH_FUNCTION, OPENINGS.WATCH_FUNCTION)
        self.assertIn(f'FUNCTION {roots.WATCH_FUNCTION}(',
                      WATCHED.WATCH_FUNCTION)
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
        _set(e4, eval_cp=185)
        _set(_unwatched_child(e4), eval_cp=200)
        (opening, _row_), = _catalogue_positions()
        _set(opening, eval_cp=321)

        roots.fold_extremes(now=T0)

        self.assertEqual(
            set(RootExtreme.objects.values_list('key', flat=True)),
            {e4.key, opening.key})

    def test_a_community_named_position_is_folded_too(self):
        ingest.expand(self.root)
        pos = _unwatched_child(_child(self.root, 'e2e4'))
        _set(pos, eval_cp=150)
        _suggest(pos, 'Rhaxier Gambit',
                 status=OpeningNameSuggestion.SState.APPROVED)
        _set(pos, eval_cp=90)

        roots.fold_extremes(now=T0)

        self.assertEqual(_summary(_row(pos)),
                         (150, 'UNKNOWN', 90, 'UNKNOWN', 90, 1))


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

    def test_0051_seeds_the_new_rows_and_keeps_the_history_of_the_old(self):
        RootExtreme.objects.create(
            key=self.root.key, best_value=1310, best_status='UNKNOWN',
            best_at=T0, worst_value=310, worst_status='UNKNOWN', worst_at=T0,
            last_value=1243, last_status='UNKNOWN', changed_at=T0, changes=7,
            since=T0)
        _set(self.root, eval_cp=545)
        nf3 = _child(self.root, 'g1f3')
        ingest.expand(nf3)
        d6 = _child(nf3, 'd7d6')
        _set(d6, backed_eval=444)
        (opening, _watched), = _catalogue_positions()
        _set(opening, eval_cp=321)

        OPENINGS.seed(django_apps, self._editor())

        self.assertEqual(_summary(_row(self.root)),
                         (1310, 'UNKNOWN', 310, 'UNKNOWN', 1243, 7))
        self.assertEqual(_summary(_row(d6)),
                         (444, 'UNKNOWN', 444, 'UNKNOWN', 444, 0))
        self.assertEqual(_row(opening).last_value, 321)

    def test_0051_on_sqlite_seeds_and_its_reverse_keeps_the_0050_rows(self):
        editor = self._editor()
        editor.execute = self.fail
        _set(self.root, eval_cp=545)
        (opening, _watched), = _catalogue_positions()
        _set(opening, eval_cp=321)

        OPENINGS.install(django_apps, editor)
        self.assertEqual(RootExtreme.objects.count(), 2)
        OPENINGS.uninstall(django_apps, editor)

        self.assertEqual(list(RootExtreme.objects.values_list('key',
                                                              flat=True)),
                         [self.root.key])

    def _before_0052(self, value=150):
        """A tree as 0052 finds it: an approved name, no watch list."""
        pos = _unwatched_child(_child(self.root, 'e2e4'))
        _set(pos, eval_cp=value)
        _suggest(pos, 'Rhaxier Gambit',
                 status=OpeningNameSuggestion.SState.APPROVED)
        WatchedKey.objects.all().delete()
        RootExtreme.objects.all().delete()
        return pos

    def test_0052_lists_the_standing_keys_and_the_approved_names(self):
        pos = self._before_0052()
        _set(self.root, eval_cp=545)
        editor = self._editor()
        editor.execute = self.fail

        WATCHED.install(django_apps, editor)

        sources = dict(WatchedKey.objects.values_list('key', 'source'))
        self.assertEqual(len(sources), 365 + 1)
        nf3_f6 = next(row.key for row in roots.first_table()
                      if row.label == '1... f6')
        catalogue_only = next(row.key for row in roots.named_openings()
                              if row.key not in roots.standing_keys()[:24])
        self.assertEqual(
            [sources[key] for key in (self.root.key, nf3_f6, catalogue_only,
                                      pos.key)],
            ['root', 'reply', 'catalog', 'community'])
        # Only the newly watched position is seeded: the others already had
        # their rows from 0050 and 0051 (here, none: their values are new).
        self.assertEqual(list(RootExtreme.objects.values_list('key',
                                                              flat=True)),
                         [pos.key])
        self.assertEqual(_row(pos).last_value, 150)

    def test_0052_on_sqlite_reverses_to_what_0051_watches(self):
        pos = self._before_0052()
        editor = self._editor()
        editor.execute = self.fail
        WATCHED.install(django_apps, editor)
        RootExtreme.objects.create(
            key=self.root.key, best_value=1, best_status='UNKNOWN',
            best_at=T0, worst_value=1, worst_status='UNKNOWN', worst_at=T0,
            last_value=1, last_status='UNKNOWN', changed_at=T0, since=T0)

        WATCHED.uninstall(django_apps, editor)

        self.assertEqual(list(RootExtreme.objects.values_list('key',
                                                              flat=True)),
                         [self.root.key])
        self.assertFalse(RootExtreme.objects.filter(key=pos.key).exists())


@skipUnless(POSTGRES, 'the trigger exists on PostgreSQL only')
class TriggerTests(TestCase):
    """Every change, through every write path, as it happens."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.e4 = _child(self.root, 'e2e4')

    def test_the_trigger_asks_the_watch_function(self):
        self.assertTrue(roots.trigger_installed())
        definition = _trigger_definition()
        self.assertIn('AFTER UPDATE OF', definition)
        for column in ('status', 'backed_eval', 'eval_cp'):
            self.assertIn(column, definition)
        self.assertIn(f'{roots.WATCH_FUNCTION}(', definition)
        # The list lives in the function, not in the WHEN that PostgreSQL
        # rebuilds for every statement.
        self.assertLess(len(definition), 1000)

    def test_the_watch_function_reads_the_watch_list(self):
        keys = roots.watched_keys()
        self.assertGreaterEqual(len(keys), 365)
        with connection.cursor() as cursor:
            cursor.execute(
                f'SELECT count(*) FILTER (WHERE {roots.WATCH_FUNCTION}(k)),'
                ' count(*) FROM unnest(%s::text[]) AS k', [keys])
            self.assertEqual(cursor.fetchone(), (len(keys), len(keys)))
        self.assertFalse(_is_watched('f' * 64))
        source, volatility = _watch_function_source()
        self.assertIn('atomicdb_watchedkey', source)
        # VOLATILE: a fresh snapshot per call, so an UPDATE that waited for
        # an approval sees the key that approval just added.
        self.assertEqual(volatility, 'v')

    def test_a_key_added_to_the_list_is_watched_from_that_moment(self):
        pos = _unwatched_child(self.e4)
        _set(pos, eval_cp=200)
        self.assertFalse(RootExtreme.objects.filter(key=pos.key).exists())

        WatchedKey.objects.create(key=pos.key,
                                  source=WatchedKey.Source.COMMUNITY)
        self.assertTrue(_is_watched(pos.key))
        _set(pos, eval_cp=230)

        self.assertEqual(_summary(_row(pos)),
                         (230, 'UNKNOWN', 230, 'UNKNOWN', 230, 0))

    def test_every_change_is_caught_not_a_sample(self):
        _set(self.e4, eval_cp=185)          # the first value starts the row
        _set(self.e4, backed_eval=401)
        _set(self.e4, backed_eval=120)
        _set(self.e4, backed_eval=300)

        self.assertEqual(_summary(_row(self.e4)),
                         (401, 'UNKNOWN', 120, 'UNKNOWN', 300, 3))

    def test_a_reply_to_nf3_and_a_named_opening_are_watched(self):
        nf3 = _child(self.root, 'g1f3')
        ingest.expand(nf3)
        d6 = _child(nf3, 'd7d6')
        _set(d6, backed_eval=444)
        (opening, _watched), = _catalogue_positions()
        _set(opening, eval_cp=321)
        _set(opening, eval_cp=280)

        self.assertEqual(_row(d6).last_value, 444)
        self.assertEqual(_summary(_row(opening)),
                         (321, 'UNKNOWN', 280, 'UNKNOWN', 280, 1))

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
        _set(_unwatched_child(self.e4), eval_cp=200, backed_eval=210)

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

    def test_0051_reverses_to_0050_and_replays(self):
        root = ingest.get_or_create_position(logic.start_fen())
        for key in (root.key, OPENINGS.WATCHED_KEYS[-1]):
            RootExtreme.objects.create(
                key=key, best_value=1, best_status='UNKNOWN', best_at=T0,
                worst_value=1, worst_status='UNKNOWN', worst_at=T0,
                last_value=1, last_status='UNKNOWN', changed_at=T0, since=T0)

        with connection.schema_editor() as editor:
            OPENINGS.uninstall(django_apps, editor)
        definition = _trigger_definition()
        self.assertIn(MIGRATION.START_KEY, definition)    # 0050's own WHEN
        self.assertNotIn(roots.WATCH_FUNCTION, definition)
        self.assertFalse(_watch_function_exists())
        self.assertEqual(list(RootExtreme.objects.values_list('key',
                                                              flat=True)),
                         [root.key])

        with connection.schema_editor() as editor:
            OPENINGS.install(django_apps, editor)
            OPENINGS.install(django_apps, editor)   # and again: no error
        self.assertIn(roots.WATCH_FUNCTION, _trigger_definition())
        self.assertTrue(_watch_function_exists())
        self.assertTrue(roots.trigger_installed())

    def test_0050_still_reverses_and_replays(self):
        with connection.schema_editor() as editor:
            MIGRATION.uninstall(django_apps, editor)
        self.assertFalse(roots.trigger_installed())

        with connection.schema_editor() as editor:
            MIGRATION.install(django_apps, editor)
            MIGRATION.install(django_apps, editor)   # and again: no error
        self.assertTrue(roots.trigger_installed())

    def test_0052_reverses_to_0051_and_replays(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        pos = _unwatched_child(_child(root, 'e2e4'))
        _set(pos, eval_cp=150)
        _suggest(pos, 'Rhaxier Gambit',
                 status=OpeningNameSuggestion.SState.APPROVED)
        self.assertTrue(RootExtreme.objects.filter(key=pos.key).exists())

        with connection.schema_editor() as editor:
            WATCHED.uninstall(django_apps, editor)
        source, volatility = _watch_function_source()
        self.assertIn(MIGRATION.START_KEY, source)        # 0051's constant
        self.assertEqual(volatility, 's')
        self.assertFalse(_is_watched(pos.key))
        self.assertFalse(RootExtreme.objects.filter(key=pos.key).exists())

        with connection.schema_editor() as editor:
            WATCHED.install(django_apps, editor)
            WATCHED.install(django_apps, editor)   # and again: no error
        source, volatility = _watch_function_source()
        self.assertIn('atomicdb_watchedkey', source)
        self.assertTrue(_is_watched(pos.key))
        self.assertEqual(_row(pos).last_value, 150)

    def test_with_the_trigger_the_capture_leaves_the_extremes_to_it(self):
        _snapshot, _created, folded, _added = roots.capture(now=T0)
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
        self.assertEqual(first['extremes']['watch_added'], 0)
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

    def _get(self, query=''):
        response = self.client.get('/atomicdb/roots/' + query)
        self.assertEqual(response.status_code, 200)
        return response

    def _page(self):
        return self._get().content.decode()

    def test_an_empty_record_says_so(self):
        response = self._get()
        body = response.content.decode()

        self.assertIn('Start position', body)
        self.assertIn('1. Nf3', body)
        self.assertEqual(body.count('Nothing recorded yet.'), 2)
        self.assertIn('id="openings"', body)
        self.assertEqual(len(response.context['openings']),
                         len(openings.load_catalog()))

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

    def test_the_replies_to_nf3_follow_it_indented_black_best_first(self):
        _set(self.nf3, eval_cp=541)
        _set(self.f6, eval_cp=1300)
        _set(_child(self.nf3, 'e7e5'), eval_cp=1500)
        _set(_child(self.nf3, 'd7d6'), eval_cp=900)

        rows = roots.page_context(now=T0)['rows']

        self.assertEqual(len(rows), 24)
        at = [row['label'] for row in rows].index('1. Nf3')
        self.assertEqual([(row['label'], row['indent'])
                          for row in rows[at:at + 4]],
                         [('1. Nf3', False), ('1... d6', True),
                          ('1... f6', True), ('1... e5', True)])
        self.assertTrue(rows[at + 1]['url'].endswith('?play=g1f3,d7d6'))
        self.assertIn('padding-left', self._page())

    def test_named_openings_carry_their_line_and_sort_by_any_column(self):
        made = _catalogue_positions(3)
        for (pos, _watched), value in zip(made, (500, 1500, -200)):
            _set(pos, eval_cp=value)
        keys = [pos.key for pos, _watched in made]
        if not POSTGRES:
            roots.fold_extremes(now=T0)

        by_name = roots.page_context(now=T0)['openings']
        names = [(row['label'].casefold(), row['line_san'])
                 for row in by_name]
        self.assertEqual(names, sorted(names))
        row = next(row for row in by_name if row['key'] == keys[0])
        self.assertEqual(row['line_san'], made[0][1].line_san)
        self.assertTrue(row['url'].endswith(
            '?play=' + ','.join(made[0][1].line)))
        self.assertEqual((row['now_h'], row['best_h'], row['changes']),
                         ('+5.00', '+5.00', 0))

        def first_three(sort):
            return [row['key'] for row in
                    roots.page_context(now=T0, sort=sort)['openings'][:3]]

        self.assertEqual(first_three('now'), [keys[1], keys[0], keys[2]])
        self.assertEqual(first_three('-now'), [keys[2], keys[0], keys[1]])
        self.assertEqual(first_three('best'), [keys[1], keys[0], keys[2]])
        self.assertEqual(first_three('worst'), [keys[2], keys[0], keys[1]])
        # Openings with no value yet go last whichever way.
        tail = roots.page_context(now=T0, sort='-now')['openings'][3:]
        self.assertTrue(all(row['value'] is None for row in tail))
        # Nonsense sorts by name.
        self.assertEqual(
            [row['key'] for row in
             roots.page_context(now=T0, sort='--bogus')['openings']],
            [row['key'] for row in by_name])

    def test_the_headers_flip_the_column_in_force(self):
        context = roots.page_context(now=T0)
        self.assertEqual(context['sort_links']['name'], '-name')
        self.assertEqual(context['sort_links']['now'], 'now')
        self.assertEqual(context['sort_arrows']['name'], ' ▴')

        context = roots.page_context(now=T0, sort='now')
        self.assertEqual(context['sort_links']['now'], '-now')
        self.assertEqual(context['sort_arrows']['now'], ' ▾')
        self.assertEqual(context['sort_arrows']['name'], '')

        body = self._get('?sort=-now').content.decode()
        self.assertIn('href="?sort=now#openings"', body)

    def test_an_opening_not_in_the_tree_says_so(self):
        body = self._page()
        self.assertIn('not in the tree yet', body)

    def test_a_community_named_position_is_listed_by_its_name(self):
        e4 = _child(self.root, 'e2e4')
        pos = _unwatched_child(e4)
        _set(pos, eval_cp=150)
        _suggest(pos, 'Rhaxier Gambit',
                 status=OpeningNameSuggestion.SState.APPROVED)
        if not POSTGRES:
            roots.fold_extremes(now=T0)

        response = self._get('?sort=now')
        rows = response.context['openings']
        row = next(row for row in rows if row['key'] == pos.key)
        self.assertEqual(
            (row['label'], row['community'], row['now_h'], row['best_h'],
             row['line_san']),
            ('Rhaxier Gambit', True, '+1.50', '+1.50', ''))
        self.assertEqual(row['url'], f'/atomicdb/explore/{pos.key}/')
        self.assertEqual(len(rows), len(openings.load_catalog()) + 1)
        self.assertIn('community name', response.content.decode())

    def test_an_approved_correction_renames_a_catalogue_row(self):
        (pos, watched), = _catalogue_positions()
        _suggest(pos, 'Corrected Name', kind=OpeningNameSuggestion.SKind.EDIT,
                 previous_name=watched.label,
                 status=OpeningNameSuggestion.SState.APPROVED)

        rows = roots.page_context(now=T0)['openings']

        row = next(row for row in rows if row['key'] == pos.key)
        self.assertEqual((row['label'], row['community'], row['line_san']),
                         ('Corrected Name', True, watched.line_san))
        self.assertEqual(len(rows), len(openings.load_catalog()))

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

        response = self._get()
        self.assertEqual(response.context['line']['text'], '1. Nf3 f6')
        body = response.content.decode()
        self.assertIn('2 plies', body)
        self.assertIn('ends at a position not analysed yet', body)
        self.assertIn(f'/atomicdb/explore/{self.f6.key}/?play=g1f3,f7f6',
                      body)

    def test_the_page_costs_a_fixed_handful_of_queries(self):
        """One read for every watched position of both tables, one for the
        community names, one for the latest line and two for its history."""
        _set(self.root, eval_cp=545, backed_move='g1f3')
        roots.capture(now=T0)
        roots.capture(now=T0 + timedelta(days=1))
        self.assertTrue(RootExtreme.objects.exists())

        with self.assertNumQueries(
                5, using=settings.ATOMICDB_DATABASE_ALIAS):
            roots.page_context(now=T0 + timedelta(days=1))

    def test_without_the_catalogue_the_page_goes_on(self):
        with mock.patch.object(
                roots, '_catalogue_rows',
                side_effect=openings.OpeningCatalogError('broken')), \
                self.assertLogs('atomicdb.roots', level='ERROR'):
            body = self._page()

        self.assertIn('Start position', body)
        self.assertIn('The opening catalogue is not available right now.',
                      body)

    def test_the_front_page_links_here(self):
        body = self.client.get('/atomicdb/').content.decode()
        self.assertIn('href="/atomicdb/roots/"', body)


class ApprovalHookTests(TestCase):
    """Approving a name on the moderation page puts its position on the watch
    list, with its first value, in the approval's own transaction."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.pos = _unwatched_child(_child(self.root, 'e2e4'))
        _set(self.pos, eval_cp=150)
        self.client.force_login(_approver())

    def _decide(self, suggestion, action):
        response = self.client.post('/atomicdb/suggestions/', {
            'action': action, 'suggestion': suggestion.id})
        self.assertEqual(response.status_code, 302)
        suggestion.refresh_from_db()
        return suggestion

    def test_a_pending_name_is_not_watched(self):
        _suggest(self.pos, 'Rhaxier Gambit')
        self.assertFalse(WatchedKey.objects.filter(key=self.pos.key).exists())

    def test_approving_a_name_watches_its_position_from_its_value(self):
        suggestion = self._decide(_suggest(self.pos, 'Rhaxier Gambit'),
                                  'approve')

        self.assertEqual(suggestion.status,
                         OpeningNameSuggestion.SState.APPROVED)
        watched = WatchedKey.objects.get(key=self.pos.key)
        self.assertEqual(watched.source, WatchedKey.Source.COMMUNITY)
        row = _row(self.pos)
        self.assertEqual(_summary(row),
                         (150, 'UNKNOWN', 150, 'UNKNOWN', 150, 0))
        self.assertEqual(row.since, watched.added)
        if POSTGRES:
            # From here on the trigger sees every change.
            _set(self.pos, eval_cp=175)
            self.assertEqual(_summary(_row(self.pos)),
                             (175, 'UNKNOWN', 150, 'UNKNOWN', 175, 1))

    def test_the_watch_rolls_back_with_the_approval(self):
        suggestion = _suggest(self.pos, 'Rhaxier Gambit')
        try:
            with atomic():
                suggestion.status = OpeningNameSuggestion.SState.APPROVED
                suggestion.save(update_fields=['status'])
                self.assertTrue(WatchedKey.objects.filter(
                    key=self.pos.key).exists())
                raise RuntimeError('the approval fails after the save')
        except RuntimeError:
            pass

        self.assertFalse(WatchedKey.objects.filter(key=self.pos.key).exists())
        self.assertFalse(RootExtreme.objects.filter(key=self.pos.key)
                         .exists())

    def test_a_rejected_name_is_not_watched(self):
        self._decide(_suggest(self.pos, 'Rhaxier Gambit'), 'reject')

        self.assertFalse(WatchedKey.objects.filter(key=self.pos.key).exists())

    def test_a_later_rejection_or_edit_keeps_the_position_and_its_history(
            self):
        self._decide(_suggest(self.pos, 'Rhaxier Gambit'), 'approve')
        self._decide(_suggest(self.pos, 'Something Else',
                              kind=OpeningNameSuggestion.SKind.EDIT,
                              previous_name='Rhaxier Gambit'), 'reject')
        self._decide(_suggest(self.pos, "Rhaxier's Gambit",
                              kind=OpeningNameSuggestion.SKind.EDIT,
                              previous_name='Rhaxier Gambit'), 'approve')

        self.assertEqual(WatchedKey.objects.filter(key=self.pos.key).count(),
                         1)
        self.assertTrue(RootExtreme.objects.filter(key=self.pos.key)
                        .exists())
        row = next(row for row in roots.page_context(now=T0)['openings']
                   if row['key'] == self.pos.key)
        self.assertEqual(row['label'], "Rhaxier's Gambit")

    def test_a_name_withdrawn_later_keeps_its_row_under_a_plain_label(self):
        suggestion = self._decide(_suggest(self.pos, 'Rhaxier Gambit'),
                                  'approve')
        # The moderation page decides pending names only: withdrawing an
        # approved one is done by hand.
        suggestion.status = OpeningNameSuggestion.SState.REJECTED
        suggestion.save(update_fields=['status'])
        community_names.invalidate()

        self.assertTrue(WatchedKey.objects.filter(key=self.pos.key).exists())
        self.assertEqual(_row(self.pos).last_value, 150)
        row = next(row for row in roots.page_context(now=T0)['openings']
                   if row['key'] == self.pos.key)
        self.assertEqual((row['label'], row['community'], row['now_h']),
                         ('Unnamed position', False, '+1.50'))

    def test_a_failing_watch_never_blocks_the_approval(self):
        with mock.patch.object(roots, 'watch',
                               side_effect=RuntimeError('broken')), \
                self.assertLogs('atomicdb.roots', level='ERROR'):
            suggestion = self._decide(_suggest(self.pos, 'Rhaxier Gambit'),
                                      'approve')

        self.assertEqual(suggestion.status,
                         OpeningNameSuggestion.SState.APPROVED)
        self.assertFalse(WatchedKey.objects.filter(key=self.pos.key).exists())
        # The hourly capture puts it right.
        self.assertEqual(roots.sync_watched_keys(now=T0), 1)
        self.assertEqual(WatchedKey.objects.get(key=self.pos.key).source,
                         WatchedKey.Source.COMMUNITY)
        self.assertEqual(_row(self.pos).last_value, 150)


class SyncTests(TestCase):
    """The hourly capture adds whatever should be watched and is not."""

    def setUp(self):
        _trigger_off()
        self.root = ingest.get_or_create_position(logic.start_fen())

    def test_a_complete_list_adds_nothing(self):
        self.assertEqual(roots.sync_watched_keys(now=T0), 0)
        self.assertEqual(set(roots.watched_keys()),
                         set(roots.standing_keys()))

    def test_missing_keys_come_back_with_their_source_and_value(self):
        _set(self.root, eval_cp=545)
        ingest.expand(self.root)
        pos = _unwatched_child(_child(self.root, 'e2e4'))
        _set(pos, eval_cp=150)
        _suggest(pos, 'Rhaxier Gambit',
                 status=OpeningNameSuggestion.SState.APPROVED)
        WatchedKey.objects.filter(key__in=[self.root.key, pos.key]).delete()
        RootExtreme.objects.all().delete()

        _snapshot, _created, _folded, added = roots.capture(now=T0)

        self.assertEqual(added, 2)
        self.assertEqual(
            dict(WatchedKey.objects.filter(key__in=[self.root.key, pos.key])
                 .values_list('key', 'source')),
            {self.root.key: 'root', pos.key: 'community'})
        self.assertEqual(_row(self.root).last_value, 545)
        self.assertEqual(_row(pos).last_value, 150)

    def test_an_existing_key_keeps_its_source_and_its_history(self):
        _set(self.root, eval_cp=545)
        RootExtreme.objects.create(
            key=self.root.key, best_value=1310, best_status='UNKNOWN',
            best_at=T0, worst_value=310, worst_status='UNKNOWN', worst_at=T0,
            last_value=545, last_status='UNKNOWN', changed_at=T0, changes=7,
            since=T0)

        self.assertEqual(
            roots.watch({self.root.key: WatchedKey.Source.COMMUNITY}), 0)

        self.assertEqual(WatchedKey.objects.get(key=self.root.key).source,
                         'root')
        self.assertEqual(_summary(_row(self.root)),
                         (1310, 'UNKNOWN', 310, 'UNKNOWN', 545, 7))
