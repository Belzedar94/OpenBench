import sys
import types
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import tb


ELIGIBLE_FEN = '4k3/8/8/8/8/8/8/QK6 w - - 0 1'


class AtomicTablebaseTests(SimpleTestCase):

    def setUp(self):
        tb.reset_cache()

    def tearDown(self):
        tb.reset_cache()

    def test_trusted_default_contains_owner(self):
        self.assertEqual(settings.ATOMICDB_TB_TRUSTED, ['belzedar'])

    def test_worker_compatible_applicability_guards(self):
        self.assertTrue(tb.is_applicable(ELIGIBLE_FEN))
        six_piece_fen = '4k3/8/8/8/8/8/PPP5/KQ6 w - - 0 1'
        self.assertTrue(tb.is_applicable(six_piece_fen))
        self.assertFalse(tb.is_applicable(six_piece_fen, max_pieces=5))
        self.assertFalse(tb.is_applicable(
            '4k3/8/8/8/8/8/8/R3K3 w Q - 0 1'))
        self.assertFalse(tb.is_applicable(
            '4k3/8/8/8/8/8/8/QK6 w - e3 0 1'))
        self.assertFalse(tb.is_applicable(
            '4k3/8/8/8/8/8/PPPP4/KQ6 w - - 0 1'))
        self.assertFalse(tb.is_applicable('not a fen'))

    @override_settings(ATOMICDB_TB_PATHS=['first', 'second'])
    def test_probe_is_lazy_cached_and_injectable(self):
        tablebase = mock.Mock()
        tablebase.probe_wdl.return_value = 2
        board = object()

        with mock.patch.object(tb, 'TABLEBASE_FACTORY',
                               return_value=tablebase) as factory, \
             mock.patch.object(tb, 'BOARD_FACTORY',
                               return_value=board) as board_factory:
            factory.assert_not_called()
            self.assertEqual(tb.probe_wdl(ELIGIBLE_FEN), 2)
            self.assertEqual(tb.probe_wdl(ELIGIBLE_FEN), 2)

        factory.assert_called_once_with(('first', 'second'))
        self.assertEqual(board_factory.call_count, 2)
        tablebase.probe_wdl.assert_has_calls([mock.call(board), mock.call(board)])

    def test_ineligible_position_does_not_load_tablebase(self):
        with mock.patch.object(tb, 'TABLEBASE_FACTORY') as factory:
            self.assertIsNone(tb.probe_wdl(
                '4k3/8/8/8/8/8/8/R3K3 w Q - 0 1'))
        factory.assert_not_called()

    def test_open_failure_is_fail_closed_and_cached(self):
        with mock.patch.object(
                tb, 'TABLEBASE_FACTORY', side_effect=OSError('missing')) as factory:
            with self.assertLogs('atomicdb.tb', level='ERROR'):
                self.assertIsNone(tb.probe_wdl(ELIGIBLE_FEN))
            self.assertIsNone(tb.probe_wdl(ELIGIBLE_FEN))
        factory.assert_called_once()

    def test_probe_failure_is_fail_closed(self):
        tablebase = mock.Mock()
        tablebase.probe_wdl.side_effect = KeyError('not in tables')
        with mock.patch.object(tb, 'TABLEBASE_FACTORY',
                               return_value=tablebase), \
             mock.patch.object(tb, 'BOARD_FACTORY', return_value=object()):
            self.assertIsNone(tb.probe_wdl(ELIGIBLE_FEN))

    def test_default_loader_uses_atomic_board_and_adds_directories(self):
        opened = mock.Mock()
        syzygy = types.ModuleType('chess.syzygy')
        variant = types.ModuleType('chess.variant')
        variant.AtomicBoard = object()
        syzygy.open_tablebase = mock.Mock(return_value=opened)
        chess = types.ModuleType('chess')
        chess.__path__ = []
        chess.syzygy = syzygy
        chess.variant = variant

        modules = {
            'chess': chess,
            'chess.syzygy': syzygy,
            'chess.variant': variant,
        }
        with mock.patch.dict(sys.modules, modules):
            self.assertIs(tb._open_tablebase(('one', 'two')), opened)

        syzygy.open_tablebase.assert_called_once_with(
            'one', VariantBoard=variant.AtomicBoard)
        opened.add_directory.assert_called_once_with('two')
