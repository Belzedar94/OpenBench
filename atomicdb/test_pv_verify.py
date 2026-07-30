"""Verify PV: encolar analisis por cada posicion de la linea vigente.

Un nodo con eval propio profundo cuyo respaldo discrepa tiene una PV que
reclama algo, y la unica forma de saber si el arbol la sostiene es mirar las
posiciones por las que pasa.  Esto es ese recorrido, de un click.
"""

from django.test import Client

from . import ingest, logic, views
from .models import AnalysisTask, Edge, Position
from .testing import TestCase

# Cuatro plies legales desde la posicion inicial, con respuesta por bando: la
# linea mas corta que el boton considera verificable.
LINE = ['e2e4', 'e7e5', 'g1f3', 'b8c6']


def _line_fens(ucis):
    """Los FEN canonicos por los que pasa ``ucis`` desde la inicial."""
    fen = logic.start_fen()
    out = []
    for uci in ucis:
        fen = logic.apply_move(fen, uci)
        out.append(logic.canonical_fen(fen))
    return out


def _analysis(pv, extra=None):
    line = {'move': pv[0], 'eval_cp': 42, 'mate': None, 'pv': list(pv)}
    if extra:
        line.update(extra)
    return line


class EnqueuePvVerificationTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())

    def _store(self, lines):
        self.root.last_analysis = lines
        self.root.save(update_fields=['last_analysis'])
        self.root.refresh_from_db()

    def test_walks_the_whole_line_and_queues_one_task_per_position(self):
        self._store([_analysis(LINE)])

        queued = ingest.enqueue_pv_verification(self.root, requested_by='ana')

        self.assertEqual(queued, 4)
        keys = [logic.key_of(fen) for fen in _line_fens(LINE)]
        for key in keys:
            self.assertTrue(Position.objects.filter(key=key).exists(), key)
        tasks = AnalysisTask.objects.filter(position_id__in=keys)
        self.assertEqual(tasks.count(), 4)
        for task in tasks:
            self.assertEqual(task.source, AnalysisTask.Source.USER)
            self.assertEqual(task.requested_by, 'ana')
            self.assertEqual(task.state, AnalysisTask.TState.PENDING)
        # La ruta queda NAVEGABLE: nodo y arista por ply, como un goto.
        parents = [self.root.key] + keys[:-1]
        for parent, child, uci in zip(parents, keys, LINE):
            self.assertTrue(Edge.objects.filter(
                parent_id=parent, move_uci=uci, child_id=child).exists(),
                f'{uci} sin arista')

    def test_the_budget_is_the_request_floor_not_a_coverage_seed(self):
        self._store([_analysis(LINE)])

        ingest.enqueue_pv_verification(self.root)

        first = AnalysisTask.objects.get(
            position_id=logic.key_of(_line_fens(LINE)[0]))
        self.assertEqual(first.budget_nodes, ingest.REQUEST_BUDGET_LADDER[0])
        self.assertGreater(ingest.REQUEST_BUDGET_LADDER[0],
                           ingest.BUDGET_LADDER[0])

    def test_a_second_click_does_not_duplicate_live_work(self):
        self._store([_analysis(LINE)])

        first = ingest.enqueue_pv_verification(self.root)
        second = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(first, 4)
        self.assertEqual(second, 0)
        self.assertEqual(AnalysisTask.objects.count(), 4)

    def test_an_autonomous_pending_task_is_promoted_and_counted(self):
        self._store([_analysis(LINE)])
        key = logic.key_of(_line_fens(LINE)[0])
        child = ingest.get_or_create_position(_line_fens(LINE)[0])
        AnalysisTask.objects.create(
            position=child, generation=child.visits, budget_nodes=8_000_000,
            source=AnalysisTask.Source.AUTO)

        queued = ingest.enqueue_pv_verification(self.root, requested_by='ana')

        task = AnalysisTask.objects.get(position_id=key)
        self.assertEqual(queued, 4)      # la promocion cuenta como comprada
        self.assertEqual(task.source, AnalysisTask.Source.USER)
        self.assertEqual(task.requested_by, 'ana')

    def test_a_closed_position_is_skipped_and_the_walk_continues(self):
        self._store([_analysis(LINE)])
        fens = _line_fens(LINE)
        middle = ingest.get_or_create_position(fens[1])
        middle.status = 'DRAW'
        middle.closure = 'MINIMAX'
        middle.save(update_fields=['status', 'closure'])

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 3)
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=middle.key).exists())
        # ...y lo de DEBAJO del cierre sigue comprandose: la discrepancia que
        # se persigue puede vivir ahi.
        self.assertTrue(AnalysisTask.objects.filter(
            position_id=logic.key_of(fens[3])).exists())

    def test_an_illegal_move_cuts_the_walk_without_an_error(self):
        # Una PV vieja (o de antes de un rekey) deja de aplicar a mitad. El
        # prefijo que si aplica es informacion; el resto no existe.
        self._store([_analysis(['e2e4', 'e7e5', 'e2e4', 'g8f6'])])

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 2)
        self.assertEqual(AnalysisTask.objects.count(), 2)

    def test_the_ply_cap_bounds_one_click(self):
        long_pv = ['g1f3', 'g8f6', 'b1c3', 'b8c6', 'e2e3', 'e7e6',
                   'd2d3', 'd7d6', 'f1e2', 'f8e7', 'e1g1', 'e8g8',
                   'a2a3', 'a7a6', 'b2b3', 'b7b6', 'h2h3', 'h7h6']
        self.assertGreater(len(long_pv), ingest.PV_VERIFY_MAX_PLIES)
        self._store([_analysis(long_pv)])

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, ingest.PV_VERIFY_MAX_PLIES)

    def test_an_earlier_wider_pass_is_not_the_current_line(self):
        # ``prior_pass`` es el escaparate ancho de un pase ANTERIOR que el
        # ingest conserva a proposito. Verificarlo seria verificar el veredicto
        # de ayer.
        self._store([_analysis(['d2d4', 'd7d5', 'c2c4', 'c7c6'],
                               {'prior_pass': True}),
                     _analysis(LINE)])

        ingest.enqueue_pv_verification(self.root)

        self.assertTrue(AnalysisTask.objects.filter(
            position_id=logic.key_of(_line_fens(LINE)[0])).exists())
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=logic.key_of(_line_fens(['d2d4'])[0])).exists())

    def test_without_a_stored_analysis_there_is_nothing_to_verify(self):
        self.assertEqual(ingest.enqueue_pv_verification(self.root), 0)
        self._store([{'move': 'e2e4', 'eval_cp': 42}])
        self.assertEqual(ingest.enqueue_pv_verification(self.root), 0)
        self.assertEqual(AnalysisTask.objects.count(), 0)


class PvVerifyEndpointTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.root.last_analysis = [_analysis(LINE)]
        self.root.save(update_fields=['last_analysis'])
        self.url = f'/atomicdb/pv-verify/{self.root.key}/'
        self.strict = Client(enforce_csrf_checks=True)

    def _token(self):
        page = self.strict.get(f'/atomicdb/explore/{self.root.key}/')
        return page.cookies['csrftoken'].value

    def test_a_foreign_post_without_token_is_rejected(self):
        response = self.strict.post(self.url)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(AnalysisTask.objects.count(), 0)

    def test_the_pages_own_fetch_goes_through(self):
        response = self.strict.post(self.url,
                                    HTTP_X_CSRFTOKEN=self._token())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {'status': 'queued', 'queued': 4})
        self.assertEqual(AnalysisTask.objects.count(), 4)

    def test_a_get_is_not_a_request(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_an_unknown_position_is_a_404(self):
        response = self.client.post(f'/atomicdb/pv-verify/{"0" * 64}/')

        self.assertEqual(response.status_code, 404)

    def test_a_solved_position_buys_nothing(self):
        self.root.status = 'DRAW'
        self.root.closure = 'MINIMAX'
        self.root.save(update_fields=['status', 'closure'])

        response = self.client.post(self.url)

        self.assertEqual(response.json()['status'], 'already-solved')
        self.assertEqual(AnalysisTask.objects.count(), 0)

    def test_a_line_already_queued_says_so(self):
        self.client.post(self.url)

        response = self.client.post(self.url)

        self.assertEqual(response.json(),
                         {'status': 'nothing-to-do', 'queued': 0})

    def test_a_logged_in_visitor_keeps_the_affinity(self):
        from .testing import worker_account

        worker_account('pv-fan', 'pw')
        self.client.login(username='pv-fan', password='pw')

        self.client.post(self.url)

        self.assertEqual(
            set(AnalysisTask.objects.values_list('requested_by', flat=True)),
            {'pv-fan'})


class PvVerifyButtonTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())

    def _page(self):
        return self.client.get(f'/atomicdb/explore/{self.root.key}/')

    def _store(self, lines):
        self.root.last_analysis = lines
        self.root.save(update_fields=['last_analysis'])

    def test_the_button_appears_with_a_line_worth_walking(self):
        self._store([_analysis(LINE)])

        page = self._page()

        self.assertEqual(page.context['pv_verify_plies'], 4)
        self.assertContains(page, 'Verify PV')
        self.assertContains(page, 'pv-verify/')

    def test_no_analysis_no_button(self):
        page = self._page()

        self.assertEqual(page.context['pv_verify_plies'], 0)
        self.assertNotContains(page, 'Verify PV')

    def test_a_line_too_short_to_contrast_offers_nothing(self):
        self._store([_analysis(['e2e4', 'e7e5', 'g1f3'])])

        page = self._page()

        self.assertEqual(page.context['pv_verify_plies'], 0)
        self.assertNotContains(page, 'Verify PV')

    def test_a_solved_position_has_no_button(self):
        self._store([_analysis(LINE)])
        self.root.status = 'DRAW'
        self.root.closure = 'MINIMAX'
        self.root.save(update_fields=['status', 'closure'])

        page = self._page()

        self.assertEqual(page.context['pv_verify_plies'], 0)
        self.assertNotContains(page, 'Verify PV')

    def test_the_offer_is_capped_like_the_walk(self):
        self._store([_analysis(['g1f3'] + ['x'] * 40)])

        self.assertEqual(views._pv_verify_plies(self.root),
                         ingest.PV_VERIFY_MAX_PLIES)
