"""The planner in shadow mode against a real (test) database.

What is defended here:
  * loading, one cycle and a second, incremental cycle produce shadow
    cycles and picks, with the expected kinds of pick;
  * a shadow cycle writes NOTHING but its own two tables: every row of
    positions, edges and tasks is identical before and after, and every
    statement it sends that is not a read targets a planner table;
  * work already queued gets virtual loss and is not picked again;
  * the overlap of the previous batch with what was served is measured;
  * ``run_planner`` refuses the phase-2 modes, honours ``off`` and its
    single-instance lock, and ``planner_status`` reads the metrics.
"""

import json
import os
import re
import tempfile
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import ingest, logic
from .database import current_alias
from .management.commands.run_planner import acquire_lock
from .models import (AnalysisTask, Edge, PlannerShadowCycle,
                     PlannerShadowPick, Position)
from .planner.shadow import ShadowRunner
from .testing import TestCase, TransactionTestCase

PLANNER_TABLES = ('atomicdb_plannershadowcycle', 'atomicdb_plannershadowpick')


def _make(fen, **kw):
    fen = logic.canonical_fen(fen)
    pos, _ = Position.objects.update_or_create(
        key=logic.key_of(fen), defaults=dict(fen=fen, **kw))
    return pos


def _searched(eval_cp, nodes=10 ** 8, **kw):
    return dict(eval_cp=eval_cp, nodes_invested=nodes, expanded=True,
                last_analysis=[{'eval_cp': eval_cp, 'move': None}], **kw)


class ShadowFixture:
    def build_tree(self):
        start = logic.start_fen()
        self.root = _make(start, **_searched(545, nodes=10 ** 10))
        self.assertEqual(self.root.key, ingest.root_key())
        self.kids = {}
        # 1.Nf3 is winning (+14); the other named moves are below +3, which
        # the cost model prices far above the horizon
        named = {'g1f3': _searched(1400), 'e2e3': {'eval_cp': 100},
                 'b1c3': {'eval_cp': 50}}
        for mv in logic.legal_moves(start):
            fen = logic.apply_move(start, mv)
            pos = _make(fen, **named.get(mv, {}))
            Edge.objects.get_or_create(parent=self.root, move_uci=mv,
                                       defaults={'child': pos})
            self.kids[mv] = pos
        nf3 = self.kids['g1f3']
        replies = {'f7f6': {'eval_cp': 1900},
                   'a7a6': {'status': 'WHITE_WIN', 'eval_cp': 10000},
                   'e7e6': dict(_searched(1700), expanded=False),
                   'h7h6': {'eval_cp': 2500}}
        self.replies = {}
        for mv in logic.legal_moves(nf3.fen):
            fen = logic.apply_move(nf3.fen, mv)
            pos = _make(fen, **replies.get(mv, {}))
            Edge.objects.get_or_create(parent=nf3, move_uci=mv,
                                       defaults={'child': pos})
            self.replies[mv] = pos


def _rows():
    return (list(Position.objects.order_by('key').values()),
            list(Edge.objects.order_by('id').values()),
            list(AnalysisTask.objects.order_by('id').values()))


_READ = re.compile(r'^\s*(SELECT|SAVEPOINT|RELEASE|ROLLBACK|BEGIN|COMMIT|SET|'
                   r'RESET|DECLARE|FETCH|CLOSE|WITH\b.*\bSELECT|PRAGMA)\b',
                   re.I | re.S)


