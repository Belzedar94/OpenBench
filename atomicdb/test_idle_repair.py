"""The idle fleet searches again the positions nobody can trust yet.

What these tests pin, and why each one:

* a landing whose showcase is a shallow search of 128M or more queues ONE
  repair, priced to win the comparison ``ingest_analysis`` makes against the
  search it repairs, and searched at two lines; a mate, a search under 128M,
  an unknown depth, a deep one or a position already repaired do not, so a
  repair that comes out shallow itself cannot start a loop;
* that price is what makes the repair count: bought at the very budget it
  repairs, a narrow engine's search would lose to the wide one's overshoot;
* a parent with no search of its own between the landed position and a
  searched one is a connector and is bought its first search; the near
  misses (no searched grandparent, already searched, solved, a dead branch)
  are not;
* the arm has its own cap of waiting tasks and its own hourly ceiling, both
  counted on its own arm, and ``0`` turns it off, from the environment too;
* a visitor's request still goes first, a repair is handed only to a narrow
  slot (a ``--jobs`` slot speaks for its own engine), a visitor's promotion
  of it to their request goes to anybody, and the pool does not count it;
* the walk down the backlog reads a bounded step, resumes where it stopped
  (on disk when there is a state directory), leaves half the room to
  landings, costs one count while the queue is full, and goes round, so a
  landing a full queue let go is found the next time;
* it shows: "server: repair" on the workers page, one hidden event per batch;
* and a failure in the arm never costs the landing it rides on.
"""

import hashlib
import json
import os
import tempfile
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.db import connections
from django.test import Client, SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import idle_repair, ingest, logic, views
from .models import AnalysisTask, DBEvent, Edge, Position, WorkerPing
from .testing import TestCase, worker_account

AUTO = AnalysisTask.Source.AUTO
USER = AnalysisTask.Source.USER
PENDING = AnalysisTask.TState.PENDING
LEASED = AnalysisTask.TState.LEASED
COMPLETED = AnalysisTask.TState.COMPLETED
ARM = idle_repair.REPAIR_ARM
FLOOR = 128_000_000
# A 128M search as a small engine reports it, and as a wide one does.
SEARCHED = 128_001_500
WIDE = 128_010_000
REPAIR_128M = 128_640_000
START = logic.start_fen()
TIGHT = {'ATOMICDB_ARM_RATE_REPAIR': '4'}


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **fields):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **fields)


def _raw(depth, nodes, uci, score=30, multipv=1):
    """An ``info`` line as the worker forwards it from the engine."""
    return (f'info depth {depth} seldepth {depth + 9} multipv {multipv} '
            f'score cp {score} nodes {nodes} nps 6000000 hashfull 412 '
            f'tbhits 0 time 21000 pv {uci}')


def _lines(fen, depth, nodes=SEARCHED, score=30, width=5, mate=None,
           raw=True):
    """A pass of ``width`` lines over the legal moves of ``fen``."""
    lines = []
    for index, uci in enumerate(logic.legal_moves(fen)[:width]):
        value = score - 10 * index
        line = {'move': uci, 'eval_cp': value, 'mate': None, 'pv': [uci]}
        if raw:
            line['raw'] = _raw(depth, nodes, uci, value, index + 1)
        lines.append(line)
    if mate is not None:
        lines[0]['mate'] = mate
    return lines


def _showcase(depth, nodes=SEARCHED, score=30, **extra):
    """``last_analysis`` as an ingest leaves it: the pass's nodes on line 1."""
    return [dict({'move': 'e1e2', 'eval_cp': score, 'mate': None,
                  'pv': ['e1e2'], 'raw': _raw(depth, nodes, 'e1e2', score),
                  '_budget': nodes}, **extra)]


def _root():
    return ingest.get_or_create_position(START)


def _play(parent, uci):
    """One edge from ``parent``, the way the explorer's goto makes it."""
    child = ingest.get_or_create_position(logic.apply_move(parent.fen, uci))
    Edge.objects.get_or_create(parent=parent, move_uci=uci,
                               defaults={'child': child})
    return child


def _land(pos, depth, nodes=SEARCHED, **kwargs):
    return ingest.ingest_analysis(pos.key, _lines(pos.fen, depth, nodes,
                                                  **kwargs), nodes)


def _repairs(**filters):
    return AnalysisTask.objects.filter(arm=ARM, **filters)


