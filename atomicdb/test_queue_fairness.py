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

from . import contributors, ingest, live_request, logic, views
from .models import AnalysisTask, Edge, Position, WorkerPing
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


class _QueueHarness(TestCase):
    """Utillaje comun.  Sin tests propios a proposito: heredarlos haria que
    la racha de 1.562 filas se montase una vez por subclase."""

    RUNG = 128_000_000

    def setUp(self):
        worker_account('w', 'p')
        self.client = Client()

    def _positions(self, count, offset=0):
        rows = [Position(key=f'{index + offset:064d}', fen=logic.start_fen(),
                         status='UNKNOWN', expanded=False)
                for index in range(count)]
        Position.objects.bulk_create(rows, batch_size=1000)
        return rows

    def _queue(self, owner, budgets, offset=0):
        """Encola ``budgets`` a nombre de ``owner``, en ese orden."""
        positions = self._positions(len(budgets), offset=offset)
        tasks = [AnalysisTask(position=position, generation=0,
                              budget_nodes=budget,
                              source=AnalysisTask.Source.USER,
                              requested_by=owner, state='PENDING')
                 for position, budget in zip(positions, budgets)]
        return AnalysisTask.objects.bulk_create(tasks, batch_size=1000)

    def _lease(self, machine, username='w'):
        return self.client.post('/atomicdb/api/lease', {
            'username': username, 'password': 'p', 'machine': machine,
            'worker_build': '2026072203', 'lease_session': machine,
        }).json()['tasks']

    def _serve_order(self, count):
        """Los ``count`` primeros ids que la cola entrega DE VERDAD."""
        served = []
        for index in range(count):
            tasks = self._lease(f'drain{index}')
            if not tasks:
                break
            served.append(tasks[0]['id'])
        return served


class FairShareTests(_QueueHarness):
    """Reparto justo ponderado por coste: el ultimo escalon ya no es FIFO.

    La racha del 6-ago a las 18:00 UTC — 1.562 peticiones de una cuenta en una
    hora, sobre un pool de nueve procesos — dejaba al siguiente en llegar
    detras de las 1.562, pidiese lo que pidiese.  Con la suma de nodos que ese
    mismo peticionario ya tiene por delante, el primero de cada uno empata a
    cero y cobra por ``id``; el segundo espera a que los demas hayan cobrado
    el suyo.
    """

    def test_the_first_request_of_each_account_precedes_every_second_one(self):
        first_a, second_a = self._queue('a', [self.RUNG] * 2)
        first_b, second_b = self._queue('b', [self.RUNG] * 2, offset=100)

        self.assertEqual(self._serve_order(4),
                         [first_a.id, first_b.id, second_a.id, second_b.id])

    def test_a_ten_billion_request_yields_its_turn_to_the_cheap_ones(self):
        """Ponderado por NODOS: 10B cuesta 78 veces lo que 128M, y cede 78."""
        deep = self._queue('heavy', [10_000_000_000] * 2)
        light = self._queue('light', [self.RUNG] * 80, offset=100)

        served = self._serve_order(81)

        # El primero de cada uno empata a cero y desempata el id; a partir de
        # ahi el de 10B no vuelve hasta que el barato ha gastado sus 10B.
        self.assertEqual(served[0], deep[0].id)
        self.assertEqual(served[1], light[0].id)
        self.assertEqual(served[2:80], [task.id for task in light[1:79]])
        self.assertEqual(served[80], deep[1].id)

    def test_the_burst_of_the_sixth_of_august_no_longer_blocks_a_newcomer(self):
        """RECIBO: 1.562 de una cuenta, y detras UNA de un recien llegado."""
        burst = self._queue('soothdest', [self.RUNG] * 1562)
        newcomer, = self._queue('newcomer', [self.RUNG], offset=2000)

        # ANTES (FIFO puro por id): las 1.562 por delante, una a una.
        fifo_ahead = AnalysisTask.objects.filter(
            state='PENDING', source=AnalysisTask.Source.USER,
            id__lt=newcomer.id).count()
        self.assertEqual(fifo_ahead, 1562)

        # DESPUES: solo la PRIMERA de la racha cobra antes.
        self.assertEqual(live_request.queue_ahead(newcomer), 1)
        self.assertEqual(self._serve_order(2), [burst[0].id, newcomer.id])

    def test_within_one_account_the_order_of_arrival_is_untouched(self):
        mine = self._queue('solo', [self.RUNG] * 5)

        self.assertEqual(self._serve_order(5), [task.id for task in mine])

    def test_the_estimator_agrees_with_the_order_actually_served(self):
        """TRAMPA B: si el orden y la cifra divergen, que lo cace el CI.

        El sitio le decia a un humano "1.500 por delante" con cuatro de
        verdad porque el estimador seguia contando en FIFO.  Aqui se pide la
        cifra ANTES de servir y se compara con el sitio real.

        Sin llegar al peldano de 10B a proposito: el estimador NO modela el
        tope de arriendos profundos — es una condicion del momento del
        reparto, no un sitio en la cola — y meterlo aqui seria pedirle que
        acierte algo que dice explicitamente que no promete."""
        self._queue('a', [self.RUNG, 2_000_000_000, self.RUNG])
        self._queue('b', [2_000_000_000, self.RUNG], offset=100)
        self._queue('c', [self.RUNG] * 4, offset=200)
        pending = list(AnalysisTask.objects.filter(state='PENDING'))
        estimated = {task.id: live_request.queue_ahead(task)
                     for task in pending}

        served = self._serve_order(len(pending))

        self.assertEqual(len(served), len(pending))
        self.assertEqual([estimated[task_id] for task_id in served],
                         list(range(len(served))))


