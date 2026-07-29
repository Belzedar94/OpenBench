"""El explorador que se refuta a si mismo, y la medida honesta de si sirve.

EL HALLAZGO QUE LO PIDIO (Wolfram, comunidad, 28-jul).  Comparando lineas
exploradas a mano contra lo que dejaba el selector automatico salio una
diferencia que no es de gusto: las humanas acumulan ``dn`` ALTO — quien explora
a mano mira las respuestas del rival, y cada respuesta mirada es una via mas de
refutacion que habria que cerrar — mientras el selector deja ESPINAS con
``dn`` 1: afirmaciones a UNA pregunta sin responder de derrumbarse.

Este modulo defiende tres cosas y ninguna es un numero de politica:

  1. que el FRENTE DE PRUEBA tiene una definicion operativa y que las metricas
     se calculan sobre ella y no sobre "el arbol", que no significa nada;
  2. que un cierre sabe DE QUIEN fue — el selector, un completado de
     cobertura, el click de un visitante o un certificado — porque despues no
     es derivable: los cuatro caminos escriben las mismas filas;
  3. que los dos brazos adversariales estan ACOTADOS, van detras de lo urgente
     y no inventan semantica nueva (un ``DISPROVED`` sigue siendo advisory).

Los umbrales son constantes parcheables y los tests los parchean: lo que se
fija aqui es el mecanismo.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from . import ingest, ingest_queue, logic, metrics, proof, solve
from .management.commands.capture_atomicdb_progress import capture_progress
from .models import (AnalysisTask, DBEvent, Edge, IngestJob, Position,
                     ProgressSnapshot, ProofCampaign, ProofNode, RequestLog,
                     SolveTask)
from .test_proofs import FORCED_MATE_FEN, FORCED_MATE_PV
from .test_solve import MATE_IN_ONE_CERT, MATE_IN_ONE_FEN
from .testing import TestCase


def _and_node(fen=None):
    """Un nodo AND real del frente: mueven negras y sus replicas existen.

    Se construye con el codigo de produccion — ``expand`` escribe la lista
    legal entera y ``refresh_proof_numbers`` calcula los pn/dn con las mismas
    recurrencias que la cascada — porque un ``ProofNode`` inventado a mano no
    prueba nada sobre el selector que lo lee.
    """
    fen = fen or logic.apply_move(logic.start_fen(),
                                  logic.legal_moves(logic.start_fen())[0])
    node = ingest.get_or_create_position(fen)
    ingest.expand(node)
    proof.refresh_proof_numbers([node.key])
    return node


def _campaign():
    return proof.default_campaign()


class ProofFrontierTests(TestCase):
    """Que ES el frente. Sin esto las tres metricas no dicen nada."""

    def test_an_expanded_defender_node_is_an_open_obligation(self):
        node = _and_node()
        rows = proof.frontier_and_rows(_campaign())
        self.assertIn(node.key, [row[0] for row in rows])

    def test_a_closed_node_is_a_leaf_of_the_proof_not_frontier(self):
        node = _and_node()
        node.status = 'WHITE_WIN'
        node.closure = 'MATE_PV'
        node.proof = 'ANDOR'
        node.save()

        rows = proof.frontier_rows(_campaign())

        self.assertNotIn(node.key, [row[0] for row in rows])

    def test_a_refuted_node_stops_being_an_obligation(self):
        """pn infinito = el objetivo esta refutado ahi. No queda que estimar."""
        node = _and_node()
        ProofNode.objects.filter(campaign=_campaign(), position=node).update(
            pn=proof.PROOF_INFINITY)

        rows = proof.frontier_rows(_campaign())

        self.assertNotIn(node.key, [row[0] for row in rows])

    def test_an_attacker_node_is_not_an_obligation(self):
        """En un nodo OR basta UNA jugada: su dn bajo no es una mala noticia."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        proof.refresh_proof_numbers([root.key])

        rows = proof.frontier_and_rows(_campaign())

        self.assertNotIn(root.key, [row[0] for row in rows])

    def test_the_median_is_a_median(self):
        """Una mediana, no una media: un saturado no tapa mil espinas."""
        campaign = _campaign()
        keys = []
        for uci in logic.legal_moves(logic.start_fen())[:5]:
            node = _and_node(logic.apply_move(logic.start_fen(), uci))
            keys.append(node.key)
        for dn, key in zip((1, 1, 7, 900, 1_000), keys):
            ProofNode.objects.filter(campaign=campaign,
                                     position_id=key).update(dn=dn)

        stats = proof.frontier_dn_stats(campaign, floor=2)

        self.assertEqual(stats['and_nodes'], 5)
        self.assertEqual(stats['dn_median'], 7)
        self.assertEqual(stats['thin'], 2)

    def test_an_empty_frontier_reports_zeroes_not_a_gap(self):
        ProofCampaign.objects.update(active=False)
        self.assertIsNone(proof.frontier_dn_headline(floor=2))

    def test_the_scan_is_bounded(self):
        """El frente se lee acotado: es una portada, no un informe."""
        for uci in logic.legal_moves(logic.start_fen())[:4]:
            _and_node(logic.apply_move(logic.start_fen(), uci))

        rows = proof.frontier_rows(_campaign(), limit=2)

        self.assertEqual(len(rows), 2)


