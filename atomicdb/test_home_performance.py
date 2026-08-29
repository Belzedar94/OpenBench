"""Lo que la portada tiene PROHIBIDO volver a costar.

La portada llego a tardar 6,2 segundos y a hacer 119 consultas con la base
real (3,5M posiciones): tres barridos de ``Position`` para tres contadores,
doce ``COUNT`` de atribucion de cierres, un rejuego de pyffish por cada fila
con ruta declarada y una BFS inversa por cada fila sin ella.  Nada de eso era
de nadie en particular y casi nada cambiaba entre una visita y la siguiente.

Lo que fijan estos tests, en orden de importancia:

* que el numero de consultas de una portada normal NO suba;
* que lo caro se sirva de la cache COMPARTIDA y se pueda publicar desde fuera
  de la peticion (que es lo que hace el servicio del selector);
* que los numeros que se pintan sigan siendo los mismos numeros.

El arbol sintetico es pequeno a proposito: el numero de consultas de esta
pagina depende de la PROFUNDIDAD de las lineas y del numero de filas que
pinta, no de cuantas posiciones haya en la tabla, asi que doce plies y las
cinco filas de cada columna reproducen la forma exacta de la portada real.
"""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.db import connections
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import contributors, logic, metrics, views
from .models import (AnalysisTask, Campaign, DBEvent, Edge, Position,
                     ProgressSnapshot, WorkerPing)
from .testing import TestCase


CACHE_FOR_TESTS = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'atomicdb-home-performance-tests',
    },
}


class _Deferred:
    """El ejecutor de los tests: apunta la medida de fondo y no la corre.

    Modela exactamente lo que hace el hilo de verdad — el lector se lleva su
    cifra ANTES de que la medida empiece — sin depender de un ``join`` ni de
    un ``sleep``, y deja contar cuantas se lanzaron, que es la mitad de lo que
    hay que demostrar.  Mismo utensilio que § test_stale_page_cache.
    """

    def __init__(self):
        self.jobs = []

    def __call__(self, work, name):
        self.jobs.append(work)

    def run_all(self):
        pending, self.jobs = self.jobs, []
        for work in pending:
            work()
        return len(pending)

# PRESUPUESTO DE CONSULTAS de una portada con la cache compartida caliente,
# que es el caso de CADA visitante en produccion (la cache de pagina varia por
# cookie, asi que casi nadie llega a una entrada suya ya hecha, pero todo el
# mundo llega a los datos ya calculados).  Medido en 15 con este arbol; el
# margen es para que anadir una fila a una tabla no rompa el test, no para que
# quepa otra tanda de consultas por fila.
#
# Baja de 26 a 21 porque bajaron las consultas: los cuatro contadores de
# actividad y el progreso de campanas se fueron a la instantanea compartida, y
# el widget "Up next" dejo de traerse la fila entera.  El techo se baja CON
# ellas a proposito — un techo que ya no aprieta no guarda nada.
#
# Si esto sube, la pregunta correcta no es "subo el numero" sino "que le puse
# a la portada que consulta por fila".
HOME_QUERY_BUDGET = 21
# Y con TODO frio: la unica visita que paga la BFS de linaje, los rejuegos de
# ruta y la medida de los agregados es la primera despues de vaciar la cache.
# Medido en 53 con la linea de doce plies de aqui — la BFS gasta una consulta
# por ply para TODAS las filas a la vez, asi que este techo crece con la
# profundidad del arbol y no con el numero de filas.  El techo existe para que
# eso no cambie sin que se note.
HOME_COLD_QUERY_BUDGET = 70


