"""The campaigns page shows each proposed line's proof numbers (BManBJ, 8-Nov-2026)."""
from django.test import Client, TestCase

from atomicdb import ingest, logic, proof
from atomicdb.models import Campaign, ProofNode


class CampaignProofNumberTests(TestCase):
    databases = '__all__'

    def setUp(self):
        self.campaign = proof.default_campaign()
        self.root = ingest.get_or_create_position(logic.start_fen())

    def _propose(self, name='line'):
        return Campaign.objects.create(name=name, root=self.root,
                                       state=Campaign.CState.PROPOSED)

    def _page(self):
        response = Client().get('/atomicdb/campaigns/')
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_open_line_shows_both_numbers(self):
        self._propose()
        ProofNode.objects.update_or_create(
            campaign=self.campaign, position=self.root,
            defaults={'pn': 1234, 'dn': 56})
        body = self._page()
        self.assertIn('1,234 positions to prove a White win', body)
        self.assertIn('56 to show it is not', body)

    def test_proven_line_says_so(self):
        self._propose()
        ProofNode.objects.update_or_create(
            campaign=self.campaign, position=self.root,
            defaults={'pn': 0, 'dn': proof.PROOF_INFINITY})
        self.assertIn('Proven win for White', self._page())

    def test_no_proof_node_shows_nothing(self):
        self._propose()
        ProofNode.objects.filter(position=self.root).delete()
        self.assertNotIn('positions to prove a White win', self._page())
