"""Mates the engine already reported are handed to the solver, once.

What these tests pin, and why each one:

* a line scores a mate when its value is beyond the clamp, and its sign is the
  goal; exactly +-9500 (a tablebase win or a huge eval, no distance) and
  anything short of it is not a claim, and neither is a line whose mate score
  contradicts its value;
* a landing whose showcase scores a mate buys ONE F0 pass of 8M for the
  position, and one for the child each mate line goes through, one ply and no
  deeper, with the goal of the side that mates; the children of a closed
  position wait, and a closed or dead position is never bought;
* once is forever: a pass of this arm in any state, a pass of any arm on its
  way, or a completed pass at least as big for the same goal, means no new
  pass, so a failed one cannot start the loop the full scan had;
* the arm has its own cap of waiting passes, which holds even passes left on a
  position that closed (the solver serves them), its own hourly ceiling
  counted on its own rows, and ``0`` turns it off, from the environment too;
* the walk down the backlog reads a bounded step of the completed searches,
  resumes where it stopped (on disk when there is a state directory), leaves
  half the room to landings, costs one count while the queue is full, and goes
  round;
* nothing scans the tree: a position at the band that no search reached is
  not found, whatever the selector's cap says;
* what is bought is what the solver leases, and a certificate for it closes the
  position through the machinery that was already there;
* it shows as one hidden event per batch, and a failure in the arm never costs
  the landing it rides on.
"""

import hashlib
import json
import os
import tempfile
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connections
from django.test import SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import autosolve, ingest, logic, solve, views
from .models import AnalysisTask, DBEvent, Edge, Position, SolveTask
from .test_solve import MATE_IN_ONE_CERT, MATE_IN_ONE_FEN
from .testing import TestCase, worker_account

ARM = autosolve.AUTOSOLVE_ARM
NODES = autosolve.AUTOSOLVE_NODES
CAP = autosolve.AUTOSOLVE_QUEUE_CAP
HALF = autosolve.AUTOSOLVE_BACKLOG_TARGET
PENDING = SolveTask.TState.PENDING
LEASED = SolveTask.TState.LEASED
COMPLETED = SolveTask.TState.COMPLETED
FAILED = SolveTask.TState.FAILED
DONE = AnalysisTask.TState.COMPLETED
FLOOR = 128_000_000
START = logic.start_fen()
TIGHT = {'ATOMICDB_ARM_RATE_AUTOSOLVE': '4'}


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **fields):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **fields)


def _mate(move, moves, **extra):
    """A line through ``move`` scoring a mate in ``moves``, as the worker
    writes it: positive when White mates."""
    sign = 1 if moves > 0 else -1
    return dict({'move': move, 'eval_cp': (10_000 - abs(moves)) * sign,
                 'mate': moves, 'pv': [move]}, **extra)


def _cp(move, score, **extra):
    return dict({'move': move, 'eval_cp': score, 'mate': None,
                 'pv': [move]}, **extra)


def _showcase(*lines, nodes=FLOOR):
    """``last_analysis`` as an ingest leaves it: the pass's nodes on line 1."""
    stored = [dict(line) for line in lines]
    if stored:
        stored[0]['_budget'] = nodes
    return stored


def _searched(name, *lines, stm='b', **fields):
    """A position searched before, with its showcase and its search."""
    fields.setdefault('last_analysis', _showcase(*lines))
    pos = _pos(name, stm=stm, visits=1, **fields)
    AnalysisTask.objects.create(position=pos, generation=0,
                                budget_nodes=FLOOR, state=DONE,
                                nodes_searched=FLOOR)
    return pos


def _child(parent, move, **fields):
    child = _pos(f'{parent.key[:12]}-{move}', **fields)
    Edge.objects.create(parent=parent, move_uci=move, child=child)
    return child


def _root():
    return ingest.get_or_create_position(START)


def _play(parent, uci):
    """One edge from ``parent``, the way the explorer's goto makes it."""
    child = ingest.get_or_create_position(logic.apply_move(parent.fen, uci))
    Edge.objects.get_or_create(parent=parent, move_uci=uci,
                               defaults={'child': child})
    return child


def _over(fen, *specs):
    """A pass whose N-th line goes through the N-th legal move of ``fen``.

    A spec is ``('mate', moves)`` or ``('cp', score)``.
    """
    lines = []
    for uci, (kind, value) in zip(logic.legal_moves(fen), specs):
        lines.append(_mate(uci, value) if kind == 'mate' else _cp(uci, value))
    return lines


