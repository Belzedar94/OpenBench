"""``--max-budget``: one worker takes only tasks up to a node budget.

A contributor's laptop spends twenty minutes on a 512M search while his other
machines take anything.  The worker sends ``max_budget`` with each lease and
the server never hands that worker a task above it.  What these tests hold
the server to:

- a capped lease never returns a task above the cap, and takes the next
  task that fits, in queue order (a lower rung of the ladder included);
- the larger tasks are not lost or delayed for anyone else: they stay
  PENDING, in their place, and an uncapped worker takes them next;
- a request without the field leases exactly as before;
- a field that is present but unusable is refused, and nothing is leased;
- proofs (``api/solve/acquire``) follow the same cap.

And the worker's side: how ``--max-budget`` reads its value.
"""

import argparse
import importlib.util
import pathlib
import subprocess
import sys
from unittest import mock

from django.test import Client

from . import ingest, logic, sweep, views
from .models import AnalysisTask, Edge, SolveTask
from .testing import TestCase, worker_account

_SPEC = importlib.util.spec_from_file_location(
    'atomicdb_worker_max_budget',
    pathlib.Path(__file__).resolve().parent.parent / 'Client'
    / 'atomicdb_worker.py')
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)

M = 1_000_000


class _QueueCase(TestCase):

    def setUp(self):
        worker_account('w', 'p')
        worker_account('other', 'p')
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.p = [edge.child for edge in
                  Edge.objects.filter(parent=self.root).order_by('move_uci')]
        self.slot = 0

    def task(self, position, budget, **fields):
        values = dict(generation=0, budget_nodes=budget,
                      source=AnalysisTask.Source.USER, requested_by='someone')
        values.update(fields)
        return AnalysisTask.objects.create(position=position, **values)

    def lease(self, max_budget=None, username='w', machine='laptop-atomicdb'):
        # Each lease from its own slot, so a held lease is not replayed.
        self.slot += 1
        name = f'{machine}#{self.slot}'
        data = {'username': username, 'password': 'p', 'machine': name,
                'worker_build': '2026111704', 'lease_session': name}
        if max_budget is not None:
            data['max_budget'] = str(max_budget)
        return self.client.post('/atomicdb/api/lease', data)

    def leased_ids(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return [task['id'] for task in response.json()['tasks']]


class CappedLeaseTests(_QueueCase):
    """The queue as each test builds it: the selector's refill, which a
    worker capped at 128M or more still triggers (§ ``RefillTests``), would
    add tasks of its own."""

    def setUp(self):
        super().setUp()
        refill = mock.patch.object(ingest, 'next_tasks', return_value=[])
        refill.start()
        self.addCleanup(refill.stop)

    def test_a_capped_lease_skips_the_larger_task_ahead(self):
        self.task(self.p[0], 512 * M)
        fits = self.task(self.p[1], 128 * M)

        self.assertEqual(self.leased_ids(self.lease(128 * M)), [fits.id])

    def test_the_cap_is_inclusive(self):
        exact = self.task(self.p[0], 512 * M)

        self.assertEqual(self.leased_ids(self.lease(512 * M)), [exact.id])

    def test_nothing_above_the_cap_is_ever_returned(self):
        budgets = [2_000 * M, 512 * M, 128 * M, 32 * M, 8 * M, 512 * M]
        for position, budget in zip(self.p, budgets):
            self.task(position, budget)

        got = []
        for _ in budgets:
            got += self.leased_ids(self.lease(128 * M))
        served = AnalysisTask.objects.filter(id__in=got)

        self.assertEqual(sorted(t.budget_nodes for t in served),
                         [8 * M, 32 * M, 128 * M])
        self.assertEqual(
            AnalysisTask.objects.filter(state='PENDING').count(), 3)
        self.assertFalse(AnalysisTask.objects.filter(
            state='LEASED', budget_nodes__gt=128 * M).exists())

    def test_with_only_larger_tasks_the_capped_worker_gets_none(self):
        big = self.task(self.p[0], 512 * M)

        self.assertEqual(self.leased_ids(self.lease(128 * M)), [])
        big.refresh_from_db()
        self.assertEqual((big.state, big.machine, big.attempts),
                         ('PENDING', '', 0))

    def test_the_larger_task_keeps_its_place_for_the_others(self):
        big = self.task(self.p[0], 512 * M)
        small = self.task(self.p[1], 128 * M)
        later = self.task(self.p[2], 128 * M)

        self.assertEqual(self.leased_ids(self.lease(128 * M)), [small.id])
        self.assertEqual(
            self.leased_ids(self.lease(username='other',
                                       machine='desktop-atomicdb')),
            [big.id])
        self.assertEqual(self.leased_ids(self.lease(128 * M)), [later.id])

    def test_a_lower_rung_of_the_ladder_is_still_served(self):
        # The next visit of a position is a new generation one rung up; a
        # capped worker keeps taking the rungs it can.
        position = self.p[0]
        position.visits = 1
        position.save(update_fields=['visits'])
        self.task(self.p[1], ingest.BUDGET_LADDER[3])
        rung = self.task(position, ingest.BUDGET_LADDER[1], generation=1,
                         source=AnalysisTask.Source.AUTO, requested_by='')

        self.assertEqual(self.leased_ids(self.lease(ingest.BUDGET_LADDER[2])),
                         [rung.id])

    def test_a_multi_line_task_is_judged_by_its_whole_budget(self):
        # MultiPV lines share one search of budget_nodes nodes.
        lines = self.task(self.p[0], 128 * M, multipv=5)

        self.assertEqual(self.leased_ids(self.lease(128 * M)), [lines.id])

    def test_a_budget_raised_after_the_ordering_is_skipped_under_the_lock(self):
        raised = self.task(self.p[0], 128 * M)
        fits = self.task(self.p[1], 128 * M)
        ordered = AnalysisTask.objects.filter(
            state='PENDING').order_by('id')
        original = views.AnalysisTask.objects.select_for_update

        def raise_first(*args, **kwargs):
            # Between step one (the id list) and step two (the locks).
            AnalysisTask.objects.filter(id=raised.id).update(
                budget_nodes=512 * M)
            return original(*args, **kwargs)

        with mock.patch.object(views.AnalysisTask.objects,
                               'select_for_update', side_effect=raise_first):
            got = [t.id for t in views._locked_in_order(ordered, 128 * M)]

        self.assertEqual(got, [fits.id])

    def test_the_id_list_already_leaves_the_larger_tasks_out(self):
        big = self.task(self.p[0], 512 * M)
        fits = self.task(self.p[1], 32 * M)
        ordered = AnalysisTask.objects.filter(
            state='PENDING').order_by('id')

        # Step two only locks the ids step one returns, so the larger task
        # is never locked and no other worker skips it meanwhile.
        self.assertEqual(views._lease_order(ordered, 128 * M), [fits.id])
        self.assertEqual(views._lease_order(ordered), [big.id, fits.id])


class RefillTests(_QueueCase):

    def test_a_worker_capped_below_server_picks_does_not_refill(self):
        with mock.patch.object(ingest, 'next_tasks',
                               return_value=[]) as refill:
            self.leased_ids(self.lease(ingest.SERVER_PICKED_MIN_NODES - 1))
        refill.assert_not_called()

    def test_a_worker_capped_at_server_picks_refills_as_before(self):
        with mock.patch.object(ingest, 'next_tasks',
                               return_value=[]) as refill:
            self.leased_ids(self.lease(ingest.SERVER_PICKED_MIN_NODES))
        refill.assert_called_once_with(views.TASK_REFILL_COUNT)

    def test_an_uncapped_worker_refills_as_before(self):
        with mock.patch.object(ingest, 'next_tasks',
                               return_value=[]) as refill:
            self.leased_ids(self.lease())
        refill.assert_called_once_with(views.TASK_REFILL_COUNT)


class SweepShareTests(_QueueCase):

    def test_the_sweep_share_respects_the_cap_and_falls_through(self):
        big = self.task(self.p[0], 512 * M)
        fits = self.task(self.p[1], 128 * M)
        sweep_rows = AnalysisTask.objects.filter(id=big.id)

        with mock.patch.object(sweep, 'take_share', return_value=True), \
                mock.patch.object(sweep, 'waiting_tasks',
                                  return_value=sweep_rows), \
                mock.patch.object(sweep, 'note_waiting'):
            self.assertEqual(self.leased_ids(self.lease(128 * M)), [fits.id])


class UncappedLeaseTests(_QueueCase):

    def test_without_the_field_the_first_task_goes_as_before(self):
        big = self.task(self.p[0], 2_000 * M)
        self.task(self.p[1], 8 * M)

        self.assertEqual(self.leased_ids(self.lease()), [big.id])

    def test_without_the_field_the_id_query_is_the_historic_one(self):
        self.task(self.p[0], 128 * M)
        ordered = AnalysisTask.objects.filter(
            state='PENDING').order_by('id')
        calls = []
        original = type(ordered).values_list

        def spy(query, *fields, **kwargs):
            calls.append((fields, kwargs))
            return original(query, *fields, **kwargs)

        with mock.patch.object(type(ordered), 'values_list', spy):
            list(views._locked_in_order(ordered))

        self.assertEqual(calls, [(('id',), {'flat': True})])


class BadValueTests(_QueueCase):

    def test_an_unusable_cap_is_refused_and_nothing_is_leased(self):
        task = self.task(self.p[0], 8 * M)
        for bad in ('512M', 'abc', '0', '-5', '1.5'):
            response = self.lease(bad)
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn('max_budget', response.json()['error'])
        task.refresh_from_db()
        self.assertEqual(task.state, 'PENDING')

    def test_an_empty_field_reads_as_no_cap(self):
        big = self.task(self.p[0], 2_000 * M)

        self.assertEqual(self.leased_ids(self.lease('')), [big.id])


class SolveCapTests(_QueueCase):

    def solve_task(self, budget):
        return SolveTask.objects.create(
            position=self.p[SolveTask.objects.count()], goal='WHITE_WIN',
            budget_nodes=budget)

    def acquire(self, max_budget=None):
        self.slot += 1
        name = f'solver-atomicdb#{self.slot}'
        data = {'username': 'w', 'password': 'p', 'machine': name,
                'worker_build': '2026111704', 'lease_session': name}
        if max_budget is not None:
            data['max_budget'] = str(max_budget)
        response = self.client.post('/atomicdb/api/solve/acquire', data)
        self.assertEqual(response.status_code, 200, response.content)
        return [task['id'] for task in response.json()['tasks']]

    def test_a_capped_solver_takes_the_largest_proof_that_fits(self):
        self.solve_task(4_000 * M)
        fits = self.solve_task(64 * M)
        self.solve_task(2 * M)

        self.assertEqual(self.acquire(128 * M), [fits.id])

    def test_with_no_proof_that_fits_the_solver_gets_none(self):
        big = self.solve_task(4_000 * M)

        self.assertEqual(self.acquire(128 * M), [])
        big.refresh_from_db()
        self.assertEqual(big.state, 'PENDING')

    def test_an_uncapped_solver_takes_the_largest_as_before(self):
        big = self.solve_task(4_000 * M)
        self.solve_task(64 * M)

        self.assertEqual(self.acquire(), [big.id])

    def test_an_unusable_cap_is_refused(self):
        response = self.client.post('/atomicdb/api/solve/acquire', {
            'username': 'w', 'password': 'p', 'machine': 'solver-atomicdb',
            'max_budget': '2B'})
        self.assertEqual(response.status_code, 400)


class WorkerFlagTests(TestCase):

    def test_suffixes_and_plain_numbers(self):
        cases = {'128M': 128 * M, '512m': 512 * M, '2B': 2_000 * M,
                 '2G': 2_000 * M, '1.5B': 1_500 * M, '64k': None,
                 '500000000': 500 * M, '512_000_000': 512 * M,
                 '1,000,000': M, '1T': 10 ** 12, ' 32M ': 32 * M}
        for text, nodes in cases.items():
            if nodes is None:
                with self.assertRaises(argparse.ArgumentTypeError, msg=text):
                    worker.parse_node_count(text)
            else:
                self.assertEqual(worker.parse_node_count(text), nodes, text)

    def test_what_is_not_a_node_count_is_refused(self):
        for text in ('', 'M', '12X', '1.5', '-128M', '128 M', '1e9', 'lots'):
            with self.assertRaises(argparse.ArgumentTypeError, msg=text):
                worker.parse_node_count(text)

    def test_the_note_warns_below_the_common_size_only(self):
        self.assertIn('128M', worker.max_budget_note(128 * M))
        self.assertNotIn('wait', worker.max_budget_note(128 * M))
        self.assertIn('wait', worker.max_budget_note(32 * M))

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(pathlib.Path(_SPEC.origin)), *args],
            capture_output=True, text=True, timeout=60)

    def test_the_flag_is_in_the_help(self):
        result = self._run('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--max-budget', result.stdout)

    def test_a_bad_value_stops_the_worker_before_it_starts(self):
        result = self._run('-U', 'u', '-P', 'p', '--max-budget', '12X')
        self.assertEqual(result.returncode, 2)
        self.assertIn('--max-budget', result.stderr)
