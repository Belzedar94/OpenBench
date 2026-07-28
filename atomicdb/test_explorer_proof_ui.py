"""pn/dn beside every reply, and one click for the replies nobody looked at."""

from unittest.mock import patch

from . import ingest, logic, proof, views
from .models import (AnalysisTask, DBEvent, Edge, Position, ProofCampaign,
                     ProofNode, RequestLog)
from .testing import TestCase
from .views import _child_moves, _proof_numbers_for


class ProofNumbersPerRowTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.campaign = ProofCampaign.objects.get(
            name=proof.DEFAULT_CAMPAIGN_NAME)

    def test_a_row_with_a_proof_node_shows_both_numbers(self):
        child = Edge.objects.filter(parent=self.root).first().child
        ProofNode.objects.create(campaign=self.campaign, position=child,
                                 pn=1_234, dn=56)

        row = next(m for m in _child_moves(self.root)
                   if m['key'] == child.key)

        self.assertEqual(row['pn_h'], '1,234')
        self.assertEqual(row['dn_h'], '56')

    def test_a_row_without_one_shows_nothing_rather_than_one_one(self):
        """1/1 is PNS for "I know nothing"; painting it would be a claim."""
        row = next(iter(_child_moves(self.root)))
        self.assertIsNone(row['pn_h'])
        self.assertIsNone(row['dn_h'])

    def test_saturation_reads_as_infinity(self):
        child = Edge.objects.filter(parent=self.root).first().child
        ProofNode.objects.create(campaign=self.campaign, position=child,
                                 pn=proof.PROOF_INFINITY, dn=0)

        row = next(m for m in _child_moves(self.root)
                   if m['key'] == child.key)

        self.assertEqual(row['pn_h'], '∞')
        self.assertEqual(row['dn_h'], '0')

    def test_the_whole_table_costs_one_extra_query(self):
        for edge in Edge.objects.filter(parent=self.root):
            ProofNode.objects.create(campaign=self.campaign,
                                     position=edge.child, pn=7, dn=9)
        keys = [edge.child_id for edge in
                Edge.objects.filter(parent=self.root)]
        # Twenty rows, two statements: the active campaign and the numbers.
        with self.assertNumQueries(2):
            _proof_numbers_for(keys)

    def test_an_inactive_campaign_paints_no_numbers(self):
        child = Edge.objects.filter(parent=self.root).first().child
        ProofNode.objects.create(campaign=self.campaign, position=child,
                                 pn=5, dn=5)
        ProofCampaign.objects.update(active=False)

        self.assertEqual(_proof_numbers_for([child.key]), {})

    def test_the_page_renders_the_column(self):
        child = Edge.objects.filter(parent=self.root).first().child
        ProofNode.objects.create(campaign=self.campaign, position=child,
                                 pn=42, dn=99)

        body = self.client.get(
            f'/atomicdb/explore/{self.root.key}/').content.decode()

        self.assertIn('pn / dn', body)
        self.assertIn('Estimated proof / disproof effort', body)
        self.assertIn('42 / 99', body)


class UnexploredButtonTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)

    def test_it_queues_every_reply_nobody_looked_at(self):
        blanks = len(ingest.unexplored_children(self.root))
        self.assertGreater(blanks, 1)

        response = self.client.post(
            f'/atomicdb/request-unexplored/{self.root.key}/')

        payload = response.json()
        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(payload['queued'], blanks)
        self.assertEqual(AnalysisTask.objects.filter(
            source=AnalysisTask.Source.USER).count(), blanks)

    def test_a_reply_that_already_has_a_value_is_left_alone(self):
        informed = Edge.objects.filter(parent=self.root).first().child
        informed.eval_cp = 30
        informed.save()

        self.client.post(f'/atomicdb/request-unexplored/{self.root.key}/')

        self.assertFalse(AnalysisTask.objects.filter(
            position=informed).exists())

    def test_a_backed_reply_counts_as_known(self):
        backed = Edge.objects.filter(parent=self.root).first().child
        backed.backed_eval = -400
        backed.save()

        self.assertNotIn(backed.key,
                         [c.key for c in
                          ingest.unexplored_children(self.root)])

    def test_the_click_is_capped(self):
        queued = ingest.enqueue_unexplored_children(self.root, cap=3)
        self.assertEqual(queued, 3)

    def test_a_second_click_queues_nothing_new(self):
        first = self.client.post(
            f'/atomicdb/request-unexplored/{self.root.key}/').json()
        second = self.client.post(
            f'/atomicdb/request-unexplored/{self.root.key}/').json()

        self.assertGreater(first['queued'], 0)
        self.assertEqual(second['queued'], 0)
        self.assertEqual(AnalysisTask.objects.count(), first['queued'])

    def test_the_daily_allowance_is_per_ip_and_per_button(self):
        """The MECHANISM, not the number.

        The allowance is policy and it moves — it was widened x100 on 28-jul —
        so a test that spends exactly ten clicks stops testing anything the
        moment the policy changes, and then fails for a reason that has
        nothing to do with what it was written to protect.  Patch the limit,
        spend it, and assert the cap fires.
        """
        with patch.object(views, 'BULK_REQUESTS_PER_IP_DAY', 3):
            for index in range(3):
                DBEvent.objects.create(kind='BULK_REQUEST',
                                       payload={'ip': '127.0.0.1',
                                                'key': f'k{index}'})

            response = self.client.post(
                f'/atomicdb/request-unexplored/{self.root.key}/')

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['status'], 'rate-limited')

    def test_a_receipt_and_an_event_are_recorded(self):
        self.client.post(f'/atomicdb/request-unexplored/{self.root.key}/')

        self.assertTrue(RequestLog.objects.filter(
            position=self.root).exists())
        event = DBEvent.objects.get(kind='BULK_REQUEST')
        self.assertEqual(event.payload['key'], self.root.key)
        self.assertGreater(event.payload['queued'], 0)

    def test_a_solved_position_has_nothing_to_offer(self):
        Position.objects.filter(key=self.root.key).update(
            status='WHITE_WIN', closure='MINIMAX')

        response = self.client.post(
            f'/atomicdb/request-unexplored/{self.root.key}/')

        self.assertEqual(response.json()['status'], 'already-solved')

    def test_a_fully_explored_position_says_so(self):
        for edge in Edge.objects.filter(parent=self.root):
            child = edge.child
            child.eval_cp = 10
            child.save()

        response = self.client.post(
            f'/atomicdb/request-unexplored/{self.root.key}/')

        self.assertEqual(response.json()['status'], 'nothing-to-do')

    def test_get_is_refused(self):
        response = self.client.get(
            f'/atomicdb/request-unexplored/{self.root.key}/')
        self.assertEqual(response.status_code, 405)

    def test_an_unknown_position_is_a_404(self):
        response = self.client.post(
            f'/atomicdb/request-unexplored/{"f" * 64}/')
        self.assertEqual(response.status_code, 404)

    def test_the_button_is_absent_once_nothing_is_unexplored(self):
        for edge in Edge.objects.filter(parent=self.root):
            child = edge.child
            child.eval_cp = 10
            child.save()

        body = self.client.get(
            f'/atomicdb/explore/{self.root.key}/').content.decode()

        # The handler script always mentions the id; what must be gone is the
        # button itself.
        self.assertNotIn('unexplored replies', body)

    def test_the_button_shows_the_count_it_would_queue(self):
        body = self.client.get(
            f'/atomicdb/explore/{self.root.key}/').content.decode()
        self.assertIn('Analyse all 20 unexplored replies', body)
