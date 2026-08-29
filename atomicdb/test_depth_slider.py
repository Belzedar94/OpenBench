"""El selector de profundidad del boton "Request analysis".

Lo que se fija aqui, en este orden de importancia:

1. Que el permiso NO vive en la plantilla.  Un POST con un peldano elegido
   desde una cuenta sin derecho no compra ese peldano — se comprueba el
   ``budget_nodes`` REAL de la tarea, no el codigo de respuesta.
2. Que el boton sin tocar el slider hace exactamente lo que hacia antes de que
   el slider existiera: el mismo peldano de la escalera.
3. Que el explorador de quien no puede elegir no cambia ni un byte: ni control,
   ni hueco, ni la lista de peldanos escondida en la pagina.
"""

from datetime import timedelta

from django.test import Client, override_settings
from django.utils import timezone

from . import depth, ingest, logic
from .models import AnalysisTask, Position, RequestLog, WorkerPing
from .testing import TestCase, worker_account


LADDER = ingest.REQUEST_BUDGET_LADDER
# Todo lo que el explorador pinta SOLO cuando hay selector: el control, los
# datos que lo alimentan y el cableado que lo lee.  Son tres formas distintas
# de filtrarse — y la tercera se filtro de verdad: el JS nombraba el bloque de
# datos en TODAS las paginas, que es una pista de que la cosa existe aunque no
# se vea nada.
SLIDER_MARKS = ('depthrow', 'atomicdb-depth-rungs', 'chosenRung')


def _position():
    return ingest.get_or_create_position(logic.start_fen())


def _completed(pos, budget):
    """Un peldano ya gastado en esta posicion, como lo dejaria un worker."""
    generation = pos.visits
    while AnalysisTask.objects.filter(position=pos,
                                      generation=generation).exists():
        generation += 1
    return AnalysisTask.objects.create(
        position=pos, generation=generation, budget_nodes=budget,
        state=AnalysisTask.TState.COMPLETED, source=AnalysisTask.Source.USER)


def _worker(username, seen_days_ago=0, delivered=True):
    """Una cuenta con maquina prestada, que ENTREGO hace ``seen_days_ago`` dias.

    LA PRUEBA ES LA ENTREGA, no el saludo.  ``last_seen`` es ``auto_now`` y se
    mueve con cualquier llamada autenticada, asi que un proceso que pide
    trabajo en bucle y no devuelve un solo analisis lo mantenia tan fresco como
    una maquina que si busca.  Desde los carriles, el permiso lo compra el
    trabajo ENTREGADO (§ ``lanes``, y la columna ``WorkerPing.last_result_at``),
    y esta funcion escribe eso.  ``delivered=False`` construye justamente al que
    solo saluda.

    La fecha se pone con un UPDATE, como antes: un ``create(...)`` con
    ``auto_now`` de por medio la pisaria con la hora actual y el test estaria
    comprobando la ventana contra si misma.
    """
    user = worker_account(username)
    ping = WorkerPing.objects.create(machine=f'{username}-box', user=username,
                                     threads=8, hash_mb=1024, os='Linux')
    if delivered:
        WorkerPing.objects.filter(pk=ping.pk).update(
            last_result_at=timezone.now() - timedelta(days=seen_days_ago))
    return user


def _signed_in(username):
    client = Client()
    client.login(username=username, password='pw')
    return client


def _staff(username='belzedar'):
    user = worker_account(username)
    user.is_staff = True
    user.save(update_fields=['is_staff'])
    return user


def _pending(pos):
    return AnalysisTask.objects.get(position=pos,
                                    state=AnalysisTask.TState.PENDING)


