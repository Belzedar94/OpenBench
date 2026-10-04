"""Verify PV: encolar analisis por cada posicion de la linea vigente.

Un nodo con eval propio profundo cuyo respaldo discrepa tiene una PV que
reclama algo, y la unica forma de saber si el arbol la sostiene es mirar las
posiciones por las que pasa.  Esto es ese recorrido, de un click.
"""

from unittest.mock import patch

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


def _covered(fen, own_pv=None, budget=512_000_000):
    """Una posicion YA VERIFICADA a ``budget``, opcionalmente con linea propia.

    Es el terreno que el paseo tiene que CRUZAR sin comprar: analisis
    COMPLETADO por encima del suelo de peticion y la visita ya contada, o sea
    que el dedup por generacion no cubre nada de esto — lo unico que salva el
    gasto es mirar lo completado.
    """
    pos = ingest.get_or_create_position(fen)
    AnalysisTask.objects.create(
        position=pos, generation=pos.visits, budget_nodes=budget,
        source=AnalysisTask.Source.USER,
        state=AnalysisTask.TState.COMPLETED)
    fields = {'visits': 1}
    if own_pv is not None:
        fields['last_analysis'] = [_analysis(own_pv)]
    Position.objects.filter(key=pos.key).update(**fields)
    return Position.objects.get(key=pos.key)


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

    def test_a_node_already_analysed_deeper_is_not_bought_again(self):
        """El suelo de peticion no encarga 128M sobre lo que ya tiene 512M.

        Es el gasto que la comunidad vio de cerca: "already has 640M analysis
        but requested 128M because clicked Verify PV in parent".  El suelo
        existe para que un click no compre calderilla, no para repetir peor
        una busqueda que ya esta guardada.
        """
        self._store([_analysis(LINE)])
        fens = _line_fens(LINE)
        deep = ingest.get_or_create_position(fens[0])
        AnalysisTask.objects.create(
            position=deep, generation=deep.visits, budget_nodes=512_000_000,
            source=AnalysisTask.Source.USER,
            state=AnalysisTask.TState.COMPLETED)
        # Ese analisis ya aterrizo, o sea que la generacion viva es otra: el
        # dedup por generacion no cubre este caso, y por eso hacia falta mirar
        # lo COMPLETADO.
        Position.objects.filter(key=deep.key).update(visits=1)

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 3)
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=deep.key,
            state=AnalysisTask.TState.PENDING).exists())
        # ...y el resto de la linea se compra igual: saltar no es cortar.
        self.assertTrue(AnalysisTask.objects.filter(
            position_id=logic.key_of(fens[3])).exists())

    def test_only_a_completed_budget_that_reaches_the_request_skips(self):
        """8M completados no son la respuesta que este click viene a comprar;
        128M completados, exactamente el suelo, si lo son."""
        self._store([_analysis(LINE)])
        fens = _line_fens(LINE)
        cheap = ingest.get_or_create_position(fens[0])
        exact = ingest.get_or_create_position(fens[1])
        for node, budget in ((cheap, 8_000_000),
                             (exact, ingest.REQUEST_BUDGET_LADDER[0])):
            AnalysisTask.objects.create(
                position=node, generation=node.visits, budget_nodes=budget,
                state=AnalysisTask.TState.COMPLETED)
            Position.objects.filter(key=node.key).update(visits=1)

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 3)
        self.assertTrue(AnalysisTask.objects.filter(
            position_id=cheap.key,
            state=AnalysisTask.TState.PENDING).exists())
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=exact.key,
            state=AnalysisTask.TState.PENDING).exists())

    def test_the_walk_descends_through_covered_ground_and_buys_below(self):
        """El uso 2 de Wolfram: 10B en un nodo interno, y luego este boton.

        "You request 10B analysis in internal node and then once it is ready
        you click Verify PV to expand better white moves than are currently in
        a tree.  Here it might be counter-productive to expand a leaf if it is
        present yet."  El hijo ya verificado no se vuelve a comprar: lo que se
        compra esta por DEBAJO, y por la linea que dice EL — no por la que su
        padre guardo antes de que ese analisis aterrizara.
        """
        self._store([_analysis(['e2e4', 'e7e5'])])
        after_e4 = logic.apply_move(logic.start_fen(), 'e2e4')
        _covered(after_e4, own_pv=['c7c5', 'g1f3'])

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 2)
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=logic.key_of(after_e4),
            state=AnalysisTask.TState.PENDING).exists())
        self.assertTrue(AnalysisTask.objects.filter(
            position_id=logic.key_of(
                logic.apply_move(after_e4, 'c7c5'))).exists())
        # ...y NADA por la continuacion que el padre guardo: 1.e4 e5 era su
        # opinion de antes, y quien esta debajo ha mirado despues y mas hondo.
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=logic.key_of(
                logic.apply_move(after_e4, 'e7e5'))).exists())
        # El recibo: un ply verificado cruzado, la compra en la linea 1.
        self.assertEqual(queued.detail['line'], 1)
        self.assertEqual(queued.detail['covered_plies'], 1)
        self.assertEqual(queued.detail['move'], 'c7c5')

    def test_a_line_covered_to_its_leaf_hands_the_click_to_the_next(self):
        """El uso 1 de Wolfram, con sus palabras: "once you covered the first
        PV, the highest leaf node can be 2nd or 3rd line a PV, and would be
        nice to have it expanded with one button"."""
        self._store([_analysis(['e2e4']), _analysis(['d2d4', 'd7d5'])])
        after_e4 = logic.apply_move(logic.start_fen(), 'e2e4')
        _covered(after_e4)          # verificado y sin linea propia: se acabo

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 2)
        self.assertEqual(queued.detail['line'], 2)
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=logic.key_of(after_e4),
            state=AnalysisTask.TState.PENDING).exists())
        for fen in _line_fens(['d2d4', 'd7d5']):
            self.assertTrue(AnalysisTask.objects.filter(
                position_id=logic.key_of(fen)).exists(), fen)

    def test_with_every_line_covered_the_click_buys_nothing_and_says_so(self):
        """Tres lineas miradas, ningun hueco: el boton no inventa gasto."""
        self._store([_analysis(['e2e4']), _analysis(['d2d4']),
                     _analysis(['g1f3'])])
        for uci in ('e2e4', 'd2d4', 'g1f3'):
            _covered(logic.apply_move(logic.start_fen(), uci))

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 0)
        self.assertEqual(AnalysisTask.objects.filter(
            state=AnalysisTask.TState.PENDING).count(), 0)
        self.assertEqual(queued.detail['line'], 0)
        self.assertEqual(queued.detail['lines_tried'], 3)
        self.assertEqual(queued.detail['covered_plies'], 1)

    def test_a_cycle_of_covered_lines_terminates(self):
        """1.Nf3 Nf6 2.Ng1 Ng8 ES la posicion inicial una vez quitados los
        contadores, asi que un paseo que solo siga lineas propias puede volver
        a casa.  El conjunto de visitados lo corta ahi mismo, igual que en el
        descenso de la frontera."""
        self._store([_analysis(['g1f3'])])
        fens = _line_fens(['g1f3', 'g8f6', 'f3g1'])
        for fen, own in zip(fens, (['g8f6'], ['f3g1'], ['f6g8'])):
            _covered(fen, own_pv=own)

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 0)
        self.assertEqual(queued.detail['covered_plies'], 3)
        self.assertEqual(queued.detail['plies'], 4)   # el 4o cierra el ciclo
        # Y sobre todo: la posicion pulsada NO se compra a si misma por haber
        # vuelto a pasar por ella.
        self.assertFalse(
            AnalysisTask.objects.filter(position_id=self.root.key).exists())

    def test_a_line_already_in_the_queue_does_not_pay_for_the_next_one(self):
        """Un segundo click no se muda a la linea 2: lo que hay debajo de la 1
        ya se pago y sigue en vuelo.  Si se mudara, doblar clicks doblaria la
        factura — que es justo lo que el dedup existe para impedir."""
        self._store([_analysis(['e2e4']), _analysis(['d2d4'])])

        first = ingest.enqueue_pv_verification(self.root)
        second = ingest.enqueue_pv_verification(self.root)

        self.assertEqual((first, second), (1, 0))
        self.assertEqual(second.detail['in_flight'], 1)
        self.assertEqual(second.detail['line'], 1)
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=logic.key_of(_line_fens(['d2d4'])[0])).exists())

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