class ClosureAttributionTests(TestCase):
    """De QUIEN fue cada cierre. Se registra o se pierde."""

    def _mate_job(self, source):
        """Un cierre REAL por la cola durable, que es como ocurren.

        No se fabrica el evento: se monta la tarea con su procedencia, se deja
        el payload en la cola y se aplica.  El cierre cae sobre el HIJO, dos
        llamadas por debajo de donde se conoce la fuente, que es exactamente
        el caso que la etiqueta existe para cubrir.
        """
        pos = ingest.get_or_create_position(FORCED_MATE_FEN)
        task = AnalysisTask.objects.create(
            position=pos, generation=0, budget_nodes=1_000, source=source)
        job = IngestJob.objects.create(
            task=task, position=pos,
            payload={'lines': [{'move': FORCED_MATE_PV[0], 'eval_cp': 9998,
                                'mate': 2, 'pv': FORCED_MATE_PV}],
                     'nodes': 1_000})
        ingest_queue.apply_job(job)
        return DBEvent.objects.filter(kind='NODE_CLOSED').first()

    def test_a_selector_closure_is_stamped_auto(self):
        event = self._mate_job(AnalysisTask.Source.AUTO)
        self.assertEqual(event.payload['source'], AnalysisTask.Source.AUTO)

    def test_a_coverage_closure_is_stamped_fill(self):
        event = self._mate_job(AnalysisTask.Source.FILL)
        self.assertEqual(event.payload['source'], AnalysisTask.Source.FILL)

    def test_a_visitor_closure_is_stamped_user(self):
        event = self._mate_job(AnalysisTask.Source.USER)
        self.assertEqual(event.payload['source'], AnalysisTask.Source.USER)

    def test_a_certificate_closure_is_stamped_solve(self):
        child = ingest.get_or_create_position(MATE_IN_ONE_FEN)
        task = SolveTask.objects.create(position=child, goal='WHITE_WIN',
                                        budget_nodes=1_000, state='LEASED',
                                        machine='m1')

        ingest.apply_solve_result(
            task, outcome='PROVED',
            certificate_blob=solve.compress(MATE_IN_ONE_CERT),
            searched_nodes=10, elapsed_seconds=1.0)

        event = DBEvent.objects.filter(kind='NODE_CLOSED',
                                       payload__key=child.key).get()
        self.assertEqual(event.payload['source'],
                         ingest.CLOSURE_SOURCE_SOLVE)

    def test_a_certificate_closure_emits_one_event_not_two(self):
        """El KPI de "cierres en 24h" contaba doble cada cierre por SOLVE.

        Este camino creaba SU ``NODE_CLOSED`` y ademas llamaba al emisor
        comun, que creaba otro.  Con una sola puerta el conteo vuelve a ser un
        conteo, y el payload rico (nodos del certificado, profundidad) se
        conserva porque viaja como ``extra``.
        """
        child = ingest.get_or_create_position(MATE_IN_ONE_FEN)
        task = SolveTask.objects.create(position=child, goal='WHITE_WIN',
                                        budget_nodes=1_000, state='LEASED',
                                        machine='m1')

        ingest.apply_solve_result(
            task, outcome='PROVED',
            certificate_blob=solve.compress(MATE_IN_ONE_CERT),
            searched_nodes=10, elapsed_seconds=1.0)

        events = DBEvent.objects.filter(kind='NODE_CLOSED',
                                        payload__key=child.key)
        self.assertEqual(events.count(), 1)
        self.assertIn('certificate_nodes', events.get().payload)

    def test_a_closure_nobody_asked_for_says_so(self):
        """Un cierre de mantenimiento no tiene tarea detras: NONE es verdad."""
        pos = ingest.get_or_create_position('4k3/8/8/8/8/8/8/R3K3 w - - 0 1')
        with patch('atomicdb.ingest.tb.probe_wdl', return_value=2):
            self.assertTrue(ingest.close_by_tb(pos.key, 2, dtz=12))

        event = DBEvent.objects.filter(kind='NODE_CLOSED',
                                       payload__key=pos.key).get()
        self.assertEqual(event.payload['source'], ingest.CLOSURE_SOURCE_NONE)
        # Regresion del embudo: el evento TB conserva su DTZ, que es lo que
        # lee el backfill de clock_slack.
        self.assertEqual(event.payload['dtz'], 12)
        self.assertEqual(event.payload['closure'], 'TB')

    def test_the_label_is_restored_even_when_the_apply_explodes(self):
        with self.assertRaises(RuntimeError):
            with ingest.closure_attribution('USER'):
                raise RuntimeError('boom')
        self.assertEqual(ingest.current_closure_source(),
                         ingest.CLOSURE_SOURCE_NONE)

    def test_an_unknown_label_degrades_to_none_instead_of_inventing_one(self):
        with ingest.closure_attribution('ROBOT'):
            self.assertEqual(ingest.current_closure_source(),
                             ingest.CLOSURE_SOURCE_NONE)

    def test_totals_ignore_closures_from_before_the_label_existed(self):
        """Un contador que mintiera para cuadrar seria peor que uno honesto."""
        pos = ingest.get_or_create_position(FORCED_MATE_FEN)
        DBEvent.objects.create(kind='NODE_CLOSED', payload={
            'key': pos.key, 'status': 'WHITE_WIN', 'closure': 'MATE_PV'})

        totals = metrics.closure_attribution_totals()

        self.assertEqual(sum(totals.values()), 0)
        window = metrics.closure_attribution_window(hours=24)
        self.assertEqual(window['stamped'], 0)
        self.assertEqual(window['total'], 1)