class AnonymousBucketTests(_QueueHarness):
    """La marea sin login es UN cubo: no se la puede distinguir, asi que se
    la reparte como a una cuenta sola.  La promocion por inanicion sigue."""

    def test_the_anonymous_flood_is_shared_out_as_a_single_account(self):
        flood = self._queue('', [self.RUNG] * 6)
        named = self._queue('quasa', [self.RUNG] * 2, offset=100)

        # El estrato nombrado va primero entero; dentro del cubo anonimo el
        # reparto no reordena nada, porque todas son del mismo cubo.
        self.assertEqual(self._serve_order(4),
                         [named[0].id, named[1].id, flood[0].id, flood[1].id])

    def test_a_starved_anonymous_click_still_reaches_the_front(self):
        flood = self._queue('', [self.RUNG] * 3)
        AnalysisTask.objects.filter(pk=flood[0].pk).update(
            created=timezone.now() - timedelta(hours=30))
        named = self._queue('quasa', [self.RUNG] * 2, offset=100)

        # La promocionada entra en el estrato nombrado y su id la pone
        # delante; el reparto no se lo impide, porque en su cubo es la
        # primera y lleva cero nodos por delante.
        self.assertEqual(self._serve_order(3),
                         [flood[0].id, named[0].id, named[1].id])

    def test_the_starved_tier_never_inherits_the_deficit_of_the_fresh_one(self):
        """Las promocionadas son SIEMPRE el prefijo por id del cubo anonimo.

        La promocion es por edad y el id crece con la edad, asi que una fila
        fresca nunca queda por delante de una promocionada dentro del mismo
        cubo — y por tanto nunca puede inflarle la suma.

        Con el peldano de 2B, no el de 10B: aqui se mide el reparto, y el tope
        de arriendos profundos (que tiene sus propios tests) se llevaria la
        segunda promocionada por delante y taparia lo que se quiere ver."""
        flood = self._queue('', [2_000_000_000] * 2 + [self.RUNG] * 3)
        for task in flood[:2]:
            AnalysisTask.objects.filter(pk=task.pk).update(
                created=timezone.now() - timedelta(hours=30))
        named = self._queue('quasa', [self.RUNG], offset=100)

        served = self._serve_order(3)

        # La primera promocionada lleva cero nodos delante y empata con la
        # nombrada; la segunda arrastra los 10B de la primera y espera.
        self.assertEqual(served[:2], [flood[0].id, named[0].id])
        self.assertEqual(served[2], flood[1].id)