def build_line(plies=12, branch=2):
    """Una linea real de ``plies`` plies desde la inicial, con hermanas.

    Devuelve ``(claves_de_la_linea, ruta_uci)``.  Las hermanas existen para
    que la tabla de primeras jugadas y la BFS inversa tengan algo que elegir,
    igual que en el arbol de verdad.
    """
    fen = logic.start_fen()
    root = Position.objects.get_or_create(
        key=logic.key_of(fen), defaults={'fen': fen})[0]
    keys, ucis, parent = [root.key], [], root
    for _ in range(plies):
        moves = logic.legal_moves(fen)
        if not moves:
            break
        for uci in moves[:branch]:
            child_fen = logic.apply_move(fen, uci)
            child = Position.objects.get_or_create(
                key=logic.key_of(child_fen), defaults={'fen': child_fen})[0]
            Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                       defaults={'child': child})
        main = moves[0]
        fen = logic.apply_move(fen, main)
        parent = Position.objects.get(key=logic.key_of(fen))
        keys.append(parent.key)
        ucis.append(main)
    return keys, ','.join(ucis)


def populate_home(now):
    """Una portada con las dos columnas llenas y un feed de hitos."""
    keys, route = build_line()
    deep = keys[-1]
    Position.objects.filter(key=deep).update(priority=10_000.0)
    # "Now analyzing": un lease vivo con su ruta declarada y su worker.
    task = AnalysisTask.objects.create(
        position_id=deep, budget_nodes=128_000_000, generation=90,
        state=AnalysisTask.TState.LEASED, machine='box0', route=route,
        lease_token='tok0', leased_at=now - timedelta(minutes=1),
        lease_heartbeat_at=now - timedelta(seconds=15))
    WorkerPing.objects.create(machine='box0', threads=8, last_nps=10_000_000,
                              nps_updated=now, last_seen=now,
                              current_task_id=task.id, user='someone')
    # "Up next": peticiones humanas pendientes sobre nodos abiertos, tambien
    # con ruta.  Van sobre las hermanas del final de la linea, que es donde el
    # selector de verdad las pone.
    siblings = list(Edge.objects.filter(parent_id=keys[-2])
                    .exclude(child_id=deep).values_list('child_id', 'move_uci'))
    for index, (child_key, uci) in enumerate(siblings[:5]):
        Position.objects.filter(key=child_key).update(priority=9_000.0 - index)
        AnalysisTask.objects.create(
            position_id=child_key, budget_nodes=32_000_000, generation=1,
            state=AnalysisTask.TState.PENDING,
            source=AnalysisTask.Source.USER,
            route=','.join(route.split(',')[:-1] + [uci]))
    # Hitos: doce cierres etiquetados, que es lo que pinta el feed.
    for index in range(12):
        DBEvent.objects.create(
            kind='NODE_CLOSED',
            payload={'key': keys[-1 - (index % 4)], 'status': 'WHITE_WIN',
                     'closure': 'MINIMAX',
                     'source': ('AUTO', 'FILL', 'USER')[index % 3]},
            ts=now - timedelta(minutes=index))
    ProgressSnapshot.objects.create(
        bucket_start=now.replace(minute=0, second=0, microsecond=0),
        positions_total=Position.objects.count(),
        positions_closed=3, engine_nodes_total=987_654_321,
        frontier_and_nodes=10, frontier_dn_median=4, frontier_dn_thin=1,
        human_close_median_seconds=600, human_close_samples=7)
    return keys, route


def _home_queries(client):
    connection = connections[settings.ATOMICDB_DATABASE_ALIAS]
    with CaptureQueriesContext(connection) as captured:
        response = client.get('/atomicdb/')
    return response, list(captured.captured_queries)


