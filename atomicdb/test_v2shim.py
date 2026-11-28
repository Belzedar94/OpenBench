"""The v2 shim's own bridge (atomicdb/v2shim, plan v2 section 7.1).

These tests exercise the Python <-> Rust bridge with the shim's own entry
points (``shim.*``), not any rule: rows exported to the v2 graph come back
unchanged, what v2 writes lands in the rows and in the instances the test
holds, created positions and edges and emitted events become rows, values v2
cannot hold refuse to run, and the interceptor sends ported functions to v2
while everything a v1 function calls inside itself stays on v1.

They skip when the ``av2`` extension is not built (v1's CI); the v2 shim job
builds it (``atomicdb-v2/scripts/build_av2_py.py``).
"""

from unittest import skipUnless

from . import ingest, logic
from .models import DBEvent, Edge, Position
from .testing import TestCase
from .v2shim import bridge, intercept

AV2 = bridge.available()
FAKE = 'f' * 64
LONE_KINGS = '8/8/8/8/8/8/8/K6k w - - 0 1'


def _rows():
    return {r['key']: r for r in Position.objects.values(*bridge.position_columns())}


@skipUnless(AV2, 'the av2 extension is not built')
class BridgeTests(TestCase):

    def setUp(self):
        bridge.STATS.reset()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)

    def test_rows_survive_the_round_trip(self):
        before = _rows()
        result = bridge.call_v2('shim.echo')
        self.assertEqual(result['positions'], len(before))
        self.assertEqual(result['edges'], Edge.objects.count())
        self.assertEqual(result['issues'], 0)
        self.assertEqual(_rows(), before)

    def test_a_v2_write_lands_in_the_row_and_the_instance(self):
        cols = {'backed_eval': 42, 'backed_move': 'e2e4', 'backed_plies': 3,
                'status': 'DRAW', 'closure': 'MINIMAX', 'proof': 'ANDOR'}
        bridge.call_v2('shim.put_rows', ({self.root.key: cols},), {'holder': self.root})
        row = Position.objects.get(pk=self.root.key)
        for col, value in cols.items():
            self.assertEqual(getattr(row, col), value, col)
            self.assertEqual(getattr(self.root, col), value, col)
        # Only that row changed.
        others = Position.objects.exclude(pk=self.root.key)
        self.assertFalse(others.filter(status='DRAW').exists())

    def test_created_positions_and_edges_become_rows(self):
        created = bridge.call_v2('shim.create', (
            [{'key': FAKE, 'fen': LONE_KINGS, 'eval_cp': 0, 'status': 'DRAW'}],
            [[{'$pos': self.root.key}, 'a1a2', {'$pos': FAKE}]]))
        self.assertEqual([p.key for p in created], [FAKE])
        self.assertIsInstance(created[0], Position)
        lone = Position.objects.get(pk=FAKE)
        self.assertEqual((lone.fen, lone.status, lone.eval_cp), (LONE_KINGS, 'DRAW', 0))
        edge = Edge.objects.get(parent=self.root, move_uci='a1a2')
        self.assertEqual(edge.child_id, FAKE)

    def test_an_emitted_event_becomes_a_dbevent(self):
        bridge.call_v2('shim.fact', ('SHIM_TEST', {'k': 1}))
        self.assertEqual(DBEvent.objects.get(kind='SHIM_TEST').payload, {'k': 1})
        # A fact v1 has no row for is counted, not dropped silently.
        bridge.call_v2('shim.fact', ('not-an-event', {}))
        self.assertEqual(bridge.STATS.unprojected_facts, 1)

    def test_out_parameters_and_follow_up_calls(self):
        report = {}
        bridge.call_v2('shim.out', (), {'report': report, 'calls': [
            {'name': 'ingest.get_or_create_position', 'args': [LONE_KINGS]}]})
        self.assertEqual(report, {'changed': [self.root.key]})
        self.assertTrue(Position.objects.filter(fen=LONE_KINGS).exists())

    def test_rows_for_other_tables_are_written(self):
        got = bridge.call_v2('shim.tables', ([{'kind': 'SHIM_ROW', 'payload': {'n': 2}}],))
        self.assertEqual(DBEvent.objects.get(kind='SHIM_ROW').payload, {'n': 2})
        # The declared table went over, and so did v1's constants and settings.
        self.assertIn('atomicdb_dbevent', got['tables'])
        self.assertEqual(got['constants'], len(bridge.CONSTANT_MODULES))
        self.assertGreater(got['settings'], 0)

    def test_patched_constants_are_sent_as_patched(self):
        from unittest import mock
        with mock.patch('atomicdb.ingest.CASCADE_GUARD_LIMIT', 7):
            self.assertEqual(bridge.export_constants()['ingest']['CASCADE_GUARD_LIMIT'], 7)

    def test_fixture_move_names_get_stand_ins_and_come_back(self):
        lone = Position.objects.create(key=FAKE, fen=LONE_KINGS)
        Edge.objects.create(parent=self.root, move_uci='wb', child=lone)
        bridge.call_v2('shim.put_rows', ({self.root.key: {'backed_move': 'wb'}},))
        self.assertEqual(Position.objects.get(pk=self.root.key).backed_move, 'wb')
        self.assertEqual(bridge.STATS.synthetic_moves, 1)
        self.assertEqual(Edge.objects.filter(move_uci='wb').count(), 1)

    def test_a_value_v2_cannot_hold_refuses_to_run(self):
        Position.objects.filter(pk=self.root.key).update(eval_cp=1_000_000)
        with self.assertRaises(bridge.V2Error) as caught:
            bridge.call_v2('shim.echo')
        self.assertEqual(caught.exception.code, 'unrepresentable')
        self.assertIn('eval_cp', caught.exception.message)
        self.assertEqual(bridge.STATS.v2_errors[0]['code'], 'unrepresentable')

    def test_arguments_and_results_keep_their_types(self):
        self.assertEqual(bridge.decode({'$set': [1, 2]}), {1, 2})
        self.assertEqual(bridge.decode({'$tuple': [1, {'$pos': self.root.key}]}),
                         (1, self.root))
        self.assertEqual(bridge.encode({self.root}), {'$set': [{'$pos': self.root.key}]})