class TreeCandidateTests(TestCase):
    """Cuando las lineas VIGENTES se acaban, el paseo sigue por el ARBOL.

    Reporte de comunidad (Wolfram, 3-ago): "top 2 lines were covered, but top 5
    lines from one of earlier passes were not, thus 4. h3 can still be covered
    by Verify PV".  El ultimo pase fue mas profundo y mas estrecho que el
    anterior, asi que el escaparate ancho de antes dejo de ser la instantanea
    vigente — ese arbitraje es deliberado y no se toca.  Pero sus JUGADAS no se
    fueron con el: el ingest las sembro como hijos con eval, y ahi siguen.
    """

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())

    def _store(self, lines):
        self.root.last_analysis = lines
        self.root.save(update_fields=['last_analysis'])
        self.root.refresh_from_db()

    def _seeded_child(self, uci, eval_cp):
        """Un hijo como lo deja un pase ancho: arista y eval, sin busqueda."""
        child = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, uci))
        Position.objects.filter(key=child.key).update(eval_cp=eval_cp)
        Edge.objects.get_or_create(parent=self.root, move_uci=uci,
                                   defaults={'child': child})
        return Position.objects.get(key=child.key)

    def test_a_child_the_current_lines_forgot_is_still_walkable(self):
        self._store([_analysis(['e2e4']), _analysis(['d2d4'])])
        for uci in ('e2e4', 'd2d4'):
            _covered(logic.apply_move(logic.start_fen(), uci))
        forgotten = self._seeded_child('h2h3', 40)

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 1)
        self.assertEqual(queued.detail['line'], 3)
        self.assertTrue(queued.detail['from_tree'])
        self.assertTrue(AnalysisTask.objects.filter(
            position_id=forgotten.key,
            state=AnalysisTask.TState.PENDING).exists())

    def test_the_receipt_says_the_candidate_came_from_the_tree(self):
        """Sin decirlo, "line 3" mentiria por omision: no hay tercera linea en
        el analisis vigente, hay una jugada que el arbol recuerda."""
        self._store([_analysis(['e2e4']), _analysis(['d2d4'])])
        for uci in ('e2e4', 'd2d4'):
            _covered(logic.apply_move(logic.start_fen(), uci))
        self._seeded_child('h2h3', 40)

        data = self.client.post(
            f'/atomicdb/pv-verify/{self.root.key}/').json()

        self.assertEqual(data['status'], 'queued')
        self.assertEqual((data['line'], data['from_tree']), (3, True))
        self.assertIn('down line 3 (from the tree), starting after h3',
                      data['message'])

    def test_the_tree_candidates_go_in_order_of_what_the_tree_knows(self):
        self._store([_analysis(['e2e4'])])
        _covered(logic.apply_move(logic.start_fen(), 'e2e4'))
        best = self._seeded_child('h2h3', 400)
        worst = self._seeded_child('a2a3', 40)

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 1)
        self.assertEqual(queued.detail['line'], 2)
        self.assertTrue(AnalysisTask.objects.filter(
            position_id=best.key).exists())
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=worst.key).exists())

    def test_a_tree_child_already_verified_hands_the_click_to_the_next(self):
        """El candidato del arbol pasa por la MISMA puerta que una linea: lo ya
        completado a este presupuesto se cruza sin gastar, y el click sigue."""
        self._store([_analysis(['e2e4'])])
        _covered(logic.apply_move(logic.start_fen(), 'e2e4'))
        strong = self._seeded_child('h2h3', 400)
        weak = self._seeded_child('a2a3', 40)
        _covered(strong.fen)

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 1)
        self.assertEqual((queued.detail['line'], queued.detail['from_tree']),
                         (3, True))
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=strong.key,
            state=AnalysisTask.TState.PENDING).exists())
        self.assertTrue(AnalysisTask.objects.filter(
            position_id=weak.key).exists())

    def test_three_current_lines_leave_no_room_for_the_tree(self):
        """El tope acota lo que UN click camina, no lo que camina cada
        fuente por su cuenta."""
        self._store([_analysis(['e2e4']), _analysis(['d2d4']),
                     _analysis(['g1f3'])])
        for uci in ('e2e4', 'd2d4', 'g1f3'):
            _covered(logic.apply_move(logic.start_fen(), uci))
        forgotten = self._seeded_child('h2h3', 400)

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 0)
        self.assertEqual(queued.detail['lines_tried'], 3)
        self.assertFalse(
            AnalysisTask.objects.filter(position_id=forgotten.key).exists())

    def test_what_the_tree_knows_nothing_about_is_not_a_candidate(self):
        """La frontera entre los dos botones: esto verifica lo que ya tiene
        valor, y comprar anchura a ciegas es el de al lado."""
        self._store([_analysis(['e2e4'])])
        _covered(logic.apply_move(logic.start_fen(), 'e2e4'))
        blank = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'a2a3'))
        Edge.objects.create(parent=self.root, move_uci='a2a3', child=blank)
        closed = self._seeded_child('h2h3', 400)
        Position.objects.filter(key=closed.key).update(
            status='DRAW', closure='MINIMAX')

        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual(queued, 0)
        self.assertEqual(AnalysisTask.objects.filter(
            state=AnalysisTask.TState.PENDING).count(), 0)

    def test_black_to_move_ranks_the_lowest_white_eval_first(self):
        """El orden es el de la tabla del explorador: mejor PARA EL QUE MUEVE.
        En blanco-POV eso es el numero mas bajo cuando mueven las negras."""
        after_e4 = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        for uci, ev in (('e7e5', 30), ('c7c5', -120)):
            child = ingest.get_or_create_position(
                logic.apply_move(after_e4.fen, uci))
            Position.objects.filter(key=child.key).update(eval_cp=ev)
            Edge.objects.create(parent=after_e4, move_uci=uci, child=child)

        self.assertEqual(ingest._verify_tree_moves(after_e4, set()),
                         ['c7c5', 'e7e5'])


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
        self.assertEqual(response.json(), {
            'status': 'queued', 'queued': 4, 'line': 1, 'from_tree': False,
            'plies': 4, 'covered_plies': 0,
            'message': 'Queued 128.0M nodes down line 1, starting after e4 '
                       '(4 positions in all).'})
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

        self.assertEqual(response.json(), {
            'status': 'nothing-to-do', 'queued': 0, 'line': 1,
            'from_tree': False, 'plies': 4, 'covered_plies': 0,
            'message': 'Line 1 is already queued; 4 positions on it are '
                       'still in flight.'})

    def test_the_receipt_names_the_line_the_plies_and_what_it_bought(self):
        """La otra mitad de lo que pidio Wolfram: "tell user directly after
        pressing the button what it actually did".  Una linea cubierta y una
        compra por debajo se veian igual desde fuera — las dos, un numero."""
        self.root.last_analysis = [_analysis(['e2e4']),
                                   _analysis(['d2d4', 'd7d5'])]
        self.root.save(update_fields=['last_analysis'])
        _covered(logic.apply_move(logic.start_fen(), 'e2e4'))

        data = self.client.post(self.url).json()

        self.assertEqual(data['status'], 'queued')
        self.assertEqual((data['line'], data['queued']), (2, 2))
        self.assertEqual(data['message'],
                         'Queued 128.0M nodes down line 2, starting after d4 '
                         '(2 positions in all).')

    def test_a_purchase_below_verified_ground_says_how_deep_it_walked(self):
        self.root.last_analysis = [_analysis(['e2e4', 'e7e5'])]
        self.root.save(update_fields=['last_analysis'])
        _covered(logic.apply_move(logic.start_fen(), 'e2e4'),
                 own_pv=['c7c5'])

        data = self.client.post(self.url).json()

        self.assertEqual((data['line'], data['covered_plies']), (1, 1))
        self.assertEqual(data['message'],
                         'Line 1 was already verified 1 ply down; queued '
                         '128.0M nodes below that, after ...c5.')

    def test_with_nothing_to_verify_the_receipt_says_that_much(self):
        self.root.last_analysis = [_analysis(['e2e4'])]
        self.root.save(update_fields=['last_analysis'])
        _covered(logic.apply_move(logic.start_fen(), 'e2e4'))

        data = self.client.post(self.url).json()

        self.assertEqual(data['status'], 'nothing-to-do')
        self.assertEqual(data['message'],
                         'Everything this button can verify is already '
                         'analysed: line 1 covered 1 ply down.')

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