def _land(pos, *specs, nodes=FLOOR):
    return ingest.ingest_analysis(pos.key, _over(pos.fen, *specs), nodes)


def _through(pos, index):
    """The child of ``pos`` through its ``index``-th legal move."""
    uci = logic.legal_moves(pos.fen)[index]
    return Edge.objects.get(parent=pos, move_uci=uci).child


def _passes(**filters):
    return SolveTask.objects.filter(arm=ARM, **filters)


def _waiting(count, status='UNKNOWN'):
    """``count`` passes of this arm waiting on as many positions."""
    for index in range(count):
        holder = _pos(f'waiting-{status}-{index}', status=status)
        SolveTask.objects.create(position=holder, goal='WHITE_WIN',
                                 budget_stage='F0', budget_nodes=NODES,
                                 arm=ARM)


def _spend(count, minutes_ago=0):
    """``count`` passes of this arm already bought, ``minutes_ago`` back."""
    holder = _pos(f'spent-{minutes_ago}-{count}', status='WHITE_WIN')
    SolveTask.objects.bulk_create([
        SolveTask(position=holder, goal='WHITE_WIN', budget_stage='F0',
                  budget_nodes=NODES, arm=ARM, state=COMPLETED,
                  outcome='UNKNOWN')
        for _index in range(count)])
    if minutes_ago:
        SolveTask.objects.filter(position=holder).update(
            created=timezone.now() - timedelta(minutes=minutes_ago))


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
class MateRuleTests(SimpleTestCase):

    def test_a_mate_score_names_the_side_that_mates(self):
        self.assertEqual(autosolve.mate_goal(_mate('a', 5)), 'WHITE_WIN')
        self.assertEqual(autosolve.mate_goal(_mate('a', -3)), 'BLACK_WIN')
        # The longest mate the worker can still write beyond the clamp.
        self.assertEqual(autosolve.mate_goal(_cp('a', -9501)), 'BLACK_WIN')

    def test_the_clamp_is_not_a_mate(self):
        """+-9500 is a tablebase win or a huge eval, with no distance."""
        for score in (9500, -9500, 9499, -9000, 0):
            self.assertIsNone(autosolve.mate_goal(_cp('a', score)))
        self.assertEqual(ingest.TB_CLAMP_CEILING,
                         ingest.AUTOSOLVE95_THRESHOLD)

    def test_a_line_that_contradicts_itself_claims_nothing(self):
        self.assertIsNone(autosolve.mate_goal(_cp('a', 9995, mate=-5)))

    def test_nothing_else_is_a_claim(self):
        for line in (None, {}, 'e2e4', {'eval_cp': None},
                     {'eval_cp': '9995'}, {'eval_cp': True},
                     _cp('a', 10_000), _cp('a', -10_000)):
            self.assertIsNone(autosolve.mate_goal(line))

    def test_only_the_current_pass_claims_and_shortest_mates_come_first(self):
        stored = _showcase(_mate('a', 9), _cp('b', 40), _mate('c', 3),
                           _mate('a', 2)) + [
            dict(_mate('d', 1), prior_pass=True)]

        own, moves = autosolve.claims(stored)

        self.assertEqual(own, 'WHITE_WIN')
        self.assertEqual(moves, [('c', 'WHITE_WIN'), ('a', 'WHITE_WIN')])

    def test_a_showcase_whose_first_line_is_no_mate_claims_its_children(self):
        own, moves = autosolve.claims(_showcase(_cp('a', 120),
                                                _mate('b', -2)))

        self.assertIsNone(own)
        self.assertEqual(moves, [('b', 'BLACK_WIN')])

    def test_an_empty_showcase_claims_nothing(self):
        self.assertEqual(autosolve.claims(None), (None, []))
        self.assertEqual(autosolve.claims([]), (None, []))
        self.assertEqual(autosolve.claims(
            [dict(_mate('a', 2), prior_pass=True)]), (None, []))


