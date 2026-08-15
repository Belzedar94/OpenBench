"""Lo que una persona puede hacer con SU cola, y lo que gana pedir en grupo.

Dos peticiones de #suggestions, y las dos son sobre el mismo sitio — el orden
en el que se sirve la banda de visitante — pero desde lados opuestos:

* ADELANTAR LO PROPIO (Wolfram): "some checkmark to push smth to the front of
  my own queue, not to the back, would be nice".  Lo que aqui se defiende no
  es que la adelantada salga primero — eso es la parte facil — sino que salga
  primero SIN quitarle el turno a nadie: la cuenta que adelanta conserva
  exactamente los mismos turnos que tenia, y lo unico que elige es cual de las
  suyas ocupa cada uno.
* PEDIR EN GRUPO (Eclipsia): "multiple people asking to analyse the same
  position should give it more precedence".  El segundo que pedia lo mismo
  desaparecia entero: sin fila, sin orden y sin aviso.  Ahora se suma a la que
  existe, y lo que compra con ese click es precedencia y campana.

La absorcion sigue teniendo trabajo despues de esto, y tiene su propia clase:
las peticiones que se solapan por PRESUPUESTO (dos generaciones sobre la misma
posicion) no las cubre el dedup del submit y se siguen cerrando al aterrizar.
"""

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from . import contributors, ingest, ingest_queue, live_request, logic
from .models import AnalysisTask, IngestJob, Position, RequestNotification
from .test_notifications import _lines_for
from .test_queue_fairness import _QueueHarness
from .testing import TestCase, worker_account


class _BumpHarness(_QueueHarness):

    def setUp(self):
        super().setUp()
        worker_account('a', 'p')
        worker_account('b', 'p')

    def _bump(self, task, username):
        """El boton, tal y como lo pulsa su duena: con sesion y por POST."""
        self.client.login(username=username, password='p')
        response = self.client.post(f'/atomicdb/queue/bump/{task.id}/')
        self.client.logout()
        return response

    def _owners(self, served, **accounts):
        """La secuencia de CUENTAS servidas, que es la que no debe moverse."""
        owner = {task.id: name
                 for name, tasks in accounts.items() for task in tasks}
        return [owner[task_id] for task_id in served]