@override_settings(CACHES=CACHE_FOR_TESTS)
class HomeQueryBudgetTests(TestCase):

    def setUp(self):
        cache.clear()
        metrics.reset_public_snapshot()
        self.now = timezone.now()
        populate_home(self.now)

    def test_a_cold_home_stays_under_its_ceiling(self):
        response, queries = _home_queries(Client())

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(queries), HOME_COLD_QUERY_BUDGET,
            f'a cold home page now costs {len(queries)} queries')

    def test_a_warm_home_stays_under_its_budget(self):
        # Calienta exactamente lo que calienta el servicio del selector mas
        # una visita: los contadores publicos, los linajes y las rutas.
        metrics.refresh_public_snapshot(now=self.now)
        Client().get('/atomicdb/')

        response, queries = _home_queries(Client())

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(queries), HOME_QUERY_BUDGET,
            'the home page now costs {} queries:\n{}'.format(
                len(queries),
                '\n'.join(q['sql'][:160] for q in queries)))
        # Y una portada barata porque se dejo de pintar no vale: las dos
        # columnas y sus rotulos numerados tienen que seguir ahi.
        body = response.content.decode()
        for markup in ('Now analyzing', 'Up next', 'Milestones', '1. '):
            self.assertIn(markup, body)

    def test_no_full_table_aggregate_survives_in_the_request(self):
        """Los tres barridos de ``Position`` no pueden volver a la peticion."""
        metrics.refresh_public_snapshot(now=self.now)
        Client().get('/atomicdb/')

        _response, queries = _home_queries(Client())

        statements = ' '.join(query['sql'].upper() for query in queries)
        self.assertNotIn('SUM("ATOMICDB_POSITION"."NODES_INVESTED")',
                         statements)