def _spend(count, minutes_ago=0):
    """``count`` repairs already bought, ``minutes_ago`` back."""
    holder = _pos(f'spent-{minutes_ago}-{count}')
    AnalysisTask.objects.bulk_create([
        AnalysisTask(position=holder, generation=index, arm=ARM,
                     state=COMPLETED, nodes_searched=FLOOR,
                     budget_nodes=REPAIR_128M)
        for index in range(count)])
    if minutes_ago:
        AnalysisTask.objects.filter(position=holder).update(
            created=timezone.now() - timedelta(minutes=minutes_ago))


def _waiting(count, status='UNKNOWN'):
    """``count`` repairs waiting on as many positions."""
    for index in range(count):
        holder = _pos(f'waiting-{status}-{index}', status=status)
        AnalysisTask.objects.create(position=holder, generation=0, arm=ARM,
                                    source=AUTO, budget_nodes=REPAIR_128M)


def _queries(work):
    """``(result, queries)`` of ``work`` on every database it may use."""
    aliases = sorted({settings.ATOMICDB_DATABASE_ALIAS, 'default'})
    captured = [CaptureQueriesContext(connections[alias])
                for alias in aliases]
    for context in captured:
        context.__enter__()
    try:
        result = work()
    finally:
        for context in reversed(captured):
            context.__exit__(None, None, None)
    return result, [query['sql'] for context in captured
                    for query in context.captured_queries]


# ---------------------------------------------------------------------------
# The rule, on its own.
# ---------------------------------------------------------------------------
class ShallowRuleTests(SimpleTestCase):

    def test_a_shallow_showcase_of_128m_is_found(self):
        self.assertEqual(idle_repair.shallow_showcase(_showcase(15)),
                         SEARCHED)

    def test_depth_18_is_not_shallow(self):
        self.assertIsNone(idle_repair.shallow_showcase(_showcase(18)))
        self.assertEqual(idle_repair.shallow_showcase(_showcase(17)),
                         SEARCHED)

    def test_under_128m_is_not_this_arms_business(self):
        self.assertIsNone(
            idle_repair.shallow_showcase(_showcase(12, nodes=127_999_000)))

    def test_an_unknown_depth_is_not_a_shallow_one(self):
        line = _showcase(15)
        del line[0]['raw']
        self.assertIsNone(idle_repair.shallow_showcase(line))
        self.assertIsNone(idle_repair.shallow_showcase(None))
        self.assertIsNone(idle_repair.shallow_showcase([]))

    def test_a_mate_is_not_repaired(self):
        self.assertIsNone(idle_repair.shallow_showcase(
            _showcase(15, score=9995, mate=5)))
        # Inside the band without a distance: a tablebase score.
        self.assertIsNone(idle_repair.shallow_showcase(
            _showcase(15, score=-9200)))

    def test_only_the_current_pass_is_the_showcase(self):
        kept = _showcase(30, nodes=512_000_400)[0]
        stored = [dict(_showcase(15)[0], prior_pass=True), kept]
        self.assertIsNone(idle_repair.shallow_showcase(stored))

    def test_the_repair_outbids_the_rung_it_repairs_by_a_hair(self):
        self.assertEqual(idle_repair.repair_budget(SEARCHED), REPAIR_128M)
        self.assertEqual(idle_repair.repair_budget(WIDE), REPAIR_128M)
        self.assertEqual(idle_repair.repair_budget(512_002_000), 514_560_000)

    def test_the_overbid_keeps_it_on_the_same_rung(self):
        self.assertLess(idle_repair.REPAIR_OVERBID_PER_MILLE / 1000,
                        ingest.RUNG_OVERSHOOT_TOLERANCE)
        self.assertEqual(ingest._rung_matching(REPAIR_128M), FLOOR)

    def test_a_2b_search_is_not_bought_again(self):
        self.assertIsNone(idle_repair.repair_budget(2_000_010_000))

    def test_a_search_reported_past_the_overbid_is_left_alone(self):
        self.assertIsNone(idle_repair.repair_budget(129_000_000))

    def test_only_a_server_picked_repair_is_routed(self):
        class Task:
            def __init__(self, arm, source):
                self.arm, self.source = arm, source

        repair = Task(ARM, AUTO)
        self.assertTrue(idle_repair.fits_slot(repair, 8))
        self.assertTrue(idle_repair.fits_slot(repair, 1))
        self.assertFalse(idle_repair.fits_slot(repair, 9))
        self.assertFalse(idle_repair.fits_slot(repair, 0))
        self.assertTrue(idle_repair.fits_slot(Task(ARM, USER), 64))
        self.assertTrue(idle_repair.fits_slot(Task('cascade', AUTO), 64))
        self.assertTrue(idle_repair.fits_slot(Task('', AUTO), 0))


