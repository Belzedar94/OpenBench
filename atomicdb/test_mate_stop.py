"""A search that stopped at a mate counts as the budget it was bought at.

Worker build 2026111101 searches with ``go nodes N mate 250``: the engine
stops at the first completed iteration whose best line is a mate, typically a
few thousand nodes into a 128M purchase.  The server keeps crediting the
nodes it really ran, but for deciding the showcase (and absorbing the
requests the pass answered) it reads the purchase: otherwise an older,
mate-blind pass of more nodes would keep the showcase and the mate would only
live in the pass history.
"""

import json
from unittest import mock

from django.test import Client

from . import ingest, ingest_queue, logic, views
from .models import AnalysisTask, Edge, Position
from .testing import TestCase, worker_account


def _lines(position, value=-320, mate=None):
    edges = list(Edge.objects.filter(parent=position).order_by('move_uci')[:3])
    lines = [{'move': edge.move_uci, 'eval_cp': value, 'mate': None,
              'pv': [edge.move_uci]} for edge in edges]
    if mate is not None:
        lines[0].update(eval_cp=(10_000 - abs(mate)) * (1 if mate > 0
                                                         else -1),
                        mate=mate)
    return lines


class ArbiterBudgetTests(TestCase):

    def test_a_mate_stop_arbitrates_with_its_budget(self):
        lines = [{'move': 'e2e4', 'mate': 3, 'eval_cp': 9997}]
        self.assertEqual(
            ingest_queue.arbiter_budget(lines, 2_432, 128_000_000),
            128_000_000)

    def test_a_mated_side_counts_too(self):
        lines = [{'move': 'e2e4', 'mate': -4, 'eval_cp': -9996}]
        self.assertEqual(
            ingest_queue.arbiter_budget(lines, 5_000, 128_000_000),
            128_000_000)

    def test_a_pass_without_a_mate_arbitrates_with_its_nodes(self):
        lines = [{'move': 'e2e4', 'mate': None, 'eval_cp': 40}]
        self.assertEqual(
            ingest_queue.arbiter_budget(lines, 2_432, 128_000_000), 2_432)

    def test_a_mate_in_a_later_line_is_not_a_mate_stop(self):
        lines = [{'move': 'e2e4', 'mate': None, 'eval_cp': 40},
                 {'move': 'd2d4', 'mate': -2, 'eval_cp': -9998}]
        self.assertEqual(
            ingest_queue.arbiter_budget(lines, 2_432, 128_000_000), 2_432)

    def test_a_full_search_is_unchanged(self):
        lines = [{'move': 'e2e4', 'mate': 3, 'eval_cp': 9997}]
        self.assertEqual(
            ingest_queue.arbiter_budget(lines, 130_000_000, 128_000_000),
            130_000_000)
        self.assertEqual(ingest_queue.arbiter_budget([], 7, 128_000_000), 7)
        self.assertEqual(ingest_queue.arbiter_budget(lines, 7, None), 7)


class ShowcaseTests(TestCase):

    def setUp(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        self.target = (Edge.objects.filter(parent=root)
                       .order_by('move_uci').first().child)
        ingest.expand(self.target)
        # The showcase today: a 512M pass that saw no mate.
        ingest.ingest_analysis(self.target.key, _lines(self.target),
                               512_000_000)

    def showcase(self):
        pos = Position.objects.get(key=self.target.key)
        return [line for line in pos.last_analysis
                if not line.get('prior_pass')], pos

    def test_a_mate_stop_takes_the_showcase(self):
        lines = _lines(self.target, mate=-5)
        ingest.ingest_analysis(self.target.key, lines, 2_432,
                               arbiter_budget=512_000_000)

        showcase, pos = self.showcase()
        self.assertEqual(showcase[0]['mate'], -5)
        self.assertEqual(showcase[0]['_budget'], 512_000_000)
        # The nodes that were really run, and nothing more.
        self.assertEqual(pos.nodes_invested, 512_000_000 + 2_432)
        self.assertEqual(pos.analysis_passes[0]['budget'], 2_432)

    def test_without_the_budget_it_would_have_lost_to_the_older_pass(self):
        lines = _lines(self.target, mate=-5)
        ingest.ingest_analysis(self.target.key, lines, 2_432)

        showcase, _ = self.showcase()
        self.assertIsNone(showcase[0]['mate'])


class SubmitTests(TestCase):
    """Through the protocol: submit, then the ingest queue."""

    def setUp(self):
        # The requester's notice walks the line to the root on a thread of
        # its own; under SQLite that thread only adds a locked-table warning.
        patcher = mock.patch.object(views, 'LINEAGE_CACHE_SECONDS', 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        worker_account('w', 'p')
        self.auth = {'username': 'w', 'password': 'p', 'machine': 'm1'}
        self.client = Client()
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        self.target = (Edge.objects.filter(parent=root)
                       .order_by('move_uci').first().child)
        ingest.expand(self.target)
        self.served = self.task(0, 128_000_000)
        self.shadowed = self.task(1, 8_000_000)

    def task(self, generation, budget):
        return AnalysisTask.objects.create(
            position=self.target, generation=generation, budget_nodes=budget,
            source=AnalysisTask.Source.USER, requested_by='w')

    def submit(self, lines, nodes):
        leased = self.client.post('/atomicdb/api/lease', dict(
            self.auth, worker_build='2026111101',
            lease_session='s-m1')).json()['tasks'][0]
        self.assertEqual(leased['id'], self.served.id)
        response = self.client.post('/atomicdb/api/submit', dict(
            self.auth, task_id=leased['id'], lines=json.dumps(lines),
            nodes=str(nodes), elapsed='0.4',
            lease_token=leased['lease_token']))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ingest_queue.drain(), {'done': 1, 'failed': 0})

    def test_a_mate_stop_is_accepted_and_credited_with_its_nodes(self):
        self.submit(_lines(self.target, mate=-3), 2_432)

        self.served.refresh_from_db()
        self.assertEqual(self.served.state, 'COMPLETED')
        self.assertEqual(self.served.nodes_searched, 2_432)
        # What it answered is answered: the 8M request has nothing to buy.
        self.shadowed.refresh_from_db()
        self.assertEqual(self.shadowed.state, 'COMPLETED')
        self.assertEqual(self.shadowed.nodes_searched, 0)

    def test_a_short_pass_without_a_mate_absorbs_only_what_it_ran(self):
        self.submit(_lines(self.target), 2_432)

        self.shadowed.refresh_from_db()
        self.assertEqual(self.shadowed.state, 'PENDING')
