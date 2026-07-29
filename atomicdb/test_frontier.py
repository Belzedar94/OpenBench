"""Frontier expansion: what a visitor request buys once the ladder is spent.

The depth ladder (128M -> 512M -> 2B -> 10B) stops being useful the moment
10B is COMPLETED on a position; buying it again would repeat a search we
already have.  From there the request is spent one ply deeper instead, the
way proof-number search grows a frontier: an OR node (White, the attacker of
the conjecture, to move) only needs one good try; an AND node (Black to move)
has to answer every reply.
"""

from unittest import mock

from django.test import override_settings

from . import ingest, logic
from .models import AnalysisTask, DBEvent, Edge, Position, RequestLog
from .testing import TestCase


def _exhaust_ladder(pos, budget=None):
    """Record the top request rung as already COMPLETED on ``pos``."""
    budget = ingest.REQUEST_BUDGET_LADDER[-1] if budget is None else budget
    generation = pos.visits
    while AnalysisTask.objects.filter(position=pos,
                                      generation=generation).exists():
        generation += 1
    return AnalysisTask.objects.create(
        position=pos, generation=generation, budget_nodes=budget,
        state=AnalysisTask.TState.COMPLETED,
        source=AnalysisTask.Source.USER)


def _queued_moves(parent):
    """Moves of ``parent`` whose child now has work waiting or running."""
    keys = set(AnalysisTask.objects.filter(
        position__edges_in__parent=parent,
        state__in=(AnalysisTask.TState.PENDING, AnalysisTask.TState.LEASED),
    ).values_list('position_id', flat=True))
    return sorted(Edge.objects.filter(parent=parent, child_id__in=keys)
                  .values_list('move_uci', flat=True))


def _multipv(*moves):
    return [{'move': uci, 'eval_cp': 40 - 10 * i, 'mate': None, 'pv': [uci]}
            for i, uci in enumerate(moves)]


def _spent_line(*moves):
    """A line whose nodes are all spent and each name their one heir.

    Used with the frontier widths pinned to one: ``_frontier_children`` then
    returns exactly the named move at every step, so a descent has a single
    legal path and a test can assert where it came out.  The last element is
    the tail the line leads to, which is deliberately NOT spent.
    """
    fen = logic.start_fen()
    nodes = []
    for uci in moves:
        pos = ingest.get_or_create_position(fen)
        ingest.expand(pos)
        pos.last_analysis = _multipv(uci)
        pos.save(update_fields=['last_analysis'])
        _exhaust_ladder(pos)
        nodes.append(pos)
        fen = logic.apply_move(fen, uci)
    nodes.append(ingest.get_or_create_position(fen))
    return nodes


