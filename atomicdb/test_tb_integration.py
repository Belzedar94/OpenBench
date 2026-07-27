from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings

from . import ingest
from .models import DBEvent
from .testing import TestCase


FIVE_MEN = '4k3/8/8/8/8/8/8/QK6 w - - 0 1'
SIX_MEN = '4k3/8/8/8/8/8/PPP5/KQ6 w - - 0 1'


class TablebaseClosureTrustTests(TestCase):

    @mock.patch('atomicdb.ingest.tb.probe_wdl', return_value=2)
    def test_five_piece_result_is_reprobed_and_accepted(self, probe):
        position = ingest.get_or_create_position(FIVE_MEN)
        self.assertTrue(ingest.close_by_tb(position.key, 2))
        probe.assert_called_once_with(position.fen, max_pieces=5)
        position.refresh_from_db()
        self.assertEqual((position.status, position.closure),
                         ('WHITE_WIN', 'TB'))
    @mock.patch('atomicdb.ingest.tb.probe_wdl', return_value=-2)
    def test_false_five_piece_result_is_rejected(self, probe):
        position = ingest.get_or_create_position(FIVE_MEN)
        self.assertFalse(ingest.close_by_tb(position.key, 2))
        position.refresh_from_db()
        self.assertEqual(position.status, 'UNKNOWN')
        event = DBEvent.objects.get(kind='TB_REJECTED')
        self.assertEqual(event.payload['reason'], 'wdl-mismatch')
        self.assertEqual(event.payload['server_wdl'], -2)

    @override_settings(ATOMICDB_TB_TRUSTED=['belzedar'])
    def test_untrusted_six_piece_result_is_rejected(self):
        user = User.objects.create_user('visitor', password='secret')
        position = ingest.get_or_create_position(SIX_MEN)
        self.assertFalse(ingest.close_by_tb(position.key, 2, user=user))
        self.assertTrue(DBEvent.objects.filter(
            kind='TB_REJECTED',
            payload__reason='untrusted-six-piece').exists())

    @override_settings(ATOMICDB_TB_TRUSTED=['belzedar'])
    def test_trusted_six_piece_result_is_accepted(self):
        user = User.objects.create_user('belzedar', password='secret')
        position = ingest.get_or_create_position(SIX_MEN)
        self.assertTrue(ingest.close_by_tb(position.key, 2, user=user))
        position.refresh_from_db()
        self.assertEqual((position.status, position.closure),
                         ('WHITE_WIN', 'TB'))
