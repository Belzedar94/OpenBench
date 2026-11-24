"""Tablebase wins keep their distance to the tables (Torom's proposal, 24-Nov).

The worker clamped every centipawn score to +-9500; the stored scale now
keeps a tablebase win as 9500 minus its plies to the tables and squeezes
huge ordinary evals below that (see atomicdb.score_bands).  These tests hold
the map to its contract (monotone, symmetric, bands in order, the old
thresholds unmoved), the ingest to deriving it from the raw UCI line, the
backup to preferring the shorter way into the tables, and the recompute
command to rewriting old rows only when asked.
"""

import hashlib
import json
from collections import Counter
from io import StringIO

from django.core.management import call_command

from . import ingest, logic, score_bands
from .management.commands import recompute_tb_distance as recompute
from .models import DBEvent, Edge, Position
from .testing import TestCase

ENGINE = 128_000_000
FEN = '4k3/8/8/8/8/8/8/4K3 {} - - 0 1'


def _raw(cp, move, multipv=1):
    return (f'info depth 30 seldepth 41 multipv {multipv} score cp {cp} '
            f'nodes 128000000 nps 6000000 hashfull 400 tbhits 12 time 21000 '
            f'pv {move}')


def _line(move, cp, eval_cp=None, multipv=1, **extra):
    """A line as the worker sends it: raw cp from the side to move."""
    return {'move': move, 'mate': None, 'pv': [move],
            'eval_cp': score_bands.legacy_value(cp) if eval_cp is None
            else eval_cp,
            'raw': _raw(cp, move, multipv), **extra}


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm, **kw):
    return Position.objects.create(key=_key(name), fen=FEN.format(stm), **kw)


class MapTests(TestCase):

    def test_the_examples(self):
        f = score_bands.engine_cp_to_stored
        self.assertEqual(f(19_981), 9_481)        # TB win 19 plies down
        self.assertEqual(f(20_000), 9_500)        # already in the tables
        self.assertEqual(f(-19_981), -9_481)
        self.assertEqual(f(20_000 - 246), 9_254)  # the engine's MAX_PLY
        self.assertEqual(f(8_999), 8_999)
        self.assertEqual(f(9_000), 9_000)
        self.assertEqual(f(15_146), 9_246)        # largest ordinary score
        self.assertEqual(f(15_200), 9_249)
        self.assertEqual(f(0), 0)
        self.assertEqual(f(-35), -35)

    def test_monotone_and_symmetric(self):
        f = score_bands.engine_cp_to_stored
        previous = None
        for cp in range(-20_100, 20_101):
            value = f(cp)
            self.assertEqual(f(-cp), -value)
            if previous is not None:
                self.assertGreaterEqual(value, previous, cp)
            previous = value

    def test_bands_are_strictly_ordered(self):
        f = score_bands.engine_cp_to_stored
        plain = [f(cp) for cp in range(0, 9_000)]
        big = [f(cp) for cp in range(9_000, 19_750)]
        tb = [f(cp) for cp in range(19_750, 20_001)]
        mate = [10_000 - moves for moves in range(1, 124)]   # MAX_PLY / 2
        self.assertLess(max(plain), min(big))
        self.assertLess(max(big), min(tb))
        self.assertLess(max(tb), min(mate))
        self.assertLess(max(mate), 10_000)
        self.assertEqual(max(plain), score_bands.PLAIN_MAX)
        self.assertEqual((min(big), max(big)),
                         (score_bands.BIG_LOW, score_bands.BIG_HIGH))
        self.assertEqual((min(tb), max(tb)),
                         (score_bands.TB_LOW, score_bands.TB_TOP))
        # Shorter way into the tables, higher value.
        self.assertEqual(sorted(tb), tb)
        self.assertEqual(len(set(tb)), len(tb))

    def test_the_thresholds_the_code_reads_did_not_move(self):
        # Everything the old clamp produced (+-9500) and every plain score
        # from 9000 up is still >= MATE_BAND, and nothing but a mate count is
        # above TB_CLAMP_CEILING.
        self.assertEqual(ingest.MATE_BAND, 9_000)
        self.assertEqual(ingest.TB_CLAMP_CEILING, 9_500)
        f = score_bands.engine_cp_to_stored
        for cp in (9_000, 9_499, 9_500, 12_000, 15_146, 19_754, 19_981,
                   20_000):
            self.assertGreaterEqual(f(cp), ingest.MATE_BAND)
            self.assertLessEqual(f(cp), ingest.TB_CLAMP_CEILING)
            self.assertEqual(f(cp) >= ingest.MATE_BAND,
                             score_bands.legacy_value(cp) >= ingest.MATE_BAND)
        for cp in (0, 500, 8_999):
            self.assertLess(f(cp), ingest.MATE_BAND)

    def test_band_names(self):
        self.assertEqual([score_bands.band(v) for v in
                          (None, 120, -9_100, 9_481, -9_997, 10_000)],
                         [None, 'plain', 'big', 'tb', 'mate', 'proven'])


