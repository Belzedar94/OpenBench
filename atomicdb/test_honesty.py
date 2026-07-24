from . import ingest, logic
from .testing import TestCase


class PublicHonestyTests(TestCase):

    def test_home_and_method_expose_practical_trust(self):
        home = self.client.get('/atomicdb/')
        self.assertContains(home, 'practically solved')
        self.assertNotContains(home, 'solved exactly')
        method = self.client.get('/atomicdb/method/')
        self.assertContains(method, 'PRACTICAL')
        self.assertContains(method, 'ANDOR')
        self.assertContains(method, 'ENGINE')
        self.assertContains(method, '≤M4')

    def test_query_contract_has_fixed_tier_trust_and_history_scope(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.status, pos.closure, pos.proof = 'WHITE_WIN', 'MATE_PV', 'ANDOR'
        pos.save()
        response = self.client.get('/atomicdb/api/query', {'fen': pos.fen})
        payload = response.json()
        self.assertEqual(payload['tier'], 'PRACTICAL')
        self.assertEqual(payload['trust'], 'ANDOR')
        self.assertEqual(payload['history_scope'],
                         'COUNTERS_AND_REPETITION_IGNORED')

    def test_terminal_trust_is_verified(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.status, pos.closure = 'DRAW', 'TERMINAL'
        pos.save()
        response = self.client.get('/atomicdb/api/query', {'fen': pos.fen})
        self.assertEqual(response.json()['trust'], 'VERIFIED')

    def test_historical_mate_without_proof_is_unclassified(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.status, pos.closure, pos.proof = 'WHITE_WIN', 'MATE_PV', None
        pos.save()
        response = self.client.get('/atomicdb/api/query', {'fen': pos.fen})
        self.assertEqual(response.json()['trust'], 'UNCLASSIFIED')

    def test_disputed_unknown_is_visible(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        pos.proof = 'DISPUTED'
        pos.save()
        response = self.client.get('/atomicdb/api/query', {'fen': pos.fen})
        self.assertEqual(response.json()['trust'], 'DISPUTED')
