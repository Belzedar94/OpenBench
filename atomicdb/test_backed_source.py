"""Salto al ORIGEN de un valor respaldado.

Una fila puede decir -1166 "backed a 12 plies" y no decir DONDE nacio ese
numero.  Bajarlo a mano son doce clicks por la espina de soporte, y el que
mira una cabecera raramente quiere doce clicks: quiere el ply que la explica.
Esto camina esa espina de una vez y redirige alli, con la historia del
visitante extendida para que aterrice dentro de SU partida.
"""

import hashlib
import re
from unittest import mock

from django.conf import settings

from . import ingest, logic, views
from .models import Edge, Position
from .testing import TestCase


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    """Posicion sintetica: al caminante solo le importan los campos backed."""
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci=None):
    uci = uci or f'a1a{1 + Edge.objects.filter(parent=parent).count()}'
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _jump(client, key, **params):
    return client.get(f'/atomicdb/backed-source/{key}/', params)


class BackedSpineWalkTests(TestCase):
    """Semantica del paseo: hasta donde baja y donde se planta."""

    def test_a_four_level_spine_lands_on_the_node_that_owns_the_value(self):
        # A -> B -> C -> D, todos prestandose el mismo -1166 por su
        # ``backed_move``.  D es el unico cuyo respaldo coincide con su eval
        # propia: ahi es donde el motor busco de verdad.
        a = _pos('A', 'w', eval_cp=-40, backed_eval=-1166, backed_move='a1a1')
        b = _pos('B', 'b', eval_cp=-80, backed_eval=-1166, backed_move='a1a1')
        c = _pos('C', 'w', eval_cp=-120, backed_eval=-1166, backed_move='a1a1')
        d = _pos('D', 'b', eval_cp=-1166, backed_eval=-1166,
                 backed_move='a1a1')
        _edge(a, b, 'a1a1')
        _edge(b, c, 'a1a1')
        _edge(c, d, 'a1a1')

        response = _jump(self.client, a.key)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{d.key}/')

    def test_the_walk_never_writes_and_is_not_cacheable(self):
        a = _pos('WA', 'w', eval_cp=10, backed_eval=300, backed_move='a1a1')
        b = _pos('WB', 'b', eval_cp=300, backed_eval=300)
        _edge(a, b, 'a1a1')
        before = list(Position.objects.values_list('key', 'updated'))

        response = _jump(self.client, a.key)

        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(list(Position.objects.values_list('key', 'updated')),
                         before)

    def test_a_node_backed_by_its_own_search_is_already_the_origin(self):
        """backed == eval propia: el valor nace aqui, no doce plies abajo."""
        here = _pos('OWN', 'w', eval_cp=512, backed_eval=512,
                    backed_move='a1a1')
        below = _pos('OWN_CHILD', 'b', eval_cp=512)
        _edge(here, below, 'a1a1')

        response = _jump(self.client, here.key)

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{here.key}/')

    def test_a_node_without_backed_value_is_the_origin(self):
        alone = _pos('ALONE', 'w', eval_cp=77)

        response = _jump(self.client, alone.key)

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{alone.key}/')

    def test_a_proven_closure_stops_the_descent(self):
        """El valor viene de una PRUEBA: debajo de eso no hay nada que ver."""
        top = _pos('PT', 'w', eval_cp=200, backed_eval=10_000,
                   backed_move='a1a1')
        won = _pos('PW', 'b', status='WHITE_WIN', closure='TERMINAL',
                   eval_cp=150, backed_eval=10_000, backed_move='a1a1')
        deeper = _pos('PD', 'w', eval_cp=10_000)
        _edge(top, won, 'a1a1')
        _edge(won, deeper, 'a1a1')

        response = _jump(self.client, top.key)

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{won.key}/')

    def test_a_missing_edge_leaves_the_visitor_where_the_walk_got(self):
        """Un respaldo puede ser mas viejo que el grafo que lo sostenia."""
        top = _pos('MT', 'w', eval_cp=5, backed_eval=400, backed_move='a1a1')
        mid = _pos('MM', 'b', eval_cp=9, backed_eval=400, backed_move='h7h8q')
        _edge(top, mid, 'a1a1')

        response = _jump(self.client, top.key)

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{mid.key}/')

    def test_a_transposition_cycle_cuts_instead_of_hanging(self):
        # 1.Nf3 Nf6 2.Ng1 Ng8 vuelve a la posicion inicial: la espina puede
        # cerrarse sobre si misma y el paseo tiene que terminar.  Y terminar
        # asi no es aterrizar en un origen: es una REPETICION, y el destino la
        # recibe etiquetada para poder decirlo.
        a = _pos('CA', 'w', eval_cp=1, backed_eval=120, backed_move='a1a1')
        b = _pos('CB', 'b', eval_cp=2, backed_eval=120, backed_move='b1b1')
        _edge(a, b, 'a1a1')
        _edge(b, a, 'b1b1')          # ciclo

        response = _jump(self.client, a.key)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{b.key}/?repetition=1')

    def test_the_ply_cap_bounds_the_descent(self):
        """El mismo tope con el que el valor SUBIO acota lo que se baja."""
        length = ingest.BACKED_MAX_PLIES + 6
        chain = [_pos(f'L{i}', 'w' if i % 2 == 0 else 'b',
                      eval_cp=i, backed_eval=9_000, backed_move='a1a1')
                 for i in range(length)]
        for upper, lower in zip(chain, chain[1:]):
            _edge(upper, lower, 'a1a1')

        response = _jump(self.client, chain[0].key)

        self.assertEqual(
            response['Location'],
            f'/atomicdb/explore/{chain[ingest.BACKED_MAX_PLIES].key}/')

    def test_the_walk_costs_one_query_per_step_at_most(self):
        alias = settings.ATOMICDB_DATABASE_ALIAS
        a = _pos('QA', 'w', eval_cp=1, backed_eval=500, backed_move='a1a1')
        b = _pos('QB', 'b', eval_cp=2, backed_eval=500, backed_move='a1a1')
        c = _pos('QC', 'w', eval_cp=500, backed_eval=500)
        _edge(a, b, 'a1a1')
        _edge(b, c, 'a1a1')

        # Dos aristas caminadas, dos consultas: el hijo viaja en el mismo
        # select_related y la parada no cuesta nada.
        with self.assertNumQueries(2, using=alias):
            origin, walked, repetition = views._walk_backed_spine(a)

        self.assertEqual(origin.key, c.key)
        self.assertEqual(walked, ['a1a1', 'a1a1'])
        self.assertFalse(repetition)

    def test_an_unknown_key_is_a_404(self):
        response = _jump(self.client, _key('nobody'))

        self.assertEqual(response.status_code, 404)


