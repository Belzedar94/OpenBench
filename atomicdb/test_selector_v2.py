"""El selector acotado da los MISMOS numeros que la foto global del grafo.

Esa frase es todo el contrato, y el ancla de este fichero la prueba literal:
sobre un grafo pequeno — bastante pequeno para que la bola explorada lo cubra
entero — la prioridad que escribe v2 tiene que ser la de v1 para CADA posicion
abierta, con tolerancia de coma flotante y nada mas.  Si eso falla, no hay
optimizacion que discutir: son dos selectores distintos.

Lo demas del fichero prueba las tres cosas que v2 hace y v1 no podia:

* el HORIZONTE realmente deja nodos fuera de la bola, y a los que deja fuera
  les pone el precio correcto — 30 unidades si cuelgan de la raiz, 5 si no;
* una arista que apunta a una fila que no existe ya no mata la pasada.  Esa
  carrera es la razon de ser de las lecturas por lotes, asi que el test la
  documenta por partida doble: v1 revienta con ``KeyError`` y v2 termina;
* ``reachable`` se propaga al expandir, y el backfill recalcula la columna
  entera — incluido el nivel que la propagacion ya habia marcado, que es lo que
  antes paraba el recorrido en seco.

Y una cuarta, que no es una mejora sino una decision: los dos motores NO
coinciden bajo un nodo cerrado, y ``BehindAClosedWallTests`` dice cual gana y
por que.
"""

import contextlib
import json
import math
import os
import tempfile
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import ingest, logic, views
from .database import atomic, connection
from .models import Campaign, Edge, Position, ProofCampaign
from .testing import TestCase, TransactionTestCase


def quiet_fen(file_index, rank=5, white_to_move=True):
    """Una posicion tranquila y distinta por casilla.

    Rey blanco en a1, rey negro en h8 y una torre blanca suelta que cambia de
    sitio.  Ni jaque ni mate ni ahogado: estos nodos existen para tener FORMA
    de grafo — transposiciones, cadenas largas, nodos sueltos — no para que el
    motor tenga nada que decir sobre ellos.  La torre nunca comparte fila ni
    columna con el rey negro, que es lo unico que hay que cuidar para que
    ``terminal_status`` no encuentre nada.
    """
    left, right = file_index, 7 - file_index
    row = ((str(left) if left else '') + 'R'
           + (str(right) if right else ''))
    rows = ['7k', '8', '8', '8', '8', '8', '8', 'K7']
    rows[8 - rank] = row
    return '{} {} - - 0 1'.format('/'.join(rows),
                                  'w' if white_to_move else 'b')


