"""One node, one number: what a position HEADLINES is its best-known value.

Community report (opabinia, 2026-08-06): a node's header quoted +3.70 — its
own eval, 640M nodes — while its subtree already backed +4.36 with 2.13G
nodes via b2b4.  Both numbers were true; the header stood on the weaker one.
Owner decision: the headline is the best-known value, the same precedence the
backend already keeps in ``ingest.best_known_eval`` — proven status > backed
(as the write-time quality guard stored it) > point eval — on every surface
that quotes the number: the explore header, the node's row in its parent's
table, the query API ``score``, the map inspector.  The raw point eval never
headlines while something better is known; it survives as own-search context
(tooltips, the API ``point`` field).

These tests pin that precedence at the VIEW level, rung by rung, driving the
backed rungs through the real cascade (``backup_backed_evals``) so the guard
semantics exercised are the stored ones, not a re-implementation.
"""

from unittest import mock

from . import conquest_map, ingest, logic, views
from .models import Edge, Position
from .testing import TestCase


def _hex(number):
    return f'{number:064x}'


def _map_row(key, *, eval_cp=None, backed_eval=None, status='UNKNOWN'):
    return {'key': key, 'fen': logic.start_fen(), 'eval_cp': eval_cp,
            'backed_eval': backed_eval, 'status': status}