class DeepLeaseCapTests(_QueueHarness):
    """Tope de tareas del peldano mas alto ARRENDADAS a la vez por cuenta.

    Lo que el reparto justo no puede arreglar: un arriendo de media hora no se
    expropia.  Medido el 7-ago a las 10:21 UTC — soothdest con 9 de los 10
    arriendos vivos, tres de ellos de 10B."""

    DEEP = 10_000_000_000

    def _pool(self, slots, prefix='slot', last_seen=None):
        """Un pool vivo de ``slots`` procesos, que es lo que fija el cupo.

        Los slots se llaman como las maquinas desde las que luego se arrienda:
        ``_touch_worker`` refresca esas mismas filas en vez de anadir otras, y
        el pool medido es el que dice el test y no el doble.  ``last_seen`` es
        ``auto_now``, asi que envejecer un slot es un UPDATE, no un save."""
        names = [f'{prefix}{index}' for index in range(slots)]
        for name in names:
            WorkerPing.objects.get_or_create(
                machine=name, user='w',
                defaults={'threads': 8, 'hash_mb': 256, 'os': 'linux'})
        if last_seen is not None:
            WorkerPing.objects.filter(machine__in=names).update(
                last_seen=last_seen)
        return names

    def _held(self, owner, count, budget):
        """``count`` tareas de ``owner`` YA arrendadas y vivas."""
        tasks = self._queue(owner, [budget] * count, offset=900)
        now = timezone.now()
        AnalysisTask.objects.filter(id__in=[t.id for t in tasks]).update(
            state='LEASED', machine='held', leased_at=now,
            lease_heartbeat_at=now, lease_token='t', attempts=1)
        return tasks

    def _drain(self, count, slots):
        """Sirve ``count`` tareas reusando SIEMPRE los mismos slots.

        Cada arriendo suelta la identidad de maquina en cuanto se le entrega:
        la tarea sigue ARRENDADA — y por tanto sigue pesando en el reparto —
        pero el slot vuelve a estar libre para pedir la siguiente.  Sin esto
        haria falta una maquina nueva por arriendo, y cada maquina nueva
        agranda el pool vivo y con el el propio cupo que se esta midiendo."""
        served = []
        for index in range(count):
            tasks = self._lease(slots[index % len(slots)])
            if not tasks:
                break
            served.append(tasks[0]['id'])
            AnalysisTask.objects.filter(id=tasks[0]['id']).update(
                machine=f'busy{index}')
        return served

    def _straddle(self):
        """Cola en la que la tarea profunda de ``heavy`` cae EXACTAMENTE en el
        puesto 16, con trabajo de otro por delante y por detras.

        ``heavy`` ya tiene tres arriendos de 10B, o sea 30B de deuda, asi que
        su cuarta profunda entra a la altura de la decimosexta de ``other``
        (15 x 2B = 30B) y gana el desempate por id.  Es el unico sitio donde
        el tope se puede VER: mas atras el reparto ya lo frenaba solo, y mas
        adelante no hay con quien repartir y la segunda pasada lo suelta."""
        self._held('heavy', 3, self.DEEP)
        deep, = self._queue('heavy', [self.DEEP], offset=950)
        other = self._queue('other', [2_000_000_000] * 20, offset=100)
        return deep, other

    def test_one_account_cannot_hold_more_deep_leases_than_its_share(self):
        slots = self._pool(9)              # 9 // 3 = 3
        deep, other = self._straddle()

        served = self._drain(16, slots)

        self.assertEqual(views.deep_lease_cap(), 3)
        # El puesto 16 le tocaba a la profunda de heavy y se lo cede.
        self.assertNotIn(deep.id, served)
        self.assertEqual(served[15], other[15].id)
        self.assertEqual(AnalysisTask.objects.filter(
            state='LEASED', requested_by='heavy',
            budget_nodes__gte=self.DEEP).count(), 3)

    def test_without_the_cap_that_same_slot_would_have_gone_deep(self):
        """El contraste que hace honesto al test de arriba."""
        slots = self._pool(90)             # 90 // 3 = 30, muy por encima
        deep, _other = self._straddle()

        served = self._drain(16, slots[:9])

        self.assertEqual(served[15], deep.id)

    def test_the_rest_are_served_as_soon_as_a_deep_lease_frees_up(self):
        slots = self._pool(9)
        deep, _other = self._straddle()
        self._drain(16, slots)
        self.assertNotIn(deep.id, AnalysisTask.objects.filter(
            state='LEASED').values_list('id', flat=True))

        # Uno de los tres profundos entrega: el cupo deja sitio y la que
        # esperaba entra en el siguiente arriendo.
        AnalysisTask.objects.filter(
            state='LEASED', requested_by='heavy',
            budget_nodes__gte=self.DEEP).order_by('id').first().delete()

        self.assertEqual(self._drain(1, slots), [deep.id])

    def test_no_slot_is_left_idle_when_only_deep_work_remains(self):
        """RECIBO: el tope ordena, nunca apaga hierro donado."""
        self._pool(9)
        self._queue('heavy', [self.DEEP] * 20)

        served = [self._lease(f'slot{index}') for index in range(9)]

        self.assertTrue(all(tasks for tasks in served),
                        'un slot se quedo sin tarea habiendo cola')
        self.assertEqual(AnalysisTask.objects.filter(state='LEASED').count(), 9)

    def test_the_cap_follows_the_pool_and_never_drops_below_one(self):
        self._pool(2)
        self.assertEqual(views.deep_lease_cap(), 1)
        WorkerPing.objects.all().delete()
        self._pool(90)
        self.assertEqual(views.deep_lease_cap(), 30)

    def test_a_dead_slot_does_not_widen_anybody_quota(self):
        self._pool(9, prefix='gone',
                   last_seen=timezone.now() - timedelta(minutes=10))
        self._pool(3)

        self.assertEqual(views.deep_lease_cap(), 1)

    def test_the_cheap_rungs_are_not_capped(self):
        self._pool(9)
        self._queue('busy', [2_000_000_000] * 20)

        for index in range(9):
            self._lease(f'slot{index}')

        self.assertEqual(AnalysisTask.objects.filter(
            state='LEASED', requested_by='busy').count(), 9)
