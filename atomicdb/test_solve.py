"""SOLVE: the certificate verifier, the protocol and the pilot harness (P1c).

The whole point of proof-carrying work is that the server never has to believe
a volunteer.  These tests are mostly about the ways a certificate can lie, and
the one shape of certificate that is real: the fixtures below came out of the
engine's own `solve` command, byte for byte.
"""

from io import StringIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from . import ingest, logic, solve
from .models import (DBEvent, Position, ProofCampaign, SolveTask)
from .testing import TestCase, worker_account

# Straight out of `solve 7k/6p1/8/8/8/8/8/Q3K3 w - - 0 1`: Qa1xg7 explodes
# g7 and the king on h8 with it.
MATE_IN_ONE_FEN = '7k/6p1/8/8/8/8/8/Q3K3 w - - 0 1'
MATE_IN_ONE_CERT = """# atomicdb-proof/1
ruleset atomic-fide-claim-v1
goal WHITE_WIN
root 7k/6p1/8/8/8/8/8/Q3K3 w - - 0 1
nodes 2
---
O a1g7
T explosion
"""

# `solve r6k/6pp/8/8/8/8/6PP/3QK2R w K - 0 1`: 40 nodes, real AND branching.
DEEP_FEN = 'r6k/6pp/8/8/8/8/6PP/3QK2R w K - 0 1'
DEEP_CERT = """# atomicdb-proof/1
ruleset atomic-fide-claim-v1
goal WHITE_WIN
root r6k/6pp/8/8/8/8/6PP/3QK2R w K - 0 1
nodes 40
---
O d1d8
A 1 a8d8
O h1f1
A 5 g7g6 h7h6 g7g5 h7h5 h8g8
O f1f8
A 1 h8g7
O f8f7
A 3 g7h8 g7g8 g7h6
O f7h7
T explosion
O f7h7
T explosion
O f7h7
T explosion
O f1f8
A 1 h8h7
O f8h8
A 1 h7g6
O h8h6
T explosion
O f1f8
A 1 h8g7
O f8f7
A 4 g7h8 g7g8 g7h6 g7g6
O f7h7
T explosion
O f7h7
T explosion
O f7h7
T explosion
O f7h7
T explosion
O f1f8
A 1 h8h7
O f8h8
A 1 h7g6
O h8h5
T explosion
O f1f8
T mate
"""