@override_settings(CACHES=CACHE_FOR_TESTS)
class PublicCounterTests(TestCase):

    def setUp(self):
        cache.clear()
        metrics.reset_public_snapshot()
        self.now = timezone.now()
        populate_home(self.now)
        Position.objects.filter(
            key=logic.key_of(logic.start_fen())).update(nodes_invested=1_000)

    def test_the_counters_are_the_same_counters(self):
        """Cacheado o no, el numero que se pinta es un conteo de verdad.

        Desde el 29-ago-2026 hay UN pagador por hora: con captura fresca se
        sirven SUS numeros con SU hora — el mismo conteo exacto, hecho por
        ``capture_atomicdb_progress``.  Sin captura, el barrido de respaldo
        mide en vivo y lo firma con la hora de ahora.  En ninguna de las dos
        ramas hay una estimacion.
        """
        snapshot = ProgressSnapshot.objects.order_by('-bucket_start').first()
        totals = metrics.tree_totals(now=self.now, force=True)

        self.assertEqual(totals['total'], snapshot.positions_total)
        self.assertEqual(totals['closed'], snapshot.positions_closed)
        self.assertEqual(totals['nodes'], snapshot.engine_nodes_total)
        self.assertEqual(totals['measured_at'], snapshot.bucket_start)

        ProgressSnapshot.objects.all().delete()
        totals = metrics.tree_totals(now=self.now, force=True)

        self.assertEqual(totals['total'], Position.objects.count())
        self.assertEqual(
            totals['closed'],
            Position.objects.exclude(status='UNKNOWN').count())
        self.assertEqual(totals['nodes'], 1_000)
        self.assertEqual(totals['measured_at'], self.now)

    def test_the_page_shows_what_the_snapshot_published(self):
        ProgressSnapshot.objects.update(engine_nodes_total=1_000)
        metrics.refresh_public_snapshot(now=self.now)
        ProgressSnapshot.objects.update(engine_nodes_total=999_000_000)
        Position.objects.filter(
            key=logic.key_of(logic.start_fen())).update(
                nodes_invested=999_000_000)

        body = Client().get('/atomicdb/').content.decode()

        # Lo publicado, no lo que acaba de cambiar debajo: eso es exactamente
        # lo que significa servir un contador compartido.
        self.assertIn('1,000', body)
        self.assertNotIn('999.0M', body)

    def test_the_page_says_when_it_counted(self):
        """Un numero de hace un rato tiene que decir de cuando es."""
        metrics.refresh_public_snapshot(now=self.now)

        body = Client().get('/atomicdb/').content.decode()

        # La hora que se pinta es la del DATO: el bucket de la captura, no
        # el momento de publicarla (29-ago-2026).
        bucket = (ProgressSnapshot.objects.order_by('-bucket_start')
                  .first().bucket_start)
        stamp = bucket.strftime('%Y-%m-%d %H:%M')
        self.assertIn(f'taken at {stamp} UTC', body)

    def test_a_second_reader_does_not_recount(self):
        metrics.refresh_public_snapshot(now=self.now)
        connection = connections[settings.ATOMICDB_DATABASE_ALIAS]

        with CaptureQueriesContext(connection) as captured:
            metrics.tree_totals(now=self.now)

        self.assertEqual(len(captured.captured_queries), 0)

    def test_a_stale_entry_is_served_while_one_reader_refreshes(self):
        """Cache vieja + cerrojo cogido = se sirve lo viejo, no se espera."""
        metrics.refresh_public_snapshot(now=self.now - timedelta(hours=2))
        cache.add(metrics.PUBLIC_COUNTERS_KEY + '.lock', 1, 60)
        connection = connections[settings.ATOMICDB_DATABASE_ALIAS]

        with CaptureQueriesContext(connection) as captured:
            totals = metrics.tree_totals(now=self.now)

        self.assertEqual(len(captured.captured_queries), 0)
        # Lo servido es la publicacion vieja, y su hora es la del dato que
        # llevaba dentro: el bucket de la captura (29-ago-2026).
        self.assertEqual(totals['measured_at'],
                         ProgressSnapshot.objects.order_by('-bucket_start')
                         .first().bucket_start)

    def test_an_empty_cache_falls_back_to_the_hourly_capture(self):
        """Sin nada publicado y con otro midiendo: la captura horaria, con su fecha.

        No es una estimacion: es el mismo conteo, hecho por
        ``capture_atomicdb_progress`` a su hora.  Lo unico viejo es la fecha,
        y la fecha se pinta.
        """
        cache.add(metrics.PUBLIC_COUNTERS_KEY + '.lock', 1, 60)

        totals = metrics.tree_totals(now=self.now)

        snapshot = ProgressSnapshot.objects.order_by('-bucket_start').first()
        self.assertEqual(totals['total'], snapshot.positions_total)
        self.assertEqual(totals['nodes'], snapshot.engine_nodes_total)
        self.assertEqual(totals['measured_at'], snapshot.bucket_start)
        self.assertTrue(totals['from_snapshot'])

    def test_with_no_capture_at_all_it_counts_rather_than_print_zero(self):
        """Sin cache, sin cerrojo libre y sin captura horaria: se mide.

        Un cero en la portada afirma que el arbol esta vacio, y eso es lo
        unico que no se puede decir por no querer pagar una consulta.
        """
        ProgressSnapshot.objects.all().delete()
        cache.add(metrics.PUBLIC_COUNTERS_KEY + '.lock', 1, 60)

        totals = metrics.tree_totals(now=self.now)

        self.assertEqual(totals['total'], Position.objects.count())
        self.assertGreater(totals['total'], 0)
        self.assertFalse(totals['from_snapshot'])

    def test_the_attribution_block_hides_itself_when_nobody_measured_it(self):
        """Cero cierres etiquetados y "no lo hemos medido" no son lo mismo."""
        cache.add(metrics.ATTRIBUTION_KEY + '.lock', 1, 60)

        health = views._proof_health(self.now)

        self.assertFalse(health['has_attribution'])
        self.assertNotIn('attribution_24h', health)


