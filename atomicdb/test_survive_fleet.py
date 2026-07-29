"""SURVIVE50 in the fleet: the F ladder, the F0 classifier, and the one
result the proof database must not misfile.

The load-bearing test in here is ``test_a_verified_survival_does_not_close
_the_position``.  Everything else is plumbing; that one is the doctrine.  A
survival certificate refutes the boolean objective WHITE_WIN and says nothing
whatever about BLACK_WIN -- Black surviving is compatible with a draw and with
a Black win, and telling those apart needs a different proof.  ``Position``
has no NOT_WHITE_WIN status, and the temptation to invent one out of this
evidence is exactly what doc 18 §6.1 forbids.  So the fact lands on the task
and in the event log, and the position stays as open as it was.
"""

import pathlib

from . import ingest, logic, survive
from .models import DBEvent, Position, SolveTask
from .test_survive import (KING_WALK_ROOT, _emit, _expand, _shuffle_policy,
                           _thresholds)
from .testing import TestCase, worker_account

MINED = (pathlib.Path(__file__).resolve().parent / 'data' / 'survive50'
         / 'mined_king_walk.cert')


def _king_walk_certificate():
    fens, white, black = _expand(KING_WALK_ROOT, _shuffle_policy)
    return _emit(KING_WALK_ROOT, 0, fens, white, black,
                 _thresholds(fens, white, black))


class FortressClassifierTests(TestCase):
    """F0 telemetry. Three of four, and it schedules -- it never concludes."""

    # The keys as STORED. Written out rather than imported so that a change to
    # the vocabulary has to be made twice on purpose.
    GOOD = {'tt_hit': 0.71, 'quiet_scc': 0.63, 'reset_rate': 0.02,
            'stagnation': 1.1, 'score': 4.0}

    def test_the_classifier_reads_the_keys_the_submit_view_actually_stores(self):
        """The bug this test exists for, found the hard way.

        The engine prints ``fortress_tt_hit``, the worker posts it under that
        name, and the submit view strips the prefix before saving. A
        classifier reading the wire names would never fire on real telemetry
        and would still pass every test written against its own invention --
        so the vocabulary is pinned against the view that fills it.
        """
        import inspect
        from . import views
        source = inspect.getsource(views)
        stored = source.split("for name in ('tt_hit'", 1)
        self.assertEqual(len(stored), 2, 'the submit view no longer looks '
                                         'like the thing this test pins')
        block = "('tt_hit'" + stored[1].split(')', 1)[0] + ')'
        for key in survive.TELEMETRY_KEYS:
            self.assertIn(f"'{key}'", block,
                          f'{key} is not a key the submit view stores')

    def test_three_of_four_is_the_bar(self):
        self.assertTrue(survive.fortress_suspected(self.GOOD))
        two = dict(self.GOOD, reset_rate=0.5, stagnation=9.0)
        self.assertFalse(survive.fortress_suspected(two))

    def test_each_indicator_is_reported_separately(self):
        fired = survive.fortress_indicators(self.GOOD)
        self.assertEqual(set(fired), {'tt_hit', 'quiet_scc', 'few_resets',
                                      'stagnant'})
        self.assertTrue(all(fired.values()))

    def test_missing_or_junk_telemetry_never_fires(self):
        for telemetry in (None, {}, 'nonsense', {'tt_hit': 'x'},
                          {'stagnation': 0.0}, {'score': 4.0}):
            self.assertFalse(survive.fortress_suspected(telemetry),
                             f'{telemetry!r} should not suggest a fortress')

    def test_it_fires_on_telemetry_that_travelled_the_real_wire(self):
        """End to end through the submit endpoint, not a hand-made dict."""
        worker_account('solver', 'pw')
        position = ingest.get_or_create_position(KING_WALK_ROOT)
        task = SolveTask.objects.create(position=position, goal='WHITE_WIN',
                                        budget_nodes=1_000, state='LEASED',
                                        machine='m1')
        response = self.client.post('/atomicdb/api/solve/submit', {
            'username': 'solver', 'password': 'pw', 'machine': 'm1',
            'task_id': task.id, 'outcome': 'UNKNOWN', 'lease_token': '',
            'fortress_tt_hit': '0.71', 'fortress_quiet_scc': '0.62',
            'fortress_reset_rate': '0.03', 'fortress_stagnation': '1.4',
            'fortress_score': '4'})
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertTrue(survive.fortress_suspected(task.telemetry),
                        f'stored telemetry {task.telemetry} did not fire')

    def test_the_ladder_is_monotone_and_matches_the_doc(self):
        stages = ['F0', 'F1', 'F2', 'F3', 'F4']
        budgets = [survive.STAGE_BUDGETS[s] for s in stages]
        self.assertEqual(budgets, sorted(budgets))
        self.assertEqual(survive.STAGE_BUDGETS['F0'], 2_000_000)
        self.assertEqual(survive.STAGE_BUDGETS['F4'], 100_000_000)
        self.assertEqual(set(survive.STAGE_BUDGETS),
                         set(SolveTask.Stage.values))


