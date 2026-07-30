"""A click is a click: the USER band is served strictly first-come.

Ordering humans by POSITION priority made a deep, losing line (strongly
negative priority — a mate the selector rightly despises) wait behind every
fresh click from anybody else.  Wolfram queued a line's replies and watched
an empty-looking queue serve everyone but him for an hour.  Position
priority still rules AUTO/FILL/SEED, where the selector computes it for
exactly that purpose.
"""

from django.test import Client

from . import ingest, logic
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
