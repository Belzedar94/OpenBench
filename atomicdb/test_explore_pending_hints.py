"""The marks of work in flight on each move row, and the (i) notes over the
moves table (cz_eclipsia, 16-Nov-2026)."""

import hashlib
import re

from django.conf import settings

from . import views
from .models import AnalysisTask, Edge, IngestJob, PendingClaim, Position
from .testing import TestCase

ROW = re.compile(r'<tr><td><span class="chip.*?</tr>', re.S)


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _task(child, state, generation=0):
    return AnalysisTask.objects.create(position=child, budget_nodes=8_000_000,
                                       generation=generation, state=state)


class PendingMarkTests(TestCase):

    def setUp(self):
        self.parent = _pos('PEND-P', 'w', expanded=True)
        self.children = {}
        for name, uci in (('A', 'e1d1'), ('B', 'e1f1'), ('C', 'e1d2'),
                          ('D', 'e1e2'), ('E', 'e1f2')):
            child = _pos(f'PEND-{name}', 'b')
            _edge(self.parent, child, uci)
            self.children[uci] = child

    def _rows(self):
        body = self.client.get(
            f'/atomicdb/explore/{self.parent.key}/').content.decode()
        return body, {uci: next(row for row in ROW.findall(body)
                                if f'data-uci="{uci}"' in row
                                or f'>{uci}<' in row)
                      for uci in self.children}

    def test_a_pending_task_marks_its_row_queued(self):
        _task(self.children['e1d1'], AnalysisTask.TState.PENDING)

        _body, rows = self._rows()

        self.assertIn('data-pending="queued"', rows['e1d1'])
        self.assertIn('>queued</span>', rows['e1d1'])
        self.assertIn('waiting for a worker', rows['e1d1'])
        for uci in ('e1f1', 'e1d2', 'e1e2', 'e1f2'):
            self.assertNotIn('pending-mark', rows[uci])

    def test_a_leased_task_marks_its_row_running(self):
        _task(self.children['e1f1'], AnalysisTask.TState.LEASED)

        _body, rows = self._rows()

        self.assertIn('data-pending="running"', rows['e1f1'])
        self.assertIn('>running</span>', rows['e1f1'])
        self.assertIn('searching this move right now', rows['e1f1'])

    def test_a_delivered_result_not_yet_written_marks_its_row_writing(self):
        task = _task(self.children['e1d2'], AnalysisTask.TState.COMPLETED)
        IngestJob.objects.create(task=task, position=self.children['e1d2'],
                                 payload={}, state=IngestJob.JState.PENDING)
        done = _task(self.children['e1e2'], AnalysisTask.TState.COMPLETED)
        IngestJob.objects.create(task=done, position=self.children['e1e2'],
                                 payload={}, state=IngestJob.JState.DONE)

        _body, rows = self._rows()

        self.assertIn('data-pending="writing"', rows['e1d2'])
        self.assertNotIn('pending-mark', rows['e1e2'])

    def test_finished_and_withdrawn_tasks_leave_no_mark(self):
        _task(self.children['e1d1'], AnalysisTask.TState.COMPLETED)
        _task(self.children['e1f1'], AnalysisTask.TState.CANCELLED)

        _body, rows = self._rows()

        self.assertNotIn('pending-mark', rows['e1d1'])
        self.assertNotIn('pending-mark', rows['e1f1'])

    def test_one_mark_per_row_the_one_closest_to_landing(self):
        child = self.children['e1d1']
        _task(child, AnalysisTask.TState.PENDING, generation=1)
        written = _task(child, AnalysisTask.TState.COMPLETED, generation=0)
        IngestJob.objects.create(task=written, position=child, payload={},
                                 state=IngestJob.JState.PROCESSING)

        _body, rows = self._rows()

        self.assertEqual(rows['e1d1'].count('pending-mark'), 1)
        self.assertIn('data-pending="writing"', rows['e1d1'])

    def test_an_open_unconfirmed_claim_is_marked(self):
        PendingClaim.objects.create(
            position_key=self.children['e1f2'].key, parent_key=self.parent.key,
            account='someone', kind=PendingClaim.Kind.MATE, winner='WHITE',
            pv='e8d8')
        PendingClaim.objects.create(
            position_key=self.children['e1e2'].key, parent_key=self.parent.key,
            account='someone', kind=PendingClaim.Kind.MATE, winner='WHITE',
            pv='e8d8', resolution=PendingClaim.Resolution.REFUTED,
            resolved='2026-11-01T00:00:00Z')

        _body, rows = self._rows()

        self.assertIn('>unconfirmed mate</span>', rows['e1f2'])
        self.assertNotIn('claim-mark', rows['e1e2'])

    def test_the_header_summary_still_counts_queued_and_running(self):
        _task(self.children['e1d1'], AnalysisTask.TState.PENDING)
        _task(self.children['e1f1'], AnalysisTask.TState.LEASED)
        written = _task(self.children['e1d2'], AnalysisTask.TState.COMPLETED)
        IngestJob.objects.create(task=written, position=self.children['e1d2'],
                                 payload={}, state=IngestJob.JState.PENDING)

        body, _rows = self._rows()

        self.assertIn('moves 0 searched · 2 queued', body)

    def test_the_marks_cost_two_statements_for_the_whole_table(self):
        for uci in self.children:
            _task(self.children[uci], AnalysisTask.TState.PENDING)
        rows = views._child_moves(self.parent)

        with self.assertNumQueries(2,
                                   using=settings.ATOMICDB_DATABASE_ALIAS):
            views._with_pending_marks(rows)

        self.assertTrue(all(row['pending_state'] == 'queued' for row in rows))


class TableHelpTests(TestCase):

    def test_the_info_toggles_sit_above_the_moves_table(self):
        parent = _pos('HELP-P', 'w', expanded=True)
        _edge(parent, _pos('HELP-C', 'b', eval_cp=9997), 'e1d1')

        body = self.client.get(
            f'/atomicdb/explore/{parent.key}/').content.decode()

        self.assertIn('class="table-help"', body)
        self.assertLess(body.index('class="table-help"'),
                        body.index('<table aria-labelledby="moves-heading">'))
        for topic in ('state', 'eval', 'colours', 'pn / dn', 'depth',
                      'pending'):
            # The page goes out minified, with the entity as its character.
            self.assertIn(f'ⓘ {topic}</summary>', body)
        # The values the eval note explains are the ones the code prints.
        self.assertIn('9997 is mate in 3 more moves', body)
        self.assertIn('capped at 9500', body)

    def test_no_toggles_without_a_table(self):
        pos = _pos('HELP-NONE', 'w')
        pos.closure = 'TERMINAL'
        pos.status = 'WHITE_WIN'
        pos.save()

        body = self.client.get(
            f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertNotIn('class="table-help"', body)
