"""Lo que el explorador tiene PROHIBIDO volver a costar.

La pagina que se pulsa a cada jugada llego a costar 0,47 s de CPU por render
con siete consultas y 0,01 s de base: no era la base, era el movegen.  Una
llamada a PyFFish cuesta lo mismo con una jugada que con veinte porque lo que
se paga es MONTAR LA POSICION (§ ``logic``), asi que el coste de esta pagina se
mide en NUMERO DE LLAMADAS y en nada mas.  Habia noventa y tantas:

* cuarenta rejugaban, ply a ply, la MISMA ruta que la vista acababa de validar
  — solo para preguntarle al catalogo de aperturas por unas claves que la vista
  ya tenia en la mano;
* treinta y cuatro reconstruian las claves de las jugadas legales sin arista,
  que son funcion pura de (FEN del padre, jugada) y no cambian nunca;
* y el mapa de nombres comunitarios se pedia una vez POR FILA, que con la cache
  compartida son cuarenta y tantos viajes a Redis para devolver siempre el
  mismo diccionario.

Lo que fijan estos tests, en orden de importancia:

* que la salida no cambie NI UN BYTE por no rejugar (``ExplorerOutputTests``);
* que la premisa sea cierta: la clave guardada de un hijo es exactamente la que
  produce ``apply_move`` sobre el padre (``StoredChildIdentityTests``);
* y que el numero de llamadas al movegen no vuelva a subir
  (``ExplorerWorkBudgetTests``), que es el equivalente al presupuesto de
  consultas de la portada (§ test_home_performance).
"""

import re
from unittest import mock

from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from . import community_names, logic, openings, views
from .models import Edge, OpeningNameSuggestion, Position
from .testing import TestCase


CACHE_FOR_TESTS = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'atomicdb-explore-performance-tests',
    },
}

# La forma de la posicion que se perfilo en produccion: una ruta declarada
# larga y una hoja con hijos materializados Y jugadas legales todavia sin
# arista.  El arbol es pequeno a proposito — lo que decide el coste de esta
# pagina es la PROFUNDIDAD de la ruta y la ANCHURA de la hoja, no cuantas
# posiciones haya en la tabla.
ROUTE_PLIES = 20
MATERIALISED_CHILDREN = 12

# El token CSRF se enmascara distinto en CADA render por diseno, asi que es lo
# unico que dos renders identicos no pueden compartir.  Se localiza por su
# campo y se sustituye alla donde aparezca; el minificador de HTML puede
# haberle quitado las comillas, asi que no se asume ninguna forma exacta.
_CSRF_FIELD = re.compile(
    rb'csrfmiddlewaretoken[^>]{0,120}?value="?([A-Za-z0-9]{32,})"?')


def _mask_csrf(body):
    for token in set(_CSRF_FIELD.findall(body)):
        body = body.replace(token, b'CSRFTOKEN')
    return body


def build_explorer_fixture(plies=ROUTE_PLIES,
                           children=MATERIALISED_CHILDREN):
    """Una ruta real desde la inicial y una hoja medio expandida.

    Devuelve ``(clave de la hoja, ruta uci, jugadas legales de la hoja)``.
    """
    fen = logic.start_fen()
    parent = Position.objects.get_or_create(
        key=logic.key_of(fen), defaults={'fen': fen})[0]
    ucis = []
    for _ply in range(plies):
        moves = logic.legal_moves(fen)
        # Una hermana por ply, para que la tabla del padre no este vacia y la
        # reconstruccion del linaje tenga donde elegir.
        for uci in moves[:2]:
            sibling_fen = logic.apply_move(fen, uci)
            sibling = Position.objects.get_or_create(
                key=logic.key_of(sibling_fen),
                defaults={'fen': sibling_fen})[0]
            Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                       defaults={'child': sibling})
        main = moves[0]
        fen = logic.apply_move(fen, main)
        parent = Position.objects.get(key=logic.key_of(fen))
        ucis.append(main)

    legal = logic.legal_moves(fen)
    for index, uci in enumerate(legal[:children]):
        child_fen = logic.apply_move(fen, uci)
        child = Position.objects.get_or_create(
            key=logic.key_of(child_fen),
            defaults={'fen': child_fen, 'eval_cp': 30 - index, 'visits': 2})[0]
        Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                   defaults={'child': child})
    return parent.key, ','.join(ucis), legal