class HumanCloseLatencyTests(TestCase):

    def _asked_and_closed(self, fen, asked_at, closed_at):
        pos = ingest.get_or_create_position(fen)
        log = RequestLog.objects.create(ip='127.0.0.1', position=pos)
        RequestLog.objects.filter(pk=log.pk).update(created=asked_at)
        event = DBEvent.objects.create(kind='NODE_CLOSED', payload={
            'key': pos.key, 'status': 'WHITE_WIN', 'closure': 'MATE_PV',
            'source': 'USER'})
        DBEvent.objects.filter(pk=event.pk).update(ts=closed_at)
        return pos

    def test_the_wait_is_measured_from_the_first_request(self):
        now = timezone.now()
        self._asked_and_closed(FORCED_MATE_FEN,
                               now - timezone.timedelta(hours=3),
                               now - timezone.timedelta(hours=1))

        latency = metrics.human_close_latency(now=now)

        self.assertEqual(latency['samples'], 1)
        self.assertEqual(latency['median_seconds'], 2 * 3600)

    def test_a_position_asked_for_after_it_closed_is_not_a_wait(self):
        now = timezone.now()
        self._asked_and_closed(FORCED_MATE_FEN, now,
                               now - timezone.timedelta(hours=1))

        self.assertEqual(metrics.human_close_latency(now=now)['samples'], 0)

    def test_a_closure_nobody_asked_for_is_not_in_the_median(self):
        now = timezone.now()
        pos = ingest.get_or_create_position(FORCED_MATE_FEN)
        DBEvent.objects.create(kind='NODE_CLOSED', payload={'key': pos.key})

        self.assertEqual(metrics.human_close_latency(now=now)['samples'], 0)

    def test_the_window_forgets_old_waits(self):
        now = timezone.now()
        self._asked_and_closed(FORCED_MATE_FEN,
                               now - timezone.timedelta(days=40),
                               now - timezone.timedelta(days=30))

        self.assertEqual(metrics.human_close_latency(now=now)['samples'], 0)