# Cuatro lineas de cuatro plies, con primera jugada distinta cada una: asi una
# tarea comprada dice por si sola QUE linea se camino, sin desempates.
SHOWCASE = [
    ['e2e4', 'e7e5', 'g1f3', 'b8c6'],
    ['d2d4', 'd7d5', 'c2c4', 'e7e6'],
    ['g1f3', 'g8f6', 'c2c4', 'e7e6'],
    ['c2c4', 'e7e5', 'b1c3', 'g8f6'],
]


class ChosenLineTests(TestCase):
    """Verificar LA LINEA QUE SE PIDE, no la que le toque al recorrido.

    Reporte de comunidad (Wolfram, 14-ago): "verify PV still completely useless
    for requesting other lines in PV than second - it always tries to request
    first line".  Y era literal: el click caia por la lista de candidatos y
    paraba en el PRIMERO con hueco, asi que la unica forma de llegar a la 2 era
    que la 1 estuviera entera, y a la 3 no se llegaba casi nunca.  El paseo
    estaba bien; lo que faltaba era poder nombrar por donde.
    """

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.root.last_analysis = [_analysis(pv) for pv in SHOWCASE]
        self.root.save(update_fields=['last_analysis'])
        self.root.refresh_from_db()

    def _first_key(self, index):
        """La clave del hijo que abre la linea ``index`` del escaparate."""
        return logic.key_of(_line_fens(SHOWCASE[index - 1])[0])

    def _bought(self):
        """Que lineas del escaparate compro el click, por su numero.

        Cuenta lo PENDIENTE y no toda tarea: el terreno ya verificado de un
        fixture llega con su tarea COMPLETADA, y eso es lo que el paseo cruza
        sin gastar, no lo que acaba de comprar.
        """
        keys = set(AnalysisTask.objects
                   .filter(state=AnalysisTask.TState.PENDING)
                   .values_list('position_id', flat=True))
        return {index for index in range(1, len(SHOWCASE) + 1)
                if self._first_key(index) in keys}

    def test_the_third_line_of_the_showcase_is_the_one_that_gets_bought(self):
        queued = ingest.enqueue_pv_verification(self.root, line=3)

        self.assertEqual(queued, 4)
        self.assertEqual(queued.detail['line'], 3)
        self.assertFalse(queued.detail['from_tree'])
        # La prueba de que fue ESA: la linea 3 entera comprada y las otras
        # intactas.  Con el bug las cuatro tareas caian en la linea 1.
        self.assertEqual(self._bought(), {3})
        for key in [logic.key_of(fen) for fen in _line_fens(SHOWCASE[2])]:
            self.assertTrue(AnalysisTask.objects.filter(
                position_id=key, state=AnalysisTask.TState.PENDING).exists(),
                key)

    def test_the_first_line_still_answers_to_its_own_number(self):
        queued = ingest.enqueue_pv_verification(self.root, line=1)

        self.assertEqual((queued, queued.detail['line']), (4, 1))
        self.assertEqual(self._bought(), {1})

    def test_the_default_click_keeps_falling_to_the_nearest_gap(self):
        """Elegir es lo NUEVO, no lo obligatorio: sin numero el boton compra lo
        mismo que compraba antes de que se pudiera elegir."""
        queued = ingest.enqueue_pv_verification(self.root)

        self.assertEqual((queued, queued.detail['line']), (4, 1))
        self.assertEqual(self._bought(), {1})

    def test_a_chosen_line_does_not_fall_through_to_the_next_one(self):
        """Quien nombra la 2 no pidio "la que sea": si esa no tiene hueco la
        respuesta es que no lo tiene, no una factura por la 3."""
        for fen in _line_fens(SHOWCASE[1]):
            _covered(fen)

        queued = ingest.enqueue_pv_verification(self.root, line=2)

        self.assertEqual(queued, 0)
        self.assertEqual(queued.detail['line'], 2)
        self.assertEqual(queued.detail['covered_plies'], 4)
        self.assertEqual(self._bought(), set())

    def test_a_line_the_walk_does_not_offer_buys_nothing(self):
        """La cuarta esta en el escaparate pero no en la lista que se camina
        (``PV_VERIFY_MAX_LINES``), asi que pedirla es pedir lo que no hay."""
        self.assertEqual(len(SHOWCASE), ingest.PV_VERIFY_MAX_LINES + 1)

        queued = ingest.enqueue_pv_verification(self.root, line=4)

        self.assertEqual(queued, 0)
        self.assertTrue(queued.detail['unknown_line'])
        self.assertEqual(AnalysisTask.objects.count(), 0)

    def test_an_impossible_number_is_refused_and_not_rounded(self):
        """Redondear al vecino mas cercano seria reproducir el bug por otra
        puerta: pedir una linea y pagar otra."""
        for line in (99, 7):
            queued = ingest.enqueue_pv_verification(self.root, line=line)

            self.assertEqual(queued, 0, line)
            self.assertTrue(queued.detail['unknown_line'], line)
        self.assertEqual(AnalysisTask.objects.count(), 0)

    def test_a_tree_candidate_answers_to_its_number_too(self):
        """Los hijos que el escaparate vigente olvido se numeran detras de las
        lineas (§ ingest._verify_candidates) y se eligen igual que ellas."""
        root = ingest.get_or_create_position(logic.start_fen())
        root.last_analysis = [_analysis(['e2e4'])]
        root.save(update_fields=['last_analysis'])
        root.refresh_from_db()
        forgotten = ingest.get_or_create_position(
            logic.apply_move(root.fen, 'h2h3'))
        Position.objects.filter(key=forgotten.key).update(eval_cp=40)
        Edge.objects.get_or_create(parent=root, move_uci='h2h3',
                                   defaults={'child': forgotten})

        queued = ingest.enqueue_pv_verification(root, line=2)

        self.assertEqual(queued, 1)
        self.assertEqual((queued.detail['line'], queued.detail['from_tree']),
                         (2, True))
        self.assertTrue(AnalysisTask.objects.filter(
            position_id=forgotten.key).exists())
        # Y la 1, que tambien tenia hueco, se queda donde estaba.
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=logic.key_of(_line_fens(['e2e4'])[0])).exists())


