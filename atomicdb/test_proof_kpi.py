"""pn/dn as the project's headline number, on the home page and in history.

Deliberately not a percentage: the denominator of "% solved" grows every time
a new obligation is discovered, so a progress bar here would be a lie that
also goes backwards.  What these two numbers say is how much effort is left to
PROVE the conjecture and how much to REFUTE it — and a pn that climbs while dn
stalls is the earliest honest sign the conjecture may be false.
"""

from io import StringIO

from django.core.management import call_command

from . import ingest, logic, proof
from .models import Edge, ProgressSnapshot, ProofCampaign, ProofNode
from .testing import TestCase


class FormatNumberTests(TestCase):

    def test_saturation_reads_as_infinity_not_as_a_huge_integer(self):
        self.assertEqual(proof.format_number(proof.PROOF_INFINITY), '∞')
        self.assertEqual(proof.format_number(proof.PROOF_INFINITY + 5),
                         '∞')

    def test_zero_is_a_real_answer_and_stays_zero(self):
        self.assertEqual(proof.format_number(0), '0')

    def test_ordinary_numbers_get_thousands_separators(self):
        self.assertEqual(proof.format_number(1_234_567), '1,234,567')

    def test_absent_is_a_dash(self):
        self.assertEqual(proof.format_number(None), '-')


class HeadlineNumbersTests(TestCase):

    def test_the_default_campaign_is_the_headline(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        proof.refresh_proof_numbers([root.key])

        pn, dn = proof.headline_numbers()

        node = ProofNode.objects.get(
            campaign__name=proof.DEFAULT_CAMPAIGN_NAME, position=root)
        self.assertEqual((pn, dn), (node.pn, node.dn))

    def test_no_active_campaign_means_no_headline(self):
        ProofCampaign.objects.update(active=False)
        self.assertEqual(proof.headline_numbers(), (None, None))

    def test_a_proved_root_reads_zero_and_infinity(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).first().child
        child.status = 'WHITE_WIN'
        child.closure = 'MATE_PV'
        child.proof = 'ANDOR'
        child.save()
        proof.refresh_proof_numbers([child.key])

        pn, dn = proof.headline_numbers()

        self.assertEqual(pn, 0)
        self.assertEqual(proof.format_number(dn), '∞')


class HomeKpiTests(TestCase):

    def test_the_home_shows_both_numbers_and_no_percentage_of_them(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        proof.refresh_proof_numbers([root.key])

        body = self.client.get('/atomicdb/').content.decode()

        self.assertIn('proof number', body)
        self.assertIn('disproof number', body)
        self.assertIn('Not a probability', body)

    def test_a_saturated_number_is_painted_as_infinity(self):
        root = ingest.get_or_create_position(logic.start_fen())
        campaign = ProofCampaign.objects.get(
            name=proof.DEFAULT_CAMPAIGN_NAME)
        ProofNode.objects.create(campaign=campaign, position=root,
                                 pn=proof.PROOF_INFINITY, dn=0)

        body = self.client.get('/atomicdb/').content.decode()

        self.assertIn('∞', body)
        self.assertNotIn(str(proof.PROOF_INFINITY), body)

    def test_without_a_campaign_the_tiles_simply_are_not_there(self):
        ProofCampaign.objects.update(active=False)

        body = self.client.get('/atomicdb/').content.decode()

        self.assertNotIn('proof number', body)


class SnapshotProofNumbersTests(TestCase):

    def test_the_hourly_capture_records_both(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        proof.refresh_proof_numbers([root.key])
        expected = proof.headline_numbers()

        call_command('capture_atomicdb_progress', stdout=StringIO())

        snapshot = ProgressSnapshot.objects.get()
        self.assertEqual((snapshot.root_pn, snapshot.root_dn), expected)

    def test_a_tree_without_a_campaign_records_zeroes_not_a_gap(self):
        ProofCampaign.objects.update(active=False)

        call_command('capture_atomicdb_progress', stdout=StringIO())

        snapshot = ProgressSnapshot.objects.get()
        self.assertEqual((snapshot.root_pn, snapshot.root_dn), (0, 0))

    def test_the_columns_are_append_only_like_the_rest_of_the_row(self):
        call_command('capture_atomicdb_progress', stdout=StringIO())
        snapshot = ProgressSnapshot.objects.get()
        snapshot.root_pn = 5
        with self.assertRaisesMessage(Exception, 'append-only'):
            snapshot.save()