class HeadlineValueTests(TestCase):

    def _explore(self, pos):
        return self.client.get(f'/atomicdb/explore/{pos.key}/')

    def _api(self, pos):
        return self.client.get('/atomicdb/api/query',
                               {'fen': pos.fen}).json()

    def test_a_proven_node_headlines_its_status_not_its_stale_eval(self):
        """Rung one: proof.  The eval that closed the gap to the proof is
        history; quoting it next to WHITE_WIN would be two verdicts."""
        pos = ingest.get_or_create_position(logic.start_fen())
        Position.objects.filter(key=pos.key).update(
            status='WHITE_WIN', closure='MINIMAX', mate_in=11,
            eval_cp=370, backed_eval=420, backed_plies=3,
            nodes_invested=640_000_000)
        pos.refresh_from_db()

        body = self._explore(pos).content.decode()
        self.assertIn('WHITE_WIN', body)
        self.assertNotIn('best line', body)   # no cp headline on a proof

        payload = self._api(pos)
        self.assertEqual(payload['score'], 10_000)   # the proof, mover-POV
        self.assertEqual(payload['point'], 370)      # the record survives

    def test_the_reported_drift_headlines_the_backed_value_end_to_end(self):
        """The reported shape, through the real cascade: own +370 @ 640M,
        the subtree backing +436 @ 2.13G via b2b4.  Heavier and favourable,
        so the guard stores it — and every surface quotes it."""
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        Position.objects.filter(key=parent.key).update(
            eval_cp=370, nodes_invested=640_000_000, visits=2)
        child = Edge.objects.get(parent=parent, move_uci='b2b4').child
        Position.objects.filter(key=child.key).update(
            backed_eval=436, backed_nodes=2_130_000_000, backed_plies=4)

        ingest.backup_backed_evals([parent.key])

        parent.refresh_from_db()
        self.assertEqual(parent.backed_eval, 436)
        self.assertEqual(parent.backed_move, 'b2b4')

        body = self._explore(parent).content.decode()
        self.assertIn('best line +436cp', body)
        self.assertNotIn('best line +370cp', body)
        # The point eval is context now, not the headline.
        self.assertIn('eval stored on this position +370cp', body)

        payload = self._api(parent)
        self.assertEqual(payload['score'], 436)
        self.assertEqual(payload['point'], 370)

    def test_a_search_only_node_headlines_its_own_point_eval(self):
        """Rung three: with nothing proven and nothing backed, the point
        eval IS the best knowledge and headlines unadorned."""
        pos = ingest.get_or_create_position(logic.start_fen())
        Position.objects.filter(key=pos.key).update(
            eval_cp=30, nodes_invested=128_000_000)
        pos.refresh_from_db()

        body = self._explore(pos).content.decode()
        self.assertIn('best line +30cp', body)
        self.assertNotIn('backed-mark', body)

        payload = self._api(pos)
        self.assertEqual(payload['score'], 30)
        self.assertEqual(payload['point'], 30)

    def test_a_backed_value_the_quality_guard_blocked_never_headlines(self):
        """The inverse of the report: 8M of support claiming better than a
        10B own search.  The write-time guard keeps the own value as the
        stored backed value, so the headline stands on the own eval — the
        block is bought out by quality convergence, never displayed away."""
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        Position.objects.filter(key=parent.key).update(
            eval_cp=369, nodes_invested=10_000_000_000)
        child = Edge.objects.get(parent=parent, move_uci='d2d4').child
        Position.objects.filter(key=child.key).update(
            backed_eval=416, backed_nodes=8_000_000, backed_plies=2)

        ingest.backup_backed_evals([parent.key])

        parent.refresh_from_db()
        self.assertEqual(parent.backed_eval, 369)    # guard held the line
        self.assertIsNone(parent.backed_move)

        body = self._explore(parent).content.decode()
        self.assertIn('best line +369cp', body)
        self.assertNotIn('best line +416cp', body)

        self.assertEqual(self._api(parent)['score'], 369)

    def test_one_node_quotes_one_number_on_every_surface(self):
        """Header, parent row, API and map inspector: the same best-known
        value, with exactly one perspective flip per convention — mover-POV
        on header/row/API (chessdb.cn), White-POV on the map."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        node = Edge.objects.get(parent=root, move_uci='e2e4').child
        Position.objects.filter(key=node.key).update(
            eval_cp=-120, nodes_invested=640_000_000,
            backed_eval=-180, backed_nodes=2_000_000_000, backed_plies=3)
        node.refresh_from_db()

        # Header: Black to move, best known -180 White-POV -> +180 mover-POV.
        body = self._explore(node).content.decode()
        self.assertIn('best line +180cp', body)

        # The node's row in its parent's table: White chooses at the root,
        # so the same value reads -180 there.
        row = next(m for m in views._child_moves(root)
                   if m['uci'] == 'e2e4')
        self.assertEqual(row['score'], -180)

        # API: mover-POV, like the header.
        payload = self._api(node)
        self.assertEqual(payload['score'], 180)
        self.assertEqual(payload['point'], 120)

        # Map snapshot: White-POV, and the display field quotes the backed
        # value, not the raw eval.
        snapshot = conquest_map.build_snapshot_from_database()
        self.assertEqual(snapshot['nodes'][node.key]['e'], -180)


class MapInspectorPrecedenceTests(TestCase):
    """The map's display field follows the one precedence, rung by rung.

    Only the display field: the map's regret arithmetic keeps measuring with
    the raw point eval (``_position_utility``), because re-weighting the
    layout was never part of the display decision.
    """

    def _snapshot(self, rows, edges, root_key):
        with mock.patch(
                'atomicdb.conquest_map._san_for_edge',
                side_effect=lambda _fen, uci: f'SAN-{uci}'):
            return conquest_map.build_snapshot_data(
                rows, edges, root_key=root_key)

    def test_the_display_field_walks_the_three_rungs(self):
        root, proven, drifted, plain = (_hex(index) for index in range(1, 5))
        rows = [
            _map_row(root, eval_cp=10, backed_eval=25),
            _map_row(proven, eval_cp=370, backed_eval=420,
                     status='WHITE_WIN'),
            _map_row(drifted, eval_cp=370, backed_eval=436),
            _map_row(plain, eval_cp=30),
        ]
        edges = [(root, proven, 'a1a2'), (root, drifted, 'b1b2'),
                 (root, plain, 'c1c2')]

        nodes = self._snapshot(rows, edges, root)['nodes']

        self.assertEqual(nodes[proven]['e'], 10_000)   # proof first
        self.assertEqual(nodes[drifted]['e'], 436)     # then backed
        self.assertEqual(nodes[plain]['e'], 30)        # then the point eval
        self.assertEqual(nodes[root]['e'], 25)

    def test_the_regret_arithmetic_still_measures_with_the_raw_eval(self):
        self.assertEqual(
            conquest_map._position_utility(
                {'status': 'UNKNOWN', 'eval_cp': 370, 'backed_eval': 436}),
            370)