@override_settings(CACHES=CACHE_FOR_TESTS)
class StoredChildIdentityTests(TestCase):
    """La premisa de todo lo demas: lo guardado ES lo que el movegen produce.

    El explorador dejo de rejugar la ruta porque las claves ya estaban — en la
    validacion de ``play`` y en las aristas del linaje.  Si una clave guardada
    difiriera de la que produce ``apply_move``, el fallo seria mudo: la pagina
    nombraria otra apertura sin que nada se rompiera.  Asi que la equivalencia
    se fija aqui, y no se deduce.
    """

    def setUp(self):
        cache.clear()
        self.leaf, self.route, _legal = build_explorer_fixture(plies=8,
                                                               children=6)

    def test_every_stored_edge_is_the_move_it_says_it_is(self):
        edges = list(Edge.objects.select_related('parent', 'child'))
        self.assertGreater(len(edges), 10)
        for edge in edges:
            self.assertEqual(
                logic.apply_move(edge.parent.fen, edge.move_uci),
                edge.child.fen,
                f'edge {edge.move_uci} from {edge.parent.key[:12]}')
            self.assertEqual(logic.key_of(edge.child.fen), edge.child.key)

    def test_the_route_keys_are_the_replayed_keys(self):
        """Lo que ``_route_prefix_keys`` entrega == rejugar la linea entera."""
        top, line, ucis = views._validated_play_route(self.route, self.leaf)

        stored = views._route_prefix_keys(top, line, ucis)

        fen = logic.start_fen()
        replayed = [logic.key_of(fen)]
        for uci in ucis:
            fen = logic.apply_move(fen, uci)
            replayed.append(logic.key_of(fen))
        self.assertEqual(stored, replayed)

    def test_the_canonical_lineage_keys_are_the_replayed_keys(self):
        """Y lo mismo por el otro camino: el linaje reconstruido de aristas."""
        pos = Position.objects.get(key=self.leaf)
        top, line, ucis = views._canonical_route(pos)

        stored = views._route_prefix_keys(top, line, ucis)

        fen = logic.start_fen()
        replayed = [logic.key_of(fen)]
        for uci in ucis:
            fen = logic.apply_move(fen, uci)
            replayed.append(logic.key_of(fen))
        self.assertEqual(stored, replayed)

    def test_naming_by_key_agrees_with_naming_by_replay(self):
        ucis = self.route.split(',')
        top, line, _ucis = views._validated_play_route(self.route, self.leaf)
        keys = views._route_prefix_keys(top, line, ucis)

        self.assertEqual(openings.match_line_keys(keys),
                         openings.match_line(ucis))

    def test_a_named_line_is_still_named_by_its_keys(self):
        """Con una apertura de verdad delante, no con una linea cualquiera."""
        ucis = ['g1f3', 'f7f6', 'b1c3']
        fen = logic.start_fen()
        keys = [logic.key_of(fen)]
        for uci in ucis:
            fen = logic.apply_move(fen, uci)
            keys.append(logic.key_of(fen))

        by_keys = openings.match_line_keys(keys)

        self.assertEqual(by_keys['name'], 'Two Knights Opening')
        self.assertEqual(by_keys, openings.match_line(ucis))

    def test_a_sequence_that_is_not_keys_falls_back_instead_of_naming(self):
        with self.assertRaises(openings.InvalidOpeningLine):
            openings.match_line_keys(['g1f3', 'f7f6'])
        with self.assertRaises(openings.InvalidOpeningLine):
            openings.match_line_keys([])

    def test_the_cached_child_keys_are_the_computed_child_keys(self):
        pos = Position.objects.get(key=self.leaf)
        known = {edge.move_uci for edge in Edge.objects.filter(parent=pos)}
        offtree = [uci for uci in logic.legal_moves(pos.fen)
                   if uci not in known]
        self.assertGreater(len(offtree), 5)
        expected = {uci: logic.key_of(logic.apply_move(pos.fen, uci))
                    for uci in offtree}

        cold = views._child_keys(pos, offtree)
        warm = views._child_keys(pos, offtree)

        self.assertEqual(cold, expected)
        self.assertEqual(warm, expected)

    def test_the_shared_map_names_what_asking_one_by_one_names(self):
        keys = [self.leaf] + [
            edge.child_id for edge in Edge.objects.all()[:20]]
        names = community_names.approved_map()

        for key in keys:
            self.assertEqual(views._named_right_here(key),
                             views._named_right_here(key, names=names))