# ---------------------------------------------------------------------------
# Found where a search lands.
# ---------------------------------------------------------------------------
class LandingTests(TestCase):

    def setUp(self):
        self.root = _root()

    def test_a_reported_mate_buys_the_position_and_the_child(self):
        summary = _land(self.root, ('mate', 5), ('cp', 40), ('cp', 30))

        self.assertEqual(summary['autosolved'], 2)
        child = _through(self.root, 0)
        passes = {task.position_id: task for task in _passes()}
        self.assertEqual(set(passes), {self.root.key, child.key})
        for task in passes.values():
            self.assertEqual(task.goal, 'WHITE_WIN')
            self.assertEqual((task.budget_stage, task.budget_nodes),
                             ('F0', NODES))
            self.assertEqual(task.state, PENDING)
        # The landing itself proved nothing: the line's PV stops at its first
        # move, so the child stays open with the line's value seeded.
        child.refresh_from_db()
        self.assertEqual((child.status, child.eval_cp), ('UNKNOWN', 9995))

    def test_the_goal_is_the_side_that_mates_not_the_side_to_move(self):
        """The pilot asked WHITE_WIN of positions at -10000."""
        # White to move and mated: the first line is the longest defence.
        _land(self.root, ('mate', -6), ('mate', -4))
        black_to_move = _play(self.root, logic.legal_moves(START)[10])
        _land(black_to_move, ('mate', 6), ('mate', 3))

        goals = dict(_passes().values_list('position_id', 'goal'))
        self.assertEqual(len(goals), 6)
        for pos, goal in ((self.root, 'BLACK_WIN'),
                          (black_to_move, 'WHITE_WIN')):
            self.assertEqual(goals[pos.key], goal)
            self.assertEqual(goals[_through(pos, 0).key], goal)
            self.assertEqual(goals[_through(pos, 1).key], goal)

    def test_one_ply_and_no_deeper(self):
        lines = _over(START, ('mate', 4))
        move = lines[0]['move']
        after = logic.apply_move(START, move)
        reply = logic.legal_moves(after)[0]
        lines[0]['pv'] = [move, reply, logic.legal_moves(
            logic.apply_move(after, reply))[0]]

        ingest.ingest_analysis(self.root.key, lines, FLOOR)

        self.assertEqual(set(_passes().values_list('position_id', flat=True)),
                         {self.root.key, _through(self.root, 0).key})

    def test_the_clamp_and_lines_short_of_a_mate_buy_nothing(self):
        summary = _land(self.root, ('cp', 9500), ('cp', 9400), ('cp', -9500))

        self.assertNotIn('autosolved', summary)
        self.assertFalse(_passes().exists())
        self.assertFalse(DBEvent.objects.filter(
            kind=autosolve.AUTOSOLVE_EVENT).exists())

    def test_a_landing_with_no_mate_costs_no_query(self):
        _land(self.root, ('cp', 40), ('cp', 30))
        self.root.refresh_from_db()

        _, queries = _queries(lambda: autosolve.queue_from_landing(self.root))

        self.assertEqual(queries, [])

    def test_a_mate_on_a_lower_line_buys_its_child_only(self):
        """A move that walks into a mate is a known mate move too."""
        _land(self.root, ('cp', 60), ('cp', 20), ('mate', -2))

        task = _passes().get()
        self.assertEqual(task.position_id, _through(self.root, 2).key)
        self.assertEqual(task.goal, 'BLACK_WIN')

    def test_each_batch_leaves_one_hidden_event(self):
        _land(self.root, ('mate', 5), ('mate', 7))

        event = DBEvent.objects.get(kind=autosolve.AUTOSOLVE_EVENT)
        self.assertEqual(event.payload['via'], 'landing')
        self.assertEqual(event.payload['key'], self.root.key)
        self.assertEqual((event.payload['queued'], event.payload['own'],
                          event.payload['child']), (3, 1, 2))
        self.assertEqual(event.payload['keys'][0], self.root.key)
        self.assertIn(autosolve.AUTOSOLVE_EVENT, views.FEED_HIDDEN_KINDS)

    def test_the_position_first_then_the_shortest_mates(self):
        black_to_move = _play(self.root, logic.legal_moves(START)[10])
        _waiting(CAP - 2)

        # Black to move and mated: the first line is the longest defence.
        _land(black_to_move, ('mate', 9), ('mate', 3), ('mate', 5))

        event = DBEvent.objects.get(kind=autosolve.AUTOSOLVE_EVENT)
        self.assertEqual(event.payload['keys'],
                         [black_to_move.key, _through(black_to_move, 1).key])