class VisibilityTests(TestCase):
    """Quien ve el selector, y quien no ve ni que existe."""

    def setUp(self):
        self.pos = _position()

    def _marks(self, response):
        """Cuales de las marcas del selector trae la pagina.

        Se devuelve la LISTA y no un booleano por marca: el fallo tiene que
        decir cual se filtro, y un ``assertIn`` contra el cuerpo entero vuelca
        cuarenta kilobytes de HTML para contarlo.
        """
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        return sorted(mark for mark in SLIDER_MARKS if mark in body)

    def assertNoSlider(self, response):
        self.assertEqual(self._marks(response), [])

    def assertSlider(self, response):
        self.assertEqual(self._marks(response), sorted(SLIDER_MARKS))

    def test_an_anonymous_visitor_sees_no_trace_of_it(self):
        self.assertNoSlider(Client().get(f'/atomicdb/explore/{self.pos.key}/'))

    def test_an_account_without_a_recent_worker_sees_no_trace_of_it(self):
        worker_account('lesha')

        self.assertNoSlider(
            _signed_in('lesha').get(f'/atomicdb/explore/{self.pos.key}/'))

    def test_a_worker_that_delivered_two_days_ago_is_a_contributor(self):
        _worker('wolfram', seen_days_ago=2)

        self.assertSlider(
            _signed_in('wolfram').get(f'/atomicdb/explore/{self.pos.key}/'))

    def test_a_worker_that_only_ever_says_hello_is_not(self):
        # El permiso lo compra el trabajo entregado.  Un proceso que pide
        # trabajo en bucle y nunca devuelve un analisis mantenia ``last_seen``
        # tan fresco como una maquina de verdad, y con eso elegia peldano.
        _worker('wolfram', delivered=False)

        self.assertNoSlider(
            _signed_in('wolfram').get(f'/atomicdb/explore/{self.pos.key}/'))

    def test_a_worker_seen_eight_days_ago_is_not(self):
        # El otro lado del mismo borde: la ventana son siete dias, y quien
        # presto una maquina hace mas de una semana ya no la esta prestando.
        _worker('wolfram', seen_days_ago=8)

        self.assertNoSlider(
            _signed_in('wolfram').get(f'/atomicdb/explore/{self.pos.key}/'))

    def test_the_owner_sees_it_without_ever_lending_a_machine(self):
        _staff()

        self.assertSlider(
            _signed_in('belzedar').get(f'/atomicdb/explore/{self.pos.key}/'))

    def test_a_revoked_account_stops_choosing_even_with_a_live_worker(self):
        # La revocacion es la misma puerta que cierra el protocolo de workers:
        # si la cuenta ya no puede entregar analisis, tampoco decide cuanto
        # cuestan. El ping reciente se deja a proposito — sin el, el test
        # pasaria por la razon equivocada.
        from OpenBench.models import Profile
        _worker('expelled', seen_days_ago=1)
        Profile.objects.filter(user__username='expelled').update(enabled=False)

        self.assertNoSlider(
            _signed_in('expelled').get(f'/atomicdb/explore/{self.pos.key}/'))

    def test_a_revoked_account_cannot_choose_through_the_endpoint_either(self):
        from OpenBench.models import Profile
        _worker('expelled', seen_days_ago=1)
        Profile.objects.filter(user__username='expelled').update(enabled=False)

        response = _signed_in('expelled').post(
            f'/atomicdb/request/{self.pos.key}/', {'budget': LADDER[-1]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[0])

    def test_a_solved_position_has_no_selector_for_anyone(self):
        _staff()
        Position.objects.filter(pk=self.pos.pk).update(status='DRAW',
                                                       closure='MINIMAX')

        self.assertNoSlider(
            _signed_in('belzedar').get(f'/atomicdb/explore/{self.pos.key}/'))


class OfferedRungTests(TestCase):
    """Que peldanos ofrece, y cual sale marcado por defecto."""

    def setUp(self):
        self.pos = _position()
        self.user = _staff()

    def _context(self):
        request = type('R', (), {'user': self.user})()
        return depth.context(request, Position.objects.get(pk=self.pos.pk))

    def test_a_virgin_position_offers_the_whole_ladder(self):
        context = self._context()

        self.assertEqual([rung['nodes'] for rung in context['depth_rungs']],
                         list(LADDER))
        self.assertEqual(context['depth_default_label'], '128.0M')
        self.assertEqual(context['depth_spent_label'], '')

    def test_a_spent_rung_is_never_offered_again(self):
        # 128M completado: la escalera empieza en 512M, que es exactamente lo
        # que el boton compraria solo.  Volver a ofrecer 128M seria ofrecer una
        # busqueda que ya tenemos.
        _completed(self.pos, LADDER[0])

        context = self._context()

        self.assertEqual([rung['nodes'] for rung in context['depth_rungs']],
                         list(LADDER[1:]))
        self.assertEqual(context['depth_default_label'], '512.0M')
        self.assertEqual(context['depth_spent_label'], '128.0M')

    def test_the_deepest_completed_search_is_reported_as_it_is(self):
        # Una sonda automatica de 8M no es un peldano de la escalera, y decir
        # "already searched to 128M" por redondear al peldano de al lado seria
        # publicar una profundidad que nadie compro.
        _completed(self.pos, 8_000_000)

        self.assertEqual(self._context()['depth_spent_label'], '8.0M')

    def test_one_rung_left_is_no_choice_at_all(self):
        # Con 2B gastado solo queda 10B: un deslizador de una sola parada no
        # elige nada y el boton ya compra exactamente eso.
        _completed(self.pos, LADDER[-2])

        self.assertEqual(self._context(), {})

    def test_a_spent_ladder_offers_nothing(self):
        # Aqui el click se convierte en anchura un ply mas abajo, asi que un
        # selector de PROFUNDIDAD prometeria algo que la vista no hace.
        _completed(self.pos, LADDER[-1])

        self.assertEqual(self._context(), {})
        self.assertTrue(ingest.ladder_exhausted(
            Position.objects.get(pk=self.pos.pk)))


class DefaultRequestTests(TestCase):
    """Sin elegir nada, el boton compra lo que compraba ayer."""

    def setUp(self):
        self.pos = _position()

    def test_a_post_without_a_choice_buys_the_ladder_rung(self):
        response = Client().post(f'/atomicdb/request/{self.pos.key}/')

        self.assertEqual(response.json()['status'], 'queued')
        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[0])

    def test_the_body_of_an_ordinary_request_does_not_grow(self):
        # El contrato que ya existia: 'queued' lleva status y ahead, y nada
        # mas.  El selector no anade una clave a la respuesta de nadie.
        body = Client().post(f'/atomicdb/request/{self.pos.key}/').json()

        self.assertEqual(set(body), {'status', 'ahead'})

    def test_an_entitled_account_that_does_not_choose_gets_the_same_rung(self):
        _worker('wolfram', seen_days_ago=1)

        response = _signed_in('wolfram').post(
            f'/atomicdb/request/{self.pos.key}/')

        self.assertEqual(response.json()['status'], 'queued')
        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[0])

    def test_the_second_rung_still_escalates_on_its_own(self):
        _completed(self.pos, LADDER[0])
        Position.objects.filter(pk=self.pos.pk).update(visits=1)

        Client().post(f'/atomicdb/request/{self.pos.key}/')

        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[1])