class ChosenLineEndpointTests(TestCase):
    """El numero elegido tiene que LLEGAR: plantilla, POST, paseo."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.root.last_analysis = [_analysis(pv) for pv in SHOWCASE]
        self.root.save(update_fields=['last_analysis'])
        self.url = f'/atomicdb/pv-verify/{self.root.key}/'

    def _first_key(self, index):
        return logic.key_of(_line_fens(SHOWCASE[index - 1])[0])

    def test_the_post_walks_the_line_it_was_given(self):
        data = self.client.post(self.url, {'line': '3'}).json()

        self.assertEqual((data['status'], data['line'], data['queued']),
                         ('queued', 3, 4))
        self.assertEqual(data['message'],
                         'Queued 128.0M nodes down line 3, starting after '
                         'Nf3 (4 positions in all).')
        self.assertTrue(AnalysisTask.objects.filter(
            position_id=self._first_key(3)).exists())
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=self._first_key(1)).exists())

    def test_line_one_goes_through_the_same_door(self):
        data = self.client.post(self.url, {'line': '1'}).json()

        self.assertEqual((data['status'], data['line'], data['queued']),
                         ('queued', 1, 4))
        self.assertTrue(AnalysisTask.objects.filter(
            position_id=self._first_key(1)).exists())
        self.assertFalse(AnalysisTask.objects.filter(
            position_id=self._first_key(3)).exists())

    def test_a_line_out_of_range_is_refused_without_buying_anything(self):
        response = self.client.post(self.url, {'line': '4'})

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual((data['status'], data['queued']),
                         ('unknown-line', 0))
        self.assertEqual(data['message'],
                         'Line 4 is not one of the lines stored here right '
                         'now, so nothing was queued. Reload the page to see '
                         'the current list.')
        self.assertEqual(AnalysisTask.objects.count(), 0)

    def test_a_line_that_is_not_a_number_is_refused_and_not_ignored(self):
        """Degradarlo a la caida automatica compraria la linea 1 en silencio
        despues de que alguien pidiera otra: el fallo se dice donde ocurre."""
        for raw in ('two', '-1', '3.5'):
            response = self.client.post(self.url, {'line': raw})

            self.assertEqual(response.status_code, 400, raw)
            self.assertEqual(response.json()['status'], 'unknown-line', raw)
        self.assertEqual(AnalysisTask.objects.count(), 0)

    def test_a_chosen_line_already_covered_says_so_by_its_own_number(self):
        """Y no "line 1", que es lo que decia el aviso cuando el numero de la
        elegida no viajaba con el."""
        for fen in _line_fens(SHOWCASE[1]):
            _covered(fen)

        data = self.client.post(self.url, {'line': '2'}).json()

        self.assertEqual(data['status'], 'nothing-to-do')
        self.assertEqual(data['message'],
                         'Everything this button can verify is already '
                         'analysed: line 2 covered 4 plies down.')

    def test_a_post_without_the_field_is_the_click_of_always(self):
        data = self.client.post(self.url).json()

        self.assertEqual((data['status'], data['line']), ('queued', 1))
        self.assertTrue(AnalysisTask.objects.filter(
            position_id=self._first_key(1)).exists())

    def test_the_declared_route_of_each_task_goes_down_the_chosen_line(self):
        """La otra prueba de que se camino ESA: la ruta que hereda cada tarea.

        No es adorno.  El aviso de cada nodo comprado vuelve contando SU linea
        de jugadas, asi que si el numero elegido no llegara hasta aqui el
        visitante veria la linea 3 pedida y la 1 escrita en el recibo.
        """
        root = ingest.get_or_create_position(logic.start_fen())
        parent = ingest.get_or_create_position(
            logic.apply_move(root.fen, 'e2e4'))
        Edge.objects.get_or_create(parent=root, move_uci='e2e4',
                                   defaults={'child': parent})
        # Tres lineas desde ahi, cada una con su primera jugada.
        pvs = [['e7e5', 'g1f3'], ['c7c5', 'g1f3'], ['e7e6', 'd2d4']]
        parent.last_analysis = [_analysis(pv) for pv in pvs]
        parent.save(update_fields=['last_analysis'])

        response = self.client.post(f'/atomicdb/pv-verify/{parent.key}/',
                                    {'route': 'e2e4', 'line': '3'})

        self.assertEqual(response.json()['line'], 3)
        routes = sorted(AnalysisTask.objects.values_list('route', flat=True))
        self.assertEqual(routes, ['e2e4,e7e6', 'e2e4,e7e6,d2d4'])


class PvLineSelectorTests(TestCase):
    """Lo que la pagina OFRECE, con los numeros que el paseo va a honrar."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())

    def _page(self):
        return self.client.get(f'/atomicdb/explore/{self.root.key}/')

    def _store(self, lines):
        self.root.last_analysis = lines
        self.root.save(update_fields=['last_analysis'])
        self.root.refresh_from_db()

    def test_the_selector_offers_the_walkable_lines_by_their_move(self):
        self._store([_analysis(pv) for pv in SHOWCASE])

        page = self._page()

        offered = page.context['pv_verify_lines']
        self.assertEqual([(cand['index'], cand['label']) for cand in offered],
                         [(1, 'e4'), (2, 'd4'), (3, 'Nf3')])
        self.assertContains(page, '<option value="3">3. Nf3</option>',
                            html=True)
        # La cuarta esta en el escaparate y no en la lista: el tope de lo que
        # un click camina es el mismo que el de lo que se puede pedir.
        self.assertNotContains(page, '4. c4')

    def test_the_offer_names_a_tree_candidate_as_such(self):
        self._store([_analysis(LINE)])
        forgotten = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'h2h3'))
        Position.objects.filter(key=forgotten.key).update(eval_cp=40)
        Edge.objects.get_or_create(parent=self.root, move_uci='h2h3',
                                   defaults={'child': forgotten})

        offered = self._page().context['pv_verify_lines']

        self.assertEqual([(cand['index'], cand['label']) for cand in offered],
                         [(1, 'e4'), (2, 'h3 (from the tree)')])

    def test_a_single_candidate_is_not_a_choice_and_gets_no_control(self):
        self._store([_analysis(LINE)])

        page = self._page()

        self.assertEqual(len(page.context['pv_verify_lines']), 1)
        self.assertContains(page, 'Verify PV')
        self.assertNotContains(page, 'id="pvline"')

    def test_without_a_button_nothing_is_offered(self):
        page = self._page()

        self.assertEqual(page.context['pv_verify_lines'], [])
        self.assertNotContains(page, 'id="pvline"')