class LadderExhaustionTests(TestCase):

    def test_exhausted_ladder_no_longer_requeues_the_parent(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        spent = _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome, 'expanded')
        self.assertEqual(
            list(AnalysisTask.objects.filter(position=pos)
                 .values_list('id', flat=True)), [spent.id])

    def test_ladder_exhausted_reports_only_the_spent_top_rung(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        self.assertFalse(ingest.ladder_exhausted(pos))
        _exhaust_ladder(pos, budget=ingest.REQUEST_BUDGET_LADDER[-2])
        self.assertFalse(ingest.ladder_exhausted(pos))
        _exhaust_ladder(pos)
        self.assertTrue(ingest.ladder_exhausted(pos))

    def test_solved_position_never_expands(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        _exhaust_ladder(pos)
        Position.objects.filter(pk=pos.pk).update(status='DRAW',
                                                  closure='MINIMAX')

        self.assertEqual(ingest.request_analysis(pos), 'already-solved')
        self.assertFalse(Edge.objects.filter(parent=pos).exists())


class UnchangedLadderTests(TestCase):
    """The ordinary path must not notice this feature at all."""

    def test_fresh_request_keeps_the_classic_single_task(self):
        pos = ingest.get_or_create_position(logic.start_fen())

        self.assertEqual(ingest.request_analysis(pos), 'queued')

        task = AnalysisTask.objects.get(position=pos)
        self.assertEqual(
            (task.source, task.state, task.budget_nodes),
            (AnalysisTask.Source.USER, AnalysisTask.TState.PENDING,
             ingest.REQUEST_BUDGET_LADDER[0]))
        self.assertFalse(Edge.objects.filter(parent=pos).exists())

    def test_middle_rung_still_escalates_on_the_position_itself(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        _exhaust_ladder(pos, budget=ingest.REQUEST_BUDGET_LADDER[0])
        Position.objects.filter(pk=pos.pk).update(visits=1)
        pos.refresh_from_db()

        self.assertEqual(ingest.request_analysis(pos), 'queued')

        follow_up = AnalysisTask.objects.get(position=pos, generation=1)
        self.assertEqual(follow_up.budget_nodes,
                         ingest.REQUEST_BUDGET_LADDER[1])
        self.assertFalse(Edge.objects.filter(parent=pos).exists())

    def test_ordinary_status_keeps_its_single_key_body(self):
        pos = ingest.get_or_create_position(logic.start_fen())

        response = self.client.post(f'/atomicdb/request/{pos.key}/')

        self.assertEqual(response.json(), {'status': 'queued'})


class BreadthSwapTests(TestCase):
    """Con ATOMICDB_BREADTH_SWAP, una revisita compra anchura, no el peldano.

    Medido en produccion (29-jul-2026, n=800 revisitas profundas reales): un
    ply de hijos a 128M reproduce el veredicto del re-search profundo el
    96-99% de las veces.  El flag mueve el pivote peticion->expansion de
    "escalera agotada" a "primera pasada hecha"; la profundidad queda para
    banda de mate, disputas y frontera saturada."""

    @override_settings(ATOMICDB_BREADTH_SWAP=True)
    def test_a_revisit_request_expands_instead_of_deepening(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        _exhaust_ladder(pos, budget=ingest.REQUEST_BUDGET_LADDER[0])
        Position.objects.filter(pk=pos.pk).update(visits=1, eval_cp=30)
        pos.refresh_from_db()

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome, 'expanded')
        self.assertEqual(AnalysisTask.objects.filter(position=pos).count(), 1)
        child_tasks = AnalysisTask.objects.exclude(position=pos)
        self.assertTrue(child_tasks.exists())
        for task in child_tasks:
            self.assertEqual(task.budget_nodes,
                             ingest.REQUEST_BUDGET_LADDER[0])
        self.assertTrue(
            DBEvent.objects.filter(kind='BREADTH_SWAP').exists())

    @override_settings(ATOMICDB_BREADTH_SWAP=True)
    def test_the_seeding_pass_is_never_swapped(self):
        pos = ingest.get_or_create_position(logic.start_fen())

        self.assertEqual(ingest.request_analysis(pos), 'queued')

        task = AnalysisTask.objects.get(position=pos)
        self.assertEqual(task.budget_nodes, ingest.REQUEST_BUDGET_LADDER[0])
        self.assertFalse(Edge.objects.filter(parent=pos).exists())

    @override_settings(ATOMICDB_BREADTH_SWAP=True)
    def test_uncertainty_gap_buys_virgin_children(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(pos)
        Position.objects.filter(pk=pos.pk).update(eval_cp=100,
                                                  backed_eval=400)
        pos.refresh_from_db()

        queued = ingest._uncertainty_expand([pos.key])

        self.assertEqual(queued, 2)
        tasks = AnalysisTask.objects.all()
        self.assertEqual(tasks.count(), 2)
        for task in tasks:
            self.assertEqual(task.source, AnalysisTask.Source.AUTO)
        self.assertTrue(
            DBEvent.objects.filter(kind='UNCERTAINTY_EXPAND').exists())

    def test_uncertainty_stays_quiet_without_flag_or_gap(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(pos)
        Position.objects.filter(pk=pos.pk).update(eval_cp=100,
                                                  backed_eval=400)
        pos.refresh_from_db()
        self.assertEqual(ingest._uncertainty_expand([pos.key]), 0)

        with override_settings(ATOMICDB_BREADTH_SWAP=True):
            Position.objects.filter(pk=pos.pk).update(backed_eval=180)
            self.assertEqual(ingest._uncertainty_expand([pos.key]), 0)
        self.assertFalse(AnalysisTask.objects.exists())

    @override_settings(ATOMICDB_BREADTH_SWAP=True)
    def test_a_mate_band_node_still_buys_depth(self):
        pos = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'g1f3'))
        _exhaust_ladder(pos, budget=ingest.REQUEST_BUDGET_LADDER[0])
        Position.objects.filter(pk=pos.pk).update(
            visits=1, eval_cp=ingest.MATE_BAND + 50)
        pos.refresh_from_db()

        self.assertEqual(ingest.request_analysis(pos), 'queued')

        self.assertEqual(AnalysisTask.objects.filter(position=pos).count(), 2)
        self.assertFalse(DBEvent.objects.filter(kind='BREADTH_SWAP').exists())


class OrNodeExpansionTests(TestCase):
    """White to move: one good try is enough, so only the top-k are bought."""

    def _root(self, *moves):
        pos = ingest.get_or_create_position(logic.start_fen())
        self.assertEqual(pos.fen.split()[1], 'w')
        if moves:
            pos.last_analysis = _multipv(*moves)
            pos.save(update_fields=['last_analysis'])
        return pos

    def test_multipv_order_picks_the_top_k(self):
        pos = self._root('e2e4', 'd2d4', 'g1f3', 'b1c3')
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome, 'expanded')
        self.assertEqual(outcome.detail['children_queued'],
                         ingest.FRONTIER_OR_WIDTH)
        self.assertEqual(_queued_moves(pos), ['d2d4', 'e2e4', 'g1f3'])

    def test_children_enter_the_ladder_at_their_own_floor(self):
        pos = self._root('e2e4', 'd2d4', 'g1f3')
        _exhaust_ladder(pos)

        ingest.request_analysis(pos)

        tasks = AnalysisTask.objects.filter(position__edges_in__parent=pos)
        self.assertEqual(tasks.count(), 3)
        for task in tasks:
            self.assertEqual(
                (task.state, task.source, task.budget_nodes, task.generation),
                (AnalysisTask.TState.PENDING, AnalysisTask.Source.USER,
                 ingest.REQUEST_BUDGET_LADDER[0], 0))

    def test_solved_children_never_take_a_slot(self):
        pos = self._root('e2e4', 'd2d4', 'g1f3', 'b1c3')
        ingest.expand(pos)
        solved = Edge.objects.get(parent=pos, move_uci='e2e4').child
        Position.objects.filter(pk=solved.pk).update(status='DRAW',
                                                     closure='TERMINAL')
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertFalse(AnalysisTask.objects.filter(position=solved).exists())
        self.assertEqual(_queued_moves(pos), ['b1c3', 'd2d4', 'g1f3'])
        self.assertEqual(outcome.detail['children_queued'],
                         ingest.FRONTIER_OR_WIDTH)

    def test_child_evals_order_the_expansion_without_multipv(self):
        pos = self._root()
        ingest.expand(pos)
        for uci, eval_cp in (('e2e4', 40), ('d2d4', 60), ('g1f3', 10)):
            Position.objects.filter(
                pk=Edge.objects.get(parent=pos, move_uci=uci).child_id
            ).update(eval_cp=eval_cp)
        _exhaust_ladder(pos)

        ingest.request_analysis(pos)

        self.assertEqual(_queued_moves(pos), ['d2d4', 'e2e4', 'g1f3'])

    def test_no_ordering_at_all_falls_back_to_the_first_legal_moves(self):
        pos = self._root()
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['children_queued'],
                         ingest.FRONTIER_BLIND_WIDTH)
        self.assertEqual(
            _queued_moves(pos),
            sorted(logic.legal_moves(pos.fen)[:ingest.FRONTIER_BLIND_WIDTH]))

    def test_multipv_entries_the_movegen_rejects_are_ignored(self):
        pos = self._root('e2e5', 'd2d4', 'g1f3', 'b1c3')
        _exhaust_ladder(pos)

        ingest.request_analysis(pos)

        self.assertEqual(_queued_moves(pos), ['b1c3', 'd2d4', 'g1f3'])


class AndNodeExpansionTests(TestCase):
    """Black to move: every reply has to be answered, so all are bought."""

    def _after_e4(self):
        pos = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        self.assertEqual(pos.fen.split()[1], 'b')
        return pos

    def test_every_legal_reply_is_queued(self):
        pos = self._after_e4()
        legal = logic.legal_moves(pos.fen)
        self.assertLess(len(legal), ingest.FRONTIER_AND_CAP)
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome, 'expanded')
        self.assertEqual(outcome.detail['children_queued'], len(legal))
        self.assertEqual(_queued_moves(pos), sorted(legal))

    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 5)
    def test_cap_bounds_a_wide_and_node(self):
        pos = self._after_e4()
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['children_queued'], 5)
        self.assertEqual(len(_queued_moves(pos)), 5)

    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 1_000)
    @mock.patch('atomicdb.ingest.FRONTIER_CLICK_CAP', 4)
    def test_no_click_can_exceed_the_hard_task_cap(self):
        pos = self._after_e4()
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['children_queued'], 4)

    def test_declared_caps_match_the_approved_design(self):
        self.assertEqual(ingest.FRONTIER_OR_WIDTH, 3)
        self.assertEqual(ingest.FRONTIER_AND_CAP, 64)
        self.assertEqual(ingest.FRONTIER_CLICK_CAP, 64)