@override_settings(CACHES=CACHE_FOR_TESTS)
class ActivityCounterTests(TestCase):
    """Los cuatro contadores que se fueron de la peticion a la instantanea.

    Lo que se prueba es el CONTRATO de la cache, no la cache de Django: que un
    acierto no consulte, que un fallo mida de verdad, que una entrada vencida
    SE SIRVA EN EL ACTO mientras se remide por detras, y que la remedida la
    lance uno solo.  Eso ultimo es lo que distingue esta maquinaria de un
    ``cache.get``/``set``.
    """

    def setUp(self):
        cache.clear()
        metrics.reset_public_snapshot()
        self.now = timezone.now()
        populate_home(self.now)
        self.refreshes = _Deferred()
        patch = mock.patch.object(metrics, 'REVALIDATE_EXECUTOR',
                                  self.refreshes)
        patch.start()
        self.addCleanup(patch.stop)

    def _counted(self, work):
        connection = connections[settings.ATOMICDB_DATABASE_ALIAS]
        with CaptureQueriesContext(connection) as captured:
            value = work()
        return value, len(captured.captured_queries)

    def test_a_miss_measures_the_real_numbers(self):
        activity, queries = self._counted(
            lambda: metrics.activity_totals(now=self.now))

        self.assertGreater(queries, 0)
        self.assertEqual(activity['analyses'],
                         AnalysisTask.objects.filter(state='COMPLETED')
                         .count())
        self.assertEqual(
            activity['requested'],
            AnalysisTask.objects.filter(state='PENDING', source='USER',
                                        position__status='UNKNOWN').count())
        self.assertEqual(activity['closed_24h'], 12)
        self.assertEqual(activity['nodes_24h'], 0)

    def test_a_hit_does_not_measure_anything(self):
        metrics.refresh_public_snapshot(now=self.now)

        _activity, queries = self._counted(
            lambda: metrics.activity_totals(now=self.now))

        self.assertEqual(queries, 0)

    def test_a_stale_entry_is_served_without_making_anyone_wait(self):
        """Vencida la frescura, el lector se lleva LA VIEJA y no consulta nada.

        Este es el cambio entero, y el numero que lo justifica esta medido
        contra la base real (7-ago): remedir esto en la peticion son 14,4 s de
        barrido de ``Position`` mas 6,8 s de atribucion.  Antes los pagaba el
        primero que llegase pasados los 90 s; ahora no los paga nadie.
        """
        metrics.refresh_public_snapshot(now=self.now)
        AnalysisTask.objects.create(
            position_id=logic.key_of(logic.start_fen()), generation=500,
            budget_nodes=1, state=AnalysisTask.TState.COMPLETED,
            completed=self.now, nodes_searched=4_321)
        stale = self.now + timedelta(seconds=metrics.PUBLIC_FRESH_SECONDS + 1)

        before = metrics.activity_totals(now=self.now)
        after, queries = self._counted(
            lambda: metrics.activity_totals(now=stale))

        self.assertEqual(queries, 0)
        self.assertEqual(after['analyses'], before['analyses'])
        self.assertEqual(after['measured_at'], self.now)

    def test_the_measure_it_launched_publishes_the_new_numbers(self):
        """Y la cifra nueva queda publicada para el siguiente, sin peticion."""
        metrics.refresh_public_snapshot(now=self.now)
        AnalysisTask.objects.create(
            position_id=logic.key_of(logic.start_fen()), generation=500,
            budget_nodes=1, state=AnalysisTask.TState.COMPLETED,
            completed=self.now, nodes_searched=4_321)
        stale = self.now + timedelta(seconds=metrics.PUBLIC_FRESH_SECONDS + 1)
        before = metrics.activity_totals(now=self.now)

        metrics.activity_totals(now=stale)
        self.assertEqual(self.refreshes.run_all(), 1)

        after = metrics.activity_totals(now=stale)
        self.assertEqual(after['analyses'], before['analyses'] + 1)
        self.assertEqual(after['nodes_24h'], 4_321)

    def test_the_staleness_launches_exactly_one_measure(self):
        """Cinco lectores que la encuentran vieja mandan a medir UNA vez."""
        metrics.refresh_public_snapshot(now=self.now)
        stale = self.now + timedelta(seconds=metrics.PUBLIC_FRESH_SECONDS + 1)

        for _ in range(5):
            metrics.activity_totals(now=stale)

        self.assertEqual(len(self.refreshes.jobs), 1)

    def test_a_measure_that_explodes_releases_the_turn(self):
        """Si revienta se anota y se suelta el cerrojo: lo reintenta el proximo.

        Un calentador que se atasca seria peor que no tenerlo: la entrada se
        quedaria vieja para siempre sin que nadie volviera a intentarlo.
        """
        metrics.refresh_public_snapshot(now=self.now)
        stale = self.now + timedelta(seconds=metrics.PUBLIC_FRESH_SECONDS + 1)

        def boom(_now):
            raise RuntimeError('la base dijo que no')

        with mock.patch.object(metrics, '_measure_activity', boom):
            metrics.activity_totals(now=stale)
            with self.assertLogs('atomicdb.metrics', level='ERROR'):
                self.refreshes.run_all()

        self.assertIsNone(cache.get(metrics.PUBLIC_ACTIVITY_KEY + '.lock'))
        metrics.activity_totals(now=stale)
        self.assertEqual(len(self.refreshes.jobs), 1)

    def test_a_stale_entry_is_served_while_one_reader_refreshes(self):
        """Vieja + cerrojo cogido = se sirve lo viejo, sin esperar ni medir."""
        metrics.refresh_public_snapshot(now=self.now)
        cache.add(metrics.PUBLIC_ACTIVITY_KEY + '.lock', 1, 60)
        stale = self.now + timedelta(hours=2)

        activity, queries = self._counted(
            lambda: metrics.activity_totals(now=stale))

        self.assertEqual(queries, 0)
        self.assertEqual(activity['measured_at'], self.now)

    def test_with_nothing_published_it_counts_rather_than_print_zero(self):
        """Sin entrada y sin cerrojo libre se mide igual.

        Un cero en "analyses" afirma que la flota no ha analizado nada nunca,
        y eso no se puede decir por no querer pagar una consulta.  Este
        agregado no tiene respaldo horario del que tirar, asi que la unica
        salida honesta es medirlo.
        """
        cache.add(metrics.PUBLIC_ACTIVITY_KEY + '.lock', 1, 60)

        activity, queries = self._counted(
            lambda: metrics.activity_totals(now=self.now))

        self.assertGreater(queries, 0)
        self.assertEqual(activity['closed_24h'], 12)

    def test_the_page_shows_what_the_snapshot_published(self):
        metrics.refresh_public_snapshot(now=self.now)
        for index in range(3):
            DBEvent.objects.create(kind='NODE_CLOSED', payload={},
                                   ts=self.now)

        body = Client().get('/atomicdb/').content.decode()

        # Doce, los publicados, y no los quince que hay debajo: eso es
        # exactamente lo que significa leer un contador compartido.
        self.assertIn('12 closures', body)
        self.assertNotIn('15 closures', body)


