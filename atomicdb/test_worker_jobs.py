"""``--jobs``: one worker, several leaseholders, one honest thread budget.

WHY AT ALL.  A single atomic position does not scale to eight threads -- SMP
search on one root is heavily sublinear -- so a contributor who lends eight
cores and runs one engine at ``-T 8`` donates far less throughput than eight
engines at one thread each.  The queue is made of independent positions, which
is the easiest parallelism there is, and the worker was leaving it on the
floor.

WHY THE SLOT LIVES IN THE MACHINE NAME.  The lease protocol already enforces,
per machine: one live assignment, a fencing token, heartbeat keepalive, and
replay of a response lost after commit.  Every one of those is exactly what a
job needs.  Presenting job K as machine ``base#K`` inherits the lot without a
new protocol field and without a migration -- and, unlike a per-machine job
counter, it cannot be got wrong by a worker that restarts mid-batch, because
the identity IS the slot.

The cap is the one thing the server had to learn, and it is keyed on the
physical machine rather than the slot: a cap per slot would be sidestepped by
asking for more slots.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.utils import timezone

from . import ingest, logic, views
from .models import AnalysisTask, Position
from .testing import TestCase

import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    'atomicdb_worker_under_test',
    pathlib.Path(__file__).resolve().parent.parent / 'Client'
    / 'atomicdb_worker.py')
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)


class LeaseCsrfTests(TestCase):
    """The worker has no CSRF token and never will.

    The first cut of the --jobs patch inserted two helpers between
    ``@csrf_exempt`` and ``api_lease``, silently moving the decorator onto a
    helper and leaving the view protected.  Every test stayed green — the
    Django test client does not enforce CSRF — while production would have
    403'd the entire fleet on deploy.  This client enforces it.
    """

    def test_a_tokenless_worker_post_can_lease(self):
        from django.test import Client

        User.objects.create_user(username='w', password='p')
        pos = ingest.get_or_create_position(logic.start_fen())
        AnalysisTask.objects.create(position=pos, generation=0,
                                    budget_nodes=8_000_000,
                                    source=AnalysisTask.Source.USER)

        response = Client(enforce_csrf_checks=True).post(
            '/atomicdb/api/lease', {
                'username': 'w', 'password': 'p', 'machine': 'm-csrf',
                'worker_build': '2026072203', 'lease_session': 'm-csrf'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['tasks']), 1)


class ThreadBudgetTests(TestCase):
    """-T is a promise about the whole machine, not a per-engine setting."""

    def test_the_budget_is_split_never_multiplied(self):
        for total in (1, 2, 4, 8, 16, 24, 64):
            for jobs in (1, 2, 3, 8, 100):
                for solve in (False, True):
                    got, per_slot, solver = worker.plan_threads(total, jobs,
                                                                solve)
                    self.assertEqual(len(per_slot), got)
                    self.assertTrue(all(n >= 1 for n in per_slot))
                    self.assertLessEqual(
                        sum(per_slot) + solver, max(total, solver + 1),
                        f'-T {total} --jobs {jobs} solve={solve} would use '
                        'more than promised')

    def test_every_promised_core_is_actually_spent(self):
        """The reason the split returns a list instead of one number.

        A flat ``available // jobs`` is tidy and quietly idles up to jobs-1
        cores: -T 24 --jobs 6 --solve would run six engines of three and leave
        five of the twenty-four doing nothing, which is exactly what this flag
        exists to stop.
        """
        for total in (2, 4, 7, 8, 16, 24, 25, 64):
            for jobs in (1, 2, 3, 5, 6, 8):
                for solve in (False, True):
                    got, per_slot, solver = worker.plan_threads(total, jobs,
                                                                solve)
                    self.assertEqual(sum(per_slot), max(1, total - solver),
                                     f'-T {total} --jobs {jobs} solve={solve} '
                                     f'left cores idle: {per_slot}')
                    self.assertLessEqual(max(per_slot) - min(per_slot), 1,
                                         'the remainder must be spread evenly')

    def test_the_awkward_case_from_the_docstring(self):
        jobs, per_slot, solver = worker.plan_threads(24, 6, solve=True)
        self.assertEqual(jobs, 6)
        self.assertEqual(sum(per_slot) + solver, 24)
        self.assertEqual(sorted(per_slot, reverse=True), [4, 4, 4, 4, 4, 3])

    def test_the_solver_core_comes_off_the_top_first(self):
        jobs, per_slot, solver = worker.plan_threads(8, 4, solve=True)
        self.assertEqual(solver, 1)
        self.assertEqual(sum(per_slot) + solver, 8)

    def test_jobs_are_clamped_rather_than_starved(self):
        """A slot with less than a whole core is contention, not parallelism."""
        jobs, per_slot, _ = worker.plan_threads(4, 16, solve=False)
        self.assertEqual(jobs, 4)
        self.assertEqual(per_slot, [1, 1, 1, 1])

    def test_a_single_job_is_exactly_the_old_behaviour(self):
        self.assertEqual(worker.plan_threads(8, 1, solve=False), (1, [8], 0))
        self.assertEqual(worker.plan_threads(8, 1, solve=True), (1, [7], 1))

    def test_degenerate_input_still_yields_something_runnable(self):
        for total, jobs in ((0, 0), (-5, -5), (1, 1)):
            got, per_slot, _ = worker.plan_threads(total, jobs, solve=False)
            self.assertGreaterEqual(got, 1)
            self.assertTrue(all(n >= 1 for n in per_slot))


class MachineIdentityTests(TestCase):

    def test_a_slot_resolves_to_its_physical_machine(self):
        self.assertEqual(views.machine_base('bob-pc-atomicdb#7'),
                         'bob-pc-atomicdb')
        self.assertEqual(views.machine_base('bob-pc-atomicdb'),
                         'bob-pc-atomicdb')
        self.assertEqual(views.machine_base(''), '')
        self.assertEqual(views.machine_base(None), '')

    def test_a_lookalike_machine_is_not_the_same_machine(self):
        """``startswith`` alone would merge two different contributors."""
        self.assertNotEqual(views.machine_base('bob-pc-atomicdb2'),
                            views.machine_base('bob-pc-atomicdb#1'))


class LeaseConcurrencyTests(TestCase):

    def setUp(self):
        User.objects.create_user('u', password='p')
        self.position = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.position)

    def _lease(self, machine, session):
        return self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': machine, 'tb': '1',
            'worker_build': '2026072806', 'lease_session': session})

    def _pending(self, count):
        made = []
        for child in Position.objects.exclude(key=self.position.key)[:count]:
            made.append(AnalysisTask.objects.create(
                position=child, generation=0, budget_nodes=1_000_000,
                state=AnalysisTask.TState.PENDING))
        return made

    def test_two_slots_of_one_machine_hold_leases_at_the_same_time(self):
        """The whole point. Before --jobs the second slot got nothing."""
        self._pending(4)
        first = self._lease('box-atomicdb#0', 's0').json()['tasks']
        second = self._lease('box-atomicdb#1', 's1').json()['tasks']

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]['id'], second[0]['id'],
                            'two slots must not be handed the same task')
        live = AnalysisTask.objects.filter(state='LEASED')
        self.assertEqual(live.count(), 2)
        self.assertEqual({t.machine for t in live},
                         {'box-atomicdb#0', 'box-atomicdb#1'})

    def test_one_slot_still_gets_only_one_task_at_a_time(self):
        """Per-slot fencing survives: a slot with live work is not topped up."""
        self._pending(4)
        first = self._lease('box-atomicdb#0', 's0').json()['tasks']
        again = self._lease('box-atomicdb#0', 's-different').json()['tasks']
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [])

    def test_a_restarted_worker_reclaims_its_dead_predecessors_lease(self):
        """Relevo tras reinicio: sesion nueva + heartbeat muerto = reciclar ya.

        Incidente real (29-jul): el worker relanzado se quedo "sin tareas"
        detras del lease de su predecesor muerto hasta agotar la ventana
        entera de caducidad, con la cola llena."""
        self._pending(1)
        first = self._lease('box-atomicdb#0', 'sesion-vieja').json()['tasks']
        self.assertEqual(len(first), 1)
        dead = timezone.now() - timedelta(
            minutes=views.RESTART_RECYCLE_MINUTES + 1)
        AnalysisTask.objects.filter(id=first[0]['id']).update(
            lease_heartbeat_at=dead, leased_at=dead)

        relevo = self._lease('box-atomicdb#0', 'sesion-nueva').json()['tasks']

        self.assertEqual(len(relevo), 1)
        self.assertEqual(relevo[0]['id'], first[0]['id'])
        self.assertNotEqual(relevo[0]['lease_token'],
                            first[0]['lease_token'])
        task = AnalysisTask.objects.get(pk=first[0]['id'])
        self.assertEqual((task.state, task.machine),
                         ('LEASED', 'box-atomicdb#0'))

    def test_a_lost_response_still_replays_within_its_slot(self):
        self._pending(4)
        first = self._lease('box-atomicdb#0', 'same-nonce').json()['tasks']
        replay = self._lease('box-atomicdb#0', 'same-nonce').json()['tasks']
        self.assertEqual(len(replay), 1)
        self.assertEqual(first[0]['id'], replay[0]['id'])
        self.assertEqual(first[0]['lease_token'], replay[0]['lease_token'])
        task = AnalysisTask.objects.get(pk=first[0]['id'])
        self.assertEqual(task.attempts, 1, 'a replay must not burn an attempt')

    def test_the_shipped_cap_is_the_agreed_policy(self):
        self.assertEqual(views.LEASES_PER_MACHINE, 32)

    def test_the_cap_is_per_physical_machine(self):
        """Patched small, because the MECHANISM is what is under test.

        Spending thirty-two real analysis tasks to prove a comparison would
        make this slow and would fail the day the policy number moves, which
        is the same trap the bulk-request allowance test fell into.
        """
        self._pending(6)
        with patch.object(views, 'LEASES_PER_MACHINE', 3):
            handed = 0
            for slot in range(5):
                tasks = self._lease(f'box-atomicdb#{slot}', f's{slot}')
                handed += len(tasks.json()['tasks'])
        self.assertEqual(handed, 3, 'the cap counts the machine, not the slot')

    def test_the_cap_does_not_penalise_a_different_machine(self):
        self._pending(6)
        with patch.object(views, 'LEASES_PER_MACHINE', 2):
            for slot in range(2):
                self._lease(f'box-atomicdb#{slot}', f's{slot}')
            other = self._lease('other-box-atomicdb#0', 'o0').json()['tasks']
        self.assertEqual(len(other), 1,
                         'one machine at its cap must not starve another')

    def test_a_capped_machine_is_told_nothing_rather_than_failing(self):
        """A worker that over-asks should back off, not crash."""
        self._pending(6)
        with patch.object(views, 'LEASES_PER_MACHINE', 2):
            for slot in range(2):
                self._lease(f'box-atomicdb#{slot}', f's{slot}')
            response = self._lease('box-atomicdb#2', 'x')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tasks'], [])

    def test_a_recycled_lease_frees_a_slot_of_the_cap(self):
        """Stale work must not hold the cap hostage."""
        self._pending(6)
        with patch.object(views, 'LEASES_PER_MACHINE', 2):
            for slot in range(2):
                self._lease(f'box-atomicdb#{slot}', f's{slot}')
            stale = timezone.now() - timedelta(minutes=60)
            AnalysisTask.objects.filter(machine='box-atomicdb#0').update(
                leased_at=stale, lease_heartbeat_at=stale)
            tasks = self._lease('box-atomicdb#0', 's0-again').json()['tasks']
        self.assertEqual(len(tasks), 1,
                         'recycling happens before the cap is counted')

    def test_an_unsuffixed_machine_is_capped_with_its_slots(self):
        """A legacy worker and a --jobs worker on one box share the budget."""
        self._pending(6)
        self._lease('box-atomicdb', 'legacy').json()
        held = AnalysisTask.objects.filter(state='LEASED').count()
        self.assertEqual(held, 1)
        self.assertTrue(views._machine_at_lease_cap('box-atomicdb#4')
                        is False)
