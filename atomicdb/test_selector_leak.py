"""The refuted subtree that came in through the least informed parent.

The selector spent eight hours drilling positions under a move that loses in
two.  The node it was drilling has TWO parents: the refuted one, which carries
an enormous regret and correctly repels the search, and a second parent that
had never been analysed directly.  That second parent had no eval of its own —
only a backed value the cascade had already lifted into it — and the Dijkstra
built its value map from the RAW eval, so that parent contributed no gap at
all.  Since the shortest path wins, the whole refuted subtree walked in
through the door nobody was watching.
"""

from unittest.mock import patch

from django.test import override_settings

from . import ingest, logic, proof
from .models import Edge, Position
from .testing import TestCase


class RegretUsesBestKnownEvalTests(TestCase):

    def _leak(self, blind_parent_backed):
        """Build the exact shape: A refuted, T reachable through blind P."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)

        # A: a refuted first move, closed as a win for the other side.
        refuted = Edge.objects.get(parent=root, move_uci='h2h4').child
        refuted.status = 'WHITE_WIN'
        refuted.closure = 'MINIMAX'
        refuted.proof = 'ENGINE'
        refuted.mate_in = 2
        refuted.eval_cp = 9_800
        refuted.save()

        # P: a sibling nobody ever analysed directly.  No eval of its own,
        # but the cascade already backed a decided value into it.
        blind = Edge.objects.get(parent=root, move_uci='g1f3').child
        blind.eval_cp = None
        blind.visits = 0
        blind.backed_eval = blind_parent_backed
        blind.save()

        # T: the transposition both of them reach.
        target = ingest.get_or_create_position('8/8/8/8/8/8/1k6/K6Q w - - 0 1')
        target.eval_cp = 1_439
        target.visits = 1
        target.save()
        Edge.objects.create(parent=refuted, move_uci='h4h5', child=target)
        Edge.objects.create(parent=blind, move_uci='f3e5', child=target)

        # An ordinary main-line node to compare against.
        mainline = Edge.objects.get(parent=root, move_uci='e2e4').child
        mainline.eval_cp = 40
        mainline.save()

        ingest._priority_refresh_cache['at'] = 0.0
        ingest.refresh_priorities()
        target.refresh_from_db()
        mainline.refresh_from_db()
        return target, mainline

    def test_the_blind_parent_now_contributes_its_gap(self):
        """The mechanism, pinned both ways round.

        Same graph twice.  The only difference is whether the never-analysed
        parent carries the backed value the cascade already gave it, and that
        alone has to move the target's priority down — because the Dijkstra
        takes the SHORTEST path and that parent was offering a free one.
        """
        target, _ = self._leak(blind_parent_backed=1_439)
        # Isolate the path under test: with the closed parent's edge in place
        # the target is reachable at zero regret through IT (see the residual
        # test below), which would mask what this one is measuring.
        Edge.objects.filter(move_uci='h4h5').delete()
        ingest._priority_refresh_cache['at'] = 0.0
        ingest.refresh_priorities()
        target.refresh_from_db()
        informed_priority = target.priority

        # Same graph, same everything, except the blind parent goes back to
        # offering no information at all.
        blind = Position.objects.get(
            key=Edge.objects.get(move_uci='f3e5').parent_id)
        blind.backed_eval = None
        blind.save(update_fields=['backed_eval'])
        ingest._priority_refresh_cache['at'] = 0.0
        ingest.refresh_priorities()
        target.refresh_from_db()

        self.assertLess(informed_priority, target.priority)

    def test_a_closed_parent_no_longer_hands_out_a_zero_gap_path(self):
        """Leak #2, now fixed: a settled node does not relax downwards.

        A closure keeps its truth value, so at the root a WHITE_WIN child IS
        the best child and its own gap is zero.  With ordinary relaxation
        every descendant inherited that zero — a free motorway into a subtree
        that is already resolved.
        """
        self._leak(blind_parent_backed=1_439)
        target = Position.objects.get(
            key=Edge.objects.get(move_uci='h4h5').child_id)
        refuted = Edge.objects.get(move_uci='h4h5').parent_id

        regret = ingest._regret_from_root()

        # The closed node itself is still the best child at the root, so its
        # OWN regret is zero.  What must not happen is that zero flowing on.
        self.assertEqual(regret[refuted], 0.0)
        self.assertGreater(regret[target.key], 1_000)

    def test_a_closed_sibling_still_creates_the_gap_it_should(self):
        """What must NOT change: a losing alternative still carries the
        distance to the win beside it."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        winning = Edge.objects.get(parent=root, move_uci='h2h4').child
        winning.status = 'WHITE_WIN'
        winning.closure = 'MINIMAX'
        winning.save()
        loser = Edge.objects.get(parent=root, move_uci='e2e4').child
        loser.eval_cp = -300
        loser.save()

        regret = ingest._regret_from_root()

        self.assertEqual(regret[root.key], 0.0)
        self.assertGreater(regret[loser.key], 1_000)

    def test_a_materialised_won_line_is_not_a_motorway(self):
        """Closures now come in CHAINS, so this had to stop being anecdotal.

        Every node of a materialised witness is closed, so the walk must not
        run down one handing out zero regret at each link.
        """
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        head = Edge.objects.get(parent=root, move_uci='h2h4').child
        chain = [head]
        squares = ('1k6', '2k5', '3k4')
        for index, rank in enumerate(squares):
            link = ingest.get_or_create_position(
                f'8/8/8/8/8/{rank}/8/K6Q w - - 0 1')
            Edge.objects.create(parent=chain[-1],
                                move_uci=f'h1h{index + 2}', child=link)
            chain.append(link)
        Position.objects.filter(key__in=[node.key for node in chain]).update(
            status='WHITE_WIN', closure='MATE_PV')

        regret = ingest._regret_from_root()

        # The head is a child of the root, so it has a regret; everything
        # behind it is only reachable THROUGH closures and is never relaxed.
        self.assertEqual(regret[head.key], 0.0)
        for link in chain[1:]:
            self.assertEqual(regret[link.key], float('inf'))

    def test_the_value_map_prefers_status_then_backed_then_eval(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.eval_cp = 10
        pos.backed_eval = 400
        pos.save()
        self.assertEqual(ingest.best_known_eval(pos), 400)
        pos.status = 'WHITE_WIN'
        pos.save()
        self.assertEqual(ingest.best_known_eval(pos), 10_000)

    def test_the_mate_band_bonus_sees_a_backed_only_node(self):
        """A node whose subtree backs a mate score is not a blank node."""
        pos = ingest.get_or_create_position('8/8/8/8/8/8/1k6/K6Q w - - 0 1')
        pos.eval_cp = None
        pos.backed_eval = 9_900
        pos.save()
        ingest._priority_refresh_cache['at'] = 0.0
        ingest.refresh_priorities()
        pos.refresh_from_db()
        self.assertGreater(pos.priority, 0.0)

    def test_the_frontier_ranks_a_backed_child_as_informed(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        child = Edge.objects.get(parent=parent, move_uci='e2e4').child
        child.eval_cp = None
        child.backed_eval = -900          # bad for White
        child.save()
        blank = Edge.objects.get(parent=parent, move_uci='d2d4').child

        # It is ranked BY its backed value instead of by the "no information
        # at all" sentinel, which is the whole point.
        self.assertEqual(ingest._frontier_rank(child, True), -900)
        self.assertEqual(ingest._frontier_rank(blank, True), -9_999.5)


class ProofDescentIsImmuneTests(TestCase):
    """The pn selector never had this leak: it does not use regret at all."""

    @override_settings(ATOMICDB_SELECTOR='pn')
    def test_the_descent_never_consults_the_regret_map(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        with patch('atomicdb.ingest._regret_from_root') as dijkstra:
            ingest.next_tasks(2)
        dijkstra.assert_not_called()

    @override_settings(ATOMICDB_SELECTOR='pn')
    def test_a_closed_child_is_never_descended_into(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        closed = Edge.objects.get(parent=root, move_uci='h2h4').child
        closed.status = 'WHITE_WIN'
        closed.closure = 'MINIMAX'
        closed.save()
        campaign = proof.default_campaign()
        proof.refresh_proof_numbers([root.key])

        for counter in range(20):
            found, _plies = proof.descend(campaign, counter=counter)
            if found is not None:
                self.assertNotEqual(found.key, closed.key)