@override_settings(CACHES=CACHE_FOR_TESTS)
class CampaignProgressTests(TestCase):
    """El progreso de campanas: compartido, y con su fallo bien definido."""

    def setUp(self):
        cache.clear()
        metrics.reset_public_snapshot()
        self.now = timezone.now()
        self.keys, _route = build_line()
        root = Position.objects.get(key=self.keys[0])
        self.campaign = Campaign.objects.create(
            name='linea de prueba', root=root, line_san='1. a3',
            state=Campaign.CState.ACTIVE)
        Position.objects.filter(key__in=self.keys[:4]).update(
            campaign=self.campaign, nodes_invested=10)

    def test_a_hit_does_not_regroup(self):
        metrics.refresh_public_snapshot(now=self.now)
        connection = connections[settings.ATOMICDB_DATABASE_ALIAS]

        with CaptureQueriesContext(connection) as captured:
            rows = metrics.campaign_progress([self.campaign.id],
                                             now=self.now)

        self.assertEqual(len(captured.captured_queries), 0)
        self.assertEqual(rows[self.campaign.id]['total'], 4)
        self.assertEqual(rows[self.campaign.id]['explored'], 4)

    def test_a_campaign_born_after_the_snapshot_is_measured_now(self):
        """Recien activada: se mide, no se pinta a cero durante minuto y medio."""
        metrics.refresh_public_snapshot(now=self.now)
        newborn = Campaign.objects.create(
            name='recien activada', root_id=self.keys[0], line_san='1. b3',
            state=Campaign.CState.ACTIVE)
        Position.objects.filter(key__in=self.keys[4:7]).update(
            campaign=newborn)

        rows = metrics.campaign_progress([newborn.id], now=self.now)

        self.assertEqual(rows[newborn.id]['total'], 3)

    def test_a_campaign_with_no_positions_is_not_a_miss(self):
        """Cero posiciones y "no la conozco" no son lo mismo, y se distinguen.

        Sin la lista de ids activos dentro de la medida, una campana vacia
        seria un fallo del snapshot en CADA visita: la consulta agrupada no
        devuelve fila para ella, y quien preguntase la mediria en vivo una y
        otra vez.
        """
        empty = Campaign.objects.create(
            name='sin posiciones', root_id=self.keys[0], line_san='1. c3',
            state=Campaign.CState.ACTIVE)
        metrics.refresh_public_snapshot(now=self.now)
        connection = connections[settings.ATOMICDB_DATABASE_ALIAS]

        with CaptureQueriesContext(connection) as captured:
            rows = metrics.campaign_progress([empty.id], now=self.now)

        self.assertEqual(len(captured.captured_queries), 0)
        self.assertEqual(rows[empty.id]['total'], 0)

    def test_the_card_shows_the_same_numbers_it_always_showed(self):
        body = Client().get('/atomicdb/').content.decode()

        self.assertIn('4 of 4 positions explored', body)


