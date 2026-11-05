"""Request + verify PV (seaside_cliff, 4-Nov-2026): a request can ask for the
line its search finds to be verified as soon as the search lands, as if the
Verify PV button were clicked then."""

from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from . import ingest, ingest_queue, logic
from .models import AnalysisTask, IngestJob
from .testing import TestCase


def _long_line(fen, plies=6):
    """A legal line of ``plies`` moves from ``fen``: the first move each time."""
    line = []
    for _ in range(plies):
        moves = logic.legal_moves(fen)
        if not moves:
            break
        line.append(moves[0])
        fen = logic.apply_move(fen, moves[0])
    return line


def _serve(task):
    """Land a search on ``task`` whose best line is six plies long."""
    pv = _long_line(task.position.fen)
    lines = [{'move': pv[0], 'eval_cp': 30, 'pv': pv}]
    job, _created = ingest_queue.enqueue(task, {
        'lines': lines, 'nodes': 1_000, 'elapsed': 1.0,
        'machine': 'm1', 'username': 'w'})
    AnalysisTask.objects.filter(pk=task.pk).update(
        state=AnalysisTask.TState.COMPLETED, completed=timezone.now())
    ingest_queue.process_job(job)
    return IngestJob.objects.get(pk=job.pk)


class ThenVerifyIngestTests(TestCase):

    def setUp(self):
        User.objects.create_user(username='lesha', password='pw')
        self.root = ingest.get_or_create_position(logic.start_fen())

    def _task(self, then_verify):
        return AnalysisTask.objects.create(
            position=self.root, generation=0,
            budget_nodes=ingest.REQUEST_BUDGET_LADDER[0],
            source=AnalysisTask.Source.USER, requested_by='lesha',
            then_verify=then_verify)

    def _waiting_below(self):
        return AnalysisTask.objects.filter(
            state=AnalysisTask.TState.PENDING,
            source=AnalysisTask.Source.USER,
            requested_by='lesha').exclude(position=self.root)

    def test_a_flagged_request_verifies_its_line_when_it_lands(self):
        job = _serve(self._task(then_verify=True))

        self.assertTrue(self._waiting_below().exists())
        self.assertGreater(job.summary.get('then_verify', 0), 0)

    def test_an_unflagged_request_buys_nothing_more(self):
        job = _serve(self._task(then_verify=False))

        self.assertFalse(self._waiting_below().exists())
        self.assertNotIn('then_verify', job.summary)


class ThenVerifyRequestTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.url = f'/atomicdb/request/{self.root.key}/'

    def test_the_box_marks_the_live_request(self):
        body = Client().post(self.url, {'then_verify': '1'}).json()

        self.assertEqual(body['status'], 'queued')
        self.assertIs(body.get('then_verify'), True)
        self.assertTrue(AnalysisTask.objects.get(
            position=self.root, state=AnalysisTask.TState.PENDING).then_verify)

    def test_without_the_box_nothing_is_marked(self):
        body = Client().post(self.url).json()

        self.assertNotIn('then_verify', body)
        self.assertFalse(AnalysisTask.objects.get(
            position=self.root, state=AnalysisTask.TState.PENDING).then_verify)

    def test_the_page_offers_the_box(self):
        page = Client().get(f'/atomicdb/explore/{self.root.key}/').content.decode()
        self.assertRegex(page, r'<input[^>]*id="thenverify"')
        self.assertIn('then verify its line', page)