class VerifierRoutingTests(TestCase):

    def test_the_state_count_is_read_from_the_header_for_routing(self):
        self.assertEqual(
            survive.declared_states(MINED.read_text(encoding='utf-8')), 263)
        self.assertEqual(survive.declared_states('not a certificate'), 0)

    def test_a_small_certificate_takes_the_reference(self):
        text = _king_walk_certificate()
        self.assertLess(survive.declared_states(text),
                        survive.NATIVE_VERIFIER_STATE_THRESHOLD)
        report = survive.verify_certificate_auto(text, root_fen=KING_WALK_ROOT)
        self.assertEqual(report['verifier'], 'reference')
        self.assertEqual(report['result'], 'DISPROVED_WHITE_WIN')

    def test_a_large_certificate_takes_the_native_tool(self):
        if not survive.native_available():
            self.skipTest('native verifier not built')
        text = MINED.read_text(encoding='utf-8')
        self.assertGreaterEqual(survive.declared_states(text),
                                survive.NATIVE_VERIFIER_STATE_THRESHOLD)
        report = survive.verify_certificate_auto(text, root_fen=KING_WALK_ROOT)
        self.assertEqual(report['verifier'], 'native')
        self.assertEqual(report['states'], 263)

    def test_the_native_path_rejects_with_the_same_code(self):
        if not survive.native_available():
            self.skipTest('native verifier not built')
        text = MINED.read_text(encoding='utf-8').replace(
            'entry_clock 0', 'entry_clock 40', 1)
        with self.assertRaises(survive.CertificateError) as caught:
            survive.verify_certificate_auto(text, root_fen=KING_WALK_ROOT)
        self.assertEqual(getattr(caught.exception, 'code', None),
                         'entry-clock-mismatch')


