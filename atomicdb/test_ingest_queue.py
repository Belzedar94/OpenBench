"""Ingesta asincrona: el submit encola y responde, el procesador aplica.

Lo que se defiende aqui:
  * el contrato con el worker no se mueve (Client/atomicdb_worker.py exige un
    2xx con ``ok`` verdadero, y trata cualquier 4xx como definitivo);
  * el resultado por la cola es IDENTICO al del camino sincrono (A/B sobre el
    mismo fixture);
  * un fallo reintenta con backoff y acaba en FAILED con su payload intacto;
  * aplicar dos veces el mismo payload no duplica nada;
  * un lease no puede caducar mientras su payload espera en la cola;
  * una peticion que el pase acaba de dejar sin trabajo se cierra, en vez de
    quedarse PENDING sobre una posicion ya analizada.
"""

import json
from unittest import mock

from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from . import contributors, ingest, ingest_queue, logic
from .models import AnalysisTask, Edge, IngestJob, Position, ProofCampaign
from .testing import TestCase, worker_account


def _worker():
    worker_account('w', 'p')
    return {'username': 'w', 'password': 'p', 'machine': 'm1'}


def _lease(client, auth, **extra):
    payload = dict(auth, worker_build='2026072203',
                   lease_session='session-m1')
    payload.update(extra)
    return client.post('/atomicdb/api/lease', payload).json()


def _analysis_lines(position, value=-320):
    return [{'move': edge.move_uci, 'eval_cp': value, 'pv': [edge.move_uci]}
            for edge in Edge.objects.filter(parent=position)
                                    .order_by('move_uci')[:5]]


# Mate forzado real (el mismo de test_mate_sign): cierra de verdad, sin
# mockear el prover.
FORCED_MATE_FEN = '4p3/8/8/7k/n7/Kp2n3/3p4/1Q6 w - - 0 1'
FORCED_MATE_PV = ['b1g6', 'h5h4', 'g6g4']


class SubmitEnqueueTests(TestCase):

    def setUp(self):
        self.auth = _worker()
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = (Edge.objects.filter(parent=self.root)
                       .order_by('move_uci').first().child)
        ingest.expand(self.target)
        self.task = AnalysisTask.objects.create(
            position=self.target, generation=0, budget_nodes=1_000_000)

    def _submit(self, **extra):
        leased = _lease(self.client, self.auth)['tasks'][0]
        payload = dict(self.auth, task_id=leased['id'],
                       lines=json.dumps(_analysis_lines(self.target)),
                       nodes='900000', elapsed='12.5',
                       lease_token=leased['lease_token'])
        payload.update(extra)
        return self.client.post('/atomicdb/api/submit', payload), leased

    def test_a_submit_acknowledges_and_leaves_the_work_queued(self):
        response, leased = self._submit()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])                 # el contrato del worker
        self.assertTrue(body['summary']['queued'])
        job = IngestJob.objects.get()
        self.assertEqual(job.state, 'PENDING')
        self.assertEqual(job.task_id, leased['id'])
        self.assertEqual(job.position_id, self.target.key)
        self.assertEqual(job.payload['nodes'], 900_000)
        self.assertEqual(job.payload['elapsed'], 12.5)
        self.assertEqual(job.payload['username'], 'w')

    def test_the_submit_itself_does_not_touch_the_tree(self):
        self._submit()

        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 0)
        self.assertIsNone(self.target.eval_cp)

    def test_the_claim_and_the_enqueue_are_one_commit(self):
        # Un lease no puede caducar con el payload esperando: la tarea ya esta
        # COMPLETED, asi que nadie la vuelve a arrendar.
        _response, leased = self._submit()

        task = AnalysisTask.objects.get(id=leased['id'])
        self.assertEqual(task.state, 'COMPLETED')
        self.assertEqual(task.nodes_searched, 900_000)
        self.assertEqual(task.elapsed_seconds, 12.5)
        self.assertTrue(IngestJob.objects.filter(task=task).exists())
        again = _lease(Client(), self.auth, machine='m2',
                       lease_session='session-m2')['tasks']
        self.assertNotIn(leased['id'], [row['id'] for row in again])

    def test_a_replayed_submit_is_still_a_duplicate(self):
        response, leased = self._submit()
        del response

        again = self.client.post('/atomicdb/api/submit', dict(
            self.auth, task_id=leased['id'],
            lines=json.dumps(_analysis_lines(self.target)),
            lease_token=leased['lease_token']))

        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()['dup'])
        self.assertEqual(IngestJob.objects.count(), 1)

    def test_the_definitive_rejections_are_unchanged(self):
        leased = _lease(self.client, self.auth)['tasks'][0]
        cases = (
            ({'machine': 'someone-else'}, 409, 'not-your-lease'),
            ({'lease_token': 'wrong'}, 409, 'stale-lease'),
            ({'lines': 'not json'}, 400, None),
        )
        for extra, status, error in cases:
            with self.subTest(extra=extra):
                payload = dict(self.auth, task_id=leased['id'],
                               lines=json.dumps([]),
                               lease_token=leased['lease_token'])
                payload.update(extra)
                response = self.client.post('/atomicdb/api/submit', payload)
                self.assertEqual(response.status_code, status)
                if error:
                    self.assertEqual(response.json()['error'], error)
        self.assertEqual(IngestJob.objects.count(), 0)


