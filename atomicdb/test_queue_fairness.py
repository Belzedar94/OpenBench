"""A click is a click: the USER band is served strictly first-come.

Ordering humans by POSITION priority made a deep, losing line (strongly
negative priority — a mate the selector rightly despises) wait behind every
fresh click from anybody else.  Wolfram queued a line's replies and watched
an empty-looking queue serve everyone but him for an hour.  Position
priority still rules AUTO/FILL/SEED, where the selector computes it for
exactly that purpose.
"""

from datetime import timedelta

from django.test import Client
from django.utils import timezone

from . import contributors, ingest, logic
from .models import AnalysisTask, Edge, Position
from .testing import TestCase, worker_account


class UserBandFifoTests(TestCase):

    def setUp(self):
        worker_account('w', 'p')
        worker_account('lesha', 'p')
        self.client = Client()
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        edges = list(Edge.objects.filter(parent=root).order_by('move_uci')[:2])
        self.despised = edges[0].child      # deep losing line
        self.beloved = edges[1].child       # fresh attractive line
        Position.objects.filter(key=self.despised.key).update(priority=-73.0)
        Position.objects.filter(key=self.beloved.key).update(priority=8.0)

    def _lease(self, machine, username='w'):
        return self.client.post('/atomicdb/api/lease', {
            'username': username, 'password': 'p', 'machine': machine,
            'worker_build': '2026072203', 'lease_session': machine,
        }).json()

    def test_an_older_click_is_served_before_a_better_positioned_one(self):
        older = AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER)
        AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER)

        leased = self._lease('m1')['tasks'][0]

        self.assertEqual(leased['id'], older.id)

    def test_the_selector_bands_still_follow_position_priority(self):
        AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.AUTO)
        wanted = AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.AUTO)

        leased = self._lease('m2')['tasks'][0]

        self.assertEqual(leased['id'], wanted.id)

    def test_a_user_click_still_outranks_every_selector_task(self):
        AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.AUTO)
        clicked = AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER)

        leased = self._lease('m3')['tasks'][0]

        self.assertEqual(leased['id'], clicked.id)


class OwnRequestAffinityTests(UserBandFifoTests):
    """Quien pone hierro cobra lo suyo primero, sin robarle el FIFO a nadie.

    Un worker autenticado con la cuenta X sirve antes las peticiones hechas
    por X; agotadas las suyas, vuelve al primero-en-llegar de siempre.  Las
    bandas AUTO/FILL y las peticiones anonimas no cambian."""

    def test_a_workers_own_request_jumps_its_band_queue(self):
        AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')
        own = AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='lesha')

        leased = self._lease('m4', username='lesha')['tasks'][0]

        self.assertEqual(leased['id'], own.id)

    def test_after_its_own_the_worker_rejoins_the_fifo(self):
        oldest_foreign = AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')
        AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='lesha')

        self._lease('m5', username='lesha')
        second = self._lease('m6', username='lesha')['tasks'][0]

        self.assertEqual(second['id'], oldest_foreign.id)

    def test_a_named_stranger_now_beats_an_older_anonymous_click(self):
        # CONTRATO CAMBIADO (31-jul, caso Lesha): antes las anonimas
        # conservaban el FIFO plano contra todos; desde el estrato
        # named_first, un humano identificado no espera detras de la marea
        # sin login.  Entre nombradas y entre anonimas, el FIFO sigue.
        AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER)
        named = AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='w')

        leased = self._lease('m7', username='lesha')['tasks'][0]

        self.assertEqual(leased['id'], named.id)

    def test_a_declared_route_is_stored_and_wins_the_label(self):
        """La peticion viaja con el ORDEN DE JUGADAS de su autor.

        El DAG transpone: el linaje canonico puede pintar otro orden y el
        autor no reconoce su propia peticion en la portada (avistamiento
        del 29-jul con 1.Nf3 d6 contra 1.Nf3 f6)."""
        route = 'g1f3,d7d6,b1c3,f7f6'
        fen = logic.start_fen()
        prev = ingest.get_or_create_position(fen)
        for uci in route.split(','):
            fen = logic.apply_move(fen, uci)
            child = ingest.get_or_create_position(fen)
            Edge.objects.get_or_create(parent=prev, move_uci=uci,
                                       defaults={'child': child})
            prev = child

        response = self.client.post(f'/atomicdb/request/{prev.key}/',
                                    {'route': route})

        self.assertEqual(response.json()['status'], 'queued')
        task = AnalysisTask.objects.get(position=prev)
        self.assertEqual(task.route, route)
        from .views import _route_labels
        preview, full = _route_labels(route, prev.key)
        self.assertEqual(preview, '1. Nf3 d6 2. Nc3 f6')
        self.assertEqual(full, '1. Nf3 d6 2. Nc3 f6')
        # Al frente de Up next: la portada solo pinta el top de prioridad.
        Position.objects.filter(pk=prev.pk).update(priority=99.0)
        home = self.client.get('/atomicdb/')
        self.assertContains(home, '1. Nf3 d6 2. Nc3 f6')

    def test_a_broken_route_is_ignored_but_the_click_still_lands(self):
        pos = ingest.get_or_create_position(logic.start_fen())

        response = self.client.post(f'/atomicdb/request/{pos.key}/',
                                    {'route': 'g1f3,zzzz'})

        self.assertEqual(response.json()['status'], 'queued')
        task = AnalysisTask.objects.get(position=pos)
        self.assertEqual(task.route, '')

    def test_the_request_endpoint_records_the_logged_in_requester(self):
        self.client.login(username='lesha', password='p')

        response = self.client.post(
            f'/atomicdb/request/{self.beloved.key}/')

        self.assertEqual(response.json()['status'], 'queued')
        task = AnalysisTask.objects.get(position=self.beloved,
                                        source=AnalysisTask.Source.USER)
        self.assertEqual(task.requested_by, 'lesha')