class NormalizeTests(TestCase):

    def test_white_and_black_to_move(self):
        line = _line('e2e4', 19_981)
        self.assertEqual(score_bands.normalize_line(line, True)['eval_cp'],
                         9_481)
        black = dict(line, eval_cp=-9_500)
        self.assertEqual(score_bands.normalize_line(black, False)['eval_cp'],
                         -9_481)

    def test_idempotent_and_copying(self):
        line = _line('e2e4', 12_000)
        once = score_bands.normalize_line(line, True)
        self.assertIsNot(once, line)
        self.assertEqual(line['eval_cp'], 9_500)          # untouched input
        self.assertEqual(once['eval_cp'], 9_120)
        self.assertIs(score_bands.normalize_line(once, True), once)

    def test_lines_it_leaves_alone(self):
        no_raw = {'move': 'e2e4', 'eval_cp': 9_500, 'mate': None}
        mate = {'move': 'e2e4', 'eval_cp': 9_997, 'mate': 3,
                'raw': 'info depth 9 multipv 1 score mate 3 pv e2e4'}
        # An eval_cp the raw line does not account for: not ours to fix.
        foreign = _line('e2e4', 19_981, eval_cp=4_000)
        for line in (no_raw, mate, foreign, 'junk'):
            self.assertIs(score_bands.normalize_line(line, True), line)

    def test_bounds_and_wdl_in_the_raw_line(self):
        line = _line('e2e4', 0)
        line['raw'] = ('info depth 30 seldepth 41 multipv 1 score cp 19970 '
                       'lowerbound wdl 1000 0 0 nodes 9 pv e2e4')
        line['eval_cp'] = 9_500
        self.assertEqual(score_bands.normalize_line(line, True)['eval_cp'],
                         9_470)