# Un mate en UNO de atomic: la torre captura en e7 y la explosion se lleva el
# peon capturado, la propia torre y todo lo que no sea peon alrededor —
# incluido el rey negro de e8.  El hijo NACE cerrado, porque
# ``get_or_create_position`` le pregunta a ``logic.terminal_status`` al
# materializarlo: el paseo no tiene que PROBAR nada aqui, solo enterarse.
MATE_PARENT_FEN = '4k3/4p3/8/8/8/8/4R3/4K3 w - - 0 1'
MATE_MOVE = 'e2e7'


class PvWalkDiscoversATerminalTests(TestCase):
    """Caminar una PV que acaba en mate CIERRA el nodo que la jugo.

    Es la misma regla que el ``goto`` del explorador aprendio el 29-jul
    ("navigating onto a terminal closes the parent at once"), y este modulo la
    prometia por escrito — "una PV es una ruta como cualquier otra y tiene que
    producir exactamente el mismo arbol que producirla a mano" — sin
    cumplirla: el paseo materializaba el terminal y seguia, y el padre se
    quedaba UNKNOWN con el mate en uno ya escrito en la tabla de al lado.
    Medido el 20-ago en produccion, 648 posiciones asi, con el respaldo ya en
    +-100 y prioridad de banda de mate: el sitio pintaba "sin resolver" sobre
    un nodo cuya respuesta ya estaba dentro, y el selector seguia comprandolo.
    """

    def _parent(self, pv, fen=MATE_PARENT_FEN):
        pos = ingest.get_or_create_position(fen)
        Position.objects.filter(key=pos.key).update(last_analysis=[
            _analysis(pv)])
        return Position.objects.get(key=pos.key)

    def test_the_last_ply_of_a_mate_pv_closes_the_node_that_played_it(self):
        parent = self._parent([MATE_MOVE])

        ingest.enqueue_pv_verification(parent, requested_by='ana')

        child_key = logic.key_of(logic.apply_move(MATE_PARENT_FEN, MATE_MOVE))
        child = Position.objects.get(key=child_key)
        self.assertEqual(child.status, 'WHITE_WIN')
        self.assertEqual(child.closure, 'TERMINAL')
        # LO QUE FALTABA: el padre.  La arista existia, el hijo estaba
        # cerrado, y el nodo de arriba seguia diciendo UNKNOWN.
        parent = Position.objects.get(key=parent.key)
        self.assertEqual(parent.status, 'WHITE_WIN')
        self.assertEqual(parent.best_move, MATE_MOVE)

    def test_the_value_rides_up_with_the_status_and_not_instead_of_it(self):
        """El respaldo ya subia solo; era el hecho exacto el que no."""
        parent = self._parent([MATE_MOVE])

        ingest.enqueue_pv_verification(parent)

        parent = Position.objects.get(key=parent.key)
        self.assertEqual(parent.status, 'WHITE_WIN')
        self.assertNotEqual(parent.closure, None)

    def test_a_parent_already_closed_does_not_pay_for_the_cascade(self):
        parent = self._parent([MATE_MOVE])
        Position.objects.filter(key=parent.key).update(
            status='WHITE_WIN', closure='MATE_PV', proof='ENGINE')
        parent = Position.objects.get(key=parent.key)

        with patch.object(ingest, 'backup_cascade') as cascade:
            ingest.enqueue_pv_verification(parent)

        cascade.assert_not_called()

    def test_a_walk_over_open_ground_never_touches_the_cascade(self):
        """El camino comun no paga nada: la guarda solo mira cierres."""
        root = ingest.get_or_create_position(logic.start_fen())
        Position.objects.filter(key=root.key).update(
            last_analysis=[_analysis(LINE)])
        root = Position.objects.get(key=root.key)

        with patch.object(ingest, 'backup_cascade') as cascade:
            ingest.enqueue_pv_verification(root)

        cascade.assert_not_called()