class NamedBeforeAnonymousTests(UserBandFifoTests):
    """Las peticiones CON NOMBRE cobran antes que la marea anonima.

    Un "Analyse all" sin login encola decenas de tareas de cobertura con
    ``requested_by=''``; un humano identificado que pide UNA posicion no debe
    esperar detras de miles de esas (Lesha, 31-jul: sus requests en el puesto
    ~1400 del FIFO).  Dentro de cada estrato, el FIFO intacto; el own-first
    del worker sigue mandando por encima."""

    def test_a_named_request_jumps_the_anonymous_flood(self):
        AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='')
        named = AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='quasa')

        leased = self._lease('m20')['tasks'][0]

        self.assertEqual(leased['id'], named.id)

    def test_own_first_still_beats_a_named_stranger(self):
        AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='quasa')
        own = AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='lesha')

        leased = self._lease('m21', username='lesha')['tasks'][0]

        self.assertEqual(leased['id'], own.id)

    def test_the_click_receipt_tells_how_many_are_ahead(self):
        # Una anonima delante; el click logueado salta la marea y su
        # recibo dice cuantas NOMBRADAS le preceden (aqui: ninguna).
        AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='')
        self.client.login(username='lesha', password='p')

        response = self.client.post(
            f'/atomicdb/request/{self.beloved.key}/')

        body = response.json()
        self.assertEqual(body['status'], 'queued')
        self.assertEqual(body['ahead'], 0)

    def test_an_anonymous_click_counts_the_whole_named_tier_ahead(self):
        named = AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='quasa')
        self.assertIsNotNone(named)

        response = self.client.post(
            f'/atomicdb/request/{self.beloved.key}/')

        body = response.json()
        self.assertEqual(body['status'], 'queued')
        self.assertEqual(body['ahead'], 1)


class BulkRouteInheritanceTests(UserBandFifoTests):
    """El click masivo tambien viaja con la ruta del autor (bug 31-jul).

    El fix de rutas cubrio el click individual, pero "Analyse all" creaba a
    las hijas sin ruta y sus avisos volvian en linaje canonico: quien pedia
    por 1.Nf3 d6 recibia campanadas contando 1.Nf3 f6."""

    def test_bulk_children_inherit_route_plus_their_move(self):
        route = 'g1f3,d7d6'
        fen = logic.start_fen()
        pos = ingest.get_or_create_position(fen)
        for uci in route.split(','):
            fen = logic.apply_move(fen, uci)
            pos = ingest.get_or_create_position(fen)
        ingest.expand(pos)

        queued = ingest.enqueue_unexplored_children(
            pos, requested_by='lesha', route=route)

        self.assertGreater(queued, 0)
        for task in AnalysisTask.objects.filter(
                position__edges_in__parent=pos,
                source=AnalysisTask.Source.USER):
            edge = Edge.objects.get(parent=pos, child=task.position)
            self.assertEqual(task.route, route + ',' + edge.move_uci)
            self.assertEqual(task.requested_by, 'lesha')