class ChosenRungTests(TestCase):
    """Con derecho a elegir, se encola el peldano elegido."""

    def setUp(self):
        self.pos = _position()

    def test_a_contributor_can_jump_straight_to_the_top_rung(self):
        _worker('wolfram', seen_days_ago=2)

        response = _signed_in('wolfram').post(
            f'/atomicdb/request/{self.pos.key}/',
            {'budget': LADDER[-1], 'confirm': '1'})

        self.assertEqual(response.json()['status'], 'queued')
        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[-1])

    def test_the_owner_can_too(self):
        _staff()

        _signed_in('belzedar').post(f'/atomicdb/request/{self.pos.key}/',
                                    {'budget': LADDER[2]})

        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[2])

    def test_the_top_rung_requires_an_explicit_confirmation(self):
        _worker('wolfram', seen_days_ago=1)

        response = _signed_in('wolfram').post(
            f'/atomicdb/request/{self.pos.key}/', {'budget': LADDER[-1]})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['status'], 'confirm-10b')
        self.assertFalse(AnalysisTask.objects.exists())

    def test_a_choice_below_the_due_rung_cannot_cheapen_the_request(self):
        # 512M es lo que toca; pedir 128M no compra una busqueda mas pobre —
        # una eleccion solo puede SUBIR el suelo.  La plantilla ni siquiera
        # ofrece ese peldano, asi que esto es la red de abajo.
        _worker('wolfram', seen_days_ago=1)
        _completed(self.pos, LADDER[0])
        Position.objects.filter(pk=self.pos.pk).update(visits=1)

        _signed_in('wolfram').post(f'/atomicdb/request/{self.pos.key}/',
                                   {'budget': LADDER[0]})

        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[1])

    def test_a_deeper_choice_lifts_a_request_that_is_still_waiting(self):
        # El dedup por ip+posicion existe para que un click repetido no compre
        # dos veces lo mismo.  Elegir un peldano no es repetir el click: es
        # decir que lo encolado se queda corto, y la tarea que espera sube.
        _worker('wolfram', seen_days_ago=1)
        client = _signed_in('wolfram')
        client.post(f'/atomicdb/request/{self.pos.key}/')

        client.post(f'/atomicdb/request/{self.pos.key}/',
                    {'budget': LADDER[-1], 'confirm': '1'})

        self.assertEqual(AnalysisTask.objects.filter(position=self.pos)
                         .count(), 1)
        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[-1])

    def test_a_repeated_click_without_a_choice_still_deduplicates(self):
        _worker('wolfram', seen_days_ago=1)
        client = _signed_in('wolfram')
        client.post(f'/atomicdb/request/{self.pos.key}/')

        response = client.post(f'/atomicdb/request/{self.pos.key}/')

        self.assertEqual(response.json()['status'], 'already-requested')

    @override_settings(ATOMICDB_BREADTH_SWAP=True)
    def test_an_explicit_choice_buys_depth_even_with_the_swap_on(self):
        # El swap convierte un click POR DEFECTO en anchura porque ahi es donde
        # esta la informacion marginal.  Quien elige un peldano esta pidiendo
        # justo lo contrario, y era esa queja la que acoto el swap.
        _worker('wolfram', seen_days_ago=1)
        _completed(self.pos, LADDER[1])
        Position.objects.filter(pk=self.pos.pk).update(visits=2, eval_cp=30)

        response = _signed_in('wolfram').post(
            f'/atomicdb/request/{self.pos.key}/',
            {'budget': LADDER[-1], 'confirm': '1'})

        self.assertEqual(response.json()['status'], 'queued')
        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[-1])


