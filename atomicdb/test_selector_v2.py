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
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import override_settings

from . import ingest, logic
from .database import connection
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
        """El modo solo calculo y el que escribe tienen que decir lo mismo."""
        ingest.refresh_priorities_v1(force=True)
        expected = dict(Position.objects.filter(status='UNKNOWN')
                        .values_list('key', 'priority'))
        Position.objects.all().update(priority=0.0)

        ingest.refresh_priorities_v2(force=True)
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

    def test_v1_dies_on_it(self):
        with child_row_missing(self.ghost.key):
            with self.assertRaises(KeyError):
                ingest._regret_from_root()

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