class StarvedRequestTests(UserBandFifoTests):
    """Una espera larga vale por un nombre.

    El estrato NOMBRADO ordena el trafico del dia, pero sin caducidad no
    ordena: mata de hambre.  30-jul en produccion: 318 peticiones USER
    PENDING, 269 de mas de 24h y 126 de mas de 72h, con la cola sirviendo sin
    parar (6 arriendos, todos jovenes).  Mientras entre UNA nombrada al dia,
    lo anonimo no llega jamas al frente.  Pasado ``STARVED_AFTER`` la fila
    entra en el estrato nombrado, donde su id la pone la primera.
    """

    def _age(self, task, hours):
        AnalysisTask.objects.filter(pk=task.pk).update(
            created=timezone.now() - timedelta(hours=hours))
        return task

    def test_a_starved_anonymous_click_beats_a_fresh_named_one(self):
        # La inversion exacta de antes: sin nombre valia 0 en ``named_first``
        # y cualquier nombrada fresca la adelantaba, para siempre.
        starved = self._age(AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by=''), hours=30)
        AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='quasa')

        leased = self._lease('m30')['tasks'][0]

        self.assertEqual(leased['id'], starved.id)

    def test_the_fresh_anonymous_flood_still_waits_its_turn(self):
        """El contrato de Lesha intacto: lo que caduca es la espera, no la regla."""
        AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='')
        named = AnalysisTask.objects.create(
            position=self.beloved, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='quasa')

        leased = self._lease('m31')['tasks'][0]

        self.assertEqual(leased['id'], named.id)

    def test_the_receipt_counts_the_starved_tier_ahead(self):
        self._age(AnalysisTask.objects.create(
            position=self.despised, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by=''), hours=30)
        self.client.login(username='lesha', password='p')

        response = self.client.post(
            f'/atomicdb/request/{self.beloved.key}/')

        # La misma anonima que antes valia cero: ahora cobra antes que este
        # click, y el recibo lo dice en vez de prometer un turno que no hay.
        self.assertEqual(response.json()['ahead'], 1)


class AheadCountTruthTests(UserBandFifoTests):
    """La cifra de "cuanta cola tengo delante" cuenta solo lo serveable.

    Sintoma reportado: tras pedir, el sitio anuncia MAS peticiones por delante
    de las que existen de verdad.  Una PENDING sobre una posicion ya cerrada no
    la sirve nadie — ``choose_pending`` salta lo que no esta en 'UNKNOWN' — asi
    que sumarla es pintarle al humano una cola que no va a moverse.
    """

    def _children(self, count):
        root = ingest.get_or_create_position(logic.start_fen())
        return [edge.child for edge in Edge.objects.filter(parent=root)
                .order_by('move_uci')[:count]]

    def _named_pending(self, position):
        return AnalysisTask.objects.create(
            position=position, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='quasa')

    def test_a_fresh_request_does_not_count_the_zombies_ahead_of_it(self):
        alive, dead_one, dead_two, mine = self._children(4)
        for position in (alive, dead_one, dead_two):
            self._named_pending(position)
        Position.objects.filter(key__in=[dead_one.key, dead_two.key]).update(
            status='WHITE_WIN', closure='MINIMAX')
        self.client.login(username='lesha', password='p')

        response = self.client.post(f'/atomicdb/request/{mine.key}/')

        body = response.json()
        self.assertEqual(body['status'], 'queued')
        # Tres filas por delante, dos de ellas zombis: cobra UNA.
        self.assertEqual(body['ahead'], 1)

    def test_the_profile_queue_counts_the_same_way(self):
        alive, dead, mine = self._children(3)
        self._named_pending(alive)
        self._named_pending(dead)
        Position.objects.filter(key=dead.key).update(status='WHITE_WIN',
                                                     closure='MINIMAX')
        waiting = AnalysisTask.objects.create(
            position=mine, generation=0, budget_nodes=8_000_000,
            source=AnalysisTask.Source.USER, requested_by='lesha')

        pending, _leased, _done = contributors._queue_rows('lesha')

        self.assertEqual([task.id for task in pending], [waiting.id])
        self.assertEqual(pending[0].ahead, 1)