class ForgedChoiceTests(TestCase):
    """La comprobacion que no puede vivir en la plantilla."""

    def setUp(self):
        self.pos = _position()

    def test_an_anonymous_post_cannot_buy_the_top_rung(self):
        response = Client().post(f'/atomicdb/request/{self.pos.key}/',
                                 {'budget': LADDER[-1]})

        self.assertEqual(response.json()['status'], 'queued')
        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[0])

    def test_an_account_without_a_recent_worker_cannot_either(self):
        worker_account('lesha')

        response = _signed_in('lesha').post(
            f'/atomicdb/request/{self.pos.key}/', {'budget': LADDER[-1]})

        self.assertEqual(response.json()['status'], 'queued')
        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[0])

    def test_a_worker_that_went_quiet_a_week_ago_cannot_either(self):
        _worker('wolfram', seen_days_ago=8)

        _signed_in('wolfram').post(f'/atomicdb/request/{self.pos.key}/',
                                   {'budget': LADDER[-1]})

        self.assertEqual(_pending(self.pos).budget_nodes, LADDER[0])

    def test_a_budget_outside_the_ladder_is_refused_and_nothing_is_queued(self):
        _worker('wolfram', seen_days_ago=1)

        response = _signed_in('wolfram').post(
            f'/atomicdb/request/{self.pos.key}/', {'budget': 999})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['status'], 'bad-budget')
        self.assertFalse(AnalysisTask.objects.exists())
        self.assertFalse(RequestLog.objects.exists())

    def test_a_budget_above_the_ladder_is_refused_too(self):
        # El caso que de verdad importa de la validacion: sin ella, un numero
        # mas grande que el ultimo peldano se colaria tal cual.
        _worker('wolfram', seen_days_ago=1)

        response = _signed_in('wolfram').post(
            f'/atomicdb/request/{self.pos.key}/',
            {'budget': LADDER[-1] * 100})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AnalysisTask.objects.exists())

    def test_a_budget_that_is_not_even_a_number_is_refused(self):
        _worker('wolfram', seen_days_ago=1)

        response = _signed_in('wolfram').post(
            f'/atomicdb/request/{self.pos.key}/', {'budget': 'deepest'})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AnalysisTask.objects.exists())