class ProcessorTests(TestCase):

    def setUp(self):
        self.auth = _worker()
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = (Edge.objects.filter(parent=self.root)
                       .order_by('move_uci').first().child)
        ingest.expand(self.target)
        self.task = AnalysisTask.objects.create(
            position=self.target, generation=0, budget_nodes=1_000_000)

    def _submit(self):
        leased = _lease(self.client, self.auth)['tasks'][0]
        return self.client.post('/atomicdb/api/submit', dict(
            self.auth, task_id=leased['id'],
            lines=json.dumps(_analysis_lines(self.target)),
            nodes='900000', elapsed='12.5',
            lease_token=leased['lease_token']))

    def test_the_processor_applies_what_the_submit_queued(self):
        self._submit()

        result = ingest_queue.drain()

        self.assertEqual(result, {'done': 1, 'failed': 0})
        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 1)
        self.assertEqual(self.target.eval_cp, -320)
        self.assertEqual(self.target.time_invested, 12.5)
        self.assertEqual(self.target.nodes_invested, 900_000)
        job = IngestJob.objects.get()
        self.assertEqual(job.state, 'DONE')
        self.assertIn('backed_evals', job.summary)

    def test_processing_twice_changes_nothing(self):
        self._submit()
        ingest_queue.drain()
        self.target.refresh_from_db()
        before = (self.target.visits, self.target.nodes_invested,
                  self.target.time_invested, self.target.eval_cp)

        job = IngestJob.objects.get()
        ingest_queue.apply_job(job)         # replay directo
        ingest_queue.drain()                # y por la via normal

        self.target.refresh_from_db()
        self.assertEqual(
            (self.target.visits, self.target.nodes_invested,
             self.target.time_invested, self.target.eval_cp), before)
        self.assertEqual(IngestJob.objects.count(), 1)

    def test_a_failure_retries_with_backoff_and_then_parks(self):
        self._submit()
        boom = mock.patch.object(ingest, 'ingest_analysis',
                                 side_effect=RuntimeError('disk on fire'))
        with boom:
            for attempt in range(1, ingest_queue.MAX_ATTEMPTS + 1):
                job = IngestJob.objects.get()
                IngestJob.objects.filter(pk=job.pk).update(
                    next_attempt_at=timezone.now())
                claimed = ingest_queue.claim_next()
                self.assertIsNotNone(claimed, f'attempt {attempt}')
                ingest_queue.process_job(claimed)

        job = IngestJob.objects.get()
        self.assertEqual(job.state, 'FAILED')
        self.assertEqual(job.attempts, ingest_queue.MAX_ATTEMPTS)
        self.assertIn('disk on fire', job.last_error)
        self.assertTrue(job.payload['lines'])       # el payload, intacto
        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 0)     # y el arbol, sin tocar

    def test_a_failed_job_can_be_requeued_and_then_succeeds(self):
        self._submit()
        with mock.patch.object(ingest, 'ingest_analysis',
                               side_effect=RuntimeError('transient')):
            for _ in range(ingest_queue.MAX_ATTEMPTS):
                IngestJob.objects.update(next_attempt_at=timezone.now())
                ingest_queue.process_job(ingest_queue.claim_next())
        self.assertEqual(IngestJob.objects.get().state, 'FAILED')

        self.assertEqual(ingest_queue.retry_failed(), 1)
        self.assertEqual(ingest_queue.drain(), {'done': 1, 'failed': 0})

        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 1)

    def test_a_backoff_keeps_a_job_out_of_the_queue_until_it_is_due(self):
        self._submit()
        with mock.patch.object(ingest, 'ingest_analysis',
                               side_effect=RuntimeError('transient')):
            ingest_queue.process_job(ingest_queue.claim_next())

        self.assertIsNone(ingest_queue.claim_next())   # aun no toca
        job = IngestJob.objects.get()
        self.assertEqual(job.state, 'PENDING')
        self.assertGreater(job.next_attempt_at, timezone.now())

    def test_a_stale_claim_is_recovered(self):
        self._submit()
        job = ingest_queue.claim_next()
        IngestJob.objects.filter(pk=job.pk).update(
            claimed_at=timezone.now() - timezone.timedelta(
                seconds=ingest_queue.CLAIM_STALE_SECONDS + 60))

        recovered = ingest_queue.claim_next()

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.pk, job.pk)

    def test_the_management_command_drains_and_reports(self):
        self._submit()
        from io import StringIO
        out = StringIO()

        call_command('process_ingest_queue', once=True, stdout=out)

        self.assertEqual(json.loads(out.getvalue().strip()),
                         {'done': 1, 'failed': 0})
        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 1)

    def test_the_management_command_reports_the_queue_depth(self):
        self._submit()
        from io import StringIO
        out = StringIO()

        call_command('process_ingest_queue', status=True, stdout=out)

        self.assertEqual(json.loads(out.getvalue().strip()), {'PENDING': 1})