@override_settings(CACHES=CACHE_FOR_TESTS)
class ExplorerOutputTests(TestCase):
    """La pagina rapida y la pagina lenta son el MISMO byte.

    El camino lento sigue en el codigo — es la caida de ``_navigation_opening``
    cuando no le llegan claves, y ``_child_keys`` sin almacen — asi que se
    puede pedir a la vista que lo tome y comparar.  Es la unica forma de
    afirmar "no cambia nada" sin fiarse de leer el diff.
    """

    def setUp(self):
        cache.clear()
        self.leaf, self.route, _legal = build_explorer_fixture()
        self.url = f'/atomicdb/explore/{self.leaf}/?play={self.route}'

    def _reference(self, url):
        """El render de antes: ruta rejugada y claves de hijo sin almacen."""
        with mock.patch('atomicdb.views._route_prefix_keys',
                        return_value=None), \
                mock.patch('atomicdb.views.CHILD_KEY_CACHE_SECONDS', 0):
            response = Client().get(url)
        self.assertEqual(response.status_code, 200)
        return _mask_csrf(response.content)

    def test_the_declared_route_renders_the_same_bytes(self):
        reference = self._reference(self.url)

        cache.clear()
        first = Client().get(self.url)
        second = Client().get(self.url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(_mask_csrf(first.content), reference)
        # Y con el almacen ya caliente, que es el camino que sirve de verdad.
        self.assertEqual(_mask_csrf(second.content), reference)

    def test_the_canonical_lineage_renders_the_same_bytes(self):
        url = f'/atomicdb/explore/{self.leaf}/'
        reference = self._reference(url)

        cache.clear()
        body = Client().get(url).content

        self.assertEqual(_mask_csrf(body), reference)

    def test_a_named_opening_still_says_its_name(self):
        """Una pagina barata porque dejo de nombrar aperturas no vale."""
        ucis = ['g1f3', 'f7f6', 'b1c3']
        fen = logic.start_fen()
        parent = Position.objects.get_or_create(
            key=logic.key_of(fen), defaults={'fen': fen})[0]
        for uci in ucis:
            fen = logic.apply_move(fen, uci)
            child = Position.objects.get_or_create(
                key=logic.key_of(fen), defaults={'fen': fen})[0]
            Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                       defaults={'child': child})
            parent = child
        url = (f'/atomicdb/explore/{parent.key}/?play=' + ','.join(ucis))

        response = Client().get(url)

        self.assertEqual(response.context['opening']['name'],
                         'Two Knights Opening')
        self.assertEqual(_mask_csrf(response.content), self._reference(url))

    def test_an_approved_community_name_survives_the_shared_map(self):
        """El nombre comunitario se pinta igual con el mapa compartido."""
        pos = Position.objects.get(key=self.leaf)
        OpeningNameSuggestion.objects.create(
            position=pos, proposed_name='Belfast Gambit', ip='10.0.0.9',
            status=OpeningNameSuggestion.SState.APPROVED,
            resolved_by='boss', resolved_at=timezone.now())
        community_names.invalidate()

        response = Client().get(self.url)

        self.assertEqual(response.context['opening']['name'],
                         'Belfast Gambit')
        self.assertContains(response, 'Belfast Gambit')


