"""The +16 sweep, and no deep rungs on interior positions from the server.

What these tests pin, and why each one:

* the automatic policy never buys more than 128M for an interior position
  (one whose value already stands on a child): the visit ladder of the
  selector, the quality arm and the idle repairs all reached 512M and 2B on
  their own.  A leaf keeps every rung, a request keeps every rung, a clamp
  keeps its price, and ``ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS=1`` restores the
  old prices;
* the sweep rule: open, searched once at about 128M, the side to move
  favoured by +16 to +90, for either colour;
* the list puts White's strategy tree first (White plays the move its value
  stands on, Black every reply), the shallowest first, and every other
  candidate after it, the highest value first;
* the drip only tops its own queue up to its cap, in the FILL band at 128M
  and one line, within its arm's hourly ceiling, never while the requests
  pile up, checks every key again, resumes where it stopped (on disk when
  there is a state directory) and queues nothing without a list or with the
  arm at 0;
* the hourly pass that absorbs shadowed duplicates leaves the sweep's tasks
  alone (each one repeats a search its position already had), and a task
  that was absorbed without a search is queued again when the walk comes
  back to it (``--rewind``), within the same cap;
* a request still goes first and the sweep goes before the selector's pool,
  which does not count it;
* it shows as "server: re-search" on the workers page, and its receipt stays
  out of the public feed.
"""

import json
import os
import tempfile
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import active_workers, idle_repair, ingest, logic, sweep, views
from .models import AnalysisTask, DBEvent, Edge, Position
from .testing import TestCase, worker_account

AUTO = AnalysisTask.Source.AUTO
FILL = AnalysisTask.Source.FILL
USER = AnalysisTask.Source.USER
PENDING = AnalysisTask.TState.PENDING
COMPLETED = AnalysisTask.TState.COMPLETED
M128 = 128_000_000
M512 = 512_000_000
B2 = 2_000_000_000
ONCE = 128_001_500
START = logic.start_fen()
OPEN_RATE = {'ATOMICDB_ARM_RATE_SWEEP16': '1000'}


def _root():
    return ingest.get_or_create_position(START)


def _play(parent, uci, **fields):
    child = ingest.get_or_create_position(logic.apply_move(parent.fen, uci))
    Edge.objects.get_or_create(parent=parent, move_uci=uci,
                               defaults={'child': child})
    if fields:
        Position.objects.filter(key=child.key).update(**fields)
        child.refresh_from_db()
    return child


def _candidate(**extra):
    """The fields of a position the sweep wants, White's side."""
    fields = {'visits': 1, 'nodes_invested': ONCE, 'eval_cp': 2000}
    fields.update(extra)
    return fields


# ---------------------------------------------------------------------------
# Deep rungs on interior positions.
# ---------------------------------------------------------------------------
class InteriorRungRuleTests(SimpleTestCase):

    def setUp(self):
        self.interior = Position(key='i', fen=START, backed_move='e2e3')
        self.leaf = Position(key='l', fen=START)

    def test_an_interior_position_is_held_at_the_first_rung(self):
        self.assertEqual(ingest.server_picked_budget(B2, pos=self.interior),
                         M128)
        self.assertEqual(ingest.server_picked_budget(M512, pos=self.interior),
                         M128)

    def test_a_leaf_keeps_every_rung(self):
        self.assertEqual(ingest.server_picked_budget(B2, pos=self.leaf), B2)
        self.assertEqual(ingest.server_picked_budget(M512, pos=self.leaf),
                         M512)

    def test_without_the_position_nothing_changes(self):
        self.assertEqual(ingest.server_picked_budget(B2), B2)

    def test_the_floor_still_applies(self):
        self.assertEqual(ingest.server_picked_budget(8_000_000,
                                                     pos=self.interior), M128)

    def test_a_clamp_keeps_its_price(self):
        self.assertEqual(ingest.server_picked_budget(
            2_000_000, clamp=(2_000_000, 1), pos=self.interior), 2_000_000)

    @override_settings(ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS='1')
    def test_the_setting_restores_the_old_prices(self):
        self.assertEqual(ingest.server_picked_budget(B2, pos=self.interior),
                         B2)

    def test_the_environment_restores_them_too(self):
        with patch.dict(os.environ,
                        {'ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS': 'true'}):
            self.assertTrue(ingest.auto_interior_deep_rungs())
        with patch.dict(os.environ,
                        {'ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS': ''}):
            self.assertFalse(ingest.auto_interior_deep_rungs())

    def test_an_interior_repair_above_the_first_rung_is_not_bought(self):
        self.assertIsNone(idle_repair.repair_budget(512_002_000,
                                                    interior=True))
        self.assertEqual(idle_repair.repair_budget(512_002_000), 514_560_000)
        self.assertEqual(idle_repair.repair_budget(128_001_500,
                                                   interior=True),
                         128_640_000)

    @override_settings(ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS='1')
    def test_the_setting_restores_the_deep_repair(self):
        self.assertEqual(idle_repair.repair_budget(512_002_000,
                                                   interior=True),
                         514_560_000)