class ShadowCycleTest(ShadowFixture, TestCase):
    def setUp(self):
        self.build_tree()

    def test_cycle_records_picks_and_writes_nothing_else(self):
        before = _rows()
        runner = ShadowRunner(batch=16)
        conn = connections[current_alias()]
        with CaptureQueriesContext(conn) as ctx:
            load = runner.start()
            report = runner.cycle()
        self.assertEqual(_rows(), before)
        for q in ctx.captured_queries:
            sql = q['sql']
            if _READ.match(sql):
                continue
            self.assertTrue(any(t in sql for t in PLANNER_TABLES),
                            'shadow cycle wrote outside its tables: %s'
                            % sql[:200])
        self.assertEqual(PlannerShadowCycle.objects.count(), 1)
        cyc = PlannerShadowCycle.objects.get()
        self.assertEqual(cyc.root_key, self.root.key)
        self.assertEqual(cyc.picks, report['picks'])
        self.assertGreater(cyc.picks, 0)
        self.assertEqual(PlannerShadowPick.objects.count(), cyc.picks)
        self.assertEqual(load['nodes'], 3)          # root, Nf3, ...e6
        self.assertIsNotNone(cyc.root_pn)
        strat = cyc.metrics['strategy']
        self.assertGreater(strat['obligations'], 0)
        self.assertIn('seconds', cyc.metrics)
        # the cheapest White move is 1.Nf3: the batch spends everything
        # below it first; once all of it is in flight, the other White moves
        # are within 4x of its (inflated) pn and get bought after it
        picks = list(PlannerShadowPick.objects.order_by('rank'))
        below = {p.key for p in self.replies.values()} | {
            self.kids['g1f3'].key}
        under = [p.rank for p in picks if p.position_key in below]
        other = [p.rank for p in picks if p.position_key not in below]
        self.assertEqual(len(under), 4)       # ...e6, the rest, ...f6, ...h6
        self.assertTrue(not other or max(under) < min(other))
        kinds = set(PlannerShadowPick.objects.values_list('kind', flat=True))
        self.assertTrue(kinds <= {'NODE', 'LEAF', 'REST'})
        self.assertIn('LEAF', kinds)
        # the more dangerous named reply (f6, +19) is picked before h6 (+25)
        ranks = dict(PlannerShadowPick.objects.filter(kind='LEAF').values_list(
            'move_uci', 'rank'))
        self.assertLess(ranks['f7f6'], ranks['h7h6'])

    def test_queued_work_is_not_picked_again(self):
        AnalysisTask.objects.create(position=self.replies['f7f6'],
                                    budget_nodes=10 ** 8, source='USER')
        runner = ShadowRunner(batch=16)
        runner.start()
        report = runner.cycle()
        self.assertEqual(report['inflight']['on_inline'], 1)
        picked = set(PlannerShadowPick.objects.values_list('position_key',
                                                           flat=True))
        self.assertNotIn(self.replies['f7f6'].key, picked)

    def test_second_cycle_is_incremental_and_measures_overlap(self):
        runner = ShadowRunner(batch=16)
        runner.start()
        first = runner.cycle()
        g = runner.graph
        pn0 = first['root_pn']
        # the fleet searches 1...f6 (one of the picks) and finds it worse
        f6 = self.replies['f7f6']
        Position.objects.filter(key=f6.key).update(
            nodes_invested=10 ** 8, eval_cp=1500, expanded=True,
            last_analysis=[{'eval_cp': 1500}], updated=timezone.now())
        for mv in logic.legal_moves(f6.fen)[:3]:
            child = _make(logic.apply_move(f6.fen, mv), eval_cp=1600)
            Edge.objects.get_or_create(parent=f6, move_uci=mv,
                                       defaults={'child': child})
        AnalysisTask.objects.create(position=f6, budget_nodes=10 ** 8,
                                    source='AUTO', state='COMPLETED',
                                    completed=timezone.now(),
                                    nodes_searched=10 ** 8)
        second = runner.cycle()
        self.assertGreaterEqual(g.lookup(f6.key), 0)
        self.assertEqual(second['recompute']['mode'], 'incremental')
        self.assertNotEqual(second['root_pn'], pn0)
        ov = second['overlap_prev']
        self.assertEqual(ov['served_tasks'], 1)
        self.assertEqual(ov['prev_picks_served'], 1)
        self.assertEqual(ov['served_on_prev_picks_by_source'], {'AUTO': 1})
        # what the incremental pass says equals a full recompute
        pn_inc = g.pn[g.root]
        runner.master.full_recompute()
        self.assertEqual(g.pn[g.root], pn_inc)


class AutocommitShadowTest(ShadowFixture, TransactionTestCase):
    """The service runs in autocommit: the loader then opens its own
    read-only transactions (and a server-side cursor on PostgreSQL)."""

    def test_cycle_outside_a_transaction(self):
        self.build_tree()
        before = _rows()
        runner = ShadowRunner(batch=8)
        runner.start()
        report = runner.cycle()
        second = runner.cycle()
        self.assertEqual(_rows(), before)
        self.assertGreater(report['picks'], 0)
        self.assertEqual(second['recompute']['mode'], 'incremental')
        self.assertEqual(PlannerShadowCycle.objects.count(), 2)


class PlannerCommandsTest(ShadowFixture, TestCase):
    def setUp(self):
        self.build_tree()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_phase2_modes_are_refused(self):
        for mode in ('fill', 'auto', 'idle', 'bogus'):
            with self.assertRaises(CommandError):
                call_command('run_planner', '--mode', mode, '--once',
                             stdout=StringIO())

    def test_off_does_nothing(self):
        out = StringIO()
        call_command('run_planner', '--mode', 'off', stdout=out)
        self.assertEqual(json.loads(out.getvalue()), {'mode': 'off',
                                                      'ran': False})
        self.assertEqual(PlannerShadowCycle.objects.count(), 0)

    def test_once_no_record_writes_nothing(self):
        out, err = StringIO(), StringIO()
        with override_settings(ATOMICDB_STATE_DIR=self.tmp.name):
            call_command('run_planner', '--mode', 'shadow', '--once',
                         '--no-record', '--batch', '8', stdout=out,
                         stderr=err)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([x['event'] for x in lines], ['loaded', 'cycle'])
        self.assertGreater(lines[1]['picks'], 0)
        self.assertEqual(PlannerShadowCycle.objects.count(), 0)

    def test_once_records_and_status_reads_it(self):
        with override_settings(ATOMICDB_STATE_DIR=self.tmp.name):
            call_command('run_planner', '--once', '--batch', '8',
                         stdout=StringIO(), stderr=StringIO())
        self.assertEqual(PlannerShadowCycle.objects.count(), 1)
        out = StringIO()
        call_command('planner_status', '--json', stdout=out)
        data = json.loads(out.getvalue())
        self.assertEqual(data['last_cycle']['picks'],
                         PlannerShadowPick.objects.count())
        self.assertEqual(data['overlap']['cycles'], 1)
        out = StringIO()
        call_command('planner_status', stdout=out)
        self.assertIn('root expected cost', out.getvalue())

    def test_single_instance_lock(self):
        path = os.path.join(self.tmp.name, 'p.lock')
        first = acquire_lock(path)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(acquire_lock(path))
            with self.assertRaises(CommandError):
                call_command('run_planner', '--once', '--lock-file', path,
                             stdout=StringIO(), stderr=StringIO())
        finally:
            first.close()
        again = acquire_lock(path)
        self.assertIsNotNone(again)
        again.close()