class OwnQueueBumpTests(_BumpHarness):

    def test_a_bumped_request_leads_the_queue_of_its_own_account(self):
        mine = self._queue('a', [self.RUNG] * 3)

        self._bump(mine[2], 'a')

        self.assertEqual(self._serve_order(3),
                         [mine[2].id, mine[0].id, mine[1].id])

    def test_a_bump_never_takes_a_turn_away_from_another_account(self):
        """LA PROPIEDAD: la secuencia de CUENTAS servidas es la misma.

        Antes y despues se sirve a, b, a, b, a, b.  Lo unico que cambia es
        cual de las tres de ``a`` ocupa el primero de sus turnos.
        """
        mine = self._queue('a', [self.RUNG] * 3)
        yours = self._queue('b', [self.RUNG] * 3, offset=100)

        self._bump(mine[2], 'a')
        served = self._serve_order(6)

        self.assertEqual(self._owners(served, a=mine, b=yours),
                         ['a', 'b', 'a', 'b', 'a', 'b'])
        self.assertEqual(served, [mine[2].id, yours[0].id,
                                  mine[0].id, yours[1].id,
                                  mine[1].id, yours[2].id])

    def test_the_queue_of_a_stranger_cannot_be_touched(self):
        mine = self._queue('a', [self.RUNG] * 2)
        yours = self._queue('b', [self.RUNG] * 2, offset=100)

        response = self._bump(yours[1], 'a')

        self.assertEqual(response.json()['status'], 'not-yours')
        self.assertEqual(AnalysisTask.objects.get(pk=yours[1].pk).queue_seq, 0)
        self.assertEqual(self._serve_order(4),
                         [mine[0].id, yours[0].id, mine[1].id, yours[1].id])

    def test_the_first_of_your_own_has_nowhere_to_move(self):
        mine = self._queue('a', [self.RUNG] * 2)

        response = self._bump(mine[0], 'a')

        self.assertEqual(response.json()['status'], 'already-first')
        self.assertEqual(AnalysisTask.objects.get(pk=mine[0].pk).queue_seq, 0)

    def test_a_second_bump_overtakes_the_first_one(self):
        mine = self._queue('a', [self.RUNG] * 3)

        self._bump(mine[1], 'a')
        self._bump(mine[2], 'a')

        self.assertEqual(self._serve_order(3),
                         [mine[2].id, mine[1].id, mine[0].id])

    def test_a_bump_does_not_erase_the_debt_of_your_running_work(self):
        """El descuento que no puede existir: saltarse el propio arriendo.

        La cuenta ``a`` tiene 10B buscandose ahora mismo, asi que sus
        pendientes cobran DESPUES de la de ``b``.  Si adelantar pusiera la
        adelantada por delante de su propio arriendo, su ``fair_ahead``
        volveria a cero y ``a`` cobraria dos turnos seguidos: adelantar antes
        de cada arriendo seria vivir en el frente para siempre.
        """
        mine = self._queue('a', [10_000_000_000, self.RUNG, self.RUNG])
        AnalysisTask.objects.filter(pk=mine[0].pk).update(
            state=AnalysisTask.TState.LEASED, machine='a#0',
            leased_at=timezone.now(), lease_heartbeat_at=timezone.now())
        yours = self._queue('b', [self.RUNG], offset=100)

        self._bump(mine[2], 'a')

        self.assertEqual(self._serve_order(3),
                         [yours[0].id, mine[2].id, mine[1].id])

    def test_the_estimator_agrees_with_the_order_actually_served(self):
        """Las dos cifras salen de la misma regla, tambien despues de mover.

        Es la trampa que ya cazo una divergencia una vez: el sitio le decia a
        un humano "N por delante" mientras la cola servia en otro orden.
        """
        mine = self._queue('a', [self.RUNG, 2_000_000_000, self.RUNG])
        self._queue('b', [self.RUNG] * 3, offset=100)

        self._bump(mine[2], 'a')
        pending = list(AnalysisTask.objects.filter(state='PENDING'))
        estimated = {task.id: live_request.queue_ahead(task)
                     for task in pending}
        served = self._serve_order(len(pending))

        self.assertEqual([estimated[task_id] for task_id in served],
                         list(range(len(served))))

    def test_a_completed_request_is_not_a_queue_position(self):
        mine = self._queue('a', [self.RUNG] * 2)
        AnalysisTask.objects.filter(pk=mine[1].pk).update(
            state=AnalysisTask.TState.COMPLETED, completed=timezone.now())

        self.assertEqual(self._bump(mine[1], 'a').json()['status'],
                         'not-yours')

    def test_without_a_session_the_button_leads_to_the_login(self):
        mine = self._queue('a', [self.RUNG] * 2)

        response = self.client.post(f'/atomicdb/queue/bump/{mine[1].id}/')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])
        self.assertEqual(AnalysisTask.objects.get(pk=mine[1].pk).queue_seq, 0)

    def test_a_get_does_not_move_anything(self):
        mine = self._queue('a', [self.RUNG] * 2)
        self.client.login(username='a', password='p')

        response = self.client.get(f'/atomicdb/queue/bump/{mine[1].id}/')

        self.assertEqual(response.status_code, 405)
        self.assertEqual(AnalysisTask.objects.get(pk=mine[1].pk).queue_seq, 0)


class BackersStayInTheVisitorBandTests(_BumpHarness):
    """El recuento de personas no tiene voz fuera de la banda de visitante.

    Una tarea que nacio de un click puede acabar DEGRADADA a AUTO por una
    expansion de cobertura, conservando encima lo que ya tenia.  Dentro de
    AUTO el reparto vale cero en todas las filas, asi que un recuento vivo
    ahi mandaria por delante de la prioridad de POSICION — que es justo lo
    que el selector calcula para decidir en esa banda.
    """

    def test_a_degraded_request_does_not_outrank_position_priority(self):
        wanted_pos, degraded_pos = self._positions(2)
        Position.objects.filter(key=wanted_pos.key).update(priority=9.0)
        Position.objects.filter(key=degraded_pos.key).update(priority=-40.0)
        AnalysisTask.objects.create(
            position=degraded_pos, generation=0, budget_nodes=self.RUNG,
            source=AnalysisTask.Source.AUTO, requested_by='a',
            also_requested_by=['b'], backers=1)
        wanted = AnalysisTask.objects.create(
            position=wanted_pos, generation=0, budget_nodes=self.RUNG,
            source=AnalysisTask.Source.AUTO)

        self.assertEqual(self._serve_order(1), [wanted.id])