# ---------------------------------------------------------------------------
# Shallow searches, found when they land.
# ---------------------------------------------------------------------------
class ShallowLandingTests(TestCase):

    def setUp(self):
        self.root = _root()

    def test_a_shallow_search_of_128m_queues_one_repair(self):
        summary = _land(self.root, depth=15)

        self.assertEqual(summary['repaired'], 1)
        task = _repairs().get()
        self.root.refresh_from_db()
        self.assertEqual(task.position_id, self.root.key)
        self.assertEqual((task.source, task.state), (AUTO, PENDING))
        self.assertEqual(task.generation, self.root.visits)
        self.assertEqual(task.budget_nodes, REPAIR_128M)
        self.assertEqual(task.multipv, ingest.DEPTH_MULTIPV)

    def test_a_deep_search_queues_nothing(self):
        summary = _land(self.root, depth=18)

        self.assertNotIn('repaired', summary)
        self.assertFalse(_repairs().exists())

    def test_a_search_under_128m_queues_nothing(self):
        _land(self.root, depth=11, nodes=32_000_400)

        self.assertFalse(_repairs().exists())

    def test_a_mate_score_queues_nothing(self):
        _land(self.root, depth=15, score=9995, mate=5)
        _land(self.root, depth=15, score=9300)

        self.assertFalse(_repairs().exists())

    def test_a_search_without_its_info_line_queues_nothing(self):
        _land(self.root, depth=15, raw=False)

        self.assertFalse(_repairs().exists())

    def test_a_second_shallow_landing_does_not_queue_again(self):
        _land(self.root, depth=15)
        _land(self.root, depth=14)

        self.assertEqual(_repairs().count(), 1)

    def test_a_repair_that_came_out_shallow_is_not_repaired(self):
        _land(self.root, depth=15)
        task = _repairs().get()
        AnalysisTask.objects.filter(pk=task.pk).update(
            state=COMPLETED, nodes_searched=REPAIR_128M + 300)

        _land(self.root, depth=16, nodes=REPAIR_128M + 300, width=2)

        self.assertEqual(_repairs().count(), 1)

    def test_a_position_repaired_once_is_never_bought_another(self):
        """The guard itself, whatever the price would have been."""
        pos = _pos('repaired-once', visits=2, last_analysis=_showcase(15))
        AnalysisTask.objects.create(position=pos, generation=1, arm=ARM,
                                    state=COMPLETED, nodes_searched=FLOOR,
                                    budget_nodes=REPAIR_128M)

        self.assertEqual(idle_repair.queue_from_landing(pos, []), 0)
        self.assertEqual(_repairs(position=pos).count(), 1)

    def test_a_repair_absorbed_unsearched_does_not_count(self):
        """Another search covered it before a worker took it."""
        _land(self.root, depth=15)
        AnalysisTask.objects.filter(arm=ARM).update(state=COMPLETED,
                                                    nodes_searched=0)

        _land(self.root, depth=15)

        self.assertEqual(_repairs(state=PENDING).count(), 1)
        self.assertEqual(_repairs().count(), 2)

    def test_a_shallow_pass_under_a_deeper_showcase_queues_nothing(self):
        _land(self.root, depth=30, nodes=512_001_000, width=2)
        _land(self.root, depth=15)

        self.assertFalse(_repairs().exists())

    def test_a_shallow_512m_buys_the_512m_rung(self):
        _land(self.root, depth=16, nodes=512_002_000, width=2)

        self.assertEqual(_repairs().get().budget_nodes, 514_560_000)

    def test_a_shallow_2b_is_not_bought_again(self):
        _land(self.root, depth=17, nodes=2_000_010_000, width=2)

        self.assertFalse(_repairs().exists())

    def test_a_closed_position_is_not_repaired(self):
        closed = _pos('closed', status='WHITE_WIN', visits=1,
                      last_analysis=_showcase(15))

        self.assertEqual(idle_repair.queue_from_landing(closed, []), 0)
        self.assertFalse(_repairs().exists())

    def test_a_task_already_on_the_next_visit_is_the_next_search(self):
        """Whoever queued it, it is not doubled by a repair beside it."""
        AnalysisTask.objects.create(position=self.root, generation=1,
                                    budget_nodes=FLOOR, source=USER,
                                    requested_by='alice')

        _land(self.root, depth=15)

        self.assertFalse(_repairs().exists())
        self.assertEqual(AnalysisTask.objects.filter(
            position=self.root).count(), 1)

    def test_each_batch_leaves_one_hidden_event(self):
        _land(self.root, depth=15)

        event = DBEvent.objects.get(kind=idle_repair.REPAIR_EVENT)
        self.assertEqual(event.payload['via'], 'landing')
        self.assertEqual(event.payload['key'], self.root.key)
        self.assertEqual((event.payload['queued'], event.payload['shallow'],
                          event.payload['connector']), (1, 1, 0))
        self.assertEqual(event.payload['keys'], [self.root.key])
        self.assertIn(idle_repair.REPAIR_EVENT, views.FEED_HIDDEN_KINDS)

    def test_a_landing_with_nothing_to_repair_writes_nothing(self):
        _land(self.root, depth=24)

        self.assertFalse(DBEvent.objects.filter(
            kind=idle_repair.REPAIR_EVENT).exists())

    def test_the_common_landing_costs_one_read_of_its_parents(self):
        _land(self.root, depth=24)
        self.root.refresh_from_db()
        child = Edge.objects.get(parent=self.root, move_uci='e2e4').child

        _, below = _queries(lambda: idle_repair.queue_from_landing(
            child, [self.root.key]))
        _, top = _queries(lambda: idle_repair.queue_from_landing(
            self.root, []))

        self.assertEqual(len(below), 1)
        self.assertEqual(top, [])