class DirectLandingTests(TestCase):
    """The same rule on positions built by hand, for the near misses."""

    def test_a_closed_position_and_its_children_are_left_alone(self):
        pos = _searched('closed', _mate('e8e7', 4), _mate('e8d8', 6),
                        status='WHITE_WIN', closure='MINIMAX')
        _child(pos, 'e8e7')
        _child(pos, 'e8d8')

        self.assertEqual(autosolve.queue_from_landing(pos), 0)
        self.assertFalse(_passes().exists())

    def test_a_closed_child_is_not_bought(self):
        pos = _searched('open', _mate('e8e7', 4), _mate('e8d8', 6))
        _child(pos, 'e8e7', status='WHITE_WIN', closure='MATE_PV')
        open_child = _child(pos, 'e8d8')

        self.assertEqual(autosolve.queue_from_landing(pos), 2)
        self.assertEqual(set(_passes().values_list('position_id', flat=True)),
                         {pos.key, open_child.key})

    def test_a_dead_branch_is_not_bought(self):
        pos = _searched('alive', _mate('e8e7', 4))
        _child(pos, 'e8e7', priority=ingest.DEAD)

        self.assertEqual(autosolve.queue_from_landing(pos), 1)
        self.assertEqual(_passes().get().position_id, pos.key)

    def test_a_move_the_tree_does_not_know_buys_nothing(self):
        pos = _searched('no-edge', _cp('e8e7', 30), _mate('e8f8', -3))

        self.assertEqual(autosolve.queue_from_landing(pos), 0)

    def test_two_moves_that_reach_one_position_buy_it_once(self):
        """Two captures can blow up the same squares."""
        pos = _searched('same-blast', _cp('e8e7', -30), _mate('d8d1', 3),
                        _mate('h4d1', 5))
        shared = _child(pos, 'd8d1')
        Edge.objects.create(parent=pos, move_uci='h4d1', child=shared)

        self.assertEqual(autosolve.queue_from_landing(pos), 1)
        self.assertEqual(_passes().get().position_id, shared.key)


# ---------------------------------------------------------------------------
# Once is forever.
# ---------------------------------------------------------------------------
class NeverAgainTests(TestCase):

    def setUp(self):
        self.pos = _searched('asked', _mate('e8e7', 5))

    def _asked(self, **fields):
        fields.setdefault('arm', ARM)
        fields.setdefault('goal', 'WHITE_WIN')
        fields.setdefault('budget_nodes', NODES)
        return SolveTask.objects.create(position=self.pos, **fields)

    def test_a_second_landing_does_not_buy_again(self):
        self.assertEqual(autosolve.queue_from_landing(self.pos), 1)
        self.assertEqual(autosolve.queue_from_landing(self.pos), 0)

        self.assertEqual(_passes().count(), 1)

    def test_a_failed_pass_is_never_bought_again(self):
        """The full scan bought it again two minutes later, forever."""
        self._asked(state=FAILED, outcome='PROVED',
                    reject_reason='PROVED without a certificate')

        self.assertEqual(autosolve.queue_from_landing(self.pos), 0)
        self.assertEqual(autosolve.drip_backlog(), 0)
        self.assertEqual(_passes().count(), 1)

    def test_a_pass_that_proved_nothing_is_never_bought_again(self):
        self._asked(state=COMPLETED, outcome='UNKNOWN')

        self.assertEqual(autosolve.queue_from_landing(self.pos), 0)
        self.assertEqual(autosolve.drip_backlog(), 0)

    def test_a_pass_still_running_is_not_doubled(self):
        self._asked(state=LEASED, machine='box')

        self.assertEqual(autosolve.queue_from_landing(self.pos), 0)

    def test_another_arms_pass_on_its_way_is_the_same_question(self):
        self._asked(arm=ingest.FRAGILE_ARM, budget_nodes=2_000_000)

        self.assertEqual(autosolve.queue_from_landing(self.pos), 0)

    def test_a_bigger_completed_pass_answered_the_same_goal(self):
        self._asked(arm='mate_band', budget_stage='F4',
                    budget_nodes=100_000_000, state=COMPLETED,
                    outcome='UNKNOWN')

        self.assertEqual(autosolve.queue_from_landing(self.pos), 0)

    def test_but_not_the_other_goal_nor_a_smaller_pass(self):
        self._asked(arm='mate_band', budget_stage='F4', goal='BLACK_WIN',
                    budget_nodes=100_000_000, state=COMPLETED,
                    outcome='DISPROVED')
        self._asked(arm=ingest.FRAGILE_ARM, budget_nodes=2_000_000,
                    state=COMPLETED, outcome='UNKNOWN')

        self.assertEqual(autosolve.queue_from_landing(self.pos), 1)


