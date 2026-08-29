"""Handing the uncertified-mate debt to the fleet (round 3).

28,747 closures are legal mate witnesses whose exhaustive certificate was
never produced: the online search ran out of budget.  They live in the exact
cascade as if they were certain, and with the fleet closing mates faster than
a nightly cron can absorb them the debt grows.  A df-pn at 2M nodes certifies
a typical one in seconds, so the debt goes to the fleet — behind everything
else, because it is important and never urgent.
"""

from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile

from . import ingest, logic, solve
from .models import DBEvent, Edge, Position, SolveTask
from .test_solve import MATE_IN_ONE_CERT, MATE_IN_ONE_FEN
from .testing import TestCase, worker_account


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

    def test_the_cap_is_counted_over_THIS_ARM_and_no_other(self):
        """Un cupo compartido no priorizaba a nadie: apagaba brazos.

        Contando la cola SOLVE entera, las cincuenta filas del brazo fragil se
        restaban de las quinientas de la deuda.  Medido el 20-ago: 550 PENDING
        contra un cap de 500, los dos brazos en ``room=0`` desde julio.  Que
        una peticion de visitante no quede enterrada lo defiende el ORDEN DE
        SERVICIO y no el cupo (§ ``DebtIsServedLastTests``), asi que separar
        los cupos no le quita el turno a nadie.
        """
        pos = ingest.get_or_create_position(logic.start_fen())
        SolveTask.objects.create(position=pos, goal='WHITE_WIN',
                                 budget_nodes=100_000_000,
                                 arm=ingest.FRAGILE_ARM)
        _engine_debt()

        self.assertEqual(ingest.enqueue_engine_debt(cap=1), 1)

    def test_the_arms_own_pending_rows_still_hold_it_back(self):
        _engine_debt()
        self.assertEqual(ingest.enqueue_engine_debt(cap=1), 1)
        _engine_debt('7k/6p1/8/8/8/1P6/8/Q3K3 w - - 0 1')
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
        worker_account('solver', 'pw')

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
        worker_account('solver', 'pw')
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


class RecycleStaleSolveLeasesTests(TestCase):
    """Un arriendo SOLVE caduca porque pasa el tiempo, no porque llegue nadie.

    El barrido vivia SOLO dentro de ``views.api_solve_acquire``.  Con la flota
    sin un solo worker en modo ``--solve`` — cero peticiones a
    ``/api/solve/acquire`` en toda la ventana de logs retenida — el unico
    reloj que podia caducar un arriendo no corria: el 20-ago quedaba una fila
    LEASED desde el 29-jul a las 16:02, veintidos dias dentro de una ventana
    de veinte minutos.
    """

    def _leased(self, minutes_ago):
        from django.utils import timezone as tz
        from datetime import timedelta as td_

        pos = ingest.get_or_create_position(logic.start_fen())
        when = tz.now() - td_(minutes=minutes_ago)
        return SolveTask.objects.create(
            position=pos, goal='WHITE_WIN', budget_nodes=2_000_000,
            arm=ingest.DEBT_ARM, state='LEASED', machine='ghost',
            leased_at=when, lease_heartbeat_at=when,
            lease_token='t', lease_session='s')

    def test_a_lease_with_a_dead_heartbeat_goes_back_to_the_queue(self):
        task = self._leased(ingest.SOLVE_LEASE_MINUTES + 5)

        self.assertEqual(ingest.recycle_stale_solve_leases(), 1)

        task.refresh_from_db()
        self.assertEqual(task.state, 'PENDING')
        self.assertEqual(task.machine, '')
        self.assertEqual(task.lease_token, '')
        self.assertEqual(task.lease_session, '')
        self.assertIsNone(task.lease_heartbeat_at)

    def test_a_live_lease_is_left_alone(self):
        task = self._leased(1)

        self.assertEqual(ingest.recycle_stale_solve_leases(), 0)

        task.refresh_from_db()
        self.assertEqual(task.state, 'LEASED')

    def test_a_clean_pass_writes_no_event(self):
        self._leased(1)

        ingest.recycle_stale_solve_leases()

        self.assertFalse(DBEvent.objects.filter(
            kind='SOLVE_LEASE_RECYCLED').exists())

    def test_recycling_says_so_in_the_diary(self):
        self._leased(ingest.SOLVE_LEASE_MINUTES + 5)

        ingest.recycle_stale_solve_leases()

        event = DBEvent.objects.get(kind='SOLVE_LEASE_RECYCLED')
        self.assertEqual(event.payload['freed'], 1)

    def test_the_recycled_row_is_servable_again(self):
        """Y vuelve a contar para el cupo de SU brazo, no del de al lado."""
        self._leased(ingest.SOLVE_LEASE_MINUTES + 5)

        ingest.recycle_stale_solve_leases()

        self.assertEqual(SolveTask.objects.filter(
            state='PENDING', arm=ingest.DEBT_ARM).count(), 1)