class SurvivalSubmissionTests(TestCase):

    def _task(self, fen):
        position = ingest.get_or_create_position(fen)
        return position, SolveTask.objects.create(
            position=position, goal='WHITE_WIN', budget_nodes=5_000_000,
            budget_stage='F1', state='LEASED', machine='m1')

    def test_a_verified_survival_is_stored_as_its_own_typed_result(self):
        position, task = self._task(KING_WALK_ROOT)
        blob = survive.compress(_king_walk_certificate())

        summary = ingest.apply_solve_result(
            task, 'DISPROVED_WHITE_WIN', certificate_blob=blob,
            searched_nodes=4_000_000, elapsed_seconds=31.0,
            solver_build='survive50-test')

        self.assertTrue(summary['verified'])
        task.refresh_from_db()
        self.assertEqual(task.state, 'COMPLETED')
        self.assertTrue(task.verified)
        self.assertEqual(task.outcome, 'DISPROVED_WHITE_WIN')
        self.assertEqual(task.certificate_format, survive.CERTIFICATE_FORMAT)
        self.assertEqual(task.survival_tau, 0)
        self.assertEqual(task.survival_states, 14)
        self.assertGreater(task.certificate_bytes, 0)

    def test_a_verified_survival_does_not_close_the_position(self):
        """THE line. A disproof of WHITE_WIN is not a proof of anything."""
        position, task = self._task(KING_WALK_ROOT)
        self.assertEqual(position.status, 'UNKNOWN')
        blob = survive.compress(_king_walk_certificate())

        summary = ingest.apply_solve_result(
            task, 'DISPROVED_WHITE_WIN', certificate_blob=blob)

        self.assertTrue(summary['verified'])
        self.assertFalse(summary.get('closed'))
        self.assertFalse(summary.get('upgraded'))
        position.refresh_from_db()
        self.assertEqual(position.status, 'UNKNOWN',
                         'a survival certificate must never close a position')
        self.assertNotEqual(position.status, 'BLACK_WIN')
        self.assertNotEqual(position.status, 'DRAW')

    def test_it_lands_in_the_event_log_for_the_orchestrator(self):
        position, task = self._task(KING_WALK_ROOT)
        ingest.apply_solve_result(task, 'DISPROVED_WHITE_WIN',
                                  certificate_blob=survive.compress(
                                      _king_walk_certificate()))
        event = DBEvent.objects.filter(kind='SURVIVE_VERIFIED').get()
        self.assertEqual(event.payload['key'], task.position_id)
        self.assertEqual(event.payload['tau'], 0)
        self.assertEqual(event.payload['states'], 14)
        self.assertEqual(event.payload['stage'], 'F1')
        self.assertIn(event.payload['verifier'], ('reference', 'native'))

    def test_a_survival_claim_without_a_certificate_is_refused(self):
        position, task = self._task(KING_WALK_ROOT)
        summary = ingest.apply_solve_result(task, 'DISPROVED_WHITE_WIN')
        self.assertTrue(summary.get('rejected'))
        task.refresh_from_db()
        self.assertEqual(task.state, 'FAILED')
        self.assertFalse(task.verified)
        self.assertIn('without a certificate', task.reject_reason)

    def test_a_tampered_survival_certificate_is_refused(self):
        position, task = self._task(KING_WALK_ROOT)
        text = _king_walk_certificate()
        victim = next(line for line in text.split('\n')
                      if line.startswith('W '))
        broken = text.replace(victim + '\n', '', 1).replace('edges 19',
                                                            'edges 18', 1)

        summary = ingest.apply_solve_result(
            task, 'DISPROVED_WHITE_WIN',
            certificate_blob=survive.compress(broken))

        self.assertTrue(summary.get('rejected'))
        task.refresh_from_db()
        self.assertEqual(task.state, 'FAILED')
        self.assertFalse(task.verified)
        self.assertIsNone(task.survival_tau)
        self.assertFalse(DBEvent.objects.filter(
            kind='SURVIVE_VERIFIED').exists())
        position.refresh_from_db()
        self.assertEqual(position.status, 'UNKNOWN')

    def test_a_certificate_for_another_position_is_refused(self):
        position, task = self._task(logic.start_fen())
        summary = ingest.apply_solve_result(
            task, 'DISPROVED_WHITE_WIN',
            certificate_blob=survive.compress(_king_walk_certificate()))
        self.assertTrue(summary.get('rejected'))
        task.refresh_from_db()
        self.assertEqual(task.state, 'FAILED')

    def test_the_ordinary_proof_path_still_records_its_own_format(self):
        """The two formats share a column, so they must not share a label."""
        from .test_solve import MATE_IN_ONE_CERT, MATE_IN_ONE_FEN
        from . import solve
        position, task = self._task(MATE_IN_ONE_FEN)
        ingest.apply_solve_result(
            task, 'PROVED',
            certificate_blob=solve.compress(MATE_IN_ONE_CERT))
        task.refresh_from_db()
        self.assertTrue(task.verified)
        self.assertEqual(task.certificate_format, solve.CERTIFICATE_FORMAT)
        self.assertIsNone(task.survival_tau)