class DnRepairTests(TestCase):
    """El brazo adversarial: comprar las replicas que nadie miro."""

    def test_a_thin_obligation_gets_its_unlooked_replies_bought(self):
        node = _and_node()
        row = ProofNode.objects.get(campaign=_campaign(), position=node)
        self.assertLessEqual(row.dn, ingest.DN_REPAIR_FLOOR)

        made = ingest.enqueue_dn_repair()

        self.assertEqual(made, ingest.DN_REPAIR_REPLIES)
        tasks = AnalysisTask.objects.filter(source=AnalysisTask.Source.FILL)
        self.assertEqual(tasks.count(), ingest.DN_REPAIR_REPLIES)
        bought = set(tasks.values_list('position_id', flat=True))
        replies = set(Edge.objects.filter(parent=node).values_list(
            'child_id', flat=True))
        self.assertTrue(bought <= replies)

    def test_the_floor_is_what_decides(self):
        """Con el suelo por debajo del dn del nodo, no hay nada que reparar.

        El mismo nodo, la misma cola: lo unico que cambia es el umbral, que
        es como se comprueba que el umbral es lo que manda y no un efecto
        lateral de otra cosa.
        """
        _and_node()

        self.assertEqual(ingest.enqueue_dn_repair(floor=0), 0)
        self.assertEqual(ingest.enqueue_dn_repair(floor=ingest.DN_REPAIR_FLOOR),
                         ingest.DN_REPAIR_REPLIES)

    def test_an_attacker_node_is_never_repaired(self):
        """Una alternativa blanca es OPCIONAL: su dn bajo no es una espina."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        proof.refresh_proof_numbers([root.key])

        ingest.enqueue_dn_repair()

        for task in AnalysisTask.objects.filter(
                source=AnalysisTask.Source.FILL):
            self.assertFalse(Edge.objects.filter(
                parent=root, child=task.position).exists())

    def test_an_unexpanded_node_is_not_a_coverage_claim(self):
        """Sin movegen corrido, "las replicas que faltan" no significa nada."""
        node = _and_node()
        Position.objects.filter(key=node.key).update(expanded=False)

        self.assertEqual(ingest.enqueue_dn_repair(), 0)

    def test_a_reply_the_tree_already_judged_is_not_bought_again(self):
        node = _and_node()
        for edge in Edge.objects.filter(parent=node):
            Position.objects.filter(key=edge.child_id).update(eval_cp=-120)

        self.assertEqual(ingest.enqueue_dn_repair(), 0)

    def test_the_batch_is_bounded_per_cycle(self):
        for uci in logic.legal_moves(logic.start_fen())[:4]:
            _and_node(logic.apply_move(logic.start_fen(), uci))

        made = ingest.enqueue_dn_repair(per_cycle=2)

        self.assertEqual(made, 2)

    def test_the_number_of_nodes_LOOKED_at_is_bounded_too(self):
        """El coste de una pasada lo fija una constante, no el arbol.

        Un frente con miles de espinas ya miradas no puede convertir un ciclo
        del selector en miles de consultas que acaban sin encolar nada, asi
        que hay tope de nodos EXAMINADOS y no solo de tareas creadas.
        """
        for uci in logic.legal_moves(logic.start_fen())[:4]:
            node = _and_node(logic.apply_move(logic.start_fen(), uci))
            for edge in Edge.objects.filter(parent=node):
                Position.objects.filter(key=edge.child_id).update(eval_cp=-90)
            # Finos pero sin nada que comprar: el peor caso para el coste.
            ProofNode.objects.filter(campaign=_campaign(),
                                     position=node).update(dn=1)

        with patch.object(ingest, 'unexplored_children',
                          return_value=[]) as looked:
            ingest.enqueue_dn_repair(max_nodes=1)

        self.assertEqual(looked.call_count, 1)

    def test_it_shares_the_fill_cap_with_coverage_completion(self):
        """Cada brazo cuenta SU cola, y la suya la cuenta entera.

        Los tres productores de FILL viven en dos procesos: si cada uno lee la
        cola de los otros como si fuera suya, el que llega segundo se encuentra
        el cupo gastado sin haber encolado nada.  Lo que si tiene que frenarle
        es su propio trabajo pendiente.
        """
        node = _and_node()
        other = Edge.objects.filter(parent=node).first().child
        AnalysisTask.objects.create(
            position=other, generation=99, budget_nodes=8_000_000,
            source=AnalysisTask.Source.FILL, arm=ingest.COVERAGE_ARM)

        # La cola de cobertura no le quita el turno...
        self.assertEqual(ingest.enqueue_dn_repair(cap=1), 1)
        # ...pero la suya propia si, en cuanto llega al tope.
        self.assertEqual(ingest.enqueue_dn_repair(cap=1), 0)

    def test_it_never_disguises_itself_as_a_visitor(self):
        _and_node()
        ingest.enqueue_dn_repair()
        self.assertFalse(AnalysisTask.objects.filter(
            source=AnalysisTask.Source.USER).exists())

    def test_the_batch_leaves_a_receipt(self):
        _and_node()

        ingest.enqueue_dn_repair()

        event = DBEvent.objects.filter(kind='DN_REPAIR').get()
        self.assertEqual(event.payload['queued'], ingest.DN_REPAIR_REPLIES)
        self.assertEqual(event.payload['nodes'], 1)
        self.assertEqual(event.payload['floor'], ingest.DN_REPAIR_FLOOR)

    def test_a_quiet_pass_leaves_no_receipt(self):
        self.assertEqual(ingest.enqueue_dn_repair(), 0)
        self.assertFalse(DBEvent.objects.filter(kind='DN_REPAIR').exists())


class FragileMateClaimTests(TestCase):
    """El caso 9994: una afirmacion de mate que nadie verifico por los lados."""

    def _claim(self, backed_eval, cover_everything=False):
        node = _and_node()
        if cover_everything:
            for edge in Edge.objects.filter(parent=node):
                Position.objects.filter(key=edge.child_id).update(eval_cp=-90)
        Position.objects.filter(key=node.key).update(backed_eval=backed_eval)
        return Position.objects.get(key=node.key)

    def test_partial_coverage_is_exactly_what_cuts_the_proof_authority(self):
        """El brazo y ``_backed_for`` leen el MISMO predicado, no una copia."""
        node = self._claim(9_994)
        children = ingest._backed_children_by_parent([node.key])[node.key]
        self.assertTrue(ingest.coverage_is_partial(node, children))

        covered = self._claim(9_994, cover_everything=True)
        children = ingest._backed_children_by_parent([covered.key])[covered.key]
        self.assertFalse(ingest.coverage_is_partial(covered, children))

    def test_the_goal_follows_the_sign_of_the_claim(self):
        """La leccion del piloto: 23 de 36 tareas fueron DISPROVED instantaneos
        porque se preguntaba WHITE_WIN sobre posiciones donde ganaban negras.
        El objetivo sale del SIGNO del valor, siempre."""
        self._claim(-9_994)

        self.assertEqual(ingest.enqueue_fragile_mate_solves(), 1)

        task = SolveTask.objects.get(arm=ingest.FRAGILE_ARM)
        self.assertEqual(task.goal, 'BLACK_WIN')

    def test_a_positive_claim_asks_for_white(self):
        self._claim(9_994)
        ingest.enqueue_fragile_mate_solves()
        self.assertEqual(SolveTask.objects.get(arm=ingest.FRAGILE_ARM).goal,
                         'WHITE_WIN')

    def test_it_asks_at_the_cheap_rung(self):
        self._claim(9_994)
        ingest.enqueue_fragile_mate_solves()
        task = SolveTask.objects.get(arm=ingest.FRAGILE_ARM)
        self.assertEqual(task.budget_stage, 'F0')
        self.assertEqual(task.budget_nodes, ingest.FRAGILE_STAGE_NODES)

    def test_a_fully_covered_claim_is_not_fragile(self):
        self._claim(9_994, cover_everything=True)
        self.assertEqual(ingest.enqueue_fragile_mate_solves(), 0)

    def test_a_claim_below_the_mate_band_is_ordinary_exploration(self):
        self._claim(ingest.MATE_BAND - 1)
        self.assertEqual(ingest.enqueue_fragile_mate_solves(), 0)

    def test_a_closed_position_is_debt_not_a_fragile_claim(self):
        node = self._claim(9_994)
        Position.objects.filter(key=node.key).update(
            status='WHITE_WIN', closure='MATE_PV', proof='ENGINE')

        self.assertEqual(ingest.enqueue_fragile_mate_solves(), 0)

    def test_the_same_question_is_not_asked_twice(self):
        self._claim(9_994)
        self.assertEqual(ingest.enqueue_fragile_mate_solves(), 1)
        self.assertEqual(ingest.enqueue_fragile_mate_solves(), 0)

    def test_a_position_with_a_live_solve_is_left_alone(self):
        node = self._claim(9_994)
        SolveTask.objects.create(position=node, goal='WHITE_WIN',
                                 budget_nodes=100_000_000, state='PENDING')

        self.assertEqual(ingest.enqueue_fragile_mate_solves(), 0)

    def test_its_cap_is_its_own_and_the_debt_queue_cannot_starve_it(self):
        """Compartir cupo: una purga de deuda mataria este brazo con ella."""
        self._claim(9_994)
        for uci in logic.legal_moves(logic.start_fen())[1:4]:
            SolveTask.objects.create(
                position=ingest.get_or_create_position(
                    logic.apply_move(logic.start_fen(), uci)),
                goal='WHITE_WIN', budget_nodes=ingest.DEBT_STAGE_NODES,
                arm=ingest.DEBT_ARM, state='PENDING')

        self.assertEqual(ingest.enqueue_fragile_mate_solves(cap=1), 1)

    def test_the_cap_bounds_the_batch(self):
        for uci in logic.legal_moves(logic.start_fen())[:4]:
            node = _and_node(logic.apply_move(logic.start_fen(), uci))
            Position.objects.filter(key=node.key).update(backed_eval=9_994)

        self.assertEqual(ingest.enqueue_fragile_mate_solves(cap=2), 2)

    def test_the_batch_leaves_a_receipt_with_both_signs(self):
        self._claim(-9_994)

        ingest.enqueue_fragile_mate_solves()

        event = DBEvent.objects.filter(kind='FRAGILE_ENQUEUED').get()
        self.assertEqual(event.payload['created'], 1)
        self.assertEqual(event.payload['black'], 1)
        self.assertEqual(event.payload['white'], 0)

    def test_a_disproved_answer_still_closes_nothing(self):
        """La semantica advisory de doc 18 no se toca desde aqui."""
        node = self._claim(9_994)
        ingest.enqueue_fragile_mate_solves()
        task = SolveTask.objects.get(arm=ingest.FRAGILE_ARM)

        ingest.apply_solve_result(task, outcome='DISPROVED',
                                  trusted_submitter=True)

        node.refresh_from_db()
        self.assertEqual(node.status, 'UNKNOWN')


class AdversarialSwitchTests(TestCase):
    """El "antes" se mide con el codigo desplegado y los brazos quietos."""

    def test_the_arms_are_off_by_default(self):
        self.assertFalse(ingest.adversarial_arms_enabled())

    @override_settings(ATOMICDB_ADVERSARIAL=True)
    def test_the_setting_turns_them_on(self):
        self.assertTrue(ingest.adversarial_arms_enabled())

    def test_the_selector_service_leaves_them_alone_while_it_is_off(self):
        _and_node()

        out = StringIO()
        call_command('refresh_selector', '--no-debt', '--no-coverage',
                     stdout=out)

        self.assertFalse(DBEvent.objects.filter(kind='DN_REPAIR').exists())
        self.assertIn('"adversarial": false', out.getvalue())

    @override_settings(ATOMICDB_ADVERSARIAL=True)
    def test_the_selector_service_runs_both_arms_when_it_is_on(self):
        node = _and_node()
        Position.objects.filter(key=node.key).update(backed_eval=9_994)

        out = StringIO()
        call_command('refresh_selector', '--no-debt', '--no-coverage',
                     stdout=out)

        self.assertTrue(DBEvent.objects.filter(kind='DN_REPAIR').exists())
        self.assertTrue(
            SolveTask.objects.filter(arm=ingest.FRAGILE_ARM).exists())
        self.assertIn('"dn_repair_enqueued"', out.getvalue())

    @override_settings(ATOMICDB_ADVERSARIAL=True)
    def test_a_single_pass_can_veto_them(self):
        _and_node()

        call_command('refresh_selector', '--no-debt', '--no-coverage',
                     '--no-adversarial', stdout=StringIO())

        self.assertFalse(DBEvent.objects.filter(kind='DN_REPAIR').exists())

    def test_a_single_pass_can_force_them(self):
        _and_node()

        call_command('refresh_selector', '--no-debt', '--no-coverage',
                     '--adversarial', stdout=StringIO())

        self.assertTrue(DBEvent.objects.filter(kind='DN_REPAIR').exists())


class SnapshotTests(TestCase):

    def test_the_hourly_capture_records_the_frontier_it_measured(self):
        _and_node()
        expected = proof.frontier_dn_headline(ingest.DN_REPAIR_FLOOR)

        snapshot, created = capture_progress()

        self.assertTrue(created)
        self.assertEqual(snapshot.frontier_and_nodes, expected['and_nodes'])
        self.assertEqual(snapshot.frontier_dn_median, expected['dn_median'])
        self.assertEqual(snapshot.frontier_dn_thin, expected['thin'])

    def test_the_capture_records_the_attribution_as_it_stands(self):
        pos = ingest.get_or_create_position(FORCED_MATE_FEN)
        with ingest.closure_attribution(AnalysisTask.Source.USER):
            ingest._emit_closure_events(pos)

        snapshot, _created = capture_progress()

        self.assertEqual(snapshot.closures_user, 1)
        self.assertEqual(snapshot.closures_auto, 0)

    def test_a_tree_without_a_campaign_records_zeroes_not_a_gap(self):
        ProofCampaign.objects.update(active=False)

        snapshot, _created = capture_progress()

        self.assertEqual(snapshot.frontier_and_nodes, 0)
        self.assertEqual(snapshot.frontier_dn_median, 0)

    def test_the_new_columns_are_append_only_like_the_rest_of_the_row(self):
        snapshot, _created = capture_progress()
        snapshot.frontier_dn_median = 5
        with self.assertRaisesMessage(Exception, 'append-only'):
            snapshot.save()

    def test_the_preview_reads_without_writing_anything(self):
        """Una hora ya capturada no se reescribe. Leerla, si.

        Justo despues de una migracion que anade columnas, la fila de esta
        hora ya existe con ceros en ellas y la garantia append-only impide
        arreglarla.  El preview deja ver los numeros sin tocar la tabla, que
        es lo que hace falta para decidir cuando encender los brazos.
        """
        _and_node()
        out = StringIO()

        call_command('capture_atomicdb_progress', '--preview', stdout=out)

        self.assertFalse(ProgressSnapshot.objects.exists())
        self.assertIn('"preview":true', out.getvalue())
        self.assertIn('"frontier_and_nodes"', out.getvalue())


class HomeKpiTests(TestCase):

    def test_the_frontier_kpi_comes_from_the_last_capture(self):
        _and_node()
        capture_progress()

        body = self.client.get('/atomicdb/').content.decode()

        self.assertIn('median disproof number on the AND frontier', body)
        self.assertIn('one unanswered question from', body)

    def test_the_attribution_percentages_are_over_what_is_labelled(self):
        pos = ingest.get_or_create_position(FORCED_MATE_FEN)
        with ingest.closure_attribution(AnalysisTask.Source.AUTO):
            ingest._emit_closure_events(pos)
        # Un cierre anterior a la etiqueta: entra en el total, en ningun
        # porcentaje, y la portada lo dice en claro.
        DBEvent.objects.create(kind='NODE_CLOSED',
                               payload={'key': pos.key, 'closure': 'MATE_PV'})

        body = self.client.get('/atomicdb/').content.decode()

        self.assertIn('Closures by source', body)
        self.assertIn('1 of 2 labelled', body)
        self.assertIn('AUTO 100.0%', body)

    def test_a_server_with_nothing_measured_yet_shows_no_tiles(self):
        ProgressSnapshot.objects.all().delete()
        DBEvent.objects.filter(kind='NODE_CLOSED').delete()

        body = self.client.get('/atomicdb/').content.decode()

        self.assertNotIn('Proof health', body)