class AnchorFixture:
    """El grafo de juguete que sostiene el ancla.

    Lleva a proposito una de cada cosa que la formula distingue: cierres
    decisivos y tablas, una transposicion (dos padres, un hijo), un nodo con
    campana votada, uno sin expandir, uno suelto sin ninguna arista y uno cuyo
    unico camino desde la raiz pasa de la saturacion del regret.  Si v1 y v2
    coinciden aqui, coinciden por la formula y no por casualidad.
    """

    def build(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.children = {edge.move_uci: edge.child
                         for edge in Edge.objects.filter(parent=self.root)
                                                 .select_related('child')}

        # Cierres: uno ganado, uno perdido, unas tablas.  Un nodo cerrado no
        # relaja hacia abajo, y esa regla tiene que sobrevivir al cambio de
        # motor.  Los tres son HOJAS a proposito: lo que cuelga por debajo de
        # un cierre es el unico sitio donde los dos motores no coinciden, y esa
        # diferencia tiene su propio test (``BehindAClosedWallTests``) en vez
        # de esconderse dentro del ancla.
        self.won = self._settle('h2h4', 'WHITE_WIN', 9_800, mate_in=2)
        self.lost = self._settle('g2g4', 'BLACK_WIN', -9_800, mate_in=3)
        self.drawn = self._settle('b1c3', 'DRAW', 0)

        # Una linea principal con eval y una refutada con eval malisima: la
        # segunda es la que produce un regret por encima del tope.
        self.mainline = self._value('e2e4', eval_cp=40)
        self.second = self._value('d2d4', eval_cp=25)
        self.refuted = self._value('a2a3', eval_cp=-6_000)
        self.blind = self._value('g1f3', eval_cp=None, backed_eval=-120)

        # Transposicion: dos padres ABIERTOS que llegan al mismo hijo.  El
        # Dijkstra tiene que quedarse con la mejor ruta, y las dos rutas
        # tienen que existir de verdad para que quedarse con una signifique
        # algo.
        self.shared = self._node(0, rank=5, eval_cp=-30, visits=1)
        Edge.objects.create(parent=self.mainline, move_uci='e4e5',
                            child=self.shared)
        Edge.objects.create(parent=self.second, move_uci='d4d5',
                            child=self.shared)

        # Un nodo bajo la linea refutada: su unico camino carga con el gap
        # entero, muy por encima de la saturacion de 3000 centipeones.
        self.beyond_cap = self._node(1, rank=5, eval_cp=-40)
        Edge.objects.create(parent=self.refuted, move_uci='a3a4',
                            child=self.beyond_cap)

        # Uno expandido y otro sin expandir: el sumando de +2 tiene que
        # aparecer en uno y solo en uno.
        self.opened = self._node(2, rank=5, eval_cp=15, expanded=True)
        self.unopened = self._node(3, rank=5, eval_cp=15, expanded=False)
        Edge.objects.create(parent=self.mainline, move_uci='e4e6',
                            child=self.opened)
        Edge.objects.create(parent=self.mainline, move_uci='e4e7',
                            child=self.unopened)

        # Banda de mate SIN cierre: el salto de 50 se lo lleva un nodo abierto.
        self.mate_band = self._node(4, rank=5, eval_cp=9_400, visits=2)
        Edge.objects.create(parent=self.second, move_uci='d4d6',
                            child=self.mate_band)

        # Una campana ACTIVE con votos, y un nodo suyo.
        self.campaign = Campaign.objects.create(
            name='ancla', root=self.mainline, votes=7,
            state=Campaign.CState.ACTIVE, active=True)
        self.sponsored = self._node(5, rank=5, eval_cp=-10,
                                    campaign_id=self.campaign.id)
        Edge.objects.create(parent=self.mainline, move_uci='e4f5',
                            child=self.sponsored)

        # Una cadena de cuatro plies para que la bola tenga profundidad.
        parent = self.shared
        self.chain = []
        for index in range(4):
            node = self._node(index, rank=4, eval_cp=-25 * index)
            Edge.objects.create(parent=parent, move_uci=f'h1h{index + 2}',
                                child=node)
            self.chain.append(node)
            parent = node

        # Y el suelto: existe, no tiene una sola arista, y por eso cobra el
        # precio de cajetin FEN pegado a mano.
        self.orphan = self._node(6, rank=4, eval_cp=120, visits=1)

        # Los hermanos que nadie ha tocado puntuan EXACTAMENTE igual — sin
        # eval, sin visitas, sin expandir — y una cima donde todo empata no
        # tiene orden que comparar: el sombra la llamaria degenerada y con
        # razon.  Una visita distinta a cada uno los separa sin tocar nada del
        # Dijkstra, que no mira las visitas.  Comparar treinta numeros
        # distintos prueba mas que comparar treinta copias del mismo.
        plain = [child for _, child in sorted(self.children.items())
                 if child.status == 'UNKNOWN' and child.eval_cp is None
                 and child.backed_eval is None]
        for index, child in enumerate(plain, start=1):
            child.visits = index
            child.save(update_fields=['visits'])

        # La marca de alcanzabilidad se siembra como en produccion: la
        # propagacion de ``expand`` cubre el primer ply, el backfill el resto.
        call_command('backfill_reachable', stdout=StringIO())
        for node in (self.shared, self.beyond_cap, self.opened, self.mate_band,
                     self.sponsored, *self.chain):
            node.refresh_from_db()
        self.orphan.refresh_from_db()
        return self

    # ---------------- utilidades del fixture ----------------

    def _settle(self, move, status, eval_cp, mate_in=None):
        node = self.children[move]
        node.status, node.closure, node.proof = status, 'MINIMAX', 'ENGINE'
        node.eval_cp, node.mate_in = eval_cp, mate_in
        node.save()
        return node

    def _value(self, move, eval_cp, backed_eval=None):
        node = self.children[move]
        node.eval_cp, node.backed_eval = eval_cp, backed_eval
        node.save()
        return node

    def _node(self, file_index, rank, **fields):
        node = ingest.get_or_create_position(
            quiet_fen(file_index, rank=rank,
                      white_to_move=rank % 2 == 1))
        for name, value in fields.items():
            setattr(node, name, value)
        node.save()
        return node


def live_keys():
    return set(Position.objects
               .filter(status='UNKNOWN', priority__gt=ingest.DEAD / 2)
               .values_list('key', flat=True))


class AnchorTests(TestCase):
    """v2 == v1, posicion a posicion, sobre un grafo que la bola cubre."""

    def setUp(self):
        self.fixture = AnchorFixture().build()

    def test_the_toy_graph_really_is_the_shape_it_claims(self):
        """Un ancla sobre un fixture degenerado no ancla nada."""
        self.assertGreaterEqual(Position.objects.count(), 24)
        self.assertEqual(Edge.objects.filter(child=self.fixture.shared)
                         .count(), 2)                     # transposicion
        self.assertFalse(Edge.objects.filter(
            child=self.fixture.orphan).exists())          # suelto
        self.assertFalse(self.fixture.orphan.reachable)
        self.assertTrue(self.fixture.shared.reachable)
        self.assertEqual(Position.objects.filter(
            status__in=('WHITE_WIN', 'BLACK_WIN', 'DRAW')).count(), 3)
        self.assertTrue(self.fixture.opened.expanded)
        self.assertFalse(self.fixture.unopened.expanded)

    def test_the_ball_covers_the_whole_graph(self):
        """Sin esto, el ancla probaria el horizonte y no la formula."""
        ball = ingest._regret_from_root_bounded()
        connected = set(Position.objects.values_list('key', flat=True))
        connected.discard(self.fixture.orphan.key)
        self.assertEqual(set(ball), connected)

    def test_computed_priorities_match_position_by_position(self):
        first = ingest.refresh_priorities_v1(force=True, top_k=10_000)
        second = ingest.refresh_priorities_v2(force=True, top_k=10_000)
        self.assertEqual(set(first), live_keys())
        self.assertEqual(set(first), set(second))
        for key, priority in first.items():
            self.assertAlmostEqual(priority, second[key], delta=1e-9,
                                   msg=f'divergen en {key}')

    def test_compute_only_mode_caps_the_head_and_writes_nothing(self):
        """El modo que hace posible comparar los dos motores a la vez.

        Si uno de los dos escribiese, el otro mediria una base que el primero
        acaba de mover; y devolver el diccionario entero seria reintroducir el
        problema que todo esto viene a quitar.
        """
        before = dict(Position.objects.values_list('key', 'priority'))
        self.assertEqual(
            len(ingest.refresh_priorities_v1(force=True, top_k=5)), 5)
        self.assertEqual(
            len(ingest.refresh_priorities_v2(force=True, top_k=5)), 5)
        self.assertEqual(dict(Position.objects.values_list('key', 'priority')),
                         before)

    def test_written_priorities_match_too(self):
        """El modo solo calculo y el que escribe tienen que decir lo mismo.

        ``delta=False`` porque la preparacion de aqui borra las prioridades con
        un ``update`` de queryset, o sea SIN mover ``updated``: una pasada
        incremental tiene todo el derecho a no ver ese borrado (§ ingest, modo
        delta), y lo que este test compara es la formula de los dos motores,
        no cuantas filas reescribe cada modo.  De eso ultimo se ocupan los
        ``DeltaPassTests``.
        """
        ingest.refresh_priorities_v1(force=True)
        expected = dict(Position.objects.filter(status='UNKNOWN')
                        .values_list('key', 'priority'))
        Position.objects.all().update(priority=0.0)

        ingest.refresh_priorities_v2(force=True, delta=False)
        written = dict(Position.objects.filter(status='UNKNOWN')
                       .values_list('key', 'priority'))
        self.assertEqual(set(expected), set(written))
        for key, priority in expected.items():
            self.assertAlmostEqual(priority, written[key], delta=1e-9,
                                   msg=f'divergen en {key}')

    def test_the_orphan_keeps_the_disconnected_price(self):
        """Un cajetin FEN suelto vale 5 unidades de regret, no 30."""
        prices = ingest.refresh_priorities_v2(force=True, top_k=10_000)
        expected = ingest.priority_of(
            120, False, 1, None, ingest.DISCONNECTED_REGRET, {})
        self.assertAlmostEqual(prices[self.fixture.orphan.key], expected,
                               delta=1e-9)

    def test_the_refuted_subtree_saturates_the_regret(self):
        """El nodo cuyo unico camino pasa del tope cobra las 30 unidades."""
        ball = ingest._regret_from_root_bounded()
        self.assertGreater(ball[self.fixture.beyond_cap.key],
                           ingest.REGRET_CAP)
        prices = ingest.refresh_priorities_v2(force=True, top_k=10_000)
        expected = ingest.priority_of(-40, False, 0, None, ingest.FAR_REGRET,
                                      {})
        self.assertAlmostEqual(prices[self.fixture.beyond_cap.key], expected,
                               delta=1e-9)

    def test_the_campaign_bonus_survives_the_new_engine(self):
        without = ingest.refresh_priorities_v2(force=True, top_k=10_000)
        self.fixture.campaign.apply_state(Campaign.CState.PAUSED)
        with_paused = ingest.refresh_priorities_v2(force=True, top_k=10_000)
        key = self.fixture.sponsored.key
        self.assertAlmostEqual(
            without[key] - with_paused[key],
            ingest.CAMPAIGN_BONUS * math.log1p(7), delta=1e-9)

    def test_tombstones_are_not_resurrected(self):
        self.fixture.unopened.priority = ingest.DEAD
        self.fixture.unopened.save(update_fields=['priority'])
        prices = ingest.refresh_priorities_v2(force=True, top_k=10_000)
        self.assertNotIn(self.fixture.unopened.key, prices)
        ingest.refresh_priorities_v2(force=True)
        self.fixture.unopened.refresh_from_db()
        self.assertEqual(self.fixture.unopened.priority, ingest.DEAD)


class HorizonTests(TestCase):
    """El horizonte deja nodos fuera, y a los de fuera les pone precio."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        children = {edge.move_uci: edge.child
                    for edge in Edge.objects.filter(parent=self.root)
                                            .select_related('child')}
        # El mejor hijo esta en banda de mate: es el que sube el corte.
        self.good = children['e2e4']
        self.good.eval_cp = 9_800
        self.good.save()
        # El peor abre un gap enorme, y su hijo hereda ese regret entero.
        self.bad = children['a2a3']
        self.bad.eval_cp = -6_000
        self.bad.save()
        self.far = ingest.get_or_create_position(quiet_fen(0, rank=5))
        Edge.objects.create(parent=self.bad, move_uci='a3a4', child=self.far)
        self.far.reachable = True
        self.far.save(update_fields=['reachable'])

    def test_the_horizon_stops_the_walk(self):
        wide = ingest._regret_from_root_bounded(top_n=1_000)
        narrow = ingest._regret_from_root_bounded(top_n=1)
        self.assertIn(self.far.key, wide)
        self.assertNotIn(self.far.key, narrow)

    def _price_beyond_the_horizon(self, reachable):
        Position.objects.filter(key=self.far.key).update(reachable=reachable)
        with patch.object(ingest, 'selector_horizon_width', return_value=1):
            return ingest.refresh_priorities_v2(
                force=True, top_k=10_000)[self.far.key]

    def test_a_connected_node_beyond_the_horizon_pays_the_saturated_regret(
            self):
        self.assertAlmostEqual(
            self._price_beyond_the_horizon(True),
            ingest.priority_of(None, False, 0, None, ingest.FAR_REGRET, {}),
            delta=1e-9)

    def test_a_disconnected_node_beyond_the_horizon_pays_the_loose_price(self):
        self.assertAlmostEqual(
            self._price_beyond_the_horizon(False),
            ingest.priority_of(None, False, 0, None,
                               ingest.DISCONNECTED_REGRET, {}),
            delta=1e-9)

    def test_the_two_prices_are_not_the_same_number(self):
        """Si coincidieran, los dos tests de arriba no probarian nada."""
        self.assertNotAlmostEqual(self._price_beyond_the_horizon(True),
                                  self._price_beyond_the_horizon(False))


class BehindAClosedWallTests(TestCase):
    """Detras de un muro cerrado los dos motores dicen el MISMO numero.

    Un nodo cuyo unico camino desde la raiz pasa por un nodo CERRADO no lo
    alcanza ninguno de los dos: un cierre no relaja hacia abajo, y esa regla no
    cambia.  La foto global le daba ``inf``, o sea las 5 unidades del cajetin
    suelto, y el acotado tiene que decir 5 tambien — para lo cual la columna
    tiene que estar sembrada con la MISMA regla que el recorrido, que es lo que
    hace ``backfill_reachable`` al no cruzar el muro.

    Esta clase existio antes para documentar lo contrario: la columna se
    sembraba con un BFS de aristas, marcaba el subarbol, y el acotado le cobraba
    30 donde la global cobraba 5.  Setenta y cinco unidades sobre una formula
    cuyo techo son 67 no son un matiz: son la cima entera, y es lo que la sombra
    del 3-ago midio como ``jaccard=0,011`` con ``tau=0,997`` — pertenencia rota
    con el orden intacto.
    """

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.wall = Edge.objects.get(parent=self.root, move_uci='h2h4').child
        self.wall.status, self.wall.closure = 'WHITE_WIN', 'MINIMAX'
        self.wall.eval_cp, self.wall.mate_in = 9_800, 2
        self.wall.save()
        self.hidden = ingest.get_or_create_position(quiet_fen(0, rank=5))
        self.hidden.eval_cp = 60
        self.hidden.save()
        Edge.objects.create(parent=self.wall, move_uci='h4h5',
                            child=self.hidden)
        call_command('backfill_reachable', stdout=StringIO())
        self.hidden.refresh_from_db()

    def test_the_walk_stops_at_the_wall_and_so_does_the_column(self):
        """La columna dice lo que dice el recorrido, no lo que dice la arista."""
        self.assertFalse(self.hidden.reachable)
        self.assertNotIn(self.hidden.key, ingest._regret_from_root_bounded())
        self.assertEqual(ingest._regret_from_root()[self.hidden.key],
                         float('inf'))

    def test_the_wall_itself_is_marked(self):
        """Se para EN el cerrado, no antes: el cerrado se alcanza."""
        self.wall.refresh_from_db()
        self.assertTrue(self.wall.reachable)

    def test_both_engines_pay_the_loose_price(self):
        first = ingest.refresh_priorities_v1(force=True, top_k=10_000)
        second = ingest.refresh_priorities_v2(force=True, top_k=10_000)
        loose = ingest.priority_of(60, False, 0, None,
                                   ingest.DISCONNECTED_REGRET, {})
        self.assertAlmostEqual(first[self.hidden.key], loose, delta=1e-9)
        self.assertAlmostEqual(second[self.hidden.key], loose, delta=1e-9)

    def test_the_heads_agree(self):
        """El sombra, en miniatura: mismo conjunto y mismo orden."""
        first = ingest.refresh_priorities_v1(force=True, top_k=10)
        second = ingest.refresh_priorities_v2(force=True, top_k=10)
        self.assertEqual(set(first), set(second))
        for key, value in first.items():
            self.assertAlmostEqual(value, second[key], delta=1e-9)


@contextlib.contextmanager
def child_row_missing(key):
    """Deja una arista apuntando a una fila que no existe.

    Es la carrera de produccion en su forma pura: la adyacencia conoce una
    clave que el mapa de valores no.  En la base viva la produce una posicion
    NACIDA entre las dos fotos del Dijkstra global; aqui se produce borrando
    la fila, que es la misma condicion vista desde el otro lado.

    El agujero se tapa al salir.  No es cortesia: el ``TransactionTestCase``
    vacia las tablas al terminar y una arista huerfana lo hace tropezar.
    """
    row = Position.objects.get(key=key)

    if connection.vendor == 'postgresql':
        with atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET CONSTRAINTS ALL DEFERRED')
                cursor.execute(
                    f'DELETE FROM {Position._meta.db_table} WHERE key = %s',
                    [key],
                )
            try:
                yield
            finally:
                row.save(force_insert=True)
        return

    if not connection.disable_constraint_checking():
        raise RuntimeError('no se pudieron desactivar las claves ajenas')
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f'DELETE FROM {Position._meta.db_table} WHERE key = %s', [key])
        yield
    finally:
        row.save(force_insert=True)
        connection.enable_constraint_checking()


class MissingRowTests(TransactionTestCase):
    """La arista que apunta al vacio mata a v1 y no a v2.

    Ese ``KeyError`` es de produccion, no de laboratorio: salta unas veces por
    hora y se lo come la red de ``step()`` en el servicio.  Cada vez que salta,
    la pasada entera se pierde.  Aqui esta escrito por partida doble — lo que
    hace v1 y lo que hace v2 — porque la diferencia ES el argumento.
    """

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.parent = Edge.objects.filter(parent=self.root).first().child
        self.parent.eval_cp = 30
        self.parent.save()
        # El fantasma tiene que ser PADRE de algo: v1 solo revienta cuando
        # tiene que preguntarle a quien mueve, y eso solo pasa si tiene hijos.
        self.ghost = ingest.get_or_create_position(quiet_fen(0, rank=5))
        self.tail = ingest.get_or_create_position(quiet_fen(1, rank=4))
        # El hijo del fantasma necesita eval: v1 solo llega a preguntar quien
        # mueve cuando hay algun valor con el que calcular el gap.
        self.tail.eval_cp = -50
        self.tail.save()
        Edge.objects.create(parent=self.parent, move_uci='h2h3',
                            child=self.ghost)
        Edge.objects.create(parent=self.ghost, move_uci='h3h4',
                            child=self.tail)

    def test_v1_skips_the_newborn(self):
        # Historico: aqui se exigia el KeyError que el servicio absorbia unas
        # veces por hora (y que mato la sombra en crudo el 4-ago).  Desde el
        # fix, v1 aplica la misma regla estructural que v2: una clave que
        # esta en la adyacencia pero no en el snapshot se salta esta pasada.
        with child_row_missing(self.ghost.key):
            regret = ingest._regret_from_root()
        self.assertIn(self.root.key, regret)
        # El fantasma no se expande y lo que colgaba de el no hereda regret
        # util: sin fila del padre no hay gap que calcular.
        self.assertEqual(regret.get(self.tail.key, float('inf')),
                         float('inf'))

    def test_v2_finishes_the_pass(self):
        with child_row_missing(self.ghost.key):
            ball = ingest._regret_from_root_bounded()
            self.assertTrue(ingest.refresh_priorities_v2(force=True))
        self.assertIn(self.root.key, ball)
        self.assertNotIn(self.ghost.key, ball)
        # Y lo que colgaba del fantasma tampoco se cuela por la puerta de
        # atras: sin fila del padre no hay gap que calcular.
        self.assertNotIn(self.tail.key, ball)


class ReachablePropagationTests(TestCase):

    def test_the_root_is_reachable_by_construction(self):
        root = ingest.get_or_create_position(logic.start_fen())
        self.assertTrue(root.reachable)

    def test_a_pre_existing_root_gets_the_mark_on_the_next_pass(self):
        root = ingest.get_or_create_position(logic.start_fen())
        Position.objects.filter(key=root.key).update(reachable=False)
        self.assertTrue(
            ingest.get_or_create_position(logic.start_fen()).reachable)

    def test_expand_hands_the_mark_to_the_children(self):
        root = ingest.get_or_create_position(logic.start_fen())
        children = ingest.expand(root)
        self.assertTrue(children)
        self.assertTrue(all(child.reachable for child in children))
        self.assertFalse(Position.objects.filter(reachable=False).exists())

    def test_an_unreachable_parent_hands_out_nothing(self):
        loose = ingest.get_or_create_position(quiet_fen(0, rank=5))
        self.assertFalse(loose.reachable)
        for child in ingest.expand(loose):
            self.assertFalse(child.reachable)

    def test_a_new_position_is_not_reachable_just_for_existing(self):
        self.assertFalse(
            ingest.get_or_create_position(quiet_fen(2, rank=4)).reachable)


class BackfillReachableTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.deep = ingest.get_or_create_position(quiet_fen(0, rank=5))
        self.deeper = ingest.get_or_create_position(quiet_fen(1, rank=4))
        parent = Edge.objects.filter(parent=self.root).first().child
        Edge.objects.create(parent=parent, move_uci='h2h3', child=self.deep)
        Edge.objects.create(parent=self.deep, move_uci='h3h4',
                            child=self.deeper)
        self.orphan = ingest.get_or_create_position(quiet_fen(2, rank=4))
        # La foto de partida: el ply de ``expand`` marcado, el resto no.
        Position.objects.all().update(reachable=False)

    def _run(self, *args):
        out = StringIO()
        call_command('backfill_reachable', *args, stdout=out)
        return json.loads(out.getvalue())

    def test_it_marks_the_whole_ball_and_only_the_ball(self):
        report = self._run()
        self.assertEqual(report['unreachable_rows_left'], 1)
        for node in (self.root, self.deep, self.deeper):
            node.refresh_from_db()
            self.assertTrue(node.reachable)
        self.orphan.refresh_from_db()
        self.assertFalse(self.orphan.reachable)

    def test_it_walks_past_the_first_ply(self):
        """El backfill existe justamente porque ``expand`` solo llega a uno."""
        report = self._run()
        self.assertEqual(report['marked'], Position.objects.count() - 1)
        self.deeper.refresh_from_db()
        self.assertTrue(self.deeper.reachable)   # dos plies por debajo

    def test_it_does_not_stall_on_a_partly_marked_base(self):
        """La marca previa de ``expand`` hacia de muro; por eso se borra.

        Sin el borrado, el primer nivel ya esta marcado, el filtro "dame los
        que faltan" devuelve vacio y el recorrido se cree acabado justo donde
        empieza lo que hay que reparar.
        """
        first_ply = Edge.objects.filter(parent=self.root).values_list(
            'child_id', flat=True)
        Position.objects.filter(key__in=list(first_ply)).update(
            reachable=True)
        Position.objects.filter(key=self.root.key).update(reachable=True)

        self._run()
        self.deeper.refresh_from_db()
        self.assertTrue(self.deeper.reachable)

    def test_a_second_run_reaches_the_same_set(self):
        """Idempotente en el RESULTADO; vuelve a escribir, y esta bien."""
        first = self._run()
        again = self._run()
        self.assertEqual(again['marked'], first['marked'])
        self.assertEqual(again['cleared'], first['marked'])
        self.assertEqual(again['unreachable_rows_left'], 1)

    def test_dry_run_counts_without_writing(self):
        report = self._run('--dry-run')
        self.assertEqual(report['reachable_nodes'],
                         Position.objects.count() - 1)
        self.assertEqual(report['unmarked_now'], Position.objects.count() - 1)
        self.assertEqual(Position.objects.filter(reachable=True).count(), 0)

    def test_dry_run_discounts_what_is_already_marked(self):
        self._run()
        report = self._run('--dry-run')
        self.assertEqual(report['reachable_nodes'],
                         Position.objects.count() - 1)
        self.assertEqual(report['unmarked_now'], 0)

    def test_without_a_root_it_says_so_instead_of_guessing(self):
        Edge.objects.all().delete()
        ProofCampaign.objects.all().delete()
        Position.objects.all().delete()
        report = self._run()
        self.assertIsNone(report['root'])
        self.assertEqual(report['marked'], 0)


class SelectorSwitchTests(TestCase):

    def setUp(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)

    def test_the_switch_defaults_to_off(self):
        self.assertFalse(ingest.selector_v2_enabled())
        with patch.object(ingest, 'refresh_priorities_v1') as engine:
            ingest.refresh_priorities(force=True)
        engine.assert_called_once()

    @override_settings(ATOMICDB_SELECTOR_V2=True)
    def test_the_switch_routes_to_the_bounded_engine(self):
        self.assertTrue(ingest.selector_v2_enabled())
        with patch.object(ingest, 'refresh_priorities_v2') as engine:
            ingest.refresh_priorities(force=True)
        engine.assert_called_once()

    @override_settings(ATOMICDB_SELECTOR_V2=True)
    def test_the_service_pass_goes_through_the_switch(self):
        out = StringIO()
        with patch.object(ingest, '_regret_from_root_bounded',
                          return_value={}) as bounded:
            with patch.object(ingest, '_regret_from_root') as globals_pass:
                call_command('refresh_selector', stdout=out)
        bounded.assert_called_once()
        globals_pass.assert_not_called()

    def test_the_horizon_follows_the_lease_batch(self):
        self.assertEqual(ingest.selector_horizon_width(),
                         ingest.SELECTOR_HORIZON_FLOOR)
        with patch('atomicdb.views.TASK_REFILL_COUNT', 500):
            self.assertEqual(ingest.selector_horizon_width(), 2_000)


class SelectorDeltaSwitchTests(SimpleTestCase):
    """La salida de emergencia del modo delta, cableada de punta a punta.

    ``selector_delta_enabled`` leia el ajuste desde el primer dia, pero
    settings no lo leia del ENTORNO: la linea que esperaba en la unidad de
    systemd para apagarlo no apagaba nada, y eso no se nota hasta el dia en que
    hace falta apagarlo.  Aqui quedan fijadas las dos mitades: que el valor por
    defecto es ENCENDIDO — distinto del resto de conmutadores del fichero, y
    por eso hay que decirlo — y que una variable de entorno devuelve la pasada
    completa.
    """

    def _mapped(self, value):
        """Lo que settings deduce del entorno, con el modulo importado fresco.

        La expresion se evalua UNA vez, al arrancar el proceso, asi que la
        unica forma de verla desde un test es volver a importar el modulo con
        el entorno puesto; el segundo import deja las cosas como estaban.
        """
        import importlib
        import sys

        with patch.dict(os.environ, {'ATOMICDB_SELECTOR_DELTA': value},
                        clear=False):
            module = importlib.reload(sys.modules['OpenSite.settings'])
            try:
                return module.ATOMICDB_SELECTOR_DELTA
            finally:
                importlib.reload(sys.modules['OpenSite.settings'])

    def test_an_empty_environment_leaves_the_incremental_pass_on(self):
        self.assertTrue(self._mapped(''))
        self.assertTrue(ingest.selector_delta_enabled())

    def test_the_environment_can_bring_the_complete_pass_back(self):
        for value in ('0', 'false', 'no', 'FALSE', 'No'):
            self.assertFalse(self._mapped(value),
                             msg='{!r} tenia que apagarlo'.format(value))

    def test_anything_that_is_not_a_no_keeps_it_on(self):
        """Un valor raro no puede apagar lo que esta desplegado."""
        for value in ('1', 'true', 'yes', 'si', 'delta'):
            self.assertTrue(self._mapped(value),
                            msg='{!r} no tenia que apagarlo'.format(value))


class DeltaFixture:
    """Una linea larga bajo una jugada refutada, para tener FUERA DE LA BOLA.

    El modo delta solo se distingue de la pasada completa en lo que queda
    fuera del horizonte: dentro, las dos repuntuan todo.  Asi que el fixture
    tiene que producir de verdad nodos que la bola no alcance, y para eso hace
    falta lo de siempre — un hijo que abra un gap grande y una cadena colgando
    de el — mas un horizonte estrecho al puntuar.
    """

    def build(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        children = {edge.move_uci: edge.child
                    for edge in Edge.objects.filter(parent=self.root)
                                            .select_related('child')}
        # El mejor sube el corte del horizonte; el peor abre el gap.
        self.good = children['e2e4']
        self.good.eval_cp = 9_800
        self.good.save()
        self.bad = children['a2a3']
        self.bad.eval_cp = -6_000
        self.bad.save()
        # Cadena bajo el refutado: padre, hijo y un tercero de control que
        # cuelga aparte y al que no va a tocar nadie.
        self.mid = self._chained(self.bad, 'a3a4', 0)
        self.leaf = self._chained(self.mid, 'a4a5', 1)
        self.bystander = self._chained(self.bad, 'a3b4', 2)
        return self

    def _chained(self, parent, move, file_index):
        node = ingest.get_or_create_position(quiet_fen(file_index, rank=5))
        Edge.objects.create(parent=parent, move_uci=move, child=node)
        Position.objects.filter(key=node.key).update(reachable=True)
        return Position.objects.get(key=node.key)


@override_settings(ATOMICDB_SELECTOR_V2=True)
class DeltaPassTests(TestCase):
    """Repuntuar lo que se movio: que se repuntua, que no, y cuando no aplica.

    El estado del delta vive en el proceso y ``testing.TestCase`` lo borra
    antes de cada test (§ ``_IsolatedViewCache``), asi que aqui la primera
    pasada es SIEMPRE completa y la segunda es la que se examina.  Sin ese
    borrado estos tests dirian una cosa u otra segun el orden de ejecucion.
    """

    NARROW = 1        # horizonte de un solo candidato: la bola se queda enana
    STOMP = -777.0    # una prioridad que ninguna formula produce

    def setUp(self):
        self.fixture = DeltaFixture().build()

    def _pass(self, **kwargs):
        with patch.object(ingest, 'selector_horizon_width',
                          return_value=self.NARROW):
            return ingest.refresh_priorities(force=True, **kwargs)

    def _ball(self):
        with patch.object(ingest, 'selector_horizon_width',
                          return_value=self.NARROW):
            return set(ingest._regret_from_root_bounded())

    def _priority(self, node):
        return Position.objects.values_list('priority', flat=True).get(
            key=node.key)

    def _stomp(self, *nodes):
        """Prioridad imposible SIN tocar ``updated``: un ``update`` de columna.

        Es la unica forma de ver si una fila se reescribio o no: si la pasada
        la repuntua, el numero imposible desaparece; si no la mira, se queda.
        """
        Position.objects.filter(key__in=[node.key for node in nodes]).update(
            priority=self.STOMP)

    def test_the_fixture_really_leaves_nodes_outside_the_ball(self):
        """Sin esto, los tests de abajo compararian dos pasadas identicas."""
        ball = self._ball()
        self.assertIn(self.fixture.root.key, ball)
        for node in (self.fixture.mid, self.fixture.leaf,
                     self.fixture.bystander):
            self.assertNotIn(node.key, ball)

    def test_the_first_pass_of_a_process_is_complete(self):
        self._pass()
        self.assertEqual(ingest.selector_pass_report()['mode'], 'full')

    def test_the_second_one_is_incremental(self):
        self._pass()
        self._pass()
        self.assertEqual(ingest.selector_pass_report()['mode'], 'delta')

    def test_a_touched_row_is_repriced(self):
        self._pass()
        self.fixture.leaf.visits = 3
        self.fixture.leaf.save()          # ``auto_now`` mueve la marca
        self._stomp(self.fixture.leaf)

        self._pass()

        self.assertEqual(ingest.selector_pass_report()['mode'], 'delta')
        self.assertAlmostEqual(
            self._priority(self.fixture.leaf),
            ingest.priority_of(None, False, 3, None, ingest.FAR_REGRET, {}),
            delta=1e-9)

    def test_the_direct_parent_of_a_touched_row_is_repriced_too(self):
        """La cascada de respaldo escribe a los padres SIN mover su marca.

        ``backup_backed_evals`` usa ``bulk_update(dirty, _BACKED_FIELDS)`` y
        esa lista no lleva ``updated``: un padre puede estrenar ``backed_eval``
        — que es un termino de la formula — sin que ``updated`` se entere.  Por
        eso el delta sube un ply desde lo tocado, y por eso este test mira al
        padre y no al hijo.
        """
        self._pass()
        self.fixture.leaf.visits = 1
        self.fixture.leaf.save()
        self._stomp(self.fixture.mid)

        self._pass()

        self.assertNotEqual(self._priority(self.fixture.mid), self.STOMP)

    def test_an_untouched_row_keeps_its_priority(self):
        """Y este es el precio declarado del delta, escrito como test."""
        self._pass()
        self.fixture.leaf.visits = 1
        self.fixture.leaf.save()
        self._stomp(self.fixture.bystander)

        self._pass()

        self.assertEqual(self._priority(self.fixture.bystander), self.STOMP)

    def test_the_ball_is_repriced_every_pass_whatever_happened(self):
        """La cima no puede quedarse vieja ni cuando no se movio nada."""
        self._pass()
        inside = list(Position.objects.filter(key__in=self._ball(),
                                              status='UNKNOWN'))
        self.assertGreater(len(inside), 1)
        self._stomp(*inside)

        self._pass()

        self.assertEqual(ingest.selector_pass_report()['mode'], 'delta')
        for node in inside:
            self.assertNotEqual(self._priority(node), self.STOMP)

    def test_a_delta_pass_reprices_fewer_rows_than_a_full_one(self):
        self._pass()
        full = ingest.selector_pass_report()['rows']
        self._pass()
        incremental = ingest.selector_pass_report()['rows']

        self.assertGreater(full, incremental)
        self.assertEqual(full, len(live_keys()))

    def test_a_long_silence_falls_back_to_a_complete_pass(self):
        """Un hueco largo no rompe el delta; dice que algo no fue normal."""
        self._pass()
        self._pass()
        self.assertEqual(ingest.selector_pass_report()['mode'], 'delta')
        ingest._selector_delta_state['at'] = (
            timezone.now() - ingest.SELECTOR_DELTA_MAX_GAP
            - timedelta(seconds=1))

        self._pass()

        self.assertEqual(ingest.selector_pass_report()['mode'], 'full')

    @override_settings(ATOMICDB_SELECTOR_DELTA=False)
    def test_the_switch_brings_the_complete_pass_back(self):
        self.assertFalse(ingest.selector_delta_enabled())
        self._pass()
        self._pass()
        self.assertEqual(ingest.selector_pass_report()['mode'], 'full')

    def test_the_shadow_never_runs_incremental(self):
        """Comparar dos cimas exige que las dos se hayan calculado enteras."""
        self._pass()
        self._pass()
        anchor = ingest._selector_delta_state['at']

        with patch.object(ingest, 'selector_horizon_width',
                          return_value=self.NARROW):
            prices = ingest.refresh_priorities_v2(force=True, top_k=10_000)

        self.assertEqual(set(prices), live_keys())
        self.assertEqual(ingest._selector_delta_state['at'], anchor)

    def test_the_priority_write_does_not_dirty_the_marker(self):
        """El supuesto sobre el que se apoya todo esto, como test.

        Si ``bulk_update`` de una columna moviese ``updated``, cada pasada
        marcaria como tocado todo lo que acaba de escribir y la siguiente
        seria una completa disfrazada.  No lo mueve — ``auto_now`` es cosa de
        ``save()`` — y aqui queda dicho.
        """
        self._pass()
        before = dict(Position.objects.values_list('key', 'updated'))
        Position.objects.filter(status='UNKNOWN').update(priority=0.0)
        ingest.refresh_priorities_v2(force=True, delta=False)

        self.assertEqual(dict(Position.objects.values_list('key', 'updated')),
                         before)


@override_settings(ATOMICDB_SELECTOR_V2=True)
class DeltaMarkerPersistenceTests(TestCase):
    """El marcador sobrevive al proceso; el reinicio deja de costar horas.

    La leccion del 4 de agosto de 2026, en dos mitades.  Primera: el hueco
    del fallback se mide entre INICIOS de pasada, asi que el umbral tiene que
    ser mas largo que cualquier pasada completa plausible — con 10 minutos el
    delta era inalcanzable por construccion y el servicio paso el dia en
    pasadas de 12M de filas.  Segunda: el marcador vivia solo en el proceso,
    y cada reinicio (deploy, guardarrail) pagaba una completa de horas justo
    despues de arrancar.  Aqui se fija el umbral, la lectura del disco en
    frio, y que el disco jamas tumba una pasada.
    """

    NARROW = 1

    def setUp(self):
        self.fixture = DeltaFixture().build()
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        overridden = override_settings(ATOMICDB_STATE_DIR=self.state_dir.name)
        overridden.enable()
        self.addCleanup(overridden.disable)

    def _pass(self, **kwargs):
        with patch.object(ingest, 'selector_horizon_width',
                          return_value=self.NARROW):
            return ingest.refresh_priorities(force=True, **kwargs)

    def _marker_file(self):
        return ingest._delta_state_path()

    def _cold_process(self):
        """Lo que queda tras un reinicio: memoria virgen, disco intacto."""
        ingest._selector_delta_state.update(
            {'at': None, 'mode': '', 'rows': 0, 'seconds': 0.0,
             'disk_checked': False})

    def test_the_gap_outlives_any_plausible_complete_pass(self):
        """El numero que hizo el dano, fijado como contrato.

        La pasada completa mas larga observada en la base viva duro 2,2
        horas.  Si alguien vuelve a bajar este umbral por debajo de eso, el
        delta vuelve a ser inalcanzable por construccion y este test es el
        unico sitio donde esa historia esta escrita en ejecutable.
        """
        self.assertGreaterEqual(ingest.SELECTOR_DELTA_MAX_GAP,
                                timedelta(hours=6))

    def test_a_completed_pass_leaves_the_marker_on_disk(self):
        self._pass()
        with open(self._marker_file(), encoding='utf-8') as fh:
            marker = json.load(fh)
        self.assertEqual(marker['at'], ingest._selector_delta_state['at']
                         .isoformat())

    def test_a_restart_resumes_incremental_from_disk(self):
        """El test del titular: reiniciar ya no cuesta una pasada completa."""
        self._pass()
        self._cold_process()

        self._pass()

        self.assertEqual(ingest.selector_pass_report()['mode'], 'delta')

    def test_reset_forgets_the_disk_too(self):
        """Forzar una completa tiene que poder mas que la persistencia."""
        self._pass()
        ingest.reset_selector_delta_state()
        self.assertFalse(os.path.exists(self._marker_file()))

        self._pass()

        self.assertEqual(ingest.selector_pass_report()['mode'], 'full')

    def test_a_broken_marker_means_a_complete_pass(self):
        with open(self._marker_file(), 'w', encoding='utf-8') as fh:
            fh.write('{esto no es json')
        self._cold_process()

        self._pass()

        self.assertEqual(ingest.selector_pass_report()['mode'], 'full')

    def test_a_naive_timestamp_is_distrusted(self):
        """Sin zona no hay aritmetica de huecos que valga."""
        with open(self._marker_file(), 'w', encoding='utf-8') as fh:
            json.dump({'at': '2026-08-04T21:00:00'}, fh)
        self._cold_process()

        self._pass()

        self.assertEqual(ingest.selector_pass_report()['mode'], 'full')

    def test_a_stale_marker_falls_back_to_a_complete_pass(self):
        """El disco entra por la MISMA puerta del hueco que la memoria."""
        stale = timezone.now() - ingest.SELECTOR_DELTA_MAX_GAP - timedelta(
            seconds=1)
        with open(self._marker_file(), 'w', encoding='utf-8') as fh:
            json.dump({'at': stale.isoformat()}, fh)
        self._cold_process()

        self._pass()

        self.assertEqual(ingest.selector_pass_report()['mode'], 'full')

    def test_an_unwritable_state_dir_does_not_kill_the_pass(self):
        with override_settings(ATOMICDB_STATE_DIR=os.path.join(
                self.state_dir.name, 'no', 'existe')):
            self.assertTrue(self._pass())
            self.assertEqual(ingest.selector_pass_report()['mode'], 'full')


@override_settings(ATOMICDB_SELECTOR_V2=True)
class WriteVolumeTests(TestCase):
    """Cuantas filas escribe una pasada, y de que tamano son esos cambios.

    ``selector_rows`` mide LECTURA, no escritura, y esa confusion salio cara:
    con el guard de ``priority != prio`` puesto, el selector reescribia unos
    cinco millones de filas por pasada — 704 UPDATE/s medidos — porque las
    prioridades si cambiaban, solo que por milesimas de micro-deriva del regret
    y de los votos.  Desde fuera, repuntuar doce millones y reescribir cinco se
    veian exactamente igual.

    Lo que se fija aqui es la medida que tiene que decidir el epsilon de
    escritura: las que se escriben, las que salen identicas y como se reparten
    por tamano del cambio.  Sin el histograma, el umbral seria un numero
    elegido a ojo, y equivocarse por arriba congela la cima.
    """

    NARROW = 1        # el mismo horizonte enano que el resto de estos tests
    STOMP = -777.0    # una prioridad que ninguna formula produce

    def setUp(self):
        self.fixture = DeltaFixture().build()

    def _pass(self, **kwargs):
        with patch.object(ingest, 'selector_horizon_width',
                          return_value=self.NARROW):
            return ingest.refresh_priorities(force=True, **kwargs)

    def _stomp(self, *nodes):
        Position.objects.filter(key__in=[node.key for node in nodes]).update(
            priority=self.STOMP)

    def test_a_rewritten_row_is_counted_and_its_change_is_sized(self):
        """Un cambio enorme se cuenta, y cae en el cajon de los enormes.

        El stomp esta a 777 unidades de cualquier precio que la formula sepa
        escribir — el techo entero son 67 — asi que no hay cajon pequeno que
        pueda quedarselo.
        """
        self._pass()
        self._stomp(self.fixture.leaf)

        self._pass(delta=False)

        report = ingest.selector_pass_report()
        self.assertGreaterEqual(report['written'], 1)
        self.assertEqual(report['hist']['gt+1'], report['written'])
        self.assertEqual(sum(report['hist'].values()), report['written'])

    def test_a_second_pass_over_a_quiet_graph_writes_nothing(self):
        """El otro extremo, y el que hace util a la medida.

        Cuando el guard ES la respuesta correcta se ve igual de claro que
        cuando no lo es; sin estos contadores, los dos casos producian
        exactamente el mismo log.
        """
        self._pass()

        self._pass()

        report = ingest.selector_pass_report()
        self.assertEqual(report['mode'], 'delta')
        self.assertEqual(report['written'], 0)
        self.assertGreater(report['unchanged'], 0)
        self.assertEqual(sum(report['hist'].values()), 0)

    def test_the_ball_is_reported_and_fits_inside_what_was_scored(self):
        """El tamano de la bola es lo que fija el coste de todo lo demas."""
        self._pass()

        report = ingest.selector_pass_report()
        self.assertGreater(report['ball'], 0)
        self.assertLessEqual(report['ball'], report['rows'])

    def test_the_shadow_does_not_disturb_the_last_real_pass(self):
        """El sombra no escribe, asi que no tiene nada que contar.

        Si tocara estos numeros, el JSON del servicio estaria hablando de una
        pasada que no cambio ni una fila.
        """
        self._pass()
        self._stomp(self.fixture.leaf)
        self._pass(delta=False)
        before = ingest.selector_pass_report()
        self.assertGreater(before['written'], 0)

        with patch.object(ingest, 'selector_horizon_width',
                          return_value=self.NARROW):
            ingest.refresh_priorities_v2(force=True, top_k=10_000)

        self.assertEqual(ingest.selector_pass_report(), before)

    def test_the_service_publishes_the_write_counters(self):
        """El sitio donde alguien va a leer esto de verdad es journalctl.

        El olvido del medio es lo que hace que la pasada del servicio sea
        completa: el stomp no mueve ``updated`` — no puede, es lo que le da
        sentido — asi que una delta no mirara una fila que quedo fuera de la
        bola, y aqui lo que se comprueba es el JSON, no el modo.
        """
        self._pass()
        self._stomp(self.fixture.leaf)
        ingest.reset_selector_delta_state()
        out = StringIO()
        with patch.object(ingest, 'selector_horizon_width',
                          return_value=self.NARROW):
            call_command('refresh_selector', stdout=out)

        report = json.loads(out.getvalue())
        self.assertGreaterEqual(report['selector_written'], 1)
        self.assertEqual(report['selector_delta_hist']['gt+1'],
                         report['selector_written'])
        self.assertGreater(report['selector_ball'], 0)
        self.assertIn('selector_unchanged', report)


@override_settings(ATOMICDB_SELECTOR_V2=True)
class DeltaAgreesWithTheCompletePassTests(TestCase):
    """Lo tocado, repuntuado: delta y completa escriben el MISMO numero.

    El ancla de ``AnchorTests`` prueba que los dos MOTORES coinciden; esto
    prueba que los dos MODOS del mismo motor tambien, sobre las filas que el
    delta se compromete a mirar.  Son dos afirmaciones distintas y hacen falta
    las dos.
    """

    def setUp(self):
        self.fixture = DeltaFixture().build()

    def test_delta_writes_what_a_complete_pass_would_write(self):
        with patch.object(ingest, 'selector_horizon_width', return_value=1):
            ingest.refresh_priorities_v2(force=True)
            self.fixture.leaf.visits = 2
            self.fixture.leaf.save()
            self.fixture.mid.eval_cp = -80
            self.fixture.mid.save()

            ingest.refresh_priorities_v2(force=True)
            self.assertEqual(ingest.selector_pass_report()['mode'], 'delta')
            incremental = dict(Position.objects.filter(status='UNKNOWN')
                               .values_list('key', 'priority'))

            ingest.refresh_priorities_v2(force=True, delta=False)
            complete = dict(Position.objects.filter(status='UNKNOWN')
                            .values_list('key', 'priority'))

        self.assertEqual(set(incremental), set(complete))
        for key, priority in complete.items():
            self.assertAlmostEqual(incremental[key], priority, delta=1e-9,
                                   msg='divergen en {}'.format(key))


class MarkingWhatMovedTests(TestCase):
    """Quien cambia un termino de la formula tiene que mover ``updated``.

    Es el contrato del que cuelga el modo delta, y NO se cumple solo: un
    ``update`` de queryset no dispara ``auto_now``, asi que cada sitio que
    escribe columnas del selector por esa via tiene que poner la marca a mano.
    Los cierres y la siembra de eval ya lo hacian; los dos de aqui no, y sin
    ellos una fila cambiaba de precio a espaldas de la pasada incremental.

    Esto se prueba sobre la MARCA y no sobre la prioridad resultante a
    proposito: la prioridad de un nodo concreto depende de la bola, del
    horizonte y de la formula entera, y lo que hay que fijar aqui es mucho mas
    simple — que la fila diga que se movio.
    """

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.child = Edge.objects.filter(parent=self.root).first().child

    def _marks(self):
        return dict(Position.objects.values_list('key', 'updated'))

    def test_reviving_a_tombstone_marks_the_row(self):
        Position.objects.filter(key=self.child.key).update(
            priority=ingest.DEAD)
        before = self._marks()

        revived = ingest._revive_tombstones([self.child.key])

        self.assertEqual(revived, 1)
        self.assertGreater(self._marks()[self.child.key],
                           before[self.child.key])

    def test_reviving_marks_the_direct_children_too(self):
        """El nivel de hijos vuelve en el mismo UPDATE, y con la misma marca."""
        grandchild = ingest.get_or_create_position(quiet_fen(0, rank=5))
        Edge.objects.create(parent=self.child, move_uci='h1h2',
                            child=grandchild)
        Position.objects.filter(key=grandchild.key).update(
            priority=ingest.DEAD)
        before = self._marks()

        ingest._revive_tombstones([self.child.key])

        self.assertGreater(self._marks()[grandchild.key],
                           before[grandchild.key])

    def test_a_row_that_was_not_a_tombstone_is_not_marked(self):
        """Resucitar lo que ya estaba vivo seria ensuciar la marca por nada."""
        before = self._marks()

        self.assertEqual(ingest._revive_tombstones([self.child.key]), 0)

        self.assertEqual(self._marks(), before)

    def test_adopting_a_subtree_into_a_campaign_marks_the_rows(self):
        """El bono de campana es un termino de la formula, no una etiqueta."""
        campaign = Campaign.objects.create(
            name='adoptante', root=self.root, votes=3,
            state=Campaign.CState.ACTIVE)
        before = self._marks()

        tagged, _seen = views._tag_campaign_subtree(campaign)

        self.assertGreater(tagged, 0)
        marks = self._marks()
        adopted = Position.objects.filter(campaign=campaign).values_list(
            'key', flat=True)
        self.assertGreater(len(adopted), 0)
        for key in adopted:
            self.assertGreater(marks[key], before[key])