class BumpOnTheProfileTests(_BumpHarness):
    """Lo que la pagina de la cuenta ensena, que es de donde sale el click."""

    def test_the_list_follows_the_order_that_the_queue_will_serve(self):
        mine = self._queue('a', [self.RUNG] * 3)

        self._bump(mine[2], 'a')
        rows = contributors.present('a')['queue_pending']

        self.assertEqual(
            [row['key'] for row in rows],
            [mine[2].position_id, mine[0].position_id, mine[1].position_id])
        # Y los numeros de sitio siguen contando desde el frente, sin huecos:
        # una lista ordenada por llegada con estos numeros al lado seria la
        # pagina contradiciendose sola.
        self.assertEqual([row['place'] for row in rows], [1, 2, 3])

    def test_no_row_of_the_queue_carries_the_control_any_more(self):
        """UNA fila por peticion era el sitio equivocado para este boton.

        La lista sale en orden de turno, asi que lo que se ve arriba es lo que
        ya iba primero: cada boton en pantalla ofrecia adelantar justo lo que
        no hacia falta ("those buttons clog the interface and essentially
        useless cause you suggest to move what is already basically in front
        of the queue to the very front", Wolfram), y la peticion que de verdad
        se quiere adelantar esta cien filas mas abajo, fuera de la pagina.  Ni
        siquiera en la propia: el control vive en la pagina de la POSICION.
        """
        self._queue('a', [self.RUNG] * 3)
        self.client.login(username='a', password='p')

        own_page = self.client.get('/atomicdb/user/a/').content.decode()

        self.assertNotIn('/atomicdb/queue/bump/', own_page)
        self.assertNotIn('Move to front', own_page)
        # Y la fila conserva lo que si es suyo: el sitio en la cola y el
        # camino hasta la posicion, que es por donde se llega al control.
        self.assertIn('#1', own_page)

    def test_the_button_falls_back_to_your_page_without_a_route(self):
        """``back`` es una pista, no una orden: solo se obedece una ruta del
        sitio, y sin ella se vuelve a la pagina de la cuenta de siempre."""
        mine = self._queue('a', [self.RUNG] * 2)
        self.client.login(username='a', password='p')

        response = self.client.post(f'/atomicdb/queue/bump/{mine[1].id}/',
                                    {'back': '1'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/atomicdb/user/a/')

    def test_a_back_that_leaves_the_site_is_refused(self):
        mine = self._queue('a', [self.RUNG] * 2)
        self.client.login(username='a', password='p')

        response = self.client.post(f'/atomicdb/queue/bump/{mine[1].id}/',
                                    {'back': '//example.invalid/'})

        self.assertEqual(response['Location'], '/atomicdb/user/a/')


class BumpOnThePositionTests(TestCase):
    """Donde vive ahora el control, y lo que tiene que decir estando ahi.

    Peticion de Opabinia en #suggestions: "it would be better if the Move to
    front was on the page of the position, because usually you want to move
    something that's like hundreds in the queue to the front".  En la lista de
    la cola el boton solo alcanzaba a las primeras veinte filas, que son
    justamente las que no necesitan adelantarse; en la pagina de la posicion
    hay exactamente una peticion de la que hablar y es la que se esta mirando.
    """

    def setUp(self):
        for name in ('alice', 'bob'):
            worker_account(name, 'p')
        self.client = Client()
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        self.first, self.second = (
            ingest.get_or_create_position(logic.apply_move(root.fen, uci))
            for uci in ('e2e4', 'd2d4'))

    def _ask(self, position, username='alice'):
        ingest.request_analysis(position, requested_by=username)
        return AnalysisTask.objects.get(position=position, state='PENDING')

    def _body(self, position, username=''):
        if username:
            self.client.login(username=username, password='p')
        body = self.client.get(
            f'/atomicdb/explore/{position.key}/').content.decode()
        self.client.logout()
        return body

    def _control(self, body, task):
        return f'/atomicdb/queue/bump/{task.id}/' in body

    def test_the_position_you_asked_for_carries_the_control(self):
        self._ask(self.first)
        waiting = self._ask(self.second)

        self.assertTrue(self._control(self._body(self.second, 'alice'),
                                      waiting))

    def test_the_one_already_at_the_front_offers_nothing(self):
        """Un boton que contesta 'already-first' es peor que su ausencia."""
        leading = self._ask(self.first)
        self._ask(self.second)

        self.assertFalse(self._control(self._body(self.first, 'alice'),
                                       leading))

    def test_a_stranger_looking_at_the_same_page_sees_no_control(self):
        self._ask(self.first)
        waiting = self._ask(self.second)

        body = self._body(self.second, 'bob')

        self.assertFalse(self._control(body, waiting))
        self.assertNotIn('Move to front', body)

    def test_without_a_session_there_is_no_control_at_all(self):
        self._ask(self.first)
        self._ask(self.second)

        self.assertNotIn('Move to front', self._body(self.second))

    def test_joining_somebody_elses_request_does_not_hand_you_its_place(self):
        """Sumarse pone tu nombre en la fila; la fila sigue siendo de su
        autor, y el endpoint tampoco se la moveria a nadie mas."""
        self._ask(self.first)
        waiting = self._ask(self.second)
        ingest.request_analysis(self.second, requested_by='bob')

        body = self._body(self.second, 'bob')

        self.assertEqual(AnalysisTask.objects.get(pk=waiting.pk).backers, 1)
        self.assertFalse(self._control(body, waiting))

    def test_the_control_shows_the_place_the_account_page_shows(self):
        """LA MISMA CIFRA, calculada por la misma regla de reparto justo.

        El control tiene que ensenar el sitio de la peticion sobre la que
        actua, y ese sitio no puede ser otro que el que la pagina de la cuenta
        imprime al lado de esa misma fila: dos numeros para un solo hecho es
        como una pagina acaba contradiciendo a la otra.
        """
        self._ask(self.first)
        waiting = self._ask(self.second)
        # Con un arriendo vivo aqui, la linea de estado narra el arriendo y no
        # la espera: el sitio de la peticion propia no esta dicho todavia, asi
        # que el control lo dice.
        AnalysisTask.objects.create(
            position=self.second, generation=1, budget_nodes=2_000_000_000,
            source=AnalysisTask.Source.USER, requested_by='bob',
            state=AnalysisTask.TState.LEASED, machine='m1',
            leased_at=timezone.now(), lease_heartbeat_at=timezone.now())

        body = self._body(self.second, 'alice')
        rows = contributors.present('alice')['queue_pending']
        listed = next(row['place'] for row in rows
                      if row['key'] == self.second.key)

        self.assertEqual(listed, live_request.queue_ahead(waiting) + 1)
        self.assertIn(f'#{listed}', body)

    def test_the_place_survives_the_click_that_takes_the_button_away(self):
        """Que hizo el click, dicho por el mismo numero de antes.

        Adelantar deja la peticion la primera de su propia cola, asi que el
        boton desaparece con el click: si el sitio desapareciera con el, lo
        unico que veria quien pulsa es que ya no hay nada que pulsar.
        """
        self._ask(self.first)
        waiting = self._ask(self.second)
        AnalysisTask.objects.create(
            position=self.second, generation=1, budget_nodes=2_000_000_000,
            source=AnalysisTask.Source.USER, requested_by='bob',
            state=AnalysisTask.TState.LEASED, machine='m1',
            leased_at=timezone.now(), lease_heartbeat_at=timezone.now())
        before = live_request.queue_ahead(waiting) + 1

        self.client.login(username='alice', password='p')
        self.client.post(f'/atomicdb/queue/bump/{waiting.id}/')
        body = self._body(self.second, 'alice')

        after = live_request.queue_ahead(
            AnalysisTask.objects.get(pk=waiting.pk)) + 1
        self.assertLess(after, before)
        self.assertNotIn('Move to front', body)
        self.assertIn(f'#{after}', body)

    def test_the_place_is_not_said_twice_on_the_same_screen(self):
        """La linea de estado ya cuenta la cola cuando narra ESTA espera."""
        self._ask(self.first)
        self._ask(self.second)

        body = self._body(self.second, 'alice')

        self.assertIn('Move to front', body)
        self.assertIn('request ahead', body)
        self.assertNotIn('in the visitor queue', body)

    def test_the_click_moves_the_request_and_comes_back_to_the_position(self):
        self._ask(self.first)
        waiting = self._ask(self.second)
        self.client.login(username='alice', password='p')
        back = f'/atomicdb/explore/{self.second.key}/'

        response = self.client.post(f'/atomicdb/queue/bump/{waiting.id}/',
                                    {'back': back})

        self.assertEqual(response['Location'], back)
        self.assertEqual(
            list(AnalysisTask.objects.filter(state='PENDING')
                 .order_by('queue_seq', 'id')
                 .values_list('position_id', flat=True))[0],
            self.second.key)

    def test_a_solved_position_has_no_queue_to_talk_about(self):
        self._ask(self.first)
        self._ask(self.second)
        Position.objects.filter(key=self.second.key).update(
            status='WHITE_WIN', closure='MINIMAX')

        self.assertNotIn('Move to front', self._body(self.second, 'alice'))

    def test_a_visitor_without_a_session_pays_nothing_for_the_control(self):
        """Un control personal no puede costarle nada a quien no lo ve.

        La linea de estado ya leyo la tarea viva de esta posicion, y sin
        sesion no hay cuenta que buscar: el explorador de un visitante
        anonimo cuesta exactamente lo que costaba (§ test_live_request,
        CostTests, que fija el presupuesto de la pagina entera).
        """
        self._ask(self.first)
        self._ask(self.second)
        live = live_request._live_task(self.second)

        with self.assertNumQueries(
                0, using=settings.ATOMICDB_DATABASE_ALIAS):
            self.assertEqual(
                live_request.bump_control(self.second, '', live=live), {})

    def test_the_control_costs_one_statement_over_what_the_page_already_read(
            self):
        """Lo que se paga por saber si la peticion tiene a donde ir.

        La tarea propia sale GRATIS cuando es la que la pagina ya narra, y el
        sitio en la cola tampoco se pregunta cuando esa linea acaba de
        decirlo: queda una sola sentencia, la que decide si el boton existe.
        """
        self._ask(self.first)
        self._ask(self.second)
        live = live_request._live_task(self.second)

        with self.assertNumQueries(
                1, using=settings.ATOMICDB_DATABASE_ALIAS):
            shown = live_request.bump_control(self.second, 'alice', live=live)

        self.assertTrue(shown['queue_bump']['can_move'])
        # El sitio no se pregunta: la linea de estado ya lo dijo.
        self.assertIsNone(shown['queue_bump']['place'])


def _serve(task, machine='m1'):
    """Reclamar y aplicar, que es lo que hacen el submit y su procesador."""
    job, _created = ingest_queue.enqueue(task, {
        'lines': _lines_for(task.position), 'nodes': 1_000, 'elapsed': 1.0,
        'machine': machine, 'username': 'w'})
    AnalysisTask.objects.filter(pk=task.pk).update(
        state=AnalysisTask.TState.COMPLETED, completed=timezone.now())
    return ingest_queue.process_job(job)


class _DuplicateHarness(TestCase):

    def setUp(self):
        for name in ('eclipsia', 'wolfram', 'lesha'):
            User.objects.create_user(username=name, password='pw')
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'e2e4'))

    def _ask(self, username='', position=None):
        return ingest.request_analysis(position or self.target,
                                       requested_by=username)