class InteriorRungPolicyTests(TestCase):

    def setUp(self):
        self.root = _root()

    def _selected(self, pos):
        with patch.object(ingest.proof, 'selector_mode',
                          return_value='regret'):
            Position.objects.filter(key=pos.key).update(priority=1e6)
            ingest.next_tasks(1)
        return AnalysisTask.objects.get(position=pos, generation=pos.visits)

    def test_the_selector_buys_an_interior_position_at_128m(self):
        child = _play(self.root, 'e2e3', visits=4, backed_move='e7e6',
                      backed_eval=300)

        self.assertEqual(self._selected(child).budget_nodes, M128)

    def test_the_selector_still_climbs_on_a_leaf(self):
        child = _play(self.root, 'e2e3', visits=4)

        self.assertEqual(self._selected(child).budget_nodes, B2)

    def test_the_quality_arm_does_not_buy_an_interior_child_again(self):
        child = _play(self.root, 'e2e3', backed_move='e7e6', backed_eval=300,
                      nodes_invested=ONCE, visits=1)

        made = ingest._queue_quality_convergence([('e2e3', self.root.key, B2)])

        self.assertEqual(made, 0)
        self.assertFalse(AnalysisTask.objects.filter(position=child).exists())

    def test_the_quality_arm_buys_an_unsearched_interior_child_at_128m(self):
        child = _play(self.root, 'e2e3', backed_move='e7e6', backed_eval=300)

        ingest._queue_quality_convergence([('e2e3', self.root.key, B2)])

        self.assertEqual(AnalysisTask.objects.get(position=child).budget_nodes,
                         M128)

    def test_the_quality_arm_still_matches_on_a_leaf(self):
        child = _play(self.root, 'e2e3', nodes_invested=ONCE, visits=1)

        ingest._queue_quality_convergence([('e2e3', self.root.key, B2)])

        self.assertEqual(AnalysisTask.objects.get(position=child).budget_nodes,
                         B2)

    def test_a_request_still_climbs_on_an_interior_position(self):
        child = _play(self.root, 'e2e3', backed_move='e7e6', backed_eval=300,
                      nodes_invested=ONCE, visits=1)
        AnalysisTask.objects.create(position=child, generation=0,
                                    budget_nodes=M128, state=COMPLETED,
                                    nodes_searched=ONCE)

        ingest.request_analysis(child, requested_by='alice')

        task = AnalysisTask.objects.get(position=child, state=PENDING)
        self.assertEqual(task.source, USER)
        self.assertEqual(task.budget_nodes, M512)


