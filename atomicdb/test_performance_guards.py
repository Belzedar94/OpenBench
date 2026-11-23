from unittest import mock

from django.test import override_settings

from . import ingest, logic
from .models import DBEvent, Edge
from .testing import TestCase


class PriorityRefreshCacheTests(TestCase):

    def setUp(self):
        ingest._priority_refresh_cache.update({'at': 0.0})

    def tearDown(self):
        ingest._priority_refresh_cache.update({'at': 0.0})

    def test_task_requests_no_longer_refresh_at_all(self):
        """P0d: the global pass moved to ``refresh_selector``."""
        ingest.get_or_create_position(logic.start_fen())
        original = ingest._regret_from_root
        with mock.patch('atomicdb.ingest._regret_from_root',
                        wraps=original) as refresh:
            ingest.next_tasks(1)
            ingest.next_tasks(1)
        self.assertEqual(refresh.call_count, 0)

    @override_settings(ATOMICDB_INLINE_SELECTOR=True)
    def test_inline_fallback_still_refreshes_only_once(self):
        ingest.get_or_create_position(logic.start_fen())
        original = ingest._regret_from_root
        with mock.patch('atomicdb.ingest._regret_from_root',
                        wraps=original) as refresh:
            ingest.next_tasks(1)
            ingest.next_tasks(1)
        self.assertEqual(refresh.call_count, 1)


class CascadeGuardTests(TestCase):

    def test_guard_exhaustion_emits_event(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).first().child
        with mock.patch('atomicdb.ingest.CASCADE_GUARD_LIMIT', 1):
            ingest.backup_cascade([child.key])
        event = DBEvent.objects.get(kind='CASCADE_GUARD')
        self.assertEqual(event.payload['iterations'], 1)
        self.assertGreater(event.payload['remaining'], 0)


def _closed(fen, **fields):
    from .models import Position
    fen = logic.canonical_fen(fen)
    pos, _ = Position.objects.update_or_create(
        key=logic.key_of(fen), defaults={'fen': fen, **fields})
    return pos


class CascadeCycleTests(TestCase):
    """23-Nov: closed positions in a repetition that hold each other's
    distance up count it upwards one ply per node and lap, for ever; one
    drainer batch reached mate_in 40,000 with its transaction open for an
    hour.  The cascade now stops rewriting such a position after
    CASCADE_REWRITE_LIMIT writes and says so."""

    def _cycle(self):
        # 1.Nf3 Nf6 2.Ng1 Ng8 is the start position again: four closed
        # nodes whose only edges are the shuffle itself.
        cycle = [('g1f3', logic.start_fen())]
        for uci in ('g8f6', 'f3g1', 'f6g8'):
            cycle.append((uci, logic.apply_move(cycle[-1][1], cycle[-1][0])))
        nodes = [(_closed(fen, status='WHITE_WIN', closure='MINIMAX',
                          proof='ENGINE', mate_in=10, best_move=uci,
                          expanded=True, clock_slack=90), uci)
                 for uci, fen in cycle]
        from .models import Position
        for node, uci in nodes:
            child = Position.objects.get(
                key=logic.key_of(logic.apply_move(node.fen, uci)))
            Edge.objects.get_or_create(parent=node, move_uci=uci,
                                       defaults={'child': child})
        return [node for node, _uci in nodes]

    def test_a_cycle_that_never_rests_is_cut_by_the_rewrite_limit(self):
        nodes = self._cycle()
        with mock.patch('atomicdb.ingest.CASCADE_GUARD_LIMIT', 5_000), \
                mock.patch('atomicdb.ingest.CASCADE_REWRITE_LIMIT', 3):
            ingest.backup_cascade([nodes[0].key])

        self.assertFalse(DBEvent.objects.filter(kind='CASCADE_GUARD').exists())
        event = DBEvent.objects.get(kind='CASCADE_CYCLE')
        self.assertEqual(event.payload['limit'], 3)
        self.assertGreaterEqual(event.payload['count'], 1)
        self.assertLess(event.payload['iterations'], 40)
        for node in nodes:
            node.refresh_from_db()
            # three writes of at most a few plies each, not a guard's worth
            self.assertLessEqual(node.mate_in, 10 + 4 * 3 + 4)
            self.assertEqual(node.status, 'WHITE_WIN')

    def test_a_long_chain_is_not_touched_by_the_limit(self):
        """The limit counts writes of ONE position: a deep chain writes
        each node once and still reaches its fixed point."""
        moves = ['g1f3', 'g8f6', 'b1c3', 'b8c6', 'e2e3', 'e7e6', 'd2d3',
                 'd7d6', 'a2a3', 'a7a6', 'h2h3', 'h7h6']
        fens = [logic.start_fen()]
        for uci in moves:
            fens.append(logic.apply_move(fens[-1], uci))
        length = len(moves)
        nodes = [_closed(fens[i], status='WHITE_WIN', closure='MINIMAX',
                         proof='ENGINE', mate_in=100, clock_slack=90,
                         expanded=True) for i in range(length)]
        terminal = _closed(fens[length], status='WHITE_WIN',
                           closure='TERMINAL', mate_in=0, clock_slack=90)
        for parent, child, uci in zip(nodes, nodes[1:] + [terminal], moves):
            Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                       defaults={'child': child})
        with mock.patch('atomicdb.ingest.CASCADE_REWRITE_LIMIT', 1):
            ingest.backup_cascade([terminal.key])

        self.assertFalse(DBEvent.objects.filter(kind='CASCADE_CYCLE').exists())
        for depth, node in enumerate(nodes):
            node.refresh_from_db()
            self.assertEqual(node.mate_in, length - depth)