@override_settings(CACHES=CACHE_FOR_TESTS)
class ExplorerWorkBudgetTests(TestCase):
    """El presupuesto de MOVEGEN de un render, que es todo su coste.

    Los numeros no son magicos y por eso no se escriben a mano: son "una
    llamada por ply de la ruta declarada" y "una por jugada legal sin arista",
    y NI UNA MAS.  Si esto sube, la pregunta correcta no es subir el techo sino
    que le puse a la pagina que vuelve a rejugar algo.
    """

    def setUp(self):
        cache.clear()
        self.leaf, self.route, self.legal = build_explorer_fixture()
        self.url = f'/atomicdb/explore/{self.leaf}/?play={self.route}'
        pos = Position.objects.get(key=self.leaf)
        known = {edge.move_uci for edge in Edge.objects.filter(parent=pos)}
        self.offtree = [uci for uci in self.legal if uci not in known]
        self.assertGreater(len(self.offtree), 10)

    def _counted(self, url):
        with mock.patch('atomicdb.logic.apply_move',
                        wraps=logic.apply_move) as applied, \
                mock.patch('atomicdb.logic.legal_moves',
                           wraps=logic.legal_moves) as generated, \
                mock.patch('atomicdb.openings.match_line',
                           wraps=openings.match_line) as replayed, \
                mock.patch('atomicdb.community_names.approved_map',
                           wraps=community_names.approved_map) as names:
            response = Client().get(url)
        self.assertEqual(response.status_code, 200)
        return {
            'apply_move': applied.call_count,
            'legal_moves': generated.call_count,
            'match_line': replayed.call_count,
            'approved_map': names.call_count,
        }

    def test_a_cold_render_applies_one_move_per_ply_and_reply(self):
        cache.clear()

        counts = self._counted(self.url)

        self.assertEqual(
            counts['apply_move'],
            len(self.route.split(',')) + len(self.offtree),
            'the explorer applies more moves than it has plies and off-tree '
            'replies')
        # Una sola generacion: la de las jugadas legales de ESTA posicion.  El
        # catalogo de aperturas ya no vuelve a comprobar la legalidad de una
        # ruta que la vista acaba de validar jugada a jugada.
        self.assertEqual(counts['legal_moves'], 1)
        self.assertEqual(counts['match_line'], 0)
        self.assertEqual(counts['approved_map'], 1)

    def test_a_warm_render_does_not_rebuild_the_offtree_keys(self):
        Client().get(self.url)

        counts = self._counted(self.url)

        self.assertEqual(counts['apply_move'], len(self.route.split(',')))
        self.assertEqual(counts['legal_moves'], 1)

    def test_a_second_visit_builds_no_position_at_all(self):
        """El presupuesto de verdad: cuantas POSICIONES monta PyFFish.

        Es lo unico que cuesta dinero (§ logic, "cuanto cuesta una pregunta al
        movegen"), y con el almacen y el proceso calientes la respuesta es
        cero: la ruta ya se camino y las claves de los hijos ya se calcularon.
        """
        Client().get(self.url)

        with mock.patch('atomicdb.logic.pf.get_fen',
                        wraps=logic.pf.get_fen) as built:
            response = Client().get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(built.call_count, 0)

    def test_a_restarted_worker_pays_one_position_per_route_ply(self):
        """Y el peor caso: un proceso recien arrancado que no recuerda nada."""
        Client().get(self.url)
        logic.apply_move.cache_clear()

        with mock.patch('atomicdb.logic.pf.get_fen',
                        wraps=logic.pf.get_fen) as built:
            Client().get(self.url)

        self.assertEqual(built.call_count, len(self.route.split(',')))

    def test_a_warm_lineage_page_asks_the_movegen_once(self):
        """Sin ruta declarada no hay ni un rejuego: el linaje sale de aristas.

        Las claves de la linea son las de las aristas y las de los hijos sin
        arista estan en el almacen, asi que lo unico que queda por preguntar es
        que jugadas legales tiene ESTA posicion.
        """
        url = f'/atomicdb/explore/{self.leaf}/'
        Client().get(url)

        counts = self._counted(url)

        self.assertEqual(counts['apply_move'], 0)
        self.assertEqual(counts['legal_moves'], 1)
        self.assertEqual(counts['match_line'], 0)

    def test_navigating_no_longer_replays_the_route_either(self):
        """``goto`` pagaba el mismo rejuego, y en cada click."""
        pos = Position.objects.get(key=self.leaf)
        uci = self.offtree[0]
        url = f'/atomicdb/goto/{pos.key}/{uci}/?play={self.route}'

        with mock.patch('atomicdb.openings.match_line',
                        wraps=openings.match_line) as replayed, \
                mock.patch('atomicdb.logic.legal_moves',
                           wraps=logic.legal_moves) as generated:
            response = Client().get(url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(replayed.call_count, 0)
        # Dos generaciones, y las dos son trabajo propio de navegar: la
        # legalidad de la jugada pulsada, y el ``terminal_status`` del nodo que
        # acaba de nacer (§ ingest.get_or_create_position).
        self.assertEqual(generated.call_count, 2)