class PartialExhaustionTests(TestCase):
    """One buyable sibling is enough: the click stays at this level."""

    def test_child_with_a_spent_ladder_is_counted_not_recursed(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.last_analysis = _multipv('e2e4', 'd2d4', 'g1f3')
        pos.save(update_fields=['last_analysis'])
        ingest.expand(pos)
        deep_child = Edge.objects.get(parent=pos, move_uci='e2e4').child
        spent = _exhaust_ladder(deep_child)
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['children_exhausted'], 1)
        self.assertEqual(outcome.detail['children_queued'], 2)
        self.assertEqual(_queued_moves(pos), ['d2d4', 'g1f3'])
        # The descent only starts when EVERY candidate is spent, so the one
        # spent child stays untouched: no grandchildren, no extra task.
        self.assertEqual(outcome.detail['descent_plies'], 0)
        self.assertFalse(Edge.objects.filter(parent=deep_child).exists())
        self.assertEqual(
            list(AnalysisTask.objects.filter(position=deep_child)
                 .values_list('id', flat=True)), [spent.id])

    def test_a_child_already_queued_is_reported_as_covered(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.last_analysis = _multipv('e2e4', 'd2d4', 'g1f3')
        pos.save(update_fields=['last_analysis'])
        ingest.expand(pos)
        child = Edge.objects.get(parent=pos, move_uci='e2e4').child
        AnalysisTask.objects.create(
            position=child, generation=0,
            budget_nodes=ingest.REQUEST_BUDGET_LADDER[0],
            source=AnalysisTask.Source.AUTO)
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['children_queued'], 3)
        self.assertEqual(AnalysisTask.objects.get(position=child).source,
                         AnalysisTask.Source.USER)