class ArbitrationTests(TestCase):
    """The value rules are not touched: the repair is priced to pass them."""

    def setUp(self):
        self.root = _root()
        # A wide engine's shallow search: it stopped further past 128M.
        _land(self.root, depth=15, nodes=WIDE, score=30)
        self.repair = _repairs().get()

    def test_the_repair_takes_the_showcase_of_the_search_it_repairs(self):
        searched = self.repair.budget_nodes + 1_500
        AnalysisTask.objects.filter(pk=self.repair.pk).update(
            state=COMPLETED, nodes_searched=searched)

        _land(self.root, depth=23, nodes=searched, score=-120, width=2)

        self.root.refresh_from_db()
        self.assertEqual(self.root.eval_cp, -120)
        self.assertIsNone(idle_repair.shallow_showcase(
            self.root.last_analysis))
        self.assertEqual(_repairs().count(), 1)

    def test_at_the_same_budget_it_would_lose_to_the_overshoot(self):
        """Why the overbid: a narrow 128M search stops closer to 128M."""
        _land(self.root, depth=23, nodes=128_000_400, score=-120, width=2)

        self.root.refresh_from_db()
        self.assertEqual(self.root.eval_cp, 30)


# ---------------------------------------------------------------------------
# Connectors, found when a search lands below them.
# ---------------------------------------------------------------------------
# The quality arm can buy the same link its first search in the same landing
# (its support against the searched grandparent's); off here, so these tests
# see this arm alone.  ``test_another_arms_search_of_the_link_is_enough`` has
# the two together.
@override_settings(ATOMICDB_ARM_RATE_QUALITY='0')
class ConnectorTests(TestCase):

    def setUp(self):
        self.root = _root()
        _land(self.root, depth=24)                 # the searched grandparent
        self.link = Edge.objects.get(parent=self.root,
                                     move_uci='e2e4').child
        self.below = _play(self.link, 'e7e5')      # the explorer's own edge

    def test_an_unsearched_link_between_searched_positions_is_bought(self):
        summary = _land(self.below, depth=24)

        self.assertEqual(summary['repaired'], 1)
        task = _repairs().get()
        self.assertEqual(task.position_id, self.link.key)
        self.assertEqual((task.source, task.state, task.generation),
                         (AUTO, PENDING, 0))
        self.assertEqual(task.budget_nodes, FLOOR)
        self.assertEqual(task.multipv, ingest.multipv_for(0, FLOOR))
        event = DBEvent.objects.get(kind=idle_repair.REPAIR_EVENT)
        self.assertEqual((event.payload['shallow'],
                          event.payload['connector']), (0, 1))

    def test_without_a_searched_parent_it_is_not_a_connector(self):
        Position.objects.filter(key=self.root.key).update(visits=0)

        _land(self.below, depth=24)

        self.assertFalse(_repairs().exists())

    def test_a_link_with_a_search_of_its_own_is_not_a_connector(self):
        Position.objects.filter(key=self.link.key).update(visits=1)

        _land(self.below, depth=24)

        self.assertFalse(_repairs().exists())

    def test_a_solved_link_is_not_a_connector(self):
        Position.objects.filter(key=self.link.key).update(
            status='WHITE_WIN', closure='MINIMAX')

        _land(self.below, depth=24)

        self.assertFalse(_repairs().exists())

    def test_a_closed_grandparent_does_not_make_one(self):
        Position.objects.filter(key=self.root.key).update(status='DRAW')

        _land(self.below, depth=24)

        self.assertFalse(_repairs().exists())

    def test_a_dead_branch_is_not_bought(self):
        Position.objects.filter(key=self.link.key).update(
            priority=ingest.DEAD)

        _land(self.below, depth=24)

        self.assertFalse(_repairs().exists())

    def test_a_connector_is_bought_once(self):
        _land(self.below, depth=24)
        _land(self.below, depth=25)

        self.assertEqual(_repairs().count(), 1)

    def test_a_shallow_landing_below_a_connector_buys_both(self):
        _land(self.below, depth=15)

        self.assertEqual(set(_repairs().values_list('position_id', flat=True)),
                         {self.link.key, self.below.key})

    def test_another_arms_search_of_the_link_is_enough(self):
        """With the quality arm on, whoever queues the link's search first
        owns it, and the link gets one search, not two."""
        with override_settings(ATOMICDB_ARM_RATE_QUALITY='120'):
            _land(self.below, depth=24)

        tasks = AnalysisTask.objects.filter(position=self.link)
        self.assertEqual(tasks.count(), 1)
        self.assertEqual(tasks.get().generation, 0)