class DuplicateRequestTests(_DuplicateHarness):
    """Pedir lo que otro ya pidio: sin fila nueva, con nombre y con turno."""

    def test_the_second_person_does_not_open_a_second_task(self):
        self._ask('eclipsia')

        self.assertEqual(self._ask('wolfram'), 'already-queued')
        self.assertEqual(
            AnalysisTask.objects.filter(position=self.target).count(), 1)

    def test_the_second_person_is_written_down_on_the_task(self):
        self._ask('eclipsia')
        self._ask('wolfram')

        task = AnalysisTask.objects.get(position=self.target)
        self.assertEqual(task.requested_by, 'eclipsia')
        self.assertEqual(task.also_requested_by, ['wolfram'])
        self.assertEqual(task.backers, 1)

    def test_asking_twice_yourself_is_still_one_person(self):
        self._ask('eclipsia')
        self._ask('eclipsia')

        task = AnalysisTask.objects.get(position=self.target)
        self.assertEqual(task.also_requested_by, [])
        self.assertEqual(task.backers, 0)

    def test_a_click_without_a_session_adds_no_precedence(self):
        """Sin nombre no hay voto: la marea anonima no se puede contar.

        Es la misma decision que toma el reparto justo con el cubo anonimo, y
        por lo mismo — dos clicks sin sesion no se distinguen de uno.
        """
        self._ask('eclipsia')
        self._ask('')

        task = AnalysisTask.objects.get(position=self.target)
        self.assertEqual(task.backers, 0)

    def test_an_anonymous_request_is_adopted_by_the_first_name(self):
        self._ask('')
        self._ask('wolfram')

        task = AnalysisTask.objects.get(position=self.target)
        self.assertEqual(task.requested_by, 'wolfram')
        self.assertEqual(task.also_requested_by, [])

    def test_joining_moves_the_request_to_the_front_of_its_own_queue(self):
        """El escalon que hace que el recuento signifique algo.

        ``eclipsia`` pide tres cosas; la tercera es la que le interesa a otra
        persona.  Sumarse la manda al frente de la cola de ``eclipsia``, que
        es donde estan los ceros de todo el mundo y donde el recuento de
        personas desempata.
        """
        first = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'd2d4'))
        second = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'g1f3'))
        self._ask('eclipsia', position=first)
        self._ask('eclipsia', position=second)
        self._ask('eclipsia')

        self._ask('wolfram')

        rows = list(AnalysisTask.objects.filter(state='PENDING')
                    .order_by('queue_seq', 'id')
                    .values_list('position_id', flat=True))
        self.assertEqual(rows[0], self.target.key)

    def test_more_people_win_the_tie_between_two_first_requests(self):
        """Dos primeras peticiones empatadas a cero: manda el recuento.

        Sin escalon de personas ganaria el ``id``, o sea el orden de llegada:
        la que espera dos personas saldria detras de la que espera una por
        haber llegado un segundo despues.
        """
        alone = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'd2d4'))
        self._ask('lesha', position=alone)
        self._ask('eclipsia')
        self._ask('wolfram')

        band = live_request.service_order(
            AnalysisTask.objects.filter(state='PENDING'))

        self.assertEqual(list(band.values_list('position_id', flat=True))[0],
                         self.target.key)

    def test_the_waiting_line_says_how_many_people_are_waiting(self):
        self._ask('eclipsia')
        self._ask('wolfram')

        text = live_request.summary(self.target)['text']

        self.assertIn('2 people asked for this', text)

    def test_one_person_is_not_announced(self):
        self._ask('eclipsia')

        self.assertNotIn('asked for this',
                         live_request.summary(self.target)['text'])