class CertificateVerifierTests(TestCase):

    def test_a_real_certificate_verifies(self):
        report = solve.verify_certificate(
            MATE_IN_ONE_CERT, root_fen=MATE_IN_ONE_FEN, goal='WHITE_WIN')
        self.assertEqual(report['nodes'], 2)
        self.assertEqual(report['goal'], 'WHITE_WIN')
        # Qa1xg7 is a CAPTURE, so it zeroes the fifty-move counter: the
        # proof contains no reversible ply at all and holds for every entry
        # clock.  That is the atomic case in miniature — captures everywhere,
        # so the clock rarely binds.
        self.assertEqual(report['worst_reversible_run'], 0)
        self.assertEqual(report['clock_slack'], 100)

    def test_a_deep_certificate_with_real_and_branching_verifies(self):
        report = solve.verify_certificate(
            DEEP_CERT, root_fen=DEEP_FEN, goal='WHITE_WIN')
        self.assertEqual(report['nodes'], 40)
        self.assertGreater(report['depth'], 4)

    def test_a_certificate_for_another_position_is_rejected(self):
        with self.assertRaisesMessage(solve.CertificateError,
                                      'different position'):
            solve.verify_certificate(MATE_IN_ONE_CERT, root_fen=DEEP_FEN)

    def test_a_certificate_for_another_goal_is_rejected(self):
        with self.assertRaisesMessage(solve.CertificateError,
                                      'different goal'):
            solve.verify_certificate(MATE_IN_ONE_CERT, goal='BLACK_WIN')

    def test_a_foreign_ruleset_is_rejected(self):
        text = MATE_IN_ONE_CERT.replace(logic.RULESET_ID, 'some-other-rules')
        with self.assertRaisesMessage(solve.CertificateError, 'ruleset'):
            solve.verify_certificate(text)

    def test_an_illegal_witness_move_is_rejected(self):
        # a1b3 is a knight's leap: no queen move, no legal move.
        text = MATE_IN_ONE_CERT.replace('O a1g7', 'O a1b3')
        with self.assertRaisesMessage(solve.CertificateError, 'illegal'):
            solve.verify_certificate(text)

    def test_an_incomplete_and_node_is_rejected(self):
        """The single most important check: coverage must be EXACT."""
        text = DEEP_CERT.replace('A 5 g7g6 h7h6 g7g5 h7h5 h8g8',
                                 'A 4 g7g6 h7h6 g7g5 h7h5')
        with self.assertRaisesMessage(solve.CertificateError,
                                      'does not cover exactly'):
            solve.verify_certificate(text, root_fen=DEEP_FEN)

    def test_an_and_node_with_an_extra_move_is_rejected(self):
        text = DEEP_CERT.replace('A 1 a8d8', 'A 2 a8d8 a8a1')
        with self.assertRaisesMessage(solve.CertificateError, 'exactly'):
            solve.verify_certificate(text, root_fen=DEEP_FEN)

    def test_a_duplicated_reply_is_rejected(self):
        text = DEEP_CERT.replace('A 1 a8d8', 'A 2 a8d8 a8d8')
        with self.assertRaisesMessage(solve.CertificateError, 'repeats'):
            solve.verify_certificate(text, root_fen=DEEP_FEN)

    def test_a_non_terminal_leaf_is_rejected(self):
        text = MATE_IN_ONE_CERT.replace('O a1g7\nT explosion',
                                        'O a1a2\nT explosion')
        with self.assertRaisesMessage(solve.CertificateError,
                                      'not terminal'):
            solve.verify_certificate(text)

    def test_swapping_or_and_and_is_rejected(self):
        text = MATE_IN_ONE_CERT.replace('O a1g7', 'A 1 a1g7')
        with self.assertRaisesMessage(solve.CertificateError, 'AND node'):
            solve.verify_certificate(text)

    def test_a_truncated_certificate_is_rejected(self):
        text = MATE_IN_ONE_CERT.replace('T explosion\n', '')
        with self.assertRaisesMessage(solve.CertificateError,
                                      'ended before'):
            solve.verify_certificate(text)

    def test_trailing_content_is_rejected(self):
        text = MATE_IN_ONE_CERT + 'O a1a2\n'
        with self.assertRaisesMessage(solve.CertificateError, 'trailing'):
            solve.verify_certificate(text)

    def test_a_wrong_node_count_is_rejected(self):
        text = MATE_IN_ONE_CERT.replace('nodes 2', 'nodes 3')
        with self.assertRaisesMessage(solve.CertificateError,
                                      'node count does not match'):
            solve.verify_certificate(text)

    def test_a_repetition_inside_the_proof_is_rejected(self):
        """A repetition the defender can hold is a draw, not a win."""
        text = """# atomicdb-proof/1
ruleset atomic-fide-claim-v1
goal WHITE_WIN
root 7k/6p1/8/8/8/8/8/Q3K3 w - - 0 1
nodes 4
---
O a1a2
A 2 g7g6 g7g5
O a2a1
T mate
O a2a1
T mate
"""
        with self.assertRaises(solve.CertificateError):
            solve.verify_certificate(text)

    def test_the_node_limit_is_enforced(self):
        with self.assertRaisesMessage(solve.CertificateError, 'node limit'):
            solve.verify_certificate(DEEP_CERT, root_fen=DEEP_FEN,
                                     max_nodes=5)

    def test_the_depth_limit_is_enforced(self):
        with self.assertRaisesMessage(solve.CertificateError, 'depth limit'):
            solve.verify_certificate(DEEP_CERT, root_fen=DEEP_FEN,
                                     max_depth=2)

    def test_the_fanout_limit_is_enforced(self):
        with self.assertRaisesMessage(solve.CertificateError, 'fan-out'):
            solve.verify_certificate(DEEP_CERT, root_fen=DEEP_FEN,
                                     max_fanout=2)

    def test_a_decompression_bomb_is_refused(self):
        bomb = solve.compress(b'A' * (16 * 1024 * 1024))
        with self.assertRaisesMessage(solve.CertificateError,
                                      'uncompressed limit'):
            solve.decompress(bomb, max_bytes=1024)

    def test_garbage_is_not_gzip(self):
        with self.assertRaisesMessage(solve.CertificateError, 'valid gzip'):
            solve.decompress(b'not actually gzip at all')

    def test_an_unknown_format_is_refused(self):
        with self.assertRaisesMessage(solve.CertificateError,
                                      'unknown certificate format'):
            solve.verify_certificate('# atomicdb-proof/99\n---\n')