# ---------------------------------------------------------------------------
# Its own cap and its own hourly ceiling.
# ---------------------------------------------------------------------------
class CapAndRateTests(TestCase):

    def setUp(self):
        self.pos = _searched('capped', _mate('e8e7', 5))

    def test_a_full_queue_buys_nothing(self):
        _waiting(CAP)

        self.assertEqual(autosolve.queue_from_landing(self.pos), 0)

    def test_passes_left_on_closed_positions_still_hold_their_place(self):
        """The solver serves them whatever their position became."""
        _waiting(CAP, status='WHITE_WIN')

        self.assertEqual(autosolve.queue_from_landing(self.pos), 0)

    def test_what_other_arms_queue_is_not_counted(self):
        for index in range(CAP):
            SolveTask.objects.create(
                position=_pos(f'debt-{index}', status='WHITE_WIN'),
                goal='WHITE_WIN', budget_stage='F0',
                budget_nodes=ingest.DEBT_STAGE_NODES, arm=ingest.DEBT_ARM)

        self.assertEqual(autosolve.queue_from_landing(self.pos), 1)

    def test_the_room_left_is_all_a_landing_takes(self):
        _child(self.pos, 'e8e7')
        _waiting(CAP - 1)

        self.assertEqual(autosolve.queue_from_landing(self.pos), 1)
        self.assertTrue(_passes(position=self.pos).exists())

    @override_settings(**TIGHT)
    def test_an_arm_at_its_hourly_count_buys_nothing(self):
        _spend(4)

        self.assertEqual(autosolve.queue_from_landing(self.pos), 0)
        self.assertTrue(DBEvent.objects.filter(
            kind=ingest.ARM_RATE_EVENT, payload__arm=ARM).exists())

    @override_settings(**TIGHT)
    def test_the_hour_rolls(self):
        _spend(4, minutes_ago=61)

        self.assertEqual(autosolve.queue_from_landing(self.pos), 1)

    @override_settings(**TIGHT)
    def test_the_ceiling_counts_this_arms_passes_only(self):
        for index in range(4):
            AnalysisTask.objects.create(position=_pos(f'rated-{index}'),
                                        generation=0, budget_nodes=FLOOR,
                                        arm=ARM)
            SolveTask.objects.create(
                position=_pos(f'fragile-{index}'), goal='WHITE_WIN',
                budget_stage='F0', budget_nodes=2_000_000,
                arm=ingest.FRAGILE_ARM, state=COMPLETED)

        self.assertEqual(autosolve.queue_from_landing(self.pos), 1)

    def test_the_default_ceiling(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ATOMICDB_ARM_RATE_AUTOSOLVE', None)
            self.assertEqual(ingest.arm_rate(ARM), 60)

    @override_settings(ATOMICDB_ARM_RATE_AUTOSOLVE='0')
    def test_zero_turns_the_arm_off(self):
        self.assertEqual(autosolve.queue_from_landing(self.pos), 0)
        self.assertEqual(autosolve.drip_backlog(), 0)

        self.assertFalse(_passes().exists())
        self.assertFalse(DBEvent.objects.filter(
            kind=ingest.ARM_RATE_EVENT).exists())

    def test_the_lever_works_from_the_environment(self):
        """The settings module does not mirror this knob."""
        with patch.dict(os.environ, {'ATOMICDB_ARM_RATE_AUTOSOLVE': '0'}):
            self.assertEqual(ingest.arm_rate(ARM), 0)
            _land(_root(), ('mate', 5))

        self.assertFalse(_passes().exists())


# ---------------------------------------------------------------------------
# Who takes them, and what a certificate for one does.
# ---------------------------------------------------------------------------
class ServingTests(TestCase):

    def setUp(self):
        worker_account('solver', 'pw')
        self.pos = ingest.get_or_create_position(MATE_IN_ONE_FEN)
        Position.objects.filter(key=self.pos.key).update(
            visits=1, last_analysis=_showcase(_mate('a1g7', 1)))
        self.pos.refresh_from_db()

    def _acquire(self):
        return self.client.post('/atomicdb/api/solve/acquire', {
            'username': 'solver', 'password': 'pw', 'machine': 'm1',
            'lease_session': 's1', 'threads': 1, 'hash': 256})

    def test_the_solver_leases_it_with_its_goal_and_stage(self):
        autosolve.queue_from_landing(self.pos)

        payload = self._acquire().json()['tasks'][0]

        self.assertEqual(payload['fen'], self.pos.fen)
        self.assertEqual((payload['goal'], payload['stage'],
                          payload['budget_nodes']), ('WHITE_WIN', 'F0', NODES))

    def test_it_goes_before_the_debt(self):
        debt = _pos('debt', status='WHITE_WIN')
        SolveTask.objects.create(position=debt, goal='WHITE_WIN',
                                 budget_stage='F0',
                                 budget_nodes=ingest.DEBT_STAGE_NODES,
                                 arm=ingest.DEBT_ARM)
        autosolve.queue_from_landing(self.pos)

        self.assertEqual(self._acquire().json()['tasks'][0]['fen'],
                         self.pos.fen)

    def test_a_verified_certificate_closes_the_position(self):
        autosolve.queue_from_landing(self.pos)
        payload = self._acquire().json()['tasks'][0]

        response = self.client.post('/atomicdb/api/solve/submit', {
            'username': 'solver', 'password': 'pw', 'machine': 'm1',
            'task_id': payload['id'], 'outcome': 'PROVED',
            'lease_token': payload['lease_token'], 'nodes': 2,
            'elapsed': '0.1',
            'certificate': SimpleUploadedFile(
                'certificate.gz', solve.compress(MATE_IN_ONE_CERT),
                content_type='application/gzip')})

        self.assertTrue(response.json()['summary']['closed'])
        self.pos.refresh_from_db()
        self.assertEqual((self.pos.status, self.pos.closure, self.pos.proof),
                         ('WHITE_WIN', 'SOLVE', 'ANDOR'))
        self.assertEqual(_passes().get().state, COMPLETED)


# ---------------------------------------------------------------------------
# The walk down the backlog, one bounded step per tick.
# ---------------------------------------------------------------------------
class BacklogTests(TestCase):

    def test_the_walk_buys_the_reported_mates_of_the_past(self):
        old = _searched('old-mate', _mate('e8d8', 9), _mate('e8e7', 7))
        child = _child(old, 'e8e7')
        _child(old, 'e8d8', status='WHITE_WIN', closure='MATE_PV')
        blunder = _searched('old-blunder', _cp('e1e2', 80), _mate('e1f1', -2),
                            stm='w')
        walked_in = _child(blunder, 'e1f1')
        _searched('old-clamp', _cp('e8e7', 9500))
        _searched('old-quiet', _cp('e8e7', -40))

        self.assertEqual(autosolve.drip_backlog(), 3)

        goals = dict(_passes().values_list('position_id', 'goal'))
        self.assertEqual(goals, {old.key: 'WHITE_WIN',
                                 child.key: 'WHITE_WIN',
                                 walked_in.key: 'BLACK_WIN'})
        event = DBEvent.objects.get(kind=autosolve.AUTOSOLVE_EVENT)
        self.assertEqual(event.payload['via'], 'backlog')
        self.assertEqual(event.payload['queued'], 3)

    def test_the_children_of_a_closed_parent_wait(self):
        closed = _searched('closed-parent', _mate('e8e7', 7),
                           status='WHITE_WIN', closure='MINIMAX')
        _child(closed, 'e8e7')

        self.assertEqual(autosolve.drip_backlog(), 0)

    def test_newest_first(self):
        first = _searched('older', _mate('e8e7', 5))
        second = _searched('newer', _mate('e8e7', 5))

        self.assertEqual(autosolve.drip_backlog(scan=1), 1)

        self.assertEqual(_passes().get().position_id, second.key)
        self.assertEqual(autosolve.drip_backlog(scan=1), 1)
        self.assertTrue(_passes(position=first).exists())

    def test_it_stops_at_half_the_cap(self):
        for index in range(HALF + 4):
            _searched(f'many-{index}', _mate('e8e7', 5))

        self.assertEqual(autosolve.drip_backlog(), HALF)
        self.assertEqual(autosolve.drip_backlog(), 0)

    def test_the_target_never_passes_half_the_cap(self):
        for index in range(HALF + 4):
            _searched(f'greedy-{index}', _mate('e8e7', 5))

        self.assertEqual(autosolve.drip_backlog(target=CAP), HALF)

    def test_the_room_left_is_where_the_next_step_starts(self):
        """Nothing is skipped when a step runs out of room."""
        for index in range(HALF + 4):
            _searched(f'resume-{index}', _mate('e8e7', 5))

        autosolve.drip_backlog()
        _passes().update(state=COMPLETED, outcome='UNKNOWN')

        self.assertEqual(autosolve.drip_backlog(), 4)
        self.assertEqual(_passes().count(), HALF + 4)

    def test_a_step_that_fills_its_room_mid_search_reads_it_again(self):
        parent = _searched('wide', _mate('e8f8', 9), _mate('e8e7', 5),
                           _mate('e8d8', 7))
        for move in ('e8e7', 'e8d8', 'e8f8'):
            _child(parent, move)
        _waiting(HALF - 2)
        waiting = set(_passes().values_list('id', flat=True))

        self.assertEqual(autosolve.drip_backlog(), 2)
        _passes().exclude(id__in=waiting).update(state=COMPLETED,
                                                 outcome='UNKNOWN')
        self.assertEqual(autosolve.drip_backlog(), 2)

        family = {parent.key, *Edge.objects.filter(parent=parent)
                  .values_list('child_id', flat=True)}
        self.assertEqual(_passes(position_id__in=family).count(), 4)

    @override_settings(**TIGHT)
    def test_half_the_hourly_ceiling_is_left_to_landings(self):
        for index in range(5):
            _searched(f'hourly-{index}', _mate('e8e7', 5))

        self.assertEqual(autosolve.drip_backlog(), 2)
        self.assertEqual(autosolve.drip_backlog(), 0)

        landed = _searched('lands-now', _mate('e8e7', 5))
        self.assertEqual(autosolve.queue_from_landing(landed), 1)

    def test_a_step_reads_a_bounded_window_and_resumes_below_it(self):
        target = _searched('far-below', _mate('e8e7', 5))
        filler = _pos('filler')
        AnalysisTask.objects.bulk_create([
            AnalysisTask(position=filler, generation=index, state=DONE,
                         budget_nodes=FLOOR, nodes_searched=FLOOR)
            for index in range(5)])

        self.assertEqual(autosolve.drip_backlog(window=3), 0)
        self.assertEqual(autosolve.drip_backlog(window=3), 1)

        self.assertEqual(_passes().get().position_id, target.key)

    def test_a_position_searched_twice_is_read_once(self):
        pos = _searched('twice', _mate('e8e7', 5))
        AnalysisTask.objects.create(position=pos, generation=1, state=DONE,
                                    budget_nodes=FLOOR, nodes_searched=FLOOR)

        self.assertEqual(autosolve.drip_backlog(), 1)
        self.assertEqual(_passes().count(), 1)

    def test_past_the_oldest_search_it_starts_again_from_the_newest(self):
        """What a full queue made a landing let go is found next time."""
        _searched('first-round', _mate('e8e7', 5))

        self.assertEqual(autosolve.drip_backlog(window=10 ** 12), 1)
        self.assertIsNone(autosolve._backlog_state()['cursor'])

        dropped = _searched('let-go-by-a-full-queue', _mate('e8e7', 5))
        self.assertEqual(autosolve.drip_backlog(window=10 ** 12), 1)

        self.assertTrue(_passes(position=dropped).exists())
        self.assertEqual(_passes().count(), 2)

    def test_a_full_queue_costs_one_count_and_no_walk(self):
        for index in range(3):
            _searched(f'waits-{index}', _mate('e8e7', 5))
        _waiting(HALF)

        made, queries = _queries(autosolve.drip_backlog)

        self.assertEqual(made, 0)
        self.assertEqual(len(queries), 1)
        self.assertIsNone(autosolve._backlog_state()['cursor'])

    def test_a_step_costs_the_same_over_ten_searches_or_a_hundred(self):
        def cost(count, prefix):
            autosolve.reset_backlog_state()
            for index in range(count):
                _searched(f'{prefix}-{index}', _mate('e8e7', 5),
                          _mate('e8d8', 7))
            SolveTask.objects.filter(arm=ARM).delete()
            return len(_queries(lambda: autosolve.drip_backlog(
                target=1))[1])

        ten = cost(10, 'ten')

        self.assertEqual(cost(100, 'hundred'), ten)

    def test_a_step_never_reads_more_than_its_scan(self):
        for index in range(7):
            _searched(f'scan-{index}', _cp('e8e7', 20))

        autosolve.drip_backlog(scan=3)

        third_newest = (AnalysisTask.objects.order_by('-id')
                        .values_list('id', flat=True)[2])
        self.assertEqual(autosolve._backlog_state()['cursor'], third_newest)

    def test_nothing_scans_the_tree(self):
        """The full scan found these; nothing reaches them now."""
        Position.objects.create(key=_key('never-searched'),
                                fen='4k3/8/8/8/8/8/8/4K3 b - - 0 1',
                                eval_cp=9990)
        Position.objects.create(key=_key('backed-only'),
                                fen='4k3/8/8/8/8/8/8/4K3 w - - 0 1',
                                backed_eval=-9990)

        self.assertEqual(autosolve.drip_backlog(), 0)
        self.assertFalse(SolveTask.objects.exists())


class BacklogStateTests(TestCase):
    """Where the walk stands survives a restart of the service."""

    def setUp(self):
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        overridden = override_settings(ATOMICDB_STATE_DIR=self.state_dir.name)
        overridden.enable()
        self.addCleanup(overridden.disable)
        autosolve.reset_backlog_state()

    def _stored(self):
        path = os.path.join(self.state_dir.name, 'autosolve.json')
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)

    def test_a_restart_resumes_the_walk(self):
        older = _searched('restart-older', _mate('e8e7', 5))
        newer = _searched('restart-newer', _mate('e8e7', 5))
        newer_task = AnalysisTask.objects.get(position=newer)

        autosolve.drip_backlog(scan=1)

        self.assertEqual(self._stored(), {'cursor': newer_task.id})
        # A new process: nothing in memory, the file on disk.
        autosolve._backlog.update(cursor=None, loaded=False)
        autosolve.drip_backlog(scan=1)
        self.assertEqual(set(_passes().values_list('position_id', flat=True)),
                         {older.key, newer.key})

    def test_it_does_not_share_the_repair_walks_file(self):
        _searched('own-file', _mate('e8e7', 5))
        _searched('own-file-older', _mate('e8e7', 5))

        autosolve.drip_backlog(scan=1)

        self.assertTrue(os.path.exists(
            os.path.join(self.state_dir.name, 'autosolve.json')))
        self.assertFalse(os.path.exists(
            os.path.join(self.state_dir.name, 'idle_repair.json')))

    def test_a_broken_file_starts_the_walk_over(self):
        path = os.path.join(self.state_dir.name, 'autosolve.json')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('{not json')
        _searched('broken', _mate('e8e7', 5))

        self.assertEqual(autosolve.drip_backlog(), 1)

    def test_resetting_forgets_the_file(self):
        _searched('forget', _mate('e8e7', 5))
        autosolve.drip_backlog()

        autosolve.reset_backlog_state()

        self.assertFalse(os.path.exists(
            os.path.join(self.state_dir.name, 'autosolve.json')))