class DescentTests(TestCase):
    """A spent frontier is not a dead end: follow the most promising line.

    Proof-number search walks to the most-proving node instead of giving up,
    and that is the whole idea here.  While every candidate at a level has a
    spent ladder there is nothing to buy, so the click steps down through the
    best one — best eval at an OR node, and at an AND node the unsolved reply
    that is best for the DEFENDER, the one hardest to refute — and asks the
    same question one ply lower.
    """

    def _eval_children(self, pos, evals):
        """Give named children an eval and spend their ladders."""
        children = {}
        ingest.expand(pos)
        for uci, eval_cp in evals.items():
            child = Edge.objects.get(parent=pos, move_uci=uci).child
            Position.objects.filter(pk=child.pk).update(eval_cp=eval_cp)
            _exhaust_ladder(child)
            children[uci] = child
        return children

    def test_or_node_follows_the_best_eval(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        children = self._eval_children(
            pos, {'e2e4': 50, 'd2d4': 30, 'g1f3': 10})
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome, 'expanded')
        self.assertEqual(outcome.detail['descent_plies'], 1)
        self.assertEqual(outcome.detail['descent_key'], children['e2e4'].key)
        self.assertTrue(AnalysisTask.objects.filter(
            position__edges_in__parent=children['e2e4'],
            state=AnalysisTask.TState.PENDING).exists())

    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 3)
    def test_and_node_follows_the_defender_s_hardest_reply(self):
        pos = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        self.assertEqual(pos.fen.split()[1], 'b')
        # White-POV evals: the lower the number the better for the defender.
        children = self._eval_children(
            pos, {'c7c5': -30, 'g8f6': 10, 'e7e5': 50})
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['descent_key'], children['c7c5'].key)

    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 3)
    def test_a_solved_reply_is_never_followed_however_good(self):
        pos = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        children = self._eval_children(
            pos, {'b8c6': -900, 'c7c5': -30, 'g8f6': 10, 'e7e5': 50})
        # The defender's dream reply is already refuted: nothing left to ask.
        Position.objects.filter(pk=children['b8c6'].pk).update(
            status='WHITE_WIN', closure='MATE_PV')
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['descent_key'], children['c7c5'].key)
        self.assertFalse(Edge.objects.filter(parent=children['b8c6']).exists())

    @mock.patch('atomicdb.ingest.FRONTIER_OR_WIDTH', 1)
    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 1)
    def test_the_descent_keeps_going_while_the_line_is_spent(self):
        nodes = _spent_line('e2e4', 'e7e5', 'g1f3')

        outcome = ingest.request_analysis(nodes[0])

        # It stops at the last node that still has something to buy, and the
        # tasks land one ply below THAT, exactly as a click there would.
        self.assertEqual(outcome, 'expanded')
        self.assertEqual(outcome.detail['descent_plies'], 2)
        self.assertEqual(outcome.detail['descent_key'], nodes[-2].key)
        self.assertEqual(outcome.detail['children_exhausted'], 2)
        self.assertTrue(AnalysisTask.objects.filter(
            position=nodes[-1],
            state=AnalysisTask.TState.PENDING).exists())

    @mock.patch('atomicdb.ingest.FRONTIER_OR_WIDTH', 1)
    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 1)
    def test_a_transposition_cycle_cannot_loop_forever(self):
        # Stripped of the counters, 1.Nf3 Nf6 2.Ng1 Ng8 IS the start position,
        # so this line is a genuine cycle in the DAG rather than a contrived
        # one: without the visited set the descent would never terminate.
        nodes = _spent_line('g1f3', 'g8f6', 'f3g1', 'f6g8')
        self.assertEqual(nodes[-1].key, nodes[0].key)

        outcome = ingest.request_analysis(nodes[0])

        self.assertEqual(outcome, 'saturated')
        self.assertEqual(outcome.detail['descent_stop'], 'no-candidate')
        self.assertEqual(outcome.detail['descent_plies'], 3)

    @mock.patch('atomicdb.ingest.FRONTIER_OR_WIDTH', 1)
    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 1)
    @mock.patch('atomicdb.ingest.FRONTIER_DESCENT_MAX_PLIES', 2)
    def test_the_ply_guard_stops_a_long_spent_line(self):
        nodes = _spent_line('e2e4', 'e7e5', 'g1f3', 'b8c6')

        outcome = ingest.request_analysis(nodes[0])

        self.assertEqual(outcome, 'saturated')
        self.assertEqual(outcome.detail['descent_stop'], 'depth-guard')
        self.assertEqual(outcome.detail['descent_plies'], 2)
        self.assertFalse(AnalysisTask.objects.filter(
            state=AnalysisTask.TState.PENDING).exists())

    def test_a_fully_solved_level_saturates_without_descending(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(pos)
        Position.objects.filter(
            pk__in=Edge.objects.filter(parent=pos).values('child_id')).update(
                status='DRAW', closure='TERMINAL')
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome, 'saturated')
        self.assertEqual(outcome.detail['descent_plies'], 0)
        self.assertEqual(outcome.detail['children_considered'], 0)
        self.assertFalse(AnalysisTask.objects.filter(
            state=AnalysisTask.TState.PENDING).exists())

    @mock.patch('atomicdb.ingest.FRONTIER_CLICK_CAP', 5)
    def test_one_descending_click_still_respects_the_task_budget(self):
        # The level it lands on is a wide AND node: without the per-click
        # ceiling a single click would buy every legal reply down there.
        pos = ingest.get_or_create_position(logic.start_fen())
        children = self._eval_children(
            pos, {'e2e4': 50, 'd2d4': 30, 'g1f3': 10})
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome.detail['descent_plies'], 1)
        self.assertEqual(outcome.detail['descent_key'], children['e2e4'].key)
        self.assertGreater(
            len(logic.legal_moves(children['e2e4'].fen)), 5)
        self.assertEqual(outcome.detail['children_queued'], 5)
        self.assertEqual(
            AnalysisTask.objects.filter(
                state=AnalysisTask.TState.PENDING).count(), 5)

    @mock.patch('atomicdb.ingest.FRONTIER_CLICK_CAP', 0)
    def test_a_spent_budget_saturates_before_touching_anything(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        _exhaust_ladder(pos)

        outcome = ingest.request_analysis(pos)

        self.assertEqual(outcome, 'saturated')
        self.assertEqual(outcome.detail['descent_stop'], 'budget-spent')
        self.assertFalse(Edge.objects.filter(parent=pos).exists())

    @mock.patch('atomicdb.ingest.FRONTIER_OR_WIDTH', 1)
    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 1)
    def test_the_way_down_queues_nothing_above_where_it_stops(self):
        nodes = _spent_line('e2e4', 'e7e5', 'g1f3')

        ingest.request_analysis(nodes[0])

        for node in nodes[:-1]:
            self.assertFalse(
                AnalysisTask.objects.filter(
                    position=node,
                    state__in=(AnalysisTask.TState.PENDING,
                               AnalysisTask.TState.LEASED)).exists())

    def test_declared_descent_guard_matches_the_approved_design(self):
        self.assertEqual(ingest.FRONTIER_DESCENT_MAX_PLIES, 32)