class Autosolve95Tests(TestCase):
    """The +-95 arm closes only measured, live candidates and obeys its cap."""

    def _position(self, suffix, **fields):
        defaults = {
            'key': f'{suffix:064d}',
            'fen': logic.start_fen(),
            'status': 'UNKNOWN',
            'expanded': False,
        }
        defaults.update(fields)
        return Position.objects.create(**defaults)

    def test_both_signs_get_the_corresponding_goal_and_fixed_f0_budget(self):
        white = self._position(100, eval_cp=ingest.AUTOSOLVE95_THRESHOLD)
        black = self._position(
            101, backed_eval=-(ingest.AUTOSOLVE95_THRESHOLD + 1))

        self.assertEqual(ingest.enqueue_autosolve95(cap=8), 2)

        tasks = {task.position_id: task for task in SolveTask.objects.all()}
        self.assertEqual(tasks[white.key].goal, 'WHITE_WIN')
        self.assertEqual(tasks[black.key].goal, 'BLACK_WIN')
        self.assertEqual({task.arm for task in tasks.values()},
                         {ingest.AUTOSOLVE95_ARM})
        self.assertEqual({task.budget_nodes for task in tasks.values()},
                         {ingest.AUTOSOLVE95_STAGE_NODES})

    def test_closed_weak_and_already_scheduled_positions_are_ignored(self):
        self._position(110, eval_cp=ingest.AUTOSOLVE95_THRESHOLD - 1)
        self._position(111, eval_cp=ingest.AUTOSOLVE95_THRESHOLD,
                       status='WHITE_WIN')
        taken = self._position(112, eval_cp=ingest.AUTOSOLVE95_THRESHOLD)
        SolveTask.objects.create(position=taken, goal='WHITE_WIN',
                                 budget_nodes=2_000_000, arm='visitor')

        self.assertEqual(ingest.enqueue_autosolve95(cap=8), 0)
        self.assertEqual(SolveTask.objects.count(), 1)

    def test_the_cap_counts_only_this_arm_but_no_position_is_duplicated(self):
        occupied = self._position(120)
        existing = self._position(121)
        candidate = self._position(
            122, eval_cp=ingest.AUTOSOLVE95_THRESHOLD + 1)
        SolveTask.objects.create(position=occupied, goal='WHITE_WIN',
                                 budget_nodes=2_000_000, arm='visitor')
        SolveTask.objects.create(position=existing, goal='WHITE_WIN',
                                 budget_nodes=ingest.AUTOSOLVE95_STAGE_NODES,
                                 arm=ingest.AUTOSOLVE95_ARM)

        self.assertEqual(ingest.enqueue_autosolve95(cap=2), 1)
        self.assertTrue(SolveTask.objects.filter(
            position=candidate, arm=ingest.AUTOSOLVE95_ARM).exists())
        self.assertEqual(SolveTask.objects.filter(position=occupied).count(), 1)

    def test_a_nonempty_pass_writes_one_bounded_receipt(self):
        self._position(130, eval_cp=ingest.AUTOSOLVE95_THRESHOLD)

        ingest.enqueue_autosolve95(cap=3)

        event = DBEvent.objects.get(kind='AUTOSOLVE95_ENQUEUED')
        self.assertEqual(event.payload,
                         {'created': 1, 'pending_before': 0, 'cap': 3})