# ---------------------------------------------------------------------------
# Its own cap and its own hourly ceiling.
# ---------------------------------------------------------------------------
class CapAndRateTests(TestCase):

    def setUp(self):
        self.root = _root()

    def test_a_full_queue_buys_nothing(self):
        _waiting(idle_repair.REPAIR_QUEUE_CAP)

        _land(self.root, depth=15)

        self.assertFalse(_repairs(position=self.root).exists())

    def test_repairs_left_on_closed_positions_hold_no_place(self):
        _waiting(idle_repair.REPAIR_QUEUE_CAP, status='WHITE_WIN')

        _land(self.root, depth=15)

        self.assertTrue(_repairs(position=self.root).exists())

    def test_what_other_arms_queue_is_not_counted(self):
        for index in range(idle_repair.REPAIR_QUEUE_CAP):
            AnalysisTask.objects.create(
                position=_pos(f'cascade-{index}'), generation=0,
                arm=ingest.VALUE_CASCADE_ARM, source=AUTO,
                budget_nodes=FLOOR)

        _land(self.root, depth=15)

        self.assertTrue(_repairs(position=self.root).exists())

    @override_settings(**TIGHT)
    def test_an_arm_at_its_hourly_count_buys_nothing(self):
        _spend(4)

        _land(self.root, depth=15)

        self.assertFalse(_repairs(position=self.root).exists())
        self.assertTrue(DBEvent.objects.filter(
            kind=ingest.ARM_RATE_EVENT, payload__arm=ARM).exists())

    @override_settings(**TIGHT)
    def test_the_hour_rolls(self):
        _spend(4, minutes_ago=61)

        _land(self.root, depth=15)

        self.assertTrue(_repairs(position=self.root).exists())

    def test_the_default_ceiling(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ATOMICDB_ARM_RATE_REPAIR', None)
            self.assertEqual(ingest.arm_rate(ARM), 60)

    @override_settings(ATOMICDB_ARM_RATE_REPAIR='0')
    def test_zero_turns_the_arm_off(self):
        _land(self.root, depth=15)

        self.assertFalse(_repairs().exists())
        self.assertFalse(DBEvent.objects.filter(
            kind=ingest.ARM_RATE_EVENT).exists())

    def test_the_lever_works_from_the_environment(self):
        """The settings module does not mirror this knob yet."""
        with patch.dict(os.environ, {'ATOMICDB_ARM_RATE_REPAIR': '0'}):
            self.assertEqual(ingest.arm_rate(ARM), 0)
            _land(self.root, depth=15)

        self.assertFalse(_repairs().exists())

    def test_the_other_arms_keep_their_settings(self):
        with override_settings(ATOMICDB_ARM_RATE_CASCADE='30'):
            self.assertEqual(ingest.arm_rate(ingest.VALUE_CASCADE_ARM), 30)


# ---------------------------------------------------------------------------
# Who gets a repair: never before a visitor, only a narrow engine.
# ---------------------------------------------------------------------------
class LeaseRoutingTests(TestCase):

    def setUp(self):
        worker_account('w', 'pw')
        self.root = _root()
        self.target = _play(self.root, 'a2a3')
        self.repair = AnalysisTask.objects.create(
            position=self.target, generation=0, budget_nodes=REPAIR_128M,
            multipv=ingest.DEPTH_MULTIPV, source=AUTO, arm=ARM)

    def _lease(self, machine, threads):
        data = {'username': 'w', 'password': 'pw', 'machine': machine,
                'worker_build': '2026072203', 'lease_session': machine}
        if threads is not None:
            data['threads'] = str(threads)
        response = self.client.post('/atomicdb/api/lease', data)
        self.assertEqual(response.status_code, 200)
        return [task['id'] for task in response.json()['tasks']]

    def test_a_narrow_slot_takes_the_repair(self):
        self.assertEqual(self._lease('small-box', 8), [self.repair.id])

    def test_a_wide_slot_is_given_something_else(self):
        leased = self._lease('big-box', 32)

        self.assertNotIn(self.repair.id, leased)
        self.repair.refresh_from_db()
        self.assertEqual(self.repair.state, PENDING)

    def test_a_jobs_slot_speaks_for_its_own_engine(self):
        """``-T 32 --jobs 4``: four slots of eight threads each."""
        self.assertEqual(self._lease('big-box#2', 8), [self.repair.id])

    def test_a_slot_that_reports_no_threads_is_not_trusted_with_one(self):
        self.assertNotIn(self.repair.id, self._lease('quiet-box', None))

    def test_a_visitor_request_still_goes_first(self):
        clicked = AnalysisTask.objects.create(
            position=_play(self.root, 'b2b3'), generation=0,
            budget_nodes=FLOOR, source=USER, requested_by='alice')

        self.assertEqual(self._lease('small-box', 4), [clicked.id])

    def test_a_repair_a_visitor_asked_for_goes_to_anybody(self):
        self.assertEqual(ingest.request_analysis(self.target), 'queued')
        self.repair.refresh_from_db()
        self.assertEqual(self.repair.source, USER)

        self.assertEqual(self._lease('big-box', 64), [self.repair.id])

    def test_the_wide_slot_does_not_stall_on_the_repair(self):
        """It is skipped like any other row the slot cannot take."""
        other = AnalysisTask.objects.create(
            position=_play(self.root, 'h2h3'), generation=0,
            budget_nodes=FLOOR, source=AUTO)
        Position.objects.filter(key=self.target.key).update(priority=50.0)

        self.assertEqual(self._lease('big-box', 32), [other.id])


class PoolTests(TestCase):
    """The pool and the arm do not eat each other's room."""

    def test_waiting_repairs_do_not_shrink_the_pool(self):
        root = _root()
        ingest.expand(root)
        for edge in Edge.objects.filter(parent=root)[:8]:
            Position.objects.filter(pk=edge.child_id).update(
                eval_cp=100, expanded=True)
        ingest.refresh_priorities()
        for index in range(3):
            holder = _pos(f'pool-repair-{index}', priority=-1000.0)
            AnalysisTask.objects.create(position=holder, generation=0,
                                        arm=ARM, source=AUTO,
                                        budget_nodes=REPAIR_128M)

        self.assertEqual(ingest.top_up_analysis_pool(target=5), 5)

        self.assertEqual(AnalysisTask.objects.filter(
            state=PENDING, source=AUTO).exclude(arm=ARM).count(), 5)
        self.assertEqual(ingest.top_up_analysis_pool(target=5), 0)


# ---------------------------------------------------------------------------
# The walk down the backlog, one bounded step per tick.
# ---------------------------------------------------------------------------
def _searched(name, depth=15, nodes=SEARCHED, **fields):
    """A position searched before the arm existed, and its task."""
    fields.setdefault('last_analysis', _showcase(depth, nodes))
    pos = _pos(name, visits=1, **fields)
    AnalysisTask.objects.create(position=pos, generation=0,
                                budget_nodes=FLOOR, state=COMPLETED,
                                nodes_searched=nodes)
    return pos


class BacklogTests(TestCase):

    def test_the_walk_buys_the_shallow_showcases_of_the_past(self):
        shallow = [_searched(f'old-shallow-{index}') for index in range(3)]
        _searched('old-deep', depth=26)
        _searched('old-mate', last_analysis=_showcase(15, score=9990,
                                                      mate=4))

        self.assertEqual(idle_repair.drip_backlog(), 3)

        tasks = _repairs()
        self.assertEqual(set(tasks.values_list('position_id', flat=True)),
                         {pos.key for pos in shallow})
        self.assertEqual({task.budget_nodes for task in tasks},
                         {REPAIR_128M})
        self.assertEqual({task.generation for task in tasks}, {1})
        event = DBEvent.objects.get(kind=idle_repair.REPAIR_EVENT)
        self.assertEqual(event.payload['via'], 'backlog')
        self.assertEqual(event.payload['queued'], 3)

    def test_newest_first(self):
        first = _searched('older')
        second = _searched('newer')

        self.assertEqual(idle_repair.drip_backlog(scan=1), 1)

        self.assertEqual(_repairs().get().position_id, second.key)
        self.assertEqual(idle_repair.drip_backlog(scan=1), 1)
        self.assertTrue(_repairs(position=first).exists())

    def test_it_stops_at_half_the_cap(self):
        for index in range(12):
            _searched(f'many-{index}')

        self.assertEqual(idle_repair.drip_backlog(),
                         idle_repair.REPAIR_BACKLOG_TARGET)
        self.assertEqual(idle_repair.drip_backlog(), 0)

    def test_the_room_left_is_where_the_next_step_starts(self):
        """Nothing is skipped when a step runs out of room."""
        for index in range(10):
            _searched(f'resume-{index}')

        idle_repair.drip_backlog()
        AnalysisTask.objects.filter(arm=ARM).update(state=COMPLETED,
                                                    nodes_searched=FLOOR)

        self.assertEqual(idle_repair.drip_backlog(), 2)
        self.assertEqual(_repairs().count(), 10)

    @override_settings(**TIGHT)
    def test_half_the_hourly_ceiling_is_left_to_landings(self):
        for index in range(5):
            _searched(f'hourly-{index}')

        self.assertEqual(idle_repair.drip_backlog(), 2)
        self.assertEqual(idle_repair.drip_backlog(), 0)

        _land(_root(), depth=15)
        self.assertTrue(_repairs(position__key=_root().key).exists())

    def test_a_step_reads_a_bounded_window_and_resumes_below_it(self):
        target = _searched('far-below')
        filler = _pos('filler')
        AnalysisTask.objects.bulk_create([
            AnalysisTask(position=filler, generation=index, state=COMPLETED,
                         budget_nodes=8_000_000, nodes_searched=8_000_100)
            for index in range(5)])

        self.assertEqual(idle_repair.drip_backlog(window=3), 0)
        self.assertEqual(idle_repair.drip_backlog(window=3), 1)

        self.assertEqual(_repairs().get().position_id, target.key)

    def test_connectors_of_the_past_are_found_too(self):
        grand = _pos('grand', visits=1)
        link = _pos('link', stm='b')
        below = _searched('below', depth=24)
        Edge.objects.create(parent=grand, move_uci='e1e2', child=link)
        Edge.objects.create(parent=link, move_uci='e8e7', child=below)

        self.assertEqual(idle_repair.drip_backlog(), 1)

        task = _repairs().get()
        self.assertEqual((task.position_id, task.budget_nodes, task.generation),
                         (link.key, FLOOR, 0))

    def test_what_is_already_repaired_is_skipped_without_room(self):
        done = _searched('already')
        AnalysisTask.objects.create(position=done, generation=1, arm=ARM,
                                    state=COMPLETED, nodes_searched=FLOOR,
                                    budget_nodes=REPAIR_128M)
        fresh = _searched('fresh')

        self.assertEqual(idle_repair.drip_backlog(), 1)

        self.assertTrue(_repairs(position=fresh).exists())
        self.assertEqual(_repairs(position=done).count(), 1)

    def test_past_the_oldest_search_it_starts_again_from_the_newest(self):
        """What a full queue made a landing let go is found next time."""
        _searched('first-round')

        # One window wide enough to reach the first task id.
        self.assertEqual(idle_repair.drip_backlog(window=10 ** 12), 1)
        self.assertIsNone(idle_repair._backlog_state()['cursor'])

        dropped = _searched('let-go-by-a-full-queue')
        self.assertEqual(idle_repair.drip_backlog(window=10 ** 12), 1)

        self.assertTrue(_repairs(position=dropped).exists())
        self.assertEqual(_repairs().count(), 2)

    def test_a_full_queue_costs_one_count_and_no_walk(self):
        for index in range(3):
            _searched(f'waits-{index}')
        _waiting(idle_repair.REPAIR_BACKLOG_TARGET)

        made, queries = _queries(idle_repair.drip_backlog)

        self.assertEqual(made, 0)
        self.assertEqual(len(queries), 1)
        self.assertIsNone(idle_repair._backlog_state()['cursor'])

    def test_a_step_costs_the_same_over_ten_searches_or_a_hundred(self):
        def cost(count, prefix):
            idle_repair.reset_backlog_state()
            for index in range(count):
                _searched(f'{prefix}-{index}', depth=26)
            return len(_queries(idle_repair.drip_backlog)[1])

        ten = cost(10, 'ten')

        self.assertEqual(cost(100, 'hundred'), ten)

    def test_a_step_never_reads_more_than_its_scan(self):
        for index in range(7):
            _searched(f'scan-{index}', depth=26)

        idle_repair.drip_backlog(scan=3)

        third_newest = (AnalysisTask.objects.order_by('-id')
                        .values_list('id', flat=True)[2])
        self.assertEqual(idle_repair._backlog_state()['cursor'],
                         third_newest)

    @override_settings(ATOMICDB_ARM_RATE_REPAIR='0')
    def test_zero_stops_the_walk_too(self):
        _searched('off')

        self.assertEqual(idle_repair.drip_backlog(), 0)
        self.assertFalse(_repairs().exists())


class BacklogStateTests(TestCase):
    """Where the walk stands survives a restart of the service."""

    def setUp(self):
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        overridden = override_settings(ATOMICDB_STATE_DIR=self.state_dir.name)
        overridden.enable()
        self.addCleanup(overridden.disable)
        idle_repair.reset_backlog_state()

    def _stored(self):
        path = os.path.join(self.state_dir.name, 'idle_repair.json')
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)

    def test_a_restart_resumes_the_walk(self):
        older = _searched('restart-older')
        newer = _searched('restart-newer')
        newer_task = AnalysisTask.objects.get(position=newer)

        idle_repair.drip_backlog(scan=1)

        self.assertEqual(self._stored(), {'cursor': newer_task.id})
        # A new process: nothing in memory, the file on disk.
        idle_repair._backlog.update(cursor=None, loaded=False)
        with patch.object(idle_repair, '_mint',
                          wraps=idle_repair._mint) as mint:
            idle_repair.drip_backlog(scan=1)
        bought = [target[0] for call in mint.call_args_list
                  for target in call.args[0]]
        self.assertEqual(bought, [older.key])

    def test_a_broken_file_starts_the_walk_over(self):
        path = os.path.join(self.state_dir.name, 'idle_repair.json')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('{not json')
        _searched('broken')

        self.assertEqual(idle_repair.drip_backlog(), 1)

    def test_resetting_forgets_the_file(self):
        _searched('forget')
        idle_repair.drip_backlog()

        idle_repair.reset_backlog_state()

        self.assertFalse(os.path.exists(
            os.path.join(self.state_dir.name, 'idle_repair.json')))