@override_settings(CACHES=CACHE_FOR_TESTS)
class FleetSnapshotTests(TestCase):
    """La flota: el agregado mas caro que quedaba dentro de una peticion."""

    def setUp(self):
        cache.clear()
        metrics.reset_public_snapshot()
        contributors.reset_cache()
        self.now = timezone.now()
        populate_home(self.now)
        WorkerPing.objects.filter(machine='box0').update(user='someone')
        AnalysisTask.objects.filter(machine='box0').update(
            state=AnalysisTask.TState.COMPLETED, completed=self.now,
            nodes_searched=1_000)
        self.refreshes = _Deferred()
        patch = mock.patch.object(metrics, 'REVALIDATE_EXECUTOR',
                                  self.refreshes)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_service_publishes_it(self):
        """Publicado desde fuera, ninguna visita lo mide."""
        metrics.refresh_public_snapshot(now=self.now)
        connection = connections[settings.ATOMICDB_DATABASE_ALIAS]

        with CaptureQueriesContext(connection) as captured:
            snapshot = contributors.fleet(now=self.now)

        self.assertEqual(len(captured.captured_queries), 0)
        self.assertEqual(snapshot['nodes_all'], 1_000)
        self.assertEqual(snapshot['users_all']['someone']['nodes'], 1_000)

    def test_a_stale_entry_is_served_while_one_reader_refreshes(self):
        """Sin cerrojo, los cinco procesos recorrian a la vez la misma tabla."""
        metrics.refresh_public_snapshot(now=self.now)
        cache.add(contributors.FLEET_CACHE_KEY + '.lock', 1, 60)
        stale = self.now + timedelta(hours=2)
        connection = connections[settings.ATOMICDB_DATABASE_ALIAS]

        with CaptureQueriesContext(connection) as captured:
            snapshot = contributors.fleet(now=stale)

        self.assertEqual(len(captured.captured_queries), 0)
        self.assertEqual(snapshot['measured_at'], self.now)

    def test_a_stale_entry_is_remeasured_behind_the_reader(self):
        """El lector se lleva la vieja; la nueva la publica el hilo de fondo.

        La flota es el agregado mas caro que llego a estar dentro de una
        peticion, asi que es justo el que no puede volver a medirse con
        alguien esperando.
        """
        metrics.refresh_public_snapshot(now=self.now)
        stale = self.now + timedelta(seconds=contributors.FLEET_CACHE_SECONDS
                                     + 1)
        AnalysisTask.objects.filter(machine='box0').update(
            nodes_searched=2_000)

        self.assertEqual(contributors.fleet(now=self.now)['nodes_all'], 1_000)
        self.assertEqual(contributors.fleet(now=stale)['nodes_all'], 1_000)

        self.assertEqual(self.refreshes.run_all(), 1)
        self.assertEqual(contributors.fleet(now=stale)['nodes_all'], 2_000)

    def test_resetting_the_cache_also_drops_the_lock(self):
        """Un cerrojo huerfano dejaria el refresco "cogido" por nadie."""
        metrics.refresh_public_snapshot(now=self.now)
        cache.add(contributors.FLEET_CACHE_KEY + '.lock', 1, 60)

        contributors.reset_cache()

        self.assertIsNone(cache.get(contributors.FLEET_CACHE_KEY + '.lock'))


