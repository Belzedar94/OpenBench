"""Real-parent pointers: the breadcrumb follows the least-regret path.

The graph below has one transposition and two move orders into it:

    root -1.Nc3-> A1 -1...Nf6-> B -2.Nf3-> T
    root -1.Nf3-> A2 -1...Nf6-> C -2.Nc3-> T

The Nc3 order is built FIRST, so its edges are the older ones: the oldest
incoming edge of T comes from B.  The evaluations decide which order the
regret Dijkstra prefers, and that is the order the pointers must publish.
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from . import ingest, logic, proof, real_parent, views
from .database import current_alias
from .models import Edge, Position, ProofNode, RealParent
from .testing import TestCase

ON = {'ATOMICDB_REAL_PARENT_WRITE': True, 'ATOMICDB_REAL_PARENT_READ': True}


class Graph:
    def build(self, nc3_eval, nf3_eval):
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.a1, self.b, self.t = self.play('b1c3', 'g8f6', 'g1f3')
        self.a2, self.c, t2 = self.play('g1f3', 'g8f6', 'b1c3')
        assert t2.key == self.t.key, 'the two orders must transpose'
        self.set_eval(self.a1, nc3_eval)
        self.set_eval(self.a2, nf3_eval)

    def play(self, *ucis):
        node, fen, made = self.root, self.root.fen, []
        for uci in ucis:
            fen = logic.apply_move(fen, uci)
            child = ingest.get_or_create_position(fen)
            Edge.objects.get_or_create(parent=node, move_uci=uci,
                                       defaults={'child': child})
            made.append(child)
            node = child
        return made

    @staticmethod
    def set_eval(pos, cp):
        Position.objects.filter(key=pos.key).update(eval_cp=cp)

    @staticmethod
    def pointer(pos):
        row = RealParent.objects.filter(position_key=pos.key).first()
        return None if row is None else (row.parent_key, row.move_uci)


def ucis(line):
    return [step['uci'] for step in line]


@override_settings(**ON)
class WriterTests(Graph, TestCase):
    def setUp(self):
        self.build(nc3_eval=-200, nf3_eval=50)

    def test_publishes_the_least_regret_parent_not_the_oldest_edge(self):
        report = real_parent.refresh(force=True)
        self.assertEqual(self.pointer(self.t), (self.c.key, 'b1c3'))
        self.assertEqual(self.pointer(self.c), (self.a2.key, 'g8f6'))
        self.assertEqual(self.pointer(self.a2), (self.root.key, 'g1f3'))
        self.assertIsNone(self.pointer(self.root))
        self.assertEqual(report['written'], 5)    # a1, a2, b, c, t

    def test_second_pass_writes_nothing(self):
        real_parent.refresh(force=True)
        report = real_parent.refresh(force=True)
        self.assertEqual(report['written'], 0)
        self.assertEqual(report['pointers'], 5)

    def test_only_the_pointer_that_changed_is_rewritten(self):
        real_parent.refresh(force=True)
        self.set_eval(self.a1, 100)
        self.set_eval(self.a2, -100)
        report = real_parent.refresh(force=True)
        self.assertEqual(report['written'], 1)
        self.assertEqual(self.pointer(self.t), (self.b.key, 'g1f3'))

    def test_interval_skips_until_forced(self):
        self.assertIsNotNone(real_parent.refresh())
        self.assertIsNone(real_parent.refresh())
        self.assertIsNotNone(real_parent.refresh(force=True))

    def test_write_cap_goes_closest_to_the_root_first(self):
        parents = {}
        done = ingest._regret_from_root_bounded(parents=parents)
        report = real_parent.publish(done, parents, max_writes=2)
        self.assertEqual(report['written'], 2)
        self.assertTrue(report['capped'])
        written = set(RealParent.objects.values_list('position_key',
                                                     flat=True))
        self.assertNotIn(self.a1.key, written)    # regret 250: last in line
        self.assertNotIn(self.b.key, written)

    @override_settings(ATOMICDB_REAL_PARENT_WRITE=False)
    def test_writer_off_writes_nothing(self):
        self.assertIsNone(real_parent.refresh(force=True))
        self.assertFalse(RealParent.objects.exists())

    def test_the_regrets_do_not_change(self):
        parents = {}
        self.assertEqual(ingest._regret_from_root_bounded(parents=parents),
                         ingest._regret_from_root_bounded())

    def test_max_settled_stops_the_pass(self):
        done = ingest._regret_from_root_bounded(parents={}, max_settled=1,
                                                batch_size=1)
        self.assertEqual(list(done), [self.root.key])

    @override_settings(ATOMICDB_SELECTOR_V2=True)
    def test_rides_the_v2_priorities_pass(self):
        ingest.refresh_priorities(force=True)
        self.assertEqual(self.pointer(self.t), (self.c.key, 'b1c3'))
        # The standalone step then has nothing to do inside its interval.
        self.assertIsNone(real_parent.refresh())

    @override_settings(ATOMICDB_SELECTOR_V2=True)
    def test_shadow_mode_writes_no_pointers(self):
        ingest.refresh_priorities(force=True, top_k=10)
        self.assertFalse(RealParent.objects.exists())


@override_settings(**ON)
class TieTests(Graph, TestCase):
    def setUp(self):
        self.build(nc3_eval=50, nf3_eval=50)     # T is reached at regret 0 twice

    def test_exact_tie_without_proof_numbers_takes_the_oldest_edge(self):
        real_parent.refresh(force=True)
        self.assertEqual(self.pointer(self.t), (self.b.key, 'g1f3'))

    def test_held_parent_survives_a_challenger_inside_the_hysteresis(self):
        real_parent.refresh(force=True)              # held: via B
        campaign = proof.default_campaign()
        ProofNode.objects.create(campaign=campaign, position=self.b, pn=20)
        ProofNode.objects.create(campaign=campaign, position=self.c, pn=10)
        real_parent.refresh(force=True)
        self.assertEqual(self.pointer(self.t), (self.b.key, 'g1f3'))
        ProofNode.objects.filter(position=self.c).update(pn=5)   # 5*3 < 20
        real_parent.refresh(force=True)
        self.assertEqual(self.pointer(self.t), (self.c.key, 'b1c3'))

    def test_without_a_held_pointer_the_smaller_pn_wins(self):
        campaign = proof.default_campaign()
        ProofNode.objects.create(campaign=campaign, position=self.b, pn=20)
        ProofNode.objects.create(campaign=campaign, position=self.c, pn=19)
        real_parent.refresh(force=True)
        self.assertEqual(self.pointer(self.t), (self.c.key, 'b1c3'))

    def test_a_few_centipawns_of_drift_do_not_rewrite_the_row(self):
        real_parent.refresh(force=True)              # held: via B (oldest)
        self.set_eval(self.a1, 47)                   # B side now 3 cp worse
        report = real_parent.refresh(force=True)
        self.assertEqual(self.pointer(self.t), (self.b.key, 'g1f3'))
        self.assertEqual(report['written'], 0)

    def test_a_clearly_better_path_always_wins(self):
        real_parent.refresh(force=True)
        self.set_eval(self.a1, 30)                   # 20 cp: past TIE_CP
        real_parent.refresh(force=True)
        self.assertEqual(self.pointer(self.t), (self.c.key, 'b1c3'))


class ChooseTests(SimpleTestCase):
    """The rule on its own, in the integer arithmetic of ``sticky_index``."""

    A = (0.0, 'pa', 'm1', 10)
    B = (0.0, 'pb', 'm2', 5)

    def test_order_without_held(self):
        self.assertIs(real_parent.choose([self.A, self.B], None, {}), self.B)
        self.assertIs(real_parent.choose([self.A, self.B], None,
                                         {'pa': 1, 'pb': 2}), self.A)

    def test_hysteresis_boundary(self):
        held = ('pb', 'm2')
        # exactly K times smaller does not dethrone; strictly more does
        self.assertIs(real_parent.choose([self.A, self.B], held,
                                         {'pa': 4, 'pb': 12}), self.B)
        self.assertIs(real_parent.choose([self.A, self.B], held,
                                         {'pa': 3, 'pb': 12}), self.A)

    def test_held_outside_the_candidates_is_replaced(self):
        self.assertIs(real_parent.choose([self.A], ('pz', 'm9'), {}), self.A)


@override_settings(**ON)
class ReaderTests(Graph, TestCase):
    def setUp(self):
        self.build(nc3_eval=-200, nf3_eval=50)

    def test_breadcrumb_follows_the_pointers(self):
        real_parent.refresh(force=True)
        top, line = views._walk_lines_to_root([self.t.key])[self.t.key]
        self.assertEqual(top.fen, logic.start_fen())
        self.assertEqual(ucis(line), ['g1f3', 'g8f6', 'b1c3'])
        self.assertEqual(line[-1]['key'], self.t.key)
        self.assertEqual([s['san'] for s in line], ['Nf3', 'Nf6', 'Nc3'])

    def test_without_pointers_the_oldest_edge_answers(self):
        walked = real_parent.lines_to_root(
            [self.t.key], self.root.key, views.LINEAGE_SEARCH_MAX_PLIES)
        ordered, hops = walked[self.t.key]
        self.assertEqual([m for m, _ in ordered], ['b1c3', 'g8f6', 'g1f3'])
        self.assertEqual(hops, 0)

    def test_a_missing_pointer_mid_chain_falls_back_for_that_step_only(self):
        real_parent.refresh(force=True)
        RealParent.objects.filter(position_key=self.c.key).delete()
        RealParent.objects.filter(position_key=self.a1.key).delete()
        walked = real_parent.lines_to_root(
            [self.t.key], self.root.key, views.LINEAGE_SEARCH_MAX_PLIES)
        ordered, hops = walked[self.t.key]
        # T -> C by pointer, C -> A2 by its only (oldest) edge, A2 by pointer
        self.assertEqual([m for m, _ in ordered], ['g1f3', 'g8f6', 'b1c3'])
        self.assertEqual(hops, 2)

    def test_a_pointer_whose_edge_is_gone_is_not_followed(self):
        RealParent.objects.create(position_key=self.t.key,
                                  parent_key=self.c.key, move_uci='h2h4')
        walked = real_parent.lines_to_root(
            [self.t.key], self.root.key, views.LINEAGE_SEARCH_MAX_PLIES)
        self.assertEqual([m for m, _ in walked[self.t.key][0]],
                         ['b1c3', 'g8f6', 'g1f3'])

    def test_bounded_queries_for_a_batch(self):
        real_parent.refresh(force=True)
        keys = [self.t.key, self.c.key, self.b.key, self.root.key]
        with self.assertNumQueries(2, using=current_alias()):
            result = views._walk_lines_to_root(keys)
        self.assertEqual(set(result), set(keys))
        self.assertEqual(result[self.root.key][1], [])

    def test_ply_budget_hands_the_key_to_the_bfs(self):
        self.assertEqual(real_parent.lines_to_root(
            [self.t.key], self.root.key, 2), {})

    def test_a_pointer_cycle_falls_back_to_the_bfs(self):
        x, y = self.play('e2e4', 'g8f6')
        Edge.objects.create(parent=y, move_uci='a1a1', child=x)  # fake back edge
        RealParent.objects.create(position_key=x.key, parent_key=y.key,
                                  move_uci='a1a1')
        RealParent.objects.create(position_key=y.key, parent_key=x.key,
                                  move_uci='g8f6')
        self.assertEqual(real_parent.lines_to_root(
            [x.key], self.root.key, views.LINEAGE_SEARCH_MAX_PLIES), {})
        _top, line = views._walk_lines_to_root([x.key])[x.key]
        self.assertEqual(ucis(line), ['e2e4'])

    @override_settings(ATOMICDB_REAL_PARENT_READ=False)
    def test_reader_off_never_walks_the_pointers(self):
        real_parent.refresh(force=True)
        with mock.patch.object(real_parent, 'lines_to_root') as walk:
            _top, line = views._walk_lines_to_root([self.t.key])[self.t.key]
        walk.assert_not_called()
        self.assertEqual(len(line), 3)

    def test_cache_key_names_the_walk(self):
        with_reader = views._lineage_cache_key('k', 96)
        with override_settings(ATOMICDB_REAL_PARENT_READ=False):
            without = views._lineage_cache_key('k', 96)
        self.assertNotEqual(with_reader, without)