class IngestTests(TestCase):

    def test_a_tablebase_line_keeps_its_distance(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        ingest.ingest_analysis(root.key, [
            _line('d2d4', 19_970, multipv=1),
            _line('e2e4', 19_981, multipv=2),
            _line('g1f3', 12_000, multipv=3),
        ], nodes_budget=1_000)
        root.refresh_from_db()
        self.assertEqual(root.eval_cp, 9_481)
        self.assertEqual(root.best_move, 'e2e4')
        self.assertEqual([ln['eval_cp'] for ln in root.last_analysis],
                         [9_470, 9_481, 9_120])
        # The raw line is kept as the engine printed it.
        self.assertIn('score cp 19981', root.last_analysis[1]['raw'])
        seeded = dict(Edge.objects.filter(parent=root).values_list(
            'move_uci', 'child__eval_cp'))
        self.assertEqual((seeded['e2e4'], seeded['d2d4'], seeded['g1f3']),
                         (9_481, 9_470, 9_120))
        self.assertEqual(root.analysis_passes[0]['eval_cp'], 9_481)
        self.assertEqual(root.status, 'UNKNOWN')     # an eval never closes

    def test_black_to_move(self):
        fen = logic.apply_move(logic.start_fen(), 'e2e4')
        pos = ingest.get_or_create_position(fen)
        ingest.expand(pos)
        ingest.ingest_analysis(pos.key, [
            {'move': 'e7e5', 'mate': None, 'pv': ['e7e5'], 'eval_cp': -9_500,
             'raw': _raw(19_990, 'e7e5')},
        ], nodes_budget=1_000)
        pos.refresh_from_db()
        self.assertEqual(pos.eval_cp, -9_490)

    def test_a_line_without_raw_keeps_its_value(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        ingest.ingest_analysis(root.key, [
            {'move': 'e2e4', 'eval_cp': 9_500, 'mate': None, 'pv': ['e2e4']},
        ], nodes_budget=1_000)
        root.refresh_from_db()
        self.assertEqual(root.eval_cp, 9_500)


class BackupTests(TestCase):

    def _family(self, stm, values):
        node = _pos('TBD-' + stm, stm, expanded=True, eval_cp=values[0][1],
                    best_move=values[0][0], nodes_invested=ENGINE)
        other = 'b' if stm == 'w' else 'w'
        for move, value in values:
            child = _pos(f'TBD-{stm}-{move}', other, eval_cp=value,
                         nodes_invested=ENGINE)
            Edge.objects.create(parent=node, move_uci=move, child=child)
        return node

    def test_the_winner_takes_the_shorter_way_in(self):
        node = self._family('w', [('a1a2', 9_470), ('a1b1', 9_481)])
        ingest.backup_backed_evals([node.key])
        node.refresh_from_db()
        self.assertEqual((node.backed_eval, node.backed_move),
                         (9_481, 'a1b1'))

    def test_the_loser_takes_the_longer_way_in(self):
        # Black to move, every reply loses to a tablebase win: Black keeps
        # the tables as far away as it can.
        node = self._family('b', [('a1a2', 9_481), ('a1b1', 9_470)])
        ingest.backup_backed_evals([node.key])
        node.refresh_from_db()
        self.assertEqual((node.backed_eval, node.backed_move),
                         (9_470, 'a1b1'))

    def test_best_child_orders_by_distance(self):
        near = ingest._ChildValue('a1b1', 9_481, ENGINE, 1)
        far = ingest._ChildValue('a1a2', 9_470, ENGINE, 1)
        big = ingest._ChildValue('a1b2', 9_240, ENGINE, 1)
        self.assertIs(ingest._best_child([far, big, near], True), near)
        self.assertIs(ingest._best_child(
            [ingest._ChildValue(c.move, -c.value, ENGINE, 1)
             for c in (far, big, near)], False).move, 'a1b1')


class RecomputeCommandTests(TestCase):
    """Old rows, stored with the clamp, rewritten from their raw lines."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        legacy = [_line('d2d4', 19_970, multipv=1, _budget=ENGINE),
                  _line('e2e4', 19_981, multipv=2)]
        Position.objects.filter(key=self.root.key).update(
            eval_cp=9_500, best_move='d2d4', nodes_invested=ENGINE,
            visits=1, last_analysis=legacy)
        self.children = dict(Edge.objects.filter(parent=self.root)
                             .values_list('move_uci', 'child_id'))
        # Both seeded from the root's lines, as the old ingest left them.
        Position.objects.filter(key__in=[self.children['e2e4'],
                                         self.children['d2d4']]).update(
            eval_cp=9_500)
        # A huge ordinary eval with a search of its own, and an old result
        # with no raw line.
        self.big = _pos('TBD-big', 'w', eval_cp=9_500, best_move='a1a2',
                        nodes_invested=ENGINE,
                        last_analysis=[_line('e1e2', 12_000,
                                             _budget=ENGINE)])
        self.bare = _pos('TBD-bare', 'w', eval_cp=9_500, best_move='e1e2',
                         nodes_invested=ENGINE,
                         last_analysis=[{'move': 'e1e2', 'eval_cp': 9_500,
                                         'mate': None, 'pv': ['e1e2']}])

    def _values(self):
        keys = [self.root.key, self.children['e2e4'], self.children['d2d4'],
                self.big.key, self.bare.key]
        rows = Position.objects.in_bulk(keys)
        return [rows[key].eval_cp for key in keys]

    def _run(self, *args):
        out = StringIO()
        call_command('recompute_tb_distance', '--json', *args, stdout=out)
        return json.loads(out.getvalue())

    def test_dry_run_writes_nothing(self):
        summary = self._run()
        self.assertFalse(summary['apply'])
        self.assertEqual(summary['planned'], 4)     # root, 2 seeds, big
        self.assertEqual(summary['written'], 0)
        self.assertEqual(summary['no_raw'], 1)
        self.assertEqual(self._values(), [9_500] * 5)
        self.assertFalse(DBEvent.objects.filter(
            kind=recompute.EVENT).exists())

    def test_apply_rewrites_and_climbs(self):
        summary = self._run('--apply')
        self.assertEqual(summary['written'], 4)
        self.assertEqual(self._values(), [9_481, 9_481, 9_470, 9_120, 9_500])
        root = Position.objects.get(key=self.root.key)
        self.assertEqual(root.best_move, 'e2e4')
        self.assertEqual([ln['eval_cp'] for ln in root.last_analysis],
                         [9_470, 9_481])
        self.assertEqual(root.last_analysis[0]['_budget'], ENGINE)
        # The climb stood the root on the shorter way into the tables.
        self.assertEqual((root.backed_eval, root.backed_move),
                         (9_481, 'e2e4'))
        event = DBEvent.objects.filter(kind=recompute.EVENT).get()
        self.assertTrue(event.payload['done'])
        # A second run finds nothing left to do.
        again = self._run('--apply')
        self.assertEqual(again['planned'], 0)

    def test_keys_mode(self):
        summary = self._run('--apply', '--keys', self.big.key)
        self.assertEqual(summary['mode'], 'keys')
        self.assertEqual(summary['written'], 1)
        self.assertEqual(self._values(), [9_500, 9_500, 9_500, 9_120, 9_500])

    def test_a_seed_that_was_searched_meanwhile_is_left(self):
        plans = recompute.Command()
        plans.counts = Counter()
        planned = plans._plan([self.children['e2e4']])
        Position.objects.filter(key=self.children['e2e4']).update(
            nodes_invested=ENGINE)
        self.assertEqual(plans._write(planned), [])
        self.assertEqual(plans.counts['moved'], 1)