class DescentRequestApiTests(TestCase):

    @mock.patch('atomicdb.ingest.FRONTIER_OR_WIDTH', 1)
    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 1)
    def test_saturated_is_reported_with_its_counters(self):
        line = _spent_line('g1f3', 'g8f6', 'f3g1', 'f6g8')

        body = self.client.post(f'/atomicdb/request/{line[0].key}/').json()

        self.assertEqual(body['status'], 'saturated')
        self.assertEqual(body['descent_stop'], 'no-candidate')
        self.assertEqual(body['descent_plies'], 3)
        self.assertEqual(body['children_exhausted'], 4)

    @mock.patch('atomicdb.ingest.FRONTIER_OR_WIDTH', 1)
    @mock.patch('atomicdb.ingest.FRONTIER_AND_CAP', 1)
    def test_a_saturated_click_still_leaves_one_receipt(self):
        line = _spent_line('g1f3', 'g8f6', 'f3g1', 'f6g8')
        before = AnalysisTask.objects.count()

        self.client.post(f'/atomicdb/request/{line[0].key}/')

        # It bought nothing, but it did the walking: the hourly allowance is
        # what bounds how often a visitor can pay for that walk.
        self.assertEqual(
            RequestLog.objects.filter(position=line[0]).count(), 1)
        self.assertEqual(AnalysisTask.objects.count(), before)

    def test_explorer_renders_the_saturated_status(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        _exhaust_ladder(pos)

        response = self.client.get(f'/atomicdb/explore/{pos.key}/')

        self.assertContains(response, 'saturated')
        self.assertContains(response, 'descent_plies')

    def test_a_saturated_click_says_what_it_walked(self):
        """The complaint this fixes: a spent ladder read as a broken queue.

        The endpoint already answered honestly; the page turned that answer
        into four words that never mentioned the walk or the counters.
        """
        pos = ingest.get_or_create_position(logic.start_fen())
        _exhaust_ladder(pos)

        response = self.client.get(f'/atomicdb/explore/{pos.key}/')

        self.assertContains(response, 'Frontier saturated')
        self.assertContains(response, 'nothing left to buy')
        self.assertContains(response, 'children_exhausted')

    def test_the_outcome_has_a_place_to_be_read_beside_the_button(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        _exhaust_ladder(pos)

        response = self.client.get(f'/atomicdb/explore/{pos.key}/')

        # Beside the button, not INSIDE it: the button still has to say what
        # it is doing while the note says what the click bought.
        self.assertContains(response, 'id="reqnote"')
        self.assertContains(response, 'class="reqnote"')


class ExpansionRequestApiTests(TestCase):

    def _exhausted_root(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.last_analysis = _multipv('e2e4', 'd2d4', 'g1f3')
        pos.save(update_fields=['last_analysis'])
        _exhaust_ladder(pos)
        return pos

    def test_expansion_reports_its_counters(self):
        pos = self._exhausted_root()

        body = self.client.post(f'/atomicdb/request/{pos.key}/').json()

        self.assertEqual(body['status'], 'expanded')
        self.assertEqual(body['children_queued'], 3)
        self.assertEqual(body['children_considered'], 3)
        self.assertEqual(body['children_solved'], 0)
        self.assertEqual(body['children_exhausted'], 0)

    def test_one_click_is_one_expansion_event(self):
        pos = self._exhausted_root()

        first = self.client.post(f'/atomicdb/request/{pos.key}/')
        second = self.client.post(f'/atomicdb/request/{pos.key}/')

        self.assertEqual(first.json()['status'], 'expanded')
        self.assertEqual(second.json(), {'status': 'already-requested'})
        self.assertEqual(RequestLog.objects.filter(position=pos).count(), 1)
        self.assertEqual(
            AnalysisTask.objects.filter(
                position__edges_in__parent=pos,
                state=AnalysisTask.TState.PENDING).count(), 3)

    def test_expansion_consumes_the_hourly_ip_allowance(self):
        pos = self._exhausted_root()
        other = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        for _ in range(30):
            RequestLog.objects.create(ip='127.0.0.1', position=other)

        response = self.client.post(f'/atomicdb/request/{pos.key}/')

        # No hourly allowance any more: receipts for OTHER positions do not
        # stand between a visitor and this one.
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()['status'], 'rate-limited')

    def test_a_different_visitor_is_not_deduplicated(self):
        pos = self._exhausted_root()
        self.client.post(f'/atomicdb/request/{pos.key}/')

        response = self.client.post(f'/atomicdb/request/{pos.key}/',
                                    HTTP_X_FORWARDED_FOR='203.0.113.7')

        self.assertEqual(response.json()['status'], 'expanded')
        self.assertEqual(RequestLog.objects.filter(position=pos).count(), 2)

    def test_an_expanded_child_is_immediately_leasable(self):
        from django.contrib.auth.models import User

        User.objects.create_user('u', password='p')
        pos = self._exhausted_root()
        self.client.post(f'/atomicdb/request/{pos.key}/')

        lease = self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'm1', 'tb': '1',
            'worker_build': '2026072203', 'lease_session': 'session-m1',
        })

        served = lease.json()['tasks'][0]
        self.assertEqual(served['budget_nodes'],
                         ingest.REQUEST_BUDGET_LADDER[0])
        self.assertIn(
            AnalysisTask.objects.get(id=served['id']).position_id,
            set(Edge.objects.filter(parent=pos)
                .values_list('child_id', flat=True)))

    def test_explorer_renders_the_expansion_status(self):
        pos = self._exhausted_root()

        response = self.client.get(f'/atomicdb/explore/{pos.key}/')

        self.assertContains(response, 'Budget exhausted here')
        self.assertContains(response, 'children_queued')

    def test_an_expansion_says_where_the_work_landed(self):
        pos = self._exhausted_root()

        response = self.client.get(f'/atomicdb/explore/{pos.key}/')

        # "below INSTEAD" is the whole point: the visitor asked for this
        # position and got work one level down, which is not a failure and is
        # not what "queued" alone conveys either.
        self.assertContains(response, 'below instead')
        self.assertContains(response, 'further down')