class DuplicateNotificationTests(_DuplicateHarness):
    """El resultado vuelve a TODOS los que lo pidieron."""

    def test_every_requester_gets_the_notification(self):
        self._ask('eclipsia')
        self._ask('wolfram')
        task = AnalysisTask.objects.get(position=self.target)

        _serve(task)

        self.assertEqual(
            sorted(RequestNotification.objects.values_list('username',
                                                           flat=True)),
            ['eclipsia', 'wolfram'])

    def test_a_second_pass_does_not_duplicate_anybody(self):
        """La unicidad la pone la base, no un ``exists()`` de la vista.

        Un trabajo puede volver a la cola (bloqueo, reintento) y aplicarse dos
        veces: la segunda vuelve a pasar por el aviso de cada peticionario.
        """
        self._ask('eclipsia')
        self._ask('wolfram')
        task = AnalysisTask.objects.get(position=self.target)
        _serve(task)

        IngestJob.objects.filter(task=task).update(
            state=IngestJob.JState.PENDING)
        for job in ingest_queue.ready_jobs():
            ingest_queue.apply_job(job)

        self.assertEqual(RequestNotification.objects.count(), 2)

    def test_a_name_without_an_account_notifies_nobody(self):
        self._ask('eclipsia')
        task = AnalysisTask.objects.get(position=self.target)
        AnalysisTask.objects.filter(pk=task.pk).update(
            also_requested_by=['fantasma'])

        _serve(AnalysisTask.objects.get(pk=task.pk))

        self.assertEqual(
            list(RequestNotification.objects.values_list('username',
                                                         flat=True)),
            ['eclipsia'])