class BackedSourceRouteTests(TestCase):
    """La historia del visitante viaja con el salto, o no viaja nada."""

    def _materialize(self, ucis):
        """Posiciones REALES conectadas: el ``play`` se replica de verdad."""
        fen = logic.start_fen()
        current = ingest.get_or_create_position(fen)
        positions = [current]
        for uci in ucis:
            fen = logic.apply_move(fen, uci)
            child = ingest.get_or_create_position(fen)
            Edge.objects.update_or_create(
                parent=current, move_uci=uci, defaults={'child': child})
            current = child
            positions.append(current)
        return positions

    def _spine(self, positions, ucis):
        """Presta el mismo valor hacia arriba por las aristas dadas."""
        for position, uci in zip(positions, ucis):
            position.eval_cp = -10
            position.backed_eval = -1166
            position.backed_move = uci
            position.save()
        origin = positions[len(ucis)]
        origin.eval_cp = -1166
        origin.backed_eval = -1166
        origin.save()
        return origin

    def test_a_valid_play_arrives_extended_by_the_walked_ucis(self):
        ucis = ['g1f3', 'd7d6', 'b1c3', 'g8f6']
        positions = self._materialize(ucis)
        start = positions[1]                       # tras 1.Nf3
        origin = self._spine(positions[1:], ucis[1:])

        response = _jump(self.client, start.key, play='g1f3')

        self.assertEqual(
            response['Location'],
            f'/atomicdb/explore/{origin.key}/?play=g1f3,d7d6,b1c3,g8f6')

    def test_the_extended_route_still_opens_the_destination(self):
        """El enlace lo emite el propio sitio: no puede acabar en un 409."""
        ucis = ['g1f3', 'f7f6', 'b1c3']
        positions = self._materialize(ucis)
        origin = self._spine(positions, ucis)

        jump = _jump(self.client, positions[0].key, play='')
        landed = self.client.get(jump['Location'])

        self.assertEqual(
            jump['Location'],
            f'/atomicdb/explore/{origin.key}/?play=' + ','.join(ucis))
        self.assertEqual(landed.status_code, 200)
        self.assertEqual(landed.context['active_play'], ucis)

    def test_an_empty_play_at_the_root_still_travels(self):
        """Cero plies de historia ES una historia: la raiz tambien extiende."""
        ucis = ['d2d4', 'd7d5']
        positions = self._materialize(ucis)
        origin = self._spine(positions, ucis)

        response = _jump(self.client, positions[0].key, play='')

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{origin.key}/?play=d2d4,d7d5')

    def test_a_play_that_does_not_reach_this_position_is_dropped(self):
        ucis = ['g1f3', 'f7f6']
        positions = self._materialize(ucis)
        start = positions[1]
        origin = self._spine(positions[1:], ucis[1:])

        response = _jump(self.client, start.key, play='d2d4')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{origin.key}/')

    def test_a_malformed_play_is_dropped_without_breaking_the_jump(self):
        ucis = ['g1f3', 'f7f6']
        positions = self._materialize(ucis)
        origin = self._spine(positions, ucis)

        response = _jump(self.client, positions[0].key, play='zzzz,nope')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{origin.key}/')

    def test_an_illegal_play_is_dropped_too(self):
        """Bien formado no es legal: el replay Atomic sigue mandando."""
        ucis = ['g1f3', 'f7f6']
        positions = self._materialize(ucis)
        origin = self._spine(positions, ucis)

        response = _jump(self.client, positions[0].key, play='a1a8')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{origin.key}/')