# ---------------------------------------------------------------------------
# The sweep rule.
# ---------------------------------------------------------------------------
class SweepRuleTests(SimpleTestCase):

    def _pos(self, stm='w', **fields):
        base = {'status': 'UNKNOWN', 'visits': 1, 'nodes_invested': ONCE,
                'eval_cp': 2000}
        base.update(fields)
        return Position(key='k', fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - -', **base)

    def test_white_to_move_and_winning_by_16_to_90(self):
        self.assertTrue(sweep.qualifies(self._pos(eval_cp=1600)))
        self.assertTrue(sweep.qualifies(self._pos(eval_cp=8999)))
        self.assertFalse(sweep.qualifies(self._pos(eval_cp=1599)))
        self.assertFalse(sweep.qualifies(self._pos(eval_cp=9000)))

    def test_black_to_move_and_winning_too(self):
        self.assertTrue(sweep.qualifies(self._pos('b', eval_cp=-2000)))
        self.assertFalse(sweep.qualifies(self._pos('b', eval_cp=2000)))
        self.assertFalse(sweep.qualifies(self._pos('w', eval_cp=-2000)))

    def test_the_value_shown_is_the_backed_one(self):
        self.assertFalse(sweep.qualifies(self._pos(eval_cp=2000,
                                                   backed_eval=1200)))
        self.assertTrue(sweep.qualifies(self._pos(eval_cp=900,
                                                  backed_eval=1700)))

    def test_only_one_search_of_about_128m(self):
        self.assertFalse(sweep.qualifies(self._pos(visits=2)))
        self.assertFalse(sweep.qualifies(self._pos(visits=0)))
        self.assertFalse(sweep.qualifies(self._pos(nodes_invested=32_000_000)))
        self.assertFalse(sweep.qualifies(self._pos(nodes_invested=512_000_000)))

    def test_only_open_positions(self):
        self.assertFalse(sweep.qualifies(self._pos(status='WHITE_WIN')))

    def test_no_value_no_sweep(self):
        self.assertFalse(sweep.qualifies(self._pos(eval_cp=None)))


# ---------------------------------------------------------------------------
# The list: the strategy tree first.
# ---------------------------------------------------------------------------
class SweepPlanTests(TestCase):
    """root -e2e3-> B (Black) -> e7e6 / d7d6 / c7c6 ; root -a2a3-> off tree.

    White's value at the root stands on e2e3, so a2a3 and what follows it
    are outside White's strategy tree.
    """

    def setUp(self):
        self.root = _root()
        self.black = _play(self.root, 'e2e3', visits=1, nodes_invested=ONCE,
                           backed_move='e7e6', backed_eval=2500)
        Position.objects.filter(key=self.root.key).update(
            backed_move='e2e3', backed_eval=2500, visits=1)
        self.in_tree = _play(self.black, 'e7e6', **_candidate(eval_cp=1700))
        self.in_tree_low = _play(self.black, 'd7d6', **_candidate(eval_cp=900))
        self.in_tree_closed = _play(self.black, 'c7c6',
                                    status='WHITE_WIN', **_candidate())
        self.off_black = _play(self.root, 'a2a3', visits=1,
                               nodes_invested=ONCE, eval_cp=100)
        self.off_tree = _play(self.off_black, 'e7e6',
                              **_candidate(eval_cp=3000))
        self.off_tree_high = _play(self.off_black, 'd7d6',
                                   **_candidate(eval_cp=5000))
        # Black to move and winning, in a line White does not play.
        self.black_wins = _play(self.off_tree, 'h2h3',
                                **_candidate(eval_cp=-2500))

    def test_the_walk_finds_only_what_the_tree_holds(self):
        keys, stats = sweep.walk_strategy_tree()

        self.assertEqual(keys, [self.in_tree.key])
        self.assertEqual(stats['stopped'], 'exhausted')

    def test_the_walk_follows_the_best_child_without_a_backed_move(self):
        Position.objects.filter(key=self.root.key).update(backed_move=None)
        Position.objects.filter(key=self.black.key).update(eval_cp=2500)
        Position.objects.filter(key=self.off_black.key).update(
            backed_eval=None, eval_cp=-100)

        keys, _stats = sweep.walk_strategy_tree()

        self.assertEqual(keys, [self.in_tree.key])

    def test_the_walk_stops_at_its_node_cap(self):
        keys, stats = sweep.walk_strategy_tree(max_nodes=1)

        self.assertEqual(keys, [])
        self.assertEqual(stats['stopped'], 'max_nodes')

    def test_the_plan_puts_the_tree_first_then_the_highest_value(self):
        plan = sweep.build_plan(save=False)

        self.assertEqual(plan['keys'], [self.in_tree.key,
                                        self.off_tree_high.key,
                                        self.off_tree.key,
                                        self.black_wins.key])
        self.assertEqual(plan['tree_count'], 1)
        self.assertEqual(plan['rest_count'], 3)


# ---------------------------------------------------------------------------
# The drip.
# ---------------------------------------------------------------------------
@override_settings(**OPEN_RATE)
class SweepDripTests(TestCase):

    def setUp(self):
        sweep.forget_plan()
        self.root = _root()
        self.parent = _play(self.root, 'e2e3', visits=1, nodes_invested=ONCE,
                            eval_cp=0)
        self.keys = []
        for uci in ('e7e6', 'd7d6', 'c7c6', 'b7b6', 'a7a6'):
            self.keys.append(_play(self.parent, uci, **_candidate()).key)
        self.addCleanup(sweep.forget_plan)

    def _plan(self, keys=None):
        sweep.save_plan({'build_id': 'b1', 'keys': keys or self.keys})

    def _swept(self):
        return AnalysisTask.objects.filter(arm=sweep.SWEEP_ARM)

    def test_without_a_list_nothing_is_queued(self):
        self.assertEqual(sweep.drip(), 0)
        self.assertFalse(self._swept().exists())

    def test_it_queues_one_line_of_128m_in_the_fill_band(self):
        self._plan()

        self.assertEqual(sweep.drip(), 5)

        for task in self._swept():
            self.assertEqual(task.source, FILL)
            self.assertEqual(task.budget_nodes, M128)
            self.assertEqual(task.multipv, 1)
            self.assertEqual(task.generation, 1)
            self.assertEqual(task.state, PENDING)
        self.assertEqual(DBEvent.objects.get(kind=sweep.SWEEP_EVENT)
                         .payload['queued'], 5)

    def test_it_only_tops_up_to_its_cap_in_list_order(self):
        self._plan()

        self.assertEqual(sweep.drip(cap=2), 2)
        self.assertEqual(sorted(self._swept().values_list(
            'position_id', flat=True)), sorted(self.keys[:2]))
        self.assertEqual(sweep.drip(cap=2), 0)

        self._swept().update(state=COMPLETED)
        self.assertEqual(sweep.drip(cap=2), 2)
        self.assertEqual(sorted(self._swept().filter(state=PENDING)
                                .values_list('position_id', flat=True)),
                         sorted(self.keys[2:4]))

    def test_every_key_is_checked_again(self):
        Position.objects.filter(key=self.keys[0]).update(status='WHITE_WIN')
        Position.objects.filter(key=self.keys[1]).update(visits=2)
        AnalysisTask.objects.create(position_id=self.keys[2], generation=1,
                                    budget_nodes=M128, source=AUTO)
        self._plan()

        self.assertEqual(sweep.drip(), 2)
        self.assertEqual(sorted(self._swept().values_list(
            'position_id', flat=True)), sorted(self.keys[3:]))

    def test_a_position_whose_parents_are_all_proven_is_passed_over(self):
        Position.objects.filter(key=self.parent.key).update(
            status='BLACK_WIN')
        self._plan()

        self.assertEqual(sweep.drip(), 0)

    def test_the_arm_at_zero_stops_it(self):
        self._plan()
        with override_settings(ATOMICDB_ARM_RATE_SWEEP16='0'):
            self.assertEqual(sweep.drip(), 0)

    def test_the_hourly_ceiling_clips_the_batch(self):
        self._plan()
        with override_settings(ATOMICDB_ARM_RATE_SWEEP16='3'):
            self.assertEqual(sweep.drip(), 3)
            self._swept().update(state=COMPLETED)
            self.assertEqual(sweep.drip(), 0)

    @override_settings(ATOMICDB_SWEEP16_SHARE='0')
    def test_it_waits_while_the_requests_pile_up(self):
        self._plan()
        AnalysisTask.objects.create(position=self.root, generation=0,
                                    budget_nodes=M128, source=USER)

        self.assertEqual(sweep.drip(user_backlog_max=0), 0)
        self.assertEqual(sweep.drip(user_backlog_max=1), 5)

    def test_with_a_share_it_queues_whatever_the_requests(self):
        self._plan()
        AnalysisTask.objects.create(position=self.root, generation=0,
                                    budget_nodes=M128, source=USER)

        self.assertEqual(sweep.drip(user_backlog_max=0), 5)
        self.assertTrue(sweep.has_waiting())

    def test_the_scan_is_bounded(self):
        Position.objects.filter(key__in=self.keys[:3]).update(visits=2)
        self._plan()

        self.assertEqual(sweep.drip(scan=2), 0)
        self.assertEqual(sweep.drip(scan=2), 1)
        self.assertEqual(sweep.status()['plan']['cursor'], 4)

    def test_a_new_list_starts_at_its_head(self):
        self._plan()
        sweep.drip(cap=1)
        sweep.save_plan({'build_id': 'b2', 'keys': list(reversed(self.keys))})

        sweep.drip(cap=2)

        self.assertIn(self.keys[-1], self._swept().values_list(
            'position_id', flat=True))

    def test_the_walk_resumes_from_disk(self):
        with tempfile.TemporaryDirectory() as root, \
                override_settings(ATOMICDB_STATE_DIR=root):
            self._plan()
            sweep.drip(cap=2)
            self.assertTrue(os.path.exists(os.path.join(root,
                                                        sweep.PLAN_FILE)))
            with open(os.path.join(root, sweep.CURSOR_FILE)) as handle:
                self.assertEqual(json.load(handle),
                                 {'build_id': 'b1', 'cursor': 2})
            sweep._memory.update(plan=None, cursor=None, plan_mtime=None)
            self._swept().update(state=COMPLETED)

            sweep.drip(cap=1)

            self.assertEqual(self._swept().filter(state=PENDING).get()
                             .position_id, self.keys[2])
            sweep.forget_plan()
            self.assertFalse(os.path.exists(os.path.join(root,
                                                         sweep.PLAN_FILE)))

    def _searched_before(self):
        """The search of 128M each candidate already had, a month ago."""
        for key in self.keys:
            AnalysisTask.objects.create(
                position_id=key, generation=0, budget_nodes=M128,
                source=USER, state=COMPLETED, machine='m1',
                nodes_searched=ONCE,
                completed=timezone.now() - timedelta(days=30))

    def test_the_hourly_absorb_pass_leaves_its_tasks_alone(self):
        # A sweep task repeats a search its position already had, which is
        # the shape of a shadowed duplicate.  The pass took the whole queue
        # every hour (8,860 tasks, 11 to 13 Nov 2026).
        self._searched_before()
        self._plan()
        sweep.drip()

        call_command('absorb_shadowed_tasks', stdout=StringIO())

        self.assertEqual(self._swept().filter(state=PENDING).count(), 5)

    def test_a_search_that_lands_while_it_waits_still_absorbs_it(self):
        self._plan()
        sweep.drip()
        AnalysisTask.objects.create(
            position_id=self.keys[0], generation=0, budget_nodes=M512,
            source=USER, state=COMPLETED, machine='m1',
            nodes_searched=M512, completed=timezone.now())

        call_command('absorb_shadowed_tasks', stdout=StringIO())

        self.assertEqual(self._swept().get(position_id=self.keys[0]).state,
                         COMPLETED)
        self.assertEqual(self._swept().filter(state=PENDING).count(), 4)

    def test_an_absorbed_task_is_queued_again_from_the_head(self):
        self._plan()
        sweep.drip()
        first = dict(self._swept().values_list('id', 'created'))
        ingest.absorb_tasks(self._swept())
        self.assertEqual(sweep.drip(), 0)          # the walk is past them

        self.assertEqual(sweep.rewind(), 'b1')
        self.assertEqual(sweep.drip(), 5)

        for task in self._swept():
            self.assertEqual(task.state, PENDING)
            self.assertIsNone(task.completed)
            self.assertGreaterEqual(task.created, first[task.id])
        self.assertEqual(self._swept().count(), 5)

    def test_a_revived_task_counts_against_the_cap(self):
        self._plan()
        sweep.drip()
        ingest.absorb_tasks(self._swept())
        sweep.rewind()

        self.assertEqual(sweep.drip(cap=2), 2)

        self.assertEqual(self._swept().filter(state=PENDING).count(), 2)

    def test_a_task_somebody_searched_is_not_queued_again(self):
        self._plan()
        sweep.drip()
        self._swept().update(state=COMPLETED, machine='m1',
                             nodes_searched=ONCE, completed=timezone.now())
        sweep.rewind()

        self.assertEqual(sweep.drip(), 0)

        self.assertFalse(self._swept().filter(state=PENDING).exists())

    def test_another_arms_absorbed_task_is_left_alone(self):
        self._plan()
        other = AnalysisTask.objects.create(
            position_id=self.keys[0], generation=1, budget_nodes=M128,
            source=AUTO, arm=ingest.VALUE_DESCEND_ARM)
        ingest.absorb_tasks(AnalysisTask.objects.filter(pk=other.pk))

        self.assertEqual(sweep.drip(), 4)

        other.refresh_from_db()
        self.assertEqual(other.state, COMPLETED)

    def test_rewinding_without_a_list_does_nothing(self):
        self.assertIsNone(sweep.rewind())

    def test_the_command_rewinds(self):
        self._plan()
        sweep.drip()
        ingest.absorb_tasks(self._swept())
        out = StringIO()

        call_command('sweep_plus16', '--rewind', '--drip', stdout=out)

        report = json.loads(out.getvalue())
        self.assertEqual(report['rewound'], 'b1')
        self.assertEqual(report['queued'], 5)

    def test_the_command_stops_and_withdraws(self):
        self._plan()
        sweep.drip()
        out = StringIO()

        call_command('sweep_plus16', '--withdraw', stdout=out)

        self.assertEqual(json.loads(out.getvalue())['withdrawn'], 5)
        self.assertFalse(self._swept().exists())
        self.assertEqual(sweep.drip(), 0)

    def test_the_command_rebuilds_and_reports(self):
        out = StringIO()

        call_command('sweep_plus16', '--rebuild', '--drip', stdout=out)

        report = json.loads(out.getvalue())
        self.assertEqual(report['rebuilt']['tree_count']
                         + report['rebuilt']['rest_count'], 5)
        self.assertEqual(report['queued'], 5)

    def test_a_dry_run_stores_nothing(self):
        call_command('sweep_plus16', '--rebuild', '--dry-run',
                     stdout=StringIO())

        self.assertIsNone(sweep.load_plan())

    def test_the_selector_service_runs_the_step(self):
        self._plan()
        out = StringIO()

        call_command('refresh_selector', '--no-priorities', '--no-debt',
                     '--no-coverage', '--no-adversarial', '--autosolve-cap',
                     '0', '--no-repair-backlog', '--pool-target', '0',
                     stdout=out)

        report = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(report['sweep16_enqueued'], 5)
        self.assertNotIn('failed_steps', report)

    def test_the_pool_does_not_count_it(self):
        self._plan()
        sweep.drip()
        with patch.object(ingest, 'next_tasks', return_value=[]) as minted:
            ingest.top_up_analysis_pool(target=3)
        minted.assert_called_once_with(3)


# ---------------------------------------------------------------------------
# Where it goes in the queue, and how it shows.
# ---------------------------------------------------------------------------
@override_settings(ATOMICDB_SWEEP16_SHARE='0')
class SweepServiceOrderTests(TestCase):

    def setUp(self):
        worker_account('w', 'pw')
        self.root = _root()
        self.swept = AnalysisTask.objects.create(
            position=_play(self.root, 'a2a3', **_candidate()), generation=1,
            budget_nodes=M128, multipv=1, source=FILL, arm=sweep.SWEEP_ARM)
        self.auto = AnalysisTask.objects.create(
            position=_play(self.root, 'b2b3', priority=1e9), generation=0,
            budget_nodes=M128, source=AUTO)

    def _lease(self):
        response = self.client.post('/atomicdb/api/lease', {
            'username': 'w', 'password': 'pw', 'machine': 'box',
            'worker_build': '2026072203', 'lease_session': 'box',
            'threads': '4'})
        self.assertEqual(response.status_code, 200)
        return [task['id'] for task in response.json()['tasks']]

    def test_it_goes_before_the_selectors_own_work(self):
        self.assertEqual(self._lease(), [self.swept.id])

    def test_a_request_still_goes_first(self):
        clicked = AnalysisTask.objects.create(
            position=_play(self.root, 'c2c3'), generation=0,
            budget_nodes=M128, source=USER, requested_by='alice')

        self.assertEqual(self._lease(), [clicked.id])

    def test_it_shows_as_a_server_re_search(self):
        row = {'source': FILL, 'arm': sweep.SWEEP_ARM}
        self.assertEqual(active_workers._requester(row),
                         {'requester': 'server', 'reason': 'sweep'})

    def test_its_receipt_stays_out_of_the_feed(self):
        self.assertIn(sweep.SWEEP_EVENT, views.FEED_HIDDEN_KINDS)


# ---------------------------------------------------------------------------
# The fleet share: a fixed fraction of the leases, ahead of the requests.
# ---------------------------------------------------------------------------
class SweepShareRuleTests(TestCase):

    def setUp(self):
        self.root = _root()

    def _waiting(self):
        AnalysisTask.objects.create(
            position=_play(self.root, 'a2a3', **_candidate()), generation=1,
            budget_nodes=M128, multipv=1, source=FILL, arm=sweep.SWEEP_ARM)

    def test_the_share_reads_its_setting(self):
        self.assertEqual(sweep.share(), sweep.SHARE_DEFAULT)
        for raw, value in (('0', 0.0), ('0.3', 0.3), ('7', 1.0),
                           ('-1', 0.0), ('nonsense', sweep.SHARE_DEFAULT)):
            with override_settings(ATOMICDB_SWEEP16_SHARE=raw):
                self.assertEqual(sweep.share(), value)

    def test_zero_never_takes_it(self):
        self._waiting()
        self.assertFalse(any(sweep.take_share(0.0) for _ in range(50)))

    def test_one_always_takes_it_when_there_is_something(self):
        self._waiting()
        self.assertTrue(all(sweep.take_share(1.0) for _ in range(50)))

    def test_nothing_waiting_nothing_taken(self):
        self.assertFalse(sweep.take_share(1.0))

    def test_the_fraction_is_exact_and_spread(self):
        self._waiting()
        taken = [sweep.take_share(0.14) for _ in range(1000)]

        self.assertEqual(sum(taken), 140)
        # Evenly spread: never two in a row at 14%.
        self.assertFalse(any(a and b for a, b in zip(taken, taken[1:])))

    def test_without_a_cache_it_falls_back_to_a_coin(self):
        self._waiting()
        with patch.object(sweep, '_next_lease_number', return_value=None):
            taken = sum(sweep.take_share(0.5) for _ in range(2000))
        self.assertGreater(taken, 850)
        self.assertLess(taken, 1150)

    def test_waiting_is_cached(self):
        sweep.note_waiting(False)
        self._waiting()
        with self.assertNumQueries(0, using=sweep.AnalysisTask.objects.db):
            self.assertFalse(sweep.has_waiting())
        from django.core.cache import cache
        cache.delete(sweep.WAITING_CACHE_KEY)
        self.assertTrue(sweep.has_waiting())


class SweepShareLeaseTests(TestCase):

    def setUp(self):
        worker_account('w', 'pw')
        self.root = _root()
        self.clicked = AnalysisTask.objects.create(
            position=_play(self.root, 'c2c3'), generation=0,
            budget_nodes=M128, source=USER, requested_by='alice')

    def _sweep_task(self, uci='a2a3'):
        return AnalysisTask.objects.create(
            position=_play(self.root, uci, **_candidate()), generation=1,
            budget_nodes=M128, multipv=1, source=FILL, arm=sweep.SWEEP_ARM)

    def _lease(self, machine='box', threads='4'):
        response = self.client.post('/atomicdb/api/lease', {
            'username': 'w', 'password': 'pw', 'machine': machine,
            'worker_build': '2026072203', 'lease_session': machine,
            'threads': threads})
        self.assertEqual(response.status_code, 200)
        return [task['id'] for task in response.json()['tasks']]

    @override_settings(ATOMICDB_SWEEP16_SHARE='1')
    def test_a_share_of_one_serves_the_sweep_before_a_request(self):
        swept = self._sweep_task()

        self.assertEqual(self._lease(), [swept.id])

    @override_settings(ATOMICDB_SWEEP16_SHARE='0')
    def test_a_share_of_zero_keeps_the_old_order(self):
        self._sweep_task()

        self.assertEqual(self._lease(), [self.clicked.id])

    @override_settings(ATOMICDB_SWEEP16_SHARE='1')
    def test_with_the_sweep_empty_the_request_is_served(self):
        self.assertEqual(self._lease(), [self.clicked.id])

    @override_settings(ATOMICDB_SWEEP16_SHARE='1')
    def test_a_stale_yes_is_corrected_by_the_lease(self):
        sweep.note_waiting(True)

        self.assertEqual(self._lease(), [self.clicked.id])
        from django.core.cache import cache
        self.assertIs(cache.get(sweep.WAITING_CACHE_KEY), False)

    @override_settings(ATOMICDB_SWEEP16_SHARE='1')
    def test_a_closed_position_is_not_served_by_the_share(self):
        swept = self._sweep_task()
        Position.objects.filter(key=swept.position_id).update(
            status='WHITE_WIN')

        self.assertEqual(self._lease(), [self.clicked.id])

    @override_settings(ATOMICDB_SWEEP16_SHARE='0.25')
    def test_over_many_leases_the_share_is_kept(self):
        # Forty requests on forty distinct positions: the replies to two
        # first moves the sweep does not use.
        replies = []
        for first in ('c2c3', 'g1f3'):
            parent = _play(self.root, first)
            replies.extend(_play(parent, uci)
                           for uci in logic.legal_moves(parent.fen)[:20])
        for pos in replies:
            AnalysisTask.objects.create(position=pos, generation=0,
                                        budget_nodes=M128, source=USER,
                                        requested_by='bob')
        swept = [self._sweep_task(uci).id
                 for uci in ('a2a3', 'b2b3', 'd2d3', 'e2e3', 'f2f3',
                             'g2g3', 'h2h3', 'a2a4', 'b2b4', 'd2d4')]

        served = []
        for index in range(40):
            served.extend(self._lease(machine=f'box{index}'))

        self.assertEqual(sum(1 for task in served if task in swept), 10)