class SelectorStepTests(TestCase):

    def _pass(self, **options):
        out = StringIO()
        call_command('refresh_selector', stdout=out, **options)
        return json.loads(out.getvalue())

    def test_the_selector_walks_the_backlog_and_says_so(self):
        _searched('selector-1')
        _searched('selector-2')

        report = self._pass(no_priorities=True)

        self.assertEqual(report['repair_backlog_enqueued'], 2)
        self.assertNotIn('failed_steps', report)
        self.assertEqual(_repairs().count(), 2)

    def test_the_walk_can_be_left_out(self):
        _searched('selector-off')

        report = self._pass(no_priorities=True, no_repair_backlog=True)

        self.assertEqual(report['repair_backlog_enqueued'], 0)
        self.assertFalse(_repairs().exists())


# ---------------------------------------------------------------------------
# Where it shows, and what it never costs.
# ---------------------------------------------------------------------------
class VisibilityTests(TestCase):

    def _busy(self, uci, **fields):
        now = timezone.now()
        task = AnalysisTask.objects.create(
            position=_play(_root(), uci), generation=0, source=AUTO,
            budget_nodes=REPAIR_128M, state=LEASED, machine=f'box-{uci}',
            lease_token='tok', leased_at=now, lease_heartbeat_at=now,
            **fields)
        ping = WorkerPing.objects.create(machine=f'box-{uci}', user='alice',
                                         threads=8, current_task_id=task.id)
        WorkerPing.objects.filter(pk=ping.pk).update(last_seen=now)
        return task

    def test_the_workers_page_names_a_repair(self):
        self._busy('a2a3', arm=ARM)
        self._busy('b2b3')

        body = Client().get('/atomicdb/workers/').content.decode()

        self.assertEqual(body.count('server: repair'), 1)
        self.assertIn('>server<', body.replace(' ', ''))


class FailureContainmentTests(TestCase):

    def test_a_broken_arm_does_not_cost_the_landing(self):
        root = _root()
        with patch.object(idle_repair, '_mint',
                          side_effect=RuntimeError('repair broken')), \
                self.assertLogs('atomicdb.idle_repair', level='ERROR'):
            summary = _land(root, depth=15)

        root.refresh_from_db()
        self.assertEqual(root.visits, 1)
        self.assertEqual(root.eval_cp, 30)
        self.assertNotIn('repaired', summary)
        self.assertFalse(_repairs().exists())
