"""Handing the uncertified-mate debt to the fleet (round 3).

28,747 closures are legal mate witnesses whose exhaustive certificate was
never produced: the online search ran out of budget.  They live in the exact
cascade as if they were certain, and with the fleet closing mates faster than
a nightly cron can absorb them the debt grows.  A df-pn at 2M nodes certifies
a typical one in seconds, so the debt goes to the fleet — behind everything
else, because it is important and never urgent.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile

from . import ingest, logic, solve
from .models import DBEvent, Edge, Position, SolveTask
from .test_solve import MATE_IN_ONE_CERT, MATE_IN_ONE_FEN
from .testing import TestCase


def _engine_debt(fen=MATE_IN_ONE_FEN, **fields):
    pos = ingest.get_or_create_position(fen)
    pos.status = fields.pop('status', 'WHITE_WIN')
    pos.closure = 'MATE_PV'
    pos.proof = fields.pop('proof', 'ENGINE')
    pos.won_line = 'a1g7'
    pos.mate_in = 1
    pos.clock_slack = fields.pop('clock_slack', 99)
    for name, value in fields.items():
        setattr(pos, name, value)
    pos.save()
    return pos


class EnqueueDebtTests(TestCase):

    def test_an_uncertified_mate_becomes_one_f0_task(self):
        pos = _engine_debt()

        self.assertEqual(ingest.enqueue_engine_debt(), 1)

        task = SolveTask.objects.get(position=pos)
        self.assertEqual(task.goal, 'WHITE_WIN')
        self.assertEqual(task.budget_stage, 'F0')
        self.assertEqual(task.budget_nodes, ingest.DEBT_STAGE_NODES)
        self.assertEqual(task.arm, ingest.DEBT_ARM)

    def test_the_goal_follows_the_status_not_the_campaign(self):
        pos = _engine_debt(status='BLACK_WIN')
        ingest.enqueue_engine_debt()
        self.assertEqual(SolveTask.objects.get(position=pos).goal,
                         'BLACK_WIN')

    def test_an_unclassified_witness_counts_as_debt_too(self):
        _engine_debt(proof=None)
        self.assertEqual(ingest.enqueue_engine_debt(), 1)

    def test_a_certified_mate_is_not_debt(self):
        _engine_debt(proof='ANDOR')
        self.assertEqual(ingest.enqueue_engine_debt(), 0)

    def test_a_witnessless_row_is_left_to_the_safety_net(self):
        pos = _engine_debt()
        pos.won_line = ''
        pos.save(update_fields=['won_line'])
        self.assertEqual(ingest.enqueue_engine_debt(), 0)

    def test_it_never_enqueues_the_same_position_twice(self):
        _engine_debt()
        self.assertEqual(ingest.enqueue_engine_debt(), 1)
        self.assertEqual(ingest.enqueue_engine_debt(), 0)
        self.assertEqual(SolveTask.objects.count(), 1)

    def test_the_cap_is_counted_over_the_WHOLE_pending_queue(self):
        """A pile of debt must not bury a visitor request."""
        pos = ingest.get_or_create_position(logic.start_fen())
        SolveTask.objects.create(position=pos, goal='WHITE_WIN',
                                 budget_nodes=100_000_000)
        _engine_debt()

        self.assertEqual(ingest.enqueue_engine_debt(cap=1), 0)

    def test_the_cap_tops_up_only_the_room_that_is_left(self):
        for index in range(5):
            _engine_debt(f'7k/6p1/8/8/8/{index}/8/Q3K3 w - - 0 1'
                         .replace('/0/', '/8/').replace('/1/', '/1P6/')
                         .replace('/2/', '/2P5/').replace('/3/', '/3P4/')
                         .replace('/4/', '/4P3/'))

        made = ingest.enqueue_engine_debt(cap=3)

        self.assertEqual(made, 3)
        self.assertEqual(SolveTask.objects.count(), 3)


class DebtIsServedLastTests(TestCase):

    def setUp(self):
        User.objects.create_user('solver', password='pw')

    def _acquire(self):
        return self.client.post('/atomicdb/api/solve/acquire', {
            'username': 'solver', 'password': 'pw', 'machine': 'm1',
            'lease_session': 's', 'threads': 1, 'hash': 64})

    def test_a_request_outranks_the_debt_even_though_it_is_bigger(self):
        debt_position = _engine_debt()
        ingest.enqueue_engine_debt()
        wanted = ingest.get_or_create_position(logic.start_fen())
        SolveTask.objects.create(position=wanted, goal='WHITE_WIN',
                                 budget_nodes=10_000_000, arm='mate_band')

        payload = self._acquire().json()['tasks'][0]

        self.assertEqual(payload['fen'], wanted.fen)
        self.assertNotEqual(payload['fen'], debt_position.fen)

    def test_the_debt_is_served_once_nothing_else_is_waiting(self):
        _engine_debt()
        ingest.enqueue_engine_debt()

        payload = self._acquire().json()['tasks'][0]

        self.assertEqual(payload['stage'], 'F0')


class UpgradeNotRecloseTests(TestCase):

    def _task(self, position, goal='WHITE_WIN'):
        return SolveTask.objects.create(
            position=position, goal=goal, budget_stage='F0',
            budget_nodes=ingest.DEBT_STAGE_NODES, arm=ingest.DEBT_ARM,
            state='LEASED', machine='m1')

    def test_a_certificate_upgrades_the_proof_without_reclosing(self):
        pos = _engine_debt(clock_slack=50)
        task = self._task(pos)

        summary = ingest.apply_solve_result(
            task, outcome='PROVED',
            certificate_blob=solve.compress(MATE_IN_ONE_CERT),
            searched_nodes=10, elapsed_seconds=1.0)

        pos.refresh_from_db()
        self.assertTrue(summary['upgraded'])
        self.assertFalse(summary['closed'])
        self.assertEqual(pos.proof, 'ANDOR')
        self.assertEqual(pos.status, 'WHITE_WIN')     # untouched
        self.assertEqual(pos.closure, 'MATE_PV')      # untouched
        # The measured slack beats the crude stored bound.
        self.assertEqual(pos.clock_slack, 100)
        self.assertTrue(DBEvent.objects.filter(
            kind='PROOF_UPGRADED', payload__key=pos.key).exists())

    def test_an_already_certified_row_is_not_upgraded_twice(self):
        pos = _engine_debt(proof='ANDOR')
        task = self._task(pos)

        summary = ingest.apply_solve_result(
            task, outcome='PROVED',
            certificate_blob=solve.compress(MATE_IN_ONE_CERT))

        self.assertFalse(summary['upgraded'])
        self.assertEqual(DBEvent.objects.filter(
            kind='PROOF_UPGRADED').count(), 0)

    def test_an_open_position_still_takes_the_closing_path(self):
        pos = ingest.get_or_create_position(MATE_IN_ONE_FEN)
        task = self._task(pos)

        summary = ingest.apply_solve_result(
            task, outcome='PROVED',
            certificate_blob=solve.compress(MATE_IN_ONE_CERT))

        pos.refresh_from_db()
        self.assertTrue(summary['closed'])
        self.assertFalse(summary['upgraded'])
        self.assertEqual(pos.closure, 'SOLVE')

    def test_a_certificate_for_another_goal_still_gets_rejected(self):
        pos = _engine_debt()
        task = self._task(pos, goal='BLACK_WIN')

        summary = ingest.apply_solve_result(
            task, outcome='PROVED',
            certificate_blob=solve.compress(MATE_IN_ONE_CERT))

        self.assertTrue(summary['rejected'])
        pos.refresh_from_db()
        self.assertEqual(pos.proof, 'ENGINE')


class DisputeFromSolverTests(TestCase):

    def _task(self, position):
        return SolveTask.objects.create(
            position=position, goal=position.status, budget_stage='F0',
            budget_nodes=ingest.DEBT_STAGE_NODES, arm=ingest.DEBT_ARM,
            state='LEASED', machine='m1')

    def test_an_untrusted_disproof_is_recorded_and_changes_nothing(self):
        """A DISPROVED carries NO certificate, so it is a claim, not a proof.

        Acting on it from anyone would hand a volunteer a button that deletes
        closures.
        """
        pos = _engine_debt()
        task = self._task(pos)

        summary = ingest.apply_solve_result(task, outcome='DISPROVED')

        pos.refresh_from_db()
        self.assertFalse(summary['disputed'])
        self.assertEqual(pos.status, 'WHITE_WIN')
        self.assertTrue(DBEvent.objects.filter(
            kind='SOLVE_DISPUTE_SIGNAL', payload__key=pos.key).exists())

    def test_a_trusted_disproof_revokes_the_closure(self):
        pos = _engine_debt()
        task = self._task(pos)

        summary = ingest.apply_solve_result(task, outcome='DISPROVED',
                                            trusted_submitter=True)

        pos.refresh_from_db()
        self.assertTrue(summary['disputed'])
        self.assertEqual(pos.status, 'UNKNOWN')
        self.assertEqual(pos.proof, 'DISPUTED')
        self.assertTrue(DBEvent.objects.filter(
            kind='CLOSURE_REVOKED', payload__key=pos.key).exists())

    def test_a_certified_closure_is_never_disputed_this_way(self):
        pos = _engine_debt(proof='ANDOR')
        task = self._task(pos)

        summary = ingest.apply_solve_result(task, outcome='DISPROVED',
                                            trusted_submitter=True)

        pos.refresh_from_db()
        self.assertFalse(summary['disputed'])
        self.assertEqual(pos.status, 'WHITE_WIN')

    def test_a_disproof_of_a_different_goal_is_not_a_dispute(self):
        pos = _engine_debt()
        task = SolveTask.objects.create(
            position=pos, goal='BLACK_WIN', state='LEASED', machine='m1')

        summary = ingest.apply_solve_result(task, outcome='DISPROVED',
                                            trusted_submitter=True)

        pos.refresh_from_db()
        self.assertFalse(summary['disputed'])
        self.assertEqual(pos.status, 'WHITE_WIN')


class TelemetryIsAdvisoryTests(TestCase):

    def setUp(self):
        User.objects.create_user('solver', password='pw')
        self.pos = ingest.get_or_create_position(MATE_IN_ONE_FEN)
        self.task = SolveTask.objects.create(
            position=self.pos, goal='WHITE_WIN', budget_nodes=1_000,
            state='LEASED', machine='m1')

    def test_the_fortress_indicators_are_stored(self):
        response = self.client.post('/atomicdb/api/solve/submit', {
            'username': 'solver', 'password': 'pw', 'machine': 'm1',
            'task_id': self.task.id, 'outcome': 'UNKNOWN',
            'lease_token': '', 'fortress_tt_hit': '0.71',
            'fortress_quiet_scc': '0.62', 'fortress_reset_rate': '0.03',
            'fortress_stagnation': '1.4', 'fortress_score': '4'})

        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.telemetry['tt_hit'], 0.71)
        self.assertEqual(self.task.telemetry['score'], 4.0)

    def test_garbage_telemetry_is_dropped_not_stored(self):
        self.client.post('/atomicdb/api/solve/submit', {
            'username': 'solver', 'password': 'pw', 'machine': 'm1',
            'task_id': self.task.id, 'outcome': 'UNKNOWN',
            'lease_token': '', 'fortress_tt_hit': 'not-a-number'})

        self.task.refresh_from_db()
        self.assertFalse(self.task.telemetry)

    def test_a_perfect_fortress_score_still_closes_nothing(self):
        """The classifier steers scheduling and authorises no result."""
        self.client.post('/atomicdb/api/solve/submit', {
            'username': 'solver', 'password': 'pw', 'machine': 'm1',
            'task_id': self.task.id, 'outcome': 'UNKNOWN',
            'lease_token': '', 'fortress_score': '4'})

        self.pos.refresh_from_db()
        self.assertEqual(self.pos.status, 'UNKNOWN')


class RecertifyDefersToTheFleetTests(TestCase):

    def test_a_witness_the_fleet_holds_is_left_alone(self):
        from io import StringIO

        from django.core.management import call_command

        pos = _engine_debt()
        ingest.enqueue_engine_debt()
        out = StringIO()

        call_command('recertify_mates', stdout=out)

        pos.refresh_from_db()
        self.assertEqual(pos.proof, 'ENGINE')   # untouched, fleet owns it
        self.assertIn('ANDOR=0', out.getvalue())

    def test_a_witness_nobody_holds_is_still_processed(self):
        from io import StringIO

        from django.core.management import call_command

        pos = _engine_debt(fen='7k/6p1/8/8/8/8/8/Q3K3 w - - 0 1')
        call_command('recertify_mates', stdout=StringIO())

        pos.refresh_from_db()
        self.assertIn(pos.proof, ('ANDOR', 'ENGINE'))


class PilotRoundTwoTests(TestCase):

    def test_the_goal_follows_the_sign_of_the_eval(self):
        from atomicdb.management.commands import solve_pilot as pilot

        pos = ingest.get_or_create_position(logic.start_fen())
        pos.eval_cp = -1_500
        pos.save()
        self.assertEqual(pilot.goal_for(pos), 'BLACK_WIN')

        pos.eval_cp = 1_500
        pos.save()
        self.assertEqual(pilot.goal_for(pos), 'WHITE_WIN')

    def test_an_undecided_eval_falls_back_to_the_campaign(self):
        from atomicdb.management.commands import solve_pilot as pilot

        pos = ingest.get_or_create_position(logic.start_fen())
        pos.eval_cp = 40
        pos.save()
        self.assertEqual(pilot.goal_for(pos), 'WHITE_WIN')

    def test_the_backed_value_wins_over_the_raw_eval(self):
        from atomicdb.management.commands import solve_pilot as pilot

        pos = ingest.get_or_create_position(logic.start_fen())
        pos.eval_cp = 40
        pos.backed_eval = -1_500
        pos.save()
        self.assertEqual(pilot.goal_for(pos), 'BLACK_WIN')

    def test_the_fortress_arm_gets_the_classifier_budget(self):
        from io import StringIO

        from django.core.management import call_command
        from atomicdb.management.commands import solve_pilot as pilot

        for index in range(4):
            key = f'{index:064d}'
            Position.objects.create(
                key=key, fen='8/8/8/8/8/8/1k6/K6Q w - - 0 1',
                status='UNKNOWN', expanded=True, eval_cp=1_200, visits=3,
                last_analysis=[{'move': 'h1h2', 'pv': ['h1h2', 'b2b3']}])

        call_command('solve_pilot', size=8, stdout=StringIO())

        fortress = SolveTask.objects.filter(arm='fortress')
        self.assertTrue(fortress.exists())
        for task in fortress:
            self.assertEqual(task.budget_stage, 'F0')
            self.assertEqual(task.budget_nodes, ingest.DEBT_STAGE_NODES)
            self.assertEqual(task.goal, 'WHITE_WIN')