class BackedJumpRenderTests(TestCase):
    """El chip deja de ser solo una etiqueta: EL CHIP ES el enlace.

    La flecha suelta de al lado (``↓12``) se leia como el sube/baja de una
    clasificacion de liga, no como una salida al ply que explica el numero.
    Ahora hay UN solo elemento: los plies viven dentro de la etiqueta tras un
    interpunto y todo el recuadro se pincha.
    """

    def _tree_with_a_backed_child(self):
        root = ingest.get_or_create_position(logic.start_fen())
        child = ingest.get_or_create_position(
            logic.apply_move(root.fen, 'd2d4'))
        child.eval_cp = 20
        child.backed_eval = 260
        child.backed_plies = 12
        child.nodes_invested = 1_000
        child.backed_nodes = 5_000
        child.save()
        Edge.objects.create(parent=root, move_uci='d2d4', child=child)
        return root, child

    def _chip(self, body, tag, text):
        """La etiqueta entera que envuelve ese texto, sea ``a`` o ``span``.

        Se busca por el texto y no por el orden de los atributos: el
        minificador los reordena y un assert atado a ese orden solo prueba
        que el minificador no ha cambiado.
        """
        found = re.search(rf'<{tag}\b[^>]*>{re.escape(text)}</{tag}>', body)
        self.assertIsNotNone(found, f'no hay <{tag}> con {text!r}')
        return found.group(0)

    def test_a_backed_row_is_itself_the_jump_link_to_the_child(self):
        root, child = self._tree_with_a_backed_child()

        body = self.client.get(
            f'/atomicdb/explore/{root.key}/').content.decode()

        chip = self._chip(body, 'a', 'backed ·12')
        self.assertIn('class="backed-mark"', chip)
        self.assertIn(
            f'href="/atomicdb/backed-source/{child.key}/?play=d2d4"', chip)
        self.assertIn(
            'aria-label="backed ·12 — jump to the source of this value, '
            '12 ply(s) below"',
            chip)
        self.assertIn('click to jump to the position this value comes from',
                      chip)
        # La flecha suelta de al lado ya no existe.
        self.assertNotIn('backed-jump', body)
        self.assertNotIn('↓12', body)

    def test_a_row_without_backing_paints_no_chip_and_no_jump(self):
        root = ingest.get_or_create_position(logic.start_fen())
        child = ingest.get_or_create_position(
            logic.apply_move(root.fen, 'd2d4'))
        # Con busqueda propia: sin respaldo no hay salto que ofrecer, y el
        # valor es de motor, asi que tampoco entra la marca de caminado.
        child.eval_cp = 20
        child.nodes_invested = 128_000_000
        child.save()
        Edge.objects.create(parent=root, move_uci='d2d4', child=child)

        body = self.client.get(
            f'/atomicdb/explore/{root.key}/').content.decode()

        self.assertNotIn('backed-jump', body)
        self.assertNotIn('backed-mark', body)
        self.assertNotIn('backed-source', body)

    def test_the_header_chip_is_itself_the_jump(self):
        _root, child = self._tree_with_a_backed_child()

        body = self.client.get(
            f'/atomicdb/explore/{child.key}/').content.decode()

        chip = self._chip(body, 'a', 'backed ·12')
        self.assertIn('class="backed-mark"', chip)
        self.assertIn(
            f'href="/atomicdb/backed-source/{child.key}/?play=d2d4"', chip)
        self.assertIn(
            'aria-label="backed ·12 — jump to the source of this value, '
            '12 ply(s) below"',
            chip)
        self.assertNotIn('backed-jump', body)

    def test_a_chip_without_a_jump_stays_a_mute_span(self):
        """Sin URL no se inventa un salto: el chip vuelve a ser etiqueta.

        Los plies se quedan dentro igual — lo que desaparece es el enlace,
        con su promesa de click en el tooltip.
        """
        root, _child = self._tree_with_a_backed_child()

        with mock.patch.object(views, '_backed_source_url', return_value=''):
            body = self.client.get(
                f'/atomicdb/explore/{root.key}/').content.decode()

        chip = self._chip(body, 'span', 'backed ·12')
        self.assertIn('class="backed-mark"', chip)
        self.assertNotIn('href', chip)
        self.assertNotIn('click to jump', chip)
        self.assertNotIn('backed-source', body)
        self.assertIsNone(re.search(r'<a\b[^>]*backed-mark', body))

    def test_painting_the_link_costs_no_queries(self):
        """El paseo se paga al pinchar; la tabla solo escribe una cadena."""
        alias = settings.ATOMICDB_DATABASE_ALIAS
        _root, child = self._tree_with_a_backed_child()

        with self.assertNumQueries(0, using=alias):
            url = views._backed_source_url(child.key, ['d2d4'])

        self.assertEqual(
            url, f'/atomicdb/backed-source/{child.key}/?play=d2d4')