class InterceptTests(TestCase):

    def setUp(self):
        bridge.STATS.reset()

    def test_unported_functions_run_on_v1_and_so_does_what_they_call(self):
        calls = []

        def inner():
            calls.append('inner')
            return 1

        def outer():
            calls.append('outer')
            return wrapped_inner() + 1

        wrapped_inner = intercept.make_wrapper('x.inner', inner, routed=set())
        wrapped_outer = intercept.make_wrapper('x.outer', outer, routed=set())
        self.assertEqual(wrapped_outer(), 2)
        self.assertEqual(calls, ['outer', 'inner'])
        # Only the test's own call is counted: the nested one is v1 business.
        self.assertEqual(bridge.STATS.v1_calls, {'x.outer': 1})
        self.assertEqual(bridge.STATS.v2_calls, {})

    def test_install_wraps_the_named_functions_and_uninstall_restores_them(self):
        original = ingest.get_or_create_position
        try:
            intercept.install(names={'ingest.get_or_create_position'})
            self.assertIs(ingest.get_or_create_position.__av2_original__, original)
            ingest.get_or_create_position(logic.start_fen())
            self.assertEqual(bridge.STATS.v1_calls, {'ingest.get_or_create_position': 1})
        finally:
            intercept.uninstall()
        self.assertIs(ingest.get_or_create_position, original)

    @skipUnless(AV2, 'the av2 extension is not built')
    def test_ported_functions_go_to_v2(self):
        ingest.get_or_create_position(logic.start_fen())
        wrapped = intercept.make_wrapper('shim.echo', lambda: 'v1', routed={'shim.echo'})
        self.assertEqual(wrapped()['positions'], Position.objects.count())
        self.assertEqual(bridge.STATS.v2_calls, {'shim.echo': 1})
        self.assertEqual(bridge.STATS.v1_calls, {})