class SelectorStepTests(TestCase):

    def _pass(self, **options):
        out = StringIO()
        call_command('refresh_selector', stdout=out, no_priorities=True,
                     **options)
        return json.loads(out.getvalue())

    def test_the_selector_walks_the_backlog_and_says_so(self):
        _searched('selector-1', _mate('e8e7', 5))
        _searched('selector-2', _mate('e8e7', 5))

        report = self._pass()

        self.assertEqual(report['autosolve95_enqueued'], 2)
        self.assertNotIn('failed_steps', report)
        self.assertEqual(_passes().count(), 2)

    def test_a_cap_of_zero_skips_the_walk(self):
        _searched('selector-off', _mate('e8e7', 5))

        report = self._pass(autosolve_cap=0)

        self.assertEqual(report['autosolve95_enqueued'], 0)
        self.assertFalse(_passes().exists())

    def test_whatever_the_cap_says_nothing_scans_the_tree(self):
        Position.objects.create(key=_key('band-unsearched'),
                                fen='4k3/8/8/8/8/8/8/4K3 b - - 0 1',
                                eval_cp=9990)

        for cap in (CAP, 10 ** 6):
            report = self._pass(autosolve_cap=cap)
            self.assertEqual(report['autosolve95_enqueued'], 0)
        self.assertFalse(SolveTask.objects.exists())

    def test_a_failed_pass_is_not_bought_again_tick_after_tick(self):
        pos = _searched('selector-loop', _mate('e8e7', 5))
        SolveTask.objects.create(position=pos, goal='WHITE_WIN',
                                 budget_stage='F0', budget_nodes=NODES,
                                 arm=ARM, state=FAILED)

        for _tick in range(3):
            self._pass()
            autosolve.reset_backlog_state()

        self.assertEqual(_passes().count(), 1)


# ---------------------------------------------------------------------------
# What it never costs.
# ---------------------------------------------------------------------------
class FailureContainmentTests(TestCase):

    def test_a_broken_arm_does_not_cost_the_landing(self):
        root = _root()
        with patch.object(autosolve, '_mint',
                          side_effect=RuntimeError('autosolve broken')), \
                self.assertLogs('atomicdb.autosolve', level='ERROR'):
            summary = _land(root, ('mate', 5), ('cp', 40))

        root.refresh_from_db()
        self.assertEqual(root.visits, 1)
        self.assertEqual(root.eval_cp, 9995)
        self.assertNotIn('autosolved', summary)
        self.assertFalse(SolveTask.objects.exists())