class AbsorptionStillWorksTests(TestCase):
    """Lo que el dedup del submit NO cubre sigue cerrandose al aterrizar.

    El submit deduplica por (posicion, generacion): dos personas pidiendo LO
    MISMO comparten fila.  Lo que sigue produciendo filas solapadas es el
    PRESUPUESTO — un relevo encolado mientras corre un arriendo mas corto — y
    esa es la absorcion de siempre, que no se ha tocado.
    """

    def setUp(self):
        worker_account('w', 'p')
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'e2e4'))

    def test_a_cheaper_pending_request_is_absorbed_by_a_deeper_analysis(self):
        served = AnalysisTask.objects.create(
            position=self.target, generation=0, budget_nodes=2_000_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')
        shadowed = AnalysisTask.objects.create(
            position=self.target, generation=1, budget_nodes=128_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')

        job, _created = ingest_queue.enqueue(served, {
            'lines': _lines_for(self.target), 'nodes': 2_000_000_000,
            'elapsed': 12.0, 'machine': 'm1', 'username': 'w'})
        AnalysisTask.objects.filter(pk=served.pk).update(
            state=AnalysisTask.TState.COMPLETED, completed=timezone.now())
        summary, error = ingest_queue.process_job(job)

        self.assertIsNone(error)
        self.assertEqual(summary.get('absorbed'), 1)
        shadowed.refresh_from_db()
        self.assertEqual(shadowed.state, AnalysisTask.TState.COMPLETED)
        self.assertEqual(shadowed.nodes_searched, 0)

    def test_a_deeper_pending_request_survives_a_cheaper_analysis(self):
        served = AnalysisTask.objects.create(
            position=self.target, generation=0, budget_nodes=128_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')
        waiting = AnalysisTask.objects.create(
            position=self.target, generation=1, budget_nodes=10_000_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')

        job, _created = ingest_queue.enqueue(served, {
            'lines': _lines_for(self.target), 'nodes': 128_000_000,
            'elapsed': 1.0, 'machine': 'm1', 'username': 'w'})
        AnalysisTask.objects.filter(pk=served.pk).update(
            state=AnalysisTask.TState.COMPLETED, completed=timezone.now())
        ingest_queue.process_job(job)

        waiting.refresh_from_db()
        self.assertEqual(waiting.state, AnalysisTask.TState.PENDING)


class EndToEndClickTests(TestCase):
    """El circuito publico entero, con dos visitantes y dos direcciones IP."""

    def setUp(self):
        for name in ('eclipsia', 'wolfram'):
            worker_account(name, 'p')
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'e2e4'))

    def _click(self, username, ip):
        self.client.login(username=username, password='p')
        response = self.client.post(f'/atomicdb/request/{self.target.key}/',
                                    REMOTE_ADDR=ip)
        self.client.logout()
        return response.json()

    def test_two_visitors_share_one_task_and_both_are_notified(self):
        self.assertEqual(self._click('eclipsia', '10.0.0.1')['status'],
                         'queued')
        self.assertEqual(self._click('wolfram', '10.0.0.2')['status'],
                         'already-queued')

        task = AnalysisTask.objects.get(position=self.target)
        self.assertEqual(task.backers, 1)

        _serve(task)

        self.assertEqual(
            sorted(RequestNotification.objects.values_list('username',
                                                           flat=True)),
            ['eclipsia', 'wolfram'])