class AbsorptionTests(TestCase):
    """Una peticion que otra busqueda ya sirvio se CIERRA, no se queda colgada.

    Reporte de comunidad: peticiones que "are stuck forever in your queue on
    profile description... if you click them, position is already analysed".
    Dos tareas sobre la misma posicion y un solo worker que llega: la que no
    llego se quedaba PENDING para siempre y la cola de su autor la seguia
    ensenando como pendiente encima de una posicion ya analizada.

    La cola del perfil se comprueba desde aqui a proposito: es la MISMA
    absorcion vista por el otro lado, y separarlas dejaria la causa y su efecto
    en dos ficheros que pueden dejar de hablarse.
    """

    def setUp(self):
        self.auth = _worker()
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = (Edge.objects.filter(parent=self.root)
                       .order_by('move_uci').first().child)
        ingest.expand(self.target)
        # La que el worker va a servir: 512M, la primera de la cola.
        self.served = self._task(0, 512_000_000)
        # Y las tres que viven a su sombra sobre la MISMA posicion.
        self.shadowed = self._task(1, 128_000_000)
        self.deeper = self._task(2, 10_000_000_000)
        self.running = self._task(3, 128_000_000,
                                  state=AnalysisTask.TState.LEASED,
                                  machine='m2', leased_at=timezone.now(),
                                  lease_heartbeat_at=timezone.now())

    def _task(self, generation, budget, **fields):
        return AnalysisTask.objects.create(
            position=self.target, generation=generation, budget_nodes=budget,
            source=AnalysisTask.Source.USER, requested_by='w', **fields)

    def _serve(self, nodes=512_000_000):
        """El worker coge la de 512M, la busca y su payload se aplica."""
        leased = _lease(self.client, self.auth)['tasks'][0]
        self.assertEqual(leased['id'], self.served.id)
        self.client.post('/atomicdb/api/submit', dict(
            self.auth, task_id=leased['id'],
            lines=json.dumps(_analysis_lines(self.target)),
            nodes=str(nodes), elapsed='12.5',
            lease_token=leased['lease_token']))
        self.assertEqual(ingest_queue.drain(), {'done': 1, 'failed': 0})

    def test_the_pass_absorbs_what_it_left_without_work(self):
        self._serve()

        for task in (self.shadowed, self.deeper, self.running):
            task.refresh_from_db()
        # 128M pedidos contra 512M ya buscados: no queda nada que comprar.
        self.assertEqual(self.shadowed.state, 'COMPLETED')
        self.assertIsNotNone(self.shadowed.completed)
        # Los 10B siguen siendo una busqueda que nadie ha hecho.
        self.assertEqual(self.deeper.state, 'PENDING')
        # Y la arrendada esta CORRIENDO: cerrara sola, con sus nodos.
        self.assertEqual(self.running.state, 'LEASED')

    def test_an_absorbed_row_claims_no_nodes_and_no_machine(self):
        self._serve()

        self.shadowed.refresh_from_db()
        self.assertEqual(self.shadowed.nodes_searched, 0)
        self.assertEqual(self.shadowed.machine, '')

    def test_the_profile_queue_shows_it_served_instead_of_waiting(self):
        self._serve()

        pending, _leased, done = contributors._queue_rows('w')

        self.assertNotIn(self.shadowed.id, [task.id for task in pending])
        self.assertIn(self.shadowed.id, [task.id for task in done])
        # Y la honda sigue esperando: lo que se cierra es lo que sobra.
        self.assertIn(self.deeper.id, [task.id for task in pending])

    def test_the_completed_ladder_reads_the_same_as_before(self):
        """El maximo COMPLETED manda, y lo absorbido siempre esta por debajo:
        su presupuesto cabia en los nodos que de verdad se buscaron."""
        self._serve()

        self.assertEqual(ingest._completed_max_budget(self.target),
                         512_000_000)

    def test_another_position_is_none_of_its_business(self):
        other = (Edge.objects.filter(parent=self.root)
                 .order_by('move_uci')[1].child)
        elsewhere = AnalysisTask.objects.create(
            position=other, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')

        self._serve()

        elsewhere.refresh_from_db()
        self.assertEqual(elsewhere.state, 'PENDING')


class ClosureAbsorbsItsQuestionsTests(TestCase):
    """Cerrar una posicion cierra sus PREGUNTAS.

    Una PENDING sobre algo ya resuelto es un zombi perfecto: ``choose_pending``
    salta lo que no esta en 'UNKNOWN', asi que no la sirve nadie nunca, y la
    absorcion por analisis solo dispara cuando un analisis ATERRIZA en esa
    posicion — justo lo que ya no puede pasar.  30-jul en produccion: 126
    peticiones USER de mas de 72h, las mas viejas del dueno sobre posiciones
    con ``eval_cp=-9999`` y ``nodes_invested=0``, cerradas por PROPAGACION sin
    que ningun motor las mirase.

    Los cinco caminos de cierre pasan por ``_emit_closure_events``, que es
    donde vive la regla; aqui se recorren los tres que un worker produce.
    """

    def _waiting(self, position, generation=0, **fields):
        return AnalysisTask.objects.create(
            position=position, generation=generation,
            budget_nodes=128_000_000, source=AnalysisTask.Source.USER,
            requested_by='w', **fields)

    def _elsewhere(self):
        """Una peticion sobre OTRA posicion, que no es asunto de este cierre."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        other = Edge.objects.filter(parent=root).order_by('move_uci')[0].child
        return self._waiting(other)

    def test_a_mate_witness_closes_the_children_it_leaves_without_work(self):
        """Cierre por testigo de mate dentro del propio pase (§ ingest)."""
        parent = ingest.get_or_create_position(FORCED_MATE_FEN)
        ingest.expand(parent)
        child = Edge.objects.get(parent=parent,
                                 move_uci=FORCED_MATE_PV[0]).child
        waiting = self._waiting(child)
        running = self._waiting(child, generation=1,
                                state=AnalysisTask.TState.LEASED,
                                machine='m1', leased_at=timezone.now())
        elsewhere = self._elsewhere()

        ingest.ingest_analysis(parent.key, [{
            'move': FORCED_MATE_PV[0], 'eval_cp': 9_999, 'mate': 2,
            'pv': FORCED_MATE_PV}], nodes_budget=1_000)

        child.refresh_from_db()
        self.assertEqual(child.status, 'WHITE_WIN')
        waiting.refresh_from_db()
        self.assertEqual(waiting.state, 'COMPLETED')
        self.assertIsNotNone(waiting.completed)
        # No cobra: el trabajo se hizo, pero no lo hizo esta fila.
        self.assertEqual(waiting.nodes_searched, 0)
        self.assertEqual(waiting.machine, '')
        # La arrendada CORRE: cerrara sola, con sus nodos y su maquina.
        running.refresh_from_db()
        self.assertEqual(running.state, 'LEASED')
        self.assertEqual(running.machine, 'm1')
        elsewhere.refresh_from_db()
        self.assertEqual(elsewhere.state, 'PENDING')

    def test_a_propagated_closure_absorbs_the_question_it_answered(self):
        """El caso de produccion: la posicion nunca vio un motor."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        for edge in Edge.objects.filter(parent=root).select_related('child'):
            child = edge.child
            child.status, child.closure = 'BLACK_WIN', 'MATE_PV'
            child.proof, child.mate_in = 'ANDOR', 1
            child.clock_slack = 90       # margen comodo: § test_clock_slack
            child.save()
        waiting = self._waiting(root)

        ingest.backup_cascade(list(Edge.objects.filter(parent=root)
                                   .values_list('child_id', flat=True)))

        root.refresh_from_db()
        self.assertEqual(root.status, 'BLACK_WIN')
        self.assertEqual(root.closure, 'MINIMAX')
        # Cero nodos invertidos y aun asi cerrada: sin esto la fila se queda
        # PENDING para siempre, porque ningun analisis va a aterrizar ya aqui.
        self.assertEqual(root.nodes_invested, 0)
        waiting.refresh_from_db()
        self.assertEqual(waiting.state, 'COMPLETED')
        self.assertEqual(waiting.nodes_searched, 0)

    def test_materialising_a_won_line_absorbs_what_it_closes(self):
        parent = ingest.get_or_create_position(FORCED_MATE_FEN)
        ingest.expand(parent)
        witness = Edge.objects.get(parent=parent,
                                   move_uci=FORCED_MATE_PV[0]).child
        witness.status, witness.closure, witness.proof = ('WHITE_WIN',
                                                          'MATE_PV', 'ENGINE')
        witness.won_line = ' '.join(FORCED_MATE_PV[1:])
        witness.best_move, witness.mate_in = FORCED_MATE_PV[1], 2
        witness.save()
        suffix = ingest.get_or_create_position(
            logic.apply_move(witness.fen, FORCED_MATE_PV[1]))
        waiting = self._waiting(suffix)

        ingest.materialise_won_line(witness)

        suffix.refresh_from_db()
        self.assertEqual(suffix.status, 'WHITE_WIN')
        self.assertEqual(suffix.closure, 'MATE_PV')
        waiting.refresh_from_db()
        self.assertEqual(waiting.state, 'COMPLETED')
        self.assertEqual(waiting.nodes_searched, 0)


class ShadowBackfillTests(TestCase):
    """La pasada UNICA para las que ya estaban colgadas (§ management)."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = (Edge.objects.filter(parent=self.root)
                       .order_by('move_uci').first().child)
        AnalysisTask.objects.create(
            position=self.target, generation=0, budget_nodes=512_000_000,
            state=AnalysisTask.TState.COMPLETED, machine='m1',
            nodes_searched=512_000_000, completed=timezone.now())
        self.shadowed = AnalysisTask.objects.create(
            position=self.target, generation=1, budget_nodes=128_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')
        self.deeper = AnalysisTask.objects.create(
            position=self.target, generation=2, budget_nodes=10_000_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')

    def _other_child(self, index=1):
        return (Edge.objects.filter(parent=self.root)
                .order_by('move_uci')[index].child)

    def _on_closed_position(self):
        """Una PENDING sobre una posicion CERRADA: nadie la sirve jamas."""
        closed = self._other_child()
        Position.objects.filter(key=closed.key).update(
            status='BLACK_WIN', closure='MINIMAX')
        return AnalysisTask.objects.create(
            position=closed, generation=0, budget_nodes=128_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')

    def _run(self, **options):
        from io import StringIO
        out = StringIO()
        call_command('absorb_shadowed_tasks', stdout=out, **options)
        return out.getvalue().strip()

    def test_the_dry_run_counts_without_touching_a_single_row(self):
        zombie = self._on_closed_position()

        output = self._run(dry_run=True)

        self.assertEqual(
            output, 'absorb_shadowed_tasks: 1 sombreadas + 1 sobre posicion '
                    'cerrada por absorber (dry-run, sin escribir)')
        for task in (self.shadowed, zombie):
            task.refresh_from_db()
            self.assertEqual(task.state, 'PENDING')

    def test_the_pass_absorbs_the_shadowed_and_leaves_the_deeper_alone(self):
        output = self._run()

        self.assertEqual(output, 'absorb_shadowed_tasks: 1 sombreadas + 0 '
                                 'sobre posicion cerrada absorbidas')
        self.shadowed.refresh_from_db()
        self.deeper.refresh_from_db()
        self.assertEqual(self.shadowed.state, 'COMPLETED')
        self.assertEqual(self.shadowed.nodes_searched, 0)
        self.assertEqual(self.shadowed.machine, '')
        self.assertIsNotNone(self.shadowed.completed)
        self.assertEqual(self.deeper.state, 'PENDING')

    def test_the_pass_absorbs_the_pending_on_a_closed_position(self):
        """La clase que nada cubria: cerrada sin analisis que la sombree."""
        zombie = self._on_closed_position()

        output = self._run()

        self.assertEqual(output, 'absorb_shadowed_tasks: 1 sombreadas + 1 '
                                 'sobre posicion cerrada absorbidas')
        zombie.refresh_from_db()
        self.assertEqual(zombie.state, 'COMPLETED')
        self.assertEqual(zombie.nodes_searched, 0)
        self.assertEqual(zombie.machine, '')
        self.assertIsNotNone(zombie.completed)

    def test_a_leased_row_on_a_closed_position_keeps_running(self):
        closed = self._other_child()
        Position.objects.filter(key=closed.key).update(status='BLACK_WIN')
        running = AnalysisTask.objects.create(
            position=closed, generation=0, budget_nodes=128_000_000,
            source=AnalysisTask.Source.USER, requested_by='w',
            state=AnalysisTask.TState.LEASED, machine='m1',
            leased_at=timezone.now())

        self._run()

        running.refresh_from_db()
        self.assertEqual(running.state, 'LEASED')
        self.assertEqual(running.machine, 'm1')

    def test_a_second_pass_finds_nothing_left(self):
        self._on_closed_position()
        self._run()

        self.assertEqual(self._run(), 'absorb_shadowed_tasks: 0 sombreadas '
                                      '+ 0 sobre posicion cerrada absorbidas')

    def test_a_tombstoned_position_is_not_a_zombie_and_is_still_served(self):
        """La LAPIDA saca a la posicion del SELECTOR, no de la cola.

        ``priority <= DEAD/2`` es lo que impide crear tareas NUEVAS ahi
        (``next_tasks`` filtra ``priority__gt=DEAD/2``), pero la posicion sigue
        en 'UNKNOWN' y ``choose_pending`` no mira la prioridad en ningun sitio:
        la peticion que ya existe se arrienda como cualquier otra.  Absorberla
        seria inventarse un cierre que nadie ha probado."""
        buried = self._other_child()
        Position.objects.filter(key=buried.key).update(priority=ingest.DEAD)
        alive = AnalysisTask.objects.create(
            position=buried, generation=0, budget_nodes=128_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')

        self._run()

        alive.refresh_from_db()
        self.assertEqual(alive.state, 'PENDING')
        auth = _worker()
        leased = _lease(Client(), auth)['tasks'][0]
        self.assertEqual(leased['id'], alive.id)


class SynchronousFallbackTests(TestCase):
    """El interruptor de despliegue usa el MISMO codigo de aplicacion."""

    def setUp(self):
        self.auth = _worker()
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = (Edge.objects.filter(parent=self.root)
                       .order_by('move_uci').first().child)
        ingest.expand(self.target)
        AnalysisTask.objects.create(position=self.target, generation=0,
                                    budget_nodes=1_000_000)

    def _submit(self):
        leased = _lease(self.client, self.auth)['tasks'][0]
        return self.client.post('/atomicdb/api/submit', dict(
            self.auth, task_id=leased['id'],
            lines=json.dumps(_analysis_lines(self.target)),
            nodes='900000', elapsed='12.5',
            lease_token=leased['lease_token']))

    @override_settings(ATOMICDB_SYNCHRONOUS_INGEST=True)
    def test_the_switch_applies_before_answering(self):
        response = self._submit()

        self.assertTrue(response.json()['ok'])
        self.assertIn('backed_evals', response.json()['summary'])
        self.target.refresh_from_db()
        self.assertEqual(self.target.visits, 1)
        self.assertEqual(IngestJob.objects.get().state, 'DONE')


class SynchronousVsQueuedTests(TestCase):
    """A/B: el mismo fixture por los dos caminos deja el mismo arbol."""

    LINES_VALUE = -450

    def _fixture(self, prefix):
        worker_account(prefix, 'p')
        auth = {'username': prefix, 'password': 'p', 'machine': prefix}
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        target = (Edge.objects.filter(parent=root)
                  .order_by('move_uci').first().child)
        ingest.expand(target)
        AnalysisTask.objects.create(position=target, generation=0,
                                    budget_nodes=1_000_000)
        client = Client()
        leased = _lease(client, auth)['tasks'][0]
        client.post('/atomicdb/api/submit', dict(
            auth, task_id=leased['id'],
            lines=json.dumps(_analysis_lines(target, self.LINES_VALUE)),
            nodes='900000', elapsed='12.5',
            lease_token=leased['lease_token']))
        return target

    def _tree_state(self):
        return {
            'positions': sorted(
                Position.objects.values_list(
                    'key', 'eval_cp', 'status', 'expanded', 'visits',
                    'nodes_invested', 'time_invested', 'backed_eval',
                    'backed_move', 'backed_plies', 'backed_nodes',
                    'best_move')),
            'edges': sorted(
                Edge.objects.values_list('parent_id', 'move_uci',
                                         'child_id')),
        }

    def test_both_paths_leave_the_same_tree(self):
        with override_settings(ATOMICDB_SYNCHRONOUS_INGEST=True):
            self._fixture('sync')
        synchronous = self._tree_state()

        IngestJob.objects.all().delete()
        AnalysisTask.objects.all().delete()
        Edge.objects.all().delete()          # PROTECT sobre Edge.child
        ProofCampaign.objects.all().delete()  # PROTECT sobre Campaign.root
        Position.objects.all().delete()

        self._fixture('async')
        ingest_queue.drain()
        queued = self._tree_state()

        self.assertEqual(synchronous['edges'], queued['edges'])
        self.assertEqual(synchronous['positions'], queued['positions'])