class SolveProtocolTests(TestCase):

    def setUp(self):
        worker_account('solver', 'pw')
        self.pos = ingest.get_or_create_position(MATE_IN_ONE_FEN)
        self.campaign = ProofCampaign.objects.filter(active=True).first()
        self.task = SolveTask.objects.create(
            position=self.pos, campaign=self.campaign, goal='WHITE_WIN',
            budget_nodes=1_000_000)

    def _acquire(self, session='s1', machine='m1'):
        return self.client.post('/atomicdb/api/solve/acquire', {
            'username': 'solver', 'password': 'pw', 'machine': machine,
            'lease_session': session, 'threads': 8, 'hash': 256})

    def _submit(self, task_id, token, outcome='PROVED',
                cert=MATE_IN_ONE_CERT, machine='m1', **extra):
        data = {'username': 'solver', 'password': 'pw', 'machine': machine,
                'task_id': task_id, 'outcome': outcome,
                'lease_token': token, 'nodes': 1234, 'elapsed': '2.5', **extra}
        if cert is not None:
            data['certificate'] = SimpleUploadedFile(
                'certificate.gz', solve.compress(cert),
                content_type='application/gzip')
        return self.client.post('/atomicdb/api/solve/submit', data)

    def test_acquire_leases_and_submit_closes(self):
        response = self._acquire()
        payload = response.json()['tasks'][0]
        self.assertEqual(payload['goal'], 'WHITE_WIN')
        self.assertEqual(payload['ruleset'], logic.RULESET_ID)

        submitted = self._submit(payload['id'], payload['lease_token'])
        self.assertEqual(submitted.status_code, 200)
        summary = submitted.json()['summary']
        self.assertTrue(summary['verified'])
        self.assertTrue(summary['closed'])

        self.pos.refresh_from_db()
        self.assertEqual(self.pos.status, 'WHITE_WIN')
        self.assertEqual(self.pos.closure, 'SOLVE')
        self.assertEqual(self.pos.proof, 'ANDOR')
        self.assertEqual(self.pos.clock_slack, 100)

    def test_a_false_certificate_fails_the_task_and_mutates_nothing(self):
        payload = self._acquire().json()['tasks'][0]
        forged = MATE_IN_ONE_CERT.replace('O a1g7', 'O a1a2')

        submitted = self._submit(payload['id'], payload['lease_token'],
                                 cert=forged)

        self.assertTrue(submitted.json()['summary']['rejected'])
        self.pos.refresh_from_db()
        self.assertEqual(self.pos.status, 'UNKNOWN')
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, 'FAILED')
        self.assertFalse(self.task.verified)
        self.assertTrue(DBEvent.objects.filter(kind='SOLVE_REJECTED').exists())

    def test_proved_without_a_certificate_is_refused(self):
        payload = self._acquire().json()['tasks'][0]
        submitted = self._submit(payload['id'], payload['lease_token'],
                                 cert=None)
        self.assertIn('without a certificate',
                      submitted.json()['summary']['reason'])
        self.pos.refresh_from_db()
        self.assertEqual(self.pos.status, 'UNKNOWN')

    def test_unknown_is_recorded_and_closes_nothing(self):
        payload = self._acquire().json()['tasks'][0]
        submitted = self._submit(payload['id'], payload['lease_token'],
                                 outcome='UNKNOWN', cert=None, pn=77, dn=99)
        self.assertFalse(submitted.json()['summary']['verified'])
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, 'COMPLETED')
        self.assertEqual(self.task.advisory_pn, 77)
        self.assertEqual(self.task.advisory_dn, 99)
        self.pos.refresh_from_db()
        self.assertEqual(self.pos.status, 'UNKNOWN')

    def test_a_stale_token_cannot_submit(self):
        payload = self._acquire().json()['tasks'][0]
        response = self._submit(payload['id'], 'not-the-token')
        self.assertEqual(response.status_code, 409)

    def test_another_machine_cannot_submit(self):
        payload = self._acquire().json()['tasks'][0]
        response = self._submit(payload['id'], payload['lease_token'],
                                machine='m2')
        self.assertEqual(response.status_code, 409)

    def test_the_same_session_replays_its_assignment(self):
        first = self._acquire(session='same').json()['tasks'][0]
        second = self._acquire(session='same').json()['tasks'][0]
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['lease_token'], second['lease_token'])
        self.task.refresh_from_db()
        self.assertEqual(self.task.attempts, 1)

    def test_a_second_submit_is_a_duplicate_not_a_reclosure(self):
        payload = self._acquire().json()['tasks'][0]
        self._submit(payload['id'], payload['lease_token'])
        again = self._submit(payload['id'], payload['lease_token'])
        self.assertTrue(again.json()['dup'])

    def test_heartbeat_keeps_the_lease_and_rejects_a_stranger(self):
        payload = self._acquire().json()['tasks'][0]
        good = self.client.post('/atomicdb/api/solve/heartbeat', {
            'username': 'solver', 'password': 'pw', 'machine': 'm1',
            'task_id': payload['id'], 'lease_token': payload['lease_token']})
        self.assertEqual(good.status_code, 200)
        bad = self.client.post('/atomicdb/api/solve/heartbeat', {
            'username': 'solver', 'password': 'pw', 'machine': 'm1',
            'task_id': payload['id'], 'lease_token': 'wrong'})
        self.assertEqual(bad.status_code, 409)

    def test_credentials_are_required(self):
        for path in ('acquire', 'heartbeat', 'submit'):
            response = self.client.post(f'/atomicdb/api/solve/{path}', {})
            self.assertEqual(response.status_code, 403)

    def test_an_oversized_certificate_is_refused_before_parsing(self):
        payload = self._acquire().json()['tasks'][0]
        oversized = SimpleUploadedFile(
            'certificate.gz', b'x' * (solve.MAX_COMPRESSED_BYTES + 10),
            content_type='application/gzip')
        response = self.client.post('/atomicdb/api/solve/submit', {
            'username': 'solver', 'password': 'pw', 'machine': 'm1',
            'task_id': payload['id'], 'outcome': 'PROVED',
            'lease_token': payload['lease_token'],
            'certificate': oversized})
        self.assertEqual(response.status_code, 413)

    def test_an_old_worker_is_untouched_by_any_of_this(self):
        """The analysis lease path must not see solve tasks at all."""
        response = self.client.post('/atomicdb/api/lease', {
            'username': 'solver', 'password': 'pw', 'machine': 'legacy',
            'worker_build': 2026072203})
        self.assertEqual(response.status_code, 200)
        for task in response.json()['tasks']:
            self.assertNotIn('goal', task)