@override_settings(CACHES=CACHE_FOR_TESTS)
class WarmCommandTests(TestCase):
    """El calentador: lo que publica, y que no pueda tumbar nada.

    Calienta la capa COMPARTIDA y solo esa.  La cache de pagina de la portada
    varia por ``Cookie`` y el token CSRF es uno por visitante, asi que no hay
    una entrada de pagina que calentar para todos; el alcance esta escrito en
    la ayuda del comando y en la unidad de systemd.
    """

    def setUp(self):
        cache.clear()
        metrics.reset_public_snapshot()
        self.now = timezone.now()
        populate_home(self.now)

    def test_it_publishes_what_the_page_reads(self):
        call_command('warm_public_snapshot', '--quiet')

        connection = connections[settings.ATOMICDB_DATABASE_ALIAS]
        with CaptureQueriesContext(connection) as captured:
            totals = metrics.tree_totals(now=self.now)
            metrics.activity_totals(now=self.now)

        self.assertEqual(len(captured.captured_queries), 0)
        self.assertEqual(totals['total'], Position.objects.count())

    def test_a_measure_that_explodes_does_not_fail_the_unit(self):
        """Un calentador que revienta se queja y sale con 0.

        Es una optimizacion, no una dependencia: si marcase el arranque como
        fallido, un contador que no se puede medir se convertiria en una
        alarma de despliegue.
        """
        def boom(_now):
            raise RuntimeError('la base dijo que no')

        with mock.patch.object(metrics, '_measure_tree_totals', boom):
            errors = StringIO()
            call_command('warm_public_snapshot', stderr=errors)

        self.assertIn('no se pudo republicar', errors.getvalue())


@override_settings(CACHES=CACHE_FOR_TESTS)
class RouteLabelCacheTests(TestCase):

    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.keys, self.route = build_line()

    def test_a_repeated_route_is_not_replayed_again(self):
        target = self.keys[-1]
        first = views._route_labels(self.route, target)
        connection = connections[settings.ATOMICDB_DATABASE_ALIAS]

        with CaptureQueriesContext(connection) as captured:
            second = views._route_labels(self.route, target)

        self.assertEqual(first, second)
        self.assertIsNotNone(first)
        self.assertEqual(len(captured.captured_queries), 0)

    def test_a_route_that_does_not_reach_the_target_is_never_cached(self):
        """Una ruta invalida hoy puede materializarse en el click siguiente."""
        other = self.keys[-2]

        self.assertIsNone(views._route_labels(self.route, other))
        self.assertIsNone(views._route_labels(self.route, other))
        # Y en cuanto la ruta correcta existe, se ve en el acto.
        self.assertIsNotNone(views._route_labels(self.route, self.keys[-1]))

    def test_an_illegal_route_still_says_so(self):
        """El lote rapido no puede tragarse el mensaje de error del bucle."""
        with self.assertRaises(views.PlayRouteError) as raised:
            views._validated_play_route('e2e5', self.keys[-1])

        self.assertIn('illegal Atomic move', str(raised.exception))

    def test_the_batched_replay_agrees_with_the_slow_one(self):
        target = self.keys[-1]
        batched = views._validated_play_route(self.route, target)
        # Mismo camino con el lote deshabilitado: el bucle ply a ply.
        original = views._batched_sans
        views._batched_sans = lambda fen, ucis: None
        try:
            slow = views._validated_play_route(self.route, target)
        finally:
            views._batched_sans = original

        self.assertEqual([step['san'] for step in batched[1]],
                         [step['san'] for step in slow[1]])
        self.assertEqual(batched[2], slow[2])