class SolveClosurePropagationTests(TestCase):

    def test_a_solve_closure_backs_up_like_any_other(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        # Point the mate-in-one certificate at a child by closing that child
        # through the same code path the endpoint uses.
        child = ingest.get_or_create_position(MATE_IN_ONE_FEN)
        task = SolveTask.objects.create(position=child, goal='WHITE_WIN',
                                        budget_nodes=1_000, state='LEASED',
                                        machine='m1')

        summary = ingest.apply_solve_result(
            task, outcome='PROVED',
            certificate_blob=solve.compress(MATE_IN_ONE_CERT),
            searched_nodes=10, elapsed_seconds=1.0)

        self.assertTrue(summary['verified'])
        child.refresh_from_db()
        self.assertEqual(child.closure, 'SOLVE')
        self.assertEqual(child.clock_slack, 100)
        self.assertTrue(DBEvent.objects.filter(
            kind='SOLVE_VERIFIED', payload__key=child.key).exists())

    def test_verify_certificate_command_replays_a_stored_proof(self):
        child = ingest.get_or_create_position(MATE_IN_ONE_FEN)
        task = SolveTask.objects.create(
            position=child, goal='WHITE_WIN', budget_nodes=1_000,
            state='COMPLETED', verified=True,
            certificate=solve.compress(MATE_IN_ONE_CERT))
        out = StringIO()

        call_command('verify_certificate', task=task.pk, stdout=out)

        self.assertIn(f'VERIFIED task {task.pk}', out.getvalue())

    def test_verify_certificate_command_reports_a_bad_stored_proof(self):
        child = ingest.get_or_create_position(MATE_IN_ONE_FEN)
        forged = MATE_IN_ONE_CERT.replace('O a1g7', 'O a1a2')
        task = SolveTask.objects.create(
            position=child, goal='WHITE_WIN', budget_nodes=1_000,
            state='COMPLETED', verified=True,
            certificate=solve.compress(forged))
        out = StringIO()

        call_command('verify_certificate', all=True, stdout=out)

        self.assertIn('REJECTED', out.getvalue())
        task.refresh_from_db()
        self.assertFalse(task.verified)
        self.assertTrue(DBEvent.objects.filter(kind='SOLVE_REJECTED').exists())


class SolvePilotTests(TestCase):

    def _frontier(self, count, eval_cp, prefix, pv=None):
        rows = []
        for index in range(count):
            key = f'{prefix}{index:056d}'
            rows.append(Position.objects.create(
                key=key, fen=f'8/8/8/8/8/8/{index % 8}k6/K6Q w - - 0 1',
                status='UNKNOWN', expanded=True, eval_cp=eval_cp, visits=3,
                last_analysis=[{'move': 'h1h2', 'pv': pv or ['h1h2', 'b2b3']}]))
        return rows

    def test_the_pilot_stratifies_and_pairs_its_arms(self):
        self._frontier(4, 9_500, 'a')                       # mate band
        self._frontier(4, 1_200, 'b', pv=['h1h2', 'b2b3'])  # quiet: fortress
        self._frontier(4, 1_200, 'c', pv=['h1xh2', 'b2b4'])  # resets: high eval
        out = StringIO()

        call_command('solve_pilot', size=12, stdout=out)
        report = __import__('json').loads(out.getvalue())

        self.assertGreater(report['queued']['solve'], 0)
        self.assertGreater(report['queued']['analyze'], 0)
        # Paired: within a stratum the arms alternate, so neither can run away
        # with the easy half of the sample.
        arms = [event.payload['arm'] for event in DBEvent.objects.filter(
            kind='SOLVE_PILOT_ARM')]
        self.assertGreater(arms.count('analyze'), 0)
        self.assertGreater(arms.count('solve'), 0)
        self.assertLessEqual(abs(arms.count('analyze') - arms.count('solve')),
                             len(report['sample']))

    def test_solve_arm_queues_both_budgets(self):
        self._frontier(2, 9_500, 'a')
        call_command('solve_pilot', size=4, stdout=StringIO())
        budgets = sorted(set(SolveTask.objects.values_list('budget_nodes',
                                                           flat=True)))
        self.assertEqual(budgets, list(sorted(__import__(
            'atomicdb.management.commands.solve_pilot',
            fromlist=['SOLVE_BUDGETS']).SOLVE_BUDGETS)))

    def test_dry_run_queues_nothing(self):
        self._frontier(4, 9_500, 'a')
        call_command('solve_pilot', size=4, dry_run=True, stdout=StringIO())
        self.assertEqual(SolveTask.objects.count(), 0)

    def test_report_computes_the_gates(self):
        self._frontier(2, 9_500, 'a')
        call_command('solve_pilot', size=4, stdout=StringIO())
        out = StringIO()

        call_command('solve_pilot', report=True, json=True, stdout=out)
        report = __import__('json').loads(out.getvalue())

        self.assertIn('gates', report)
        self.assertTrue(report['gates']['no_false_certificates'])
        self.assertIn('closure_ratio', report['gates'])
        self.assertIn('certificate_bytes_total', report['solve'])

    def test_zeroing_density_orders_grinds_below_tactics(self):
        from atomicdb.management.commands import solve_pilot as pilot
        quiet = pilot.zeroing_density([{'pv': ['h1h2', 'b2b3', 'h2h3']}])
        sharp = pilot.zeroing_density([{'pv': ['e2e4', 'd7d5', 'e4d5']}])
        self.assertIsNotNone(quiet)
        self.assertLess(quiet, sharp)
        self.assertIsNone(pilot.zeroing_density(None))
