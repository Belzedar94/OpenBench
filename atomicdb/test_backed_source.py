"""Salto al ORIGEN de un valor respaldado.

Una fila puede decir -1166 "backed a 12 plies" y no decir DONDE nacio ese
numero.  Bajarlo a mano son doce clicks por la espina de soporte, y el que
mira una cabecera raramente quiere doce clicks: quiere el ply que la explica.
Esto camina esa espina de una vez y redirige alli, con la historia del
visitante extendida para que aterrice dentro de SU partida.

And the jump behind a ``borrowed`` row, which walks no spine: it goes to the
position whose search wrote the row's number (BorrowedSourceTests onwards).
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


def _borrowed_jump(client, key, uci, **params):
    return client.get(f'/atomicdb/borrowed-source/{key}/{uci}/', params)


def _line(uci, white_pov, prior=False):
    """A stored engine line: White-POV, like every stored number."""
    line = {'move': uci, 'eval_cp': white_pov, 'pv': [uci]}
    if prior:
        line['prior_pass'] = True
    return line


def _borrowed_chip(body):
    """The ``borrowed`` chip of a page, whole, link or mute span, or None."""
    found = re.search(r'<(a|span)\b[^>]*>borrowed</\1>', body)
    return found.group(0) if found else None


def _knights_transposition():
    """1.Nc3 Nf6 2.Nf3 and 1.Nf3 Nf6 2.Nc3, real positions from the start.

    Returns ``(here, there, child)``: the positions after 1.Nc3 Nf6 and after
    1.Nf3 Nf6, and the one both reach.  ``here`` searched and named another
    move; the line of ``there`` for 2.Nc3 wrote the +45 the child holds.
    """
    start = ingest.get_or_create_position(logic.start_fen())

    def play(position, uci):
        child = ingest.get_or_create_position(
            logic.apply_move(position.fen, uci))
        Edge.objects.update_or_create(parent=position, move_uci=uci,
                                      defaults={'child': child})
        return child

    here = play(play(start, 'b1c3'), 'g8f6')
    there = play(play(start, 'g1f3'), 'g8f6')
    child = play(here, 'g1f3')
    assert play(there, 'b1c3').key == child.key, 'no transposition'
    Position.objects.filter(key=here.key).update(
        eval_cp=30, nodes_invested=128_000_000,
        last_analysis=[_line('e2e4', 30)])
    Position.objects.filter(key=there.key).update(
        eval_cp=45, nodes_invested=64_000_000,
        last_analysis=[_line('b1c3', 45)])
    Position.objects.filter(key=child.key).update(eval_cp=45)
    return tuple(Position.objects.get(key=position.key)
                 for position in (here, there, child))


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

    def test_a_missing_edge_lands_where_the_walk_got_and_says_so(self):
        """Un respaldo puede ser mas viejo que el grafo que lo sostenia.

        Aterrizar ahi callando era el "doesn't always work correctly" que
        reporto Wolfram: el chip promete la fuente del numero y el visitante
        acaba en un nodo cualquiera sin nada que le diga que la cadena se
        rompio.  Se aterriza igual y se dice, con la misma marca con la que se
        dice una repeticion.
        """
        # La jugada de la arista es LEGAL en el fen sintetico: el aterrizaje
        # pinta tambien su migaja de pan, y esa replica la linea de verdad.
        top = _pos('MT', 'w', eval_cp=5, backed_eval=400, backed_move='e1e2')
        mid = _pos('MM', 'b', eval_cp=9, backed_eval=400, backed_move='h7h8q')
        _edge(top, mid, 'e1e2')

        response = _jump(self.client, top.key)

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{mid.key}/?incomplete=1')
        body = self.client.get(response['Location']).content.decode()
        self.assertIn('>chain breaks here</span>', body)

    def test_a_walk_that_reaches_the_origin_says_nothing_extra(self):
        here = _pos('QUIET', 'w', eval_cp=512, backed_eval=512)

        response = _jump(self.client, here.key)

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{here.key}/')
        body = self.client.get(response['Location']).content.decode()
        self.assertNotIn('chain breaks here', body)

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

    def test_the_ply_cap_bounds_the_descent_and_the_landing_says_it(self):
        """Running out of the cap is not reaching an origin either: the
        landing is labelled like any other chain that did not end."""
        chain = [_pos(f'L{i}', 'w' if i % 2 == 0 else 'b',
                      eval_cp=i, backed_eval=9_000, backed_move='a1a1')
                 for i in range(14)]
        for upper, lower in zip(chain, chain[1:]):
            _edge(upper, lower, 'a1a1')

        with mock.patch.object(views, 'BACKED_SPINE_MAX_PLIES', 8):
            response = _jump(self.client, chain[0].key)

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{chain[8].key}/?incomplete=1')

    def test_a_spine_longer_than_one_ascent_reaches_its_origin(self):
        """The main line of 27-Oct-2026: 63 to 66 plies of pointers below the
        start position, more than one ascent climbs.  The chip of the start
        position landed short of the origin and called the chain broken."""
        length = ingest.BACKED_MAX_PLIES + 6
        chain = [_pos(f'M{i}', 'w' if i % 2 == 0 else 'b',
                      eval_cp=i, backed_eval=9_000, backed_move='a1a1')
                 for i in range(length - 1)]
        chain.append(_pos('M-origin', 'w' if length % 2 else 'b',
                          eval_cp=9_000, backed_eval=9_000))
        for upper, lower in zip(chain, chain[1:]):
            _edge(upper, lower, 'a1a1')

        response = _jump(self.client, chain[0].key)

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{chain[-1].key}/')

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
            origin, walked, outcome = views._walk_backed_spine(a)

        self.assertEqual(origin.key, c.key)
        self.assertEqual(walked, ['a1a1', 'a1a1'])
        self.assertEqual(outcome, '')

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
            'aria-label="jump to the position this value comes from, '
            '12 plies below"',
            chip)
        self.assertIn('Click to jump to the position this value comes from',
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
            'aria-label="jump to the position this value comes from, '
            '12 plies below"',
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


class BorrowedSourceTests(TestCase):
    """The jump behind a ``borrowed`` row: to the position whose search wrote
    its number.

    The position a move leads to keeps one number, the last one any search
    wrote into it, and a pass writes into every child its lines name exactly
    the value of the line (see ingest._seed_child_eval).  A borrowed number
    was written that way by ANOTHER parent of the same position, so the jump
    reads the child's other parents and lands on the one whose stored lines
    give it.  Asked for on Discord about the live chip: "use it as a button
    to get to the position that gave that walked value".
    """

    def _transposition(self):
        """``here`` and ``there`` both reach ``child``, by e1e2 and e1d2.

        ``here`` searched and never named e1e2; ``there`` named e1d2 at the
        +250 the child holds.  The moves are legal on the synthetic FEN, so a
        landing page can replay its breadcrumb.
        """
        here = _pos('BW-HERE', 'w', expanded=True, eval_cp=40,
                    nodes_invested=128_000_000,
                    last_analysis=[_line('e1f1', 40)])
        there = _pos('BW-THERE', 'w', expanded=True, eval_cp=250,
                     nodes_invested=64_000_000,
                     last_analysis=[_line('e1d2', 250), _line('e1f2', 120)])
        child = _pos('BW-CHILD', 'b', eval_cp=250)
        _edge(here, child, 'e1e2')
        _edge(there, child, 'e1d2')
        return here, there, child

    @staticmethod
    def _borrowed(parent, uci):
        """The row's own verdict, so that every scene here is the chip's."""
        parent = Position.objects.get(key=parent.key)
        return next(row for row in views._child_moves(parent)
                    if row['uci'] == uci)['borrowed']

    @staticmethod
    def _state():
        """Everything the jump could write, as plain rows."""
        return (list(Position.objects.order_by('key').values_list(
                    'key', 'updated', 'eval_cp', 'backed_eval',
                    'last_analysis')),
                list(Edge.objects.order_by('id').values_list(
                    'parent_id', 'move_uci', 'child_id')))

    def test_the_jump_lands_on_the_parent_whose_line_wrote_the_number(self):
        here, there, _child = self._transposition()
        self.assertTrue(self._borrowed(here, 'e1e2'))

        response = _borrowed_jump(self.client, here.key, 'e1e2')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{there.key}/')

    def test_the_number_is_compared_white_pov_as_it_is_stored(self):
        """The shape of a4295de0, Black to move: the row shows -1182 for a
        child that holds +1182, the value the writer's line stored.  A line
        that gave its move -1182 wrote some other number, however deep the
        search behind it."""
        here = _pos('BW-B-HERE', 'b', expanded=True, eval_cp=1353,
                    nodes_invested=128_000_000,
                    last_analysis=[_line('e8f7', 1353)])
        child = _pos('BW-B-CHILD', 'w', eval_cp=1182)
        writer = _pos('BW-B-WRITER', 'b', expanded=True,
                      nodes_invested=8_000_000,
                      last_analysis=[_line('e8d7', 1182)])
        mirror = _pos('BW-B-MIRROR', 'b', expanded=True,
                      nodes_invested=512_000_000,
                      last_analysis=[_line('e8d8', -1182)])
        _edge(here, child, 'e8e7')
        _edge(writer, child, 'e8d7')
        _edge(mirror, child, 'e8d8')
        self.assertTrue(self._borrowed(here, 'e8e7'))

        response = _borrowed_jump(self.client, here.key, 'e8e7')

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{writer.key}/')

    def test_with_several_writers_the_deepest_search_wins(self):
        """The deepest is neither the first edge nor the last, so the order
        the database returns them in cannot pick it by accident."""
        here, _there, child = self._transposition()      # 64M behind it
        deeper = _pos('BW-DEEPER', 'w', expanded=True,
                      nodes_invested=512_000_000,
                      last_analysis=[_line('e1f2', 250)])
        shallower = _pos('BW-SHALLOWER', 'w', expanded=True,
                         nodes_invested=8_000_000,
                         last_analysis=[_line('e1f1', 250)])
        _edge(deeper, child, 'e1f2')
        _edge(shallower, child, 'e1f1')

        response = _borrowed_jump(self.client, here.key, 'e1e2')

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{deeper.key}/')

    def test_a_line_of_the_earlier_wider_pass_counts_too(self):
        """A pass seeds every child its lines name, and the page prints the
        earlier wider pass as the position's own (see
        views._own_line_values)."""
        here, there, _child = self._transposition()
        Position.objects.filter(key=there.key).update(last_analysis=[
            _line('e1f2', 300), _line('e1d2', 250, prior=True)])

        response = _borrowed_jump(self.client, here.key, 'e1e2')

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{there.key}/')

    def test_with_no_writer_it_lands_on_the_child_and_says_so(self):
        """``there`` wrote +180 into the child first, and a pass of ``here``
        wrote the +250 over it later, whose lines are not kept.  No line
        anywhere gives the number, so the jump lands where the number lives,
        labelled as a chain that did not reach its origin."""
        here, there, child = self._transposition()
        Position.objects.filter(key=there.key).update(
            last_analysis=[_line('e1d2', 180)])
        self.assertTrue(self._borrowed(here, 'e1e2'))

        response = _borrowed_jump(self.client, here.key, 'e1e2')

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{child.key}/?incomplete=1')
        body = self.client.get(response['Location']).content.decode()
        self.assertIn('>chain breaks here</span>', body)

    def test_a_move_searched_since_lands_on_its_own_position(self):
        """A page painted before the move was searched still offers the chip.
        The number is the child's own now, so the child is where it comes
        from, even with a line elsewhere that gives the same number, and the
        jump lands there with nothing to explain."""
        here, _there, child = self._transposition()
        Position.objects.filter(key=child.key).update(
            nodes_invested=128_000_000)
        self.assertFalse(self._borrowed(here, 'e1e2'))

        response = _borrowed_jump(self.client, here.key, 'e1e2')

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{child.key}/')

    def test_an_unknown_position_or_a_move_with_no_edge_is_a_404(self):
        here, _there, _child = self._transposition()

        self.assertEqual(
            _borrowed_jump(self.client, _key('nobody'), 'e1e2').status_code,
            404)
        # e1f1 is legal here and its line names it, but no edge carries it.
        self.assertEqual(
            _borrowed_jump(self.client, here.key, 'e1f1').status_code, 404)

    def test_the_jump_never_writes_and_is_not_cacheable(self):
        here, _there, _child = self._transposition()
        # And a move nobody else reaches, so both landings are exercised.
        _edge(here, _pos('BW-COLD', 'b', eval_cp=999), 'e1f2')
        before = self._state()

        found = _borrowed_jump(self.client, here.key, 'e1e2')
        cold = _borrowed_jump(self.client, here.key, 'e1f2')

        self.assertEqual(self._state(), before)
        self.assertNotIn('incomplete', found['Location'])
        self.assertIn('incomplete=1', cold['Location'])
        for response in (found, cold):
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response['Cache-Control'], 'no-store')
        # A GET and nothing else.
        self.assertEqual(self.client.post(
            f'/atomicdb/borrowed-source/{here.key}/e1e2/').status_code, 405)

    def test_finding_the_writer_costs_two_statements_whatever_the_parents(
            self):
        """The edge, then all the other parents with their lines in one go:
        not one statement per parent."""
        alias = settings.ATOMICDB_DATABASE_ALIAS
        here, there, child = self._transposition()

        with self.assertNumQueries(2, using=alias):
            few = _borrowed_jump(self.client, here.key, 'e1e2')
        for index in range(6):
            _edge(_pos(f'BW-MORE-{index}', 'w', expanded=True,
                       nodes_invested=1_000_000,
                       last_analysis=[_line('e1f1', 100 + index)]),
                  child, 'e1f1')
        with self.assertNumQueries(2, using=alias):
            many = _borrowed_jump(self.client, here.key, 'e1e2')

        self.assertEqual(few['Location'], f'/atomicdb/explore/{there.key}/')
        self.assertEqual(many['Location'], few['Location'])


class BorrowedSourceRouteTests(TestCase):
    """The writer reaches the child by another move order, so the visitor's
    route does not lead there and that landing is bare.  A landing on the
    child itself keeps the route, extended by the move, as ``goto`` would.
    """

    def test_the_writer_is_reached_without_the_route(self):
        here, there, _child = _knights_transposition()

        response = _borrowed_jump(self.client, here.key, 'g1f3',
                                  play='b1c3,g8f6')

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{there.key}/')

    def test_landing_on_the_child_extends_the_route_by_the_move(self):
        alias = settings.ATOMICDB_DATABASE_ALIAS
        here, there, child = _knights_transposition()
        # ``there`` says +20 of 2.Nc3 now: nobody's line gives the +45.
        Position.objects.filter(key=there.key).update(
            last_analysis=[_line('b1c3', 20)])

        # The route costs the one statement that checks its positions exist.
        with self.assertNumQueries(3, using=alias):
            response = _borrowed_jump(self.client, here.key, 'g1f3',
                                      play='b1c3,g8f6')
        landed = self.client.get(response['Location'])

        self.assertEqual(
            response['Location'],
            f'/atomicdb/explore/{child.key}/?play=b1c3,g8f6,g1f3&incomplete=1')
        self.assertEqual(landed.status_code, 200)
        self.assertEqual(landed.context['active_play'],
                         ['b1c3', 'g8f6', 'g1f3'])
        self.assertContains(landed, '>chain breaks here</span>')

    def test_a_route_that_does_not_reach_this_position_is_dropped(self):
        here, there, child = _knights_transposition()
        Position.objects.filter(key=there.key).update(
            last_analysis=[_line('b1c3', 20)])

        # 1.Nf3 Nf6 is a real route, to ``there``.
        response = _borrowed_jump(self.client, here.key, 'g1f3',
                                  play='g1f3,g8f6')

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{child.key}/?incomplete=1')


class BorrowedJumpRenderTests(TestCase):
    """The ``borrowed`` chip IS the jump, like the backed chip: one element,
    a link where the row offers the jump and a mute span where it does not.
    """

    def test_the_chip_is_the_jump_with_the_visitors_route(self):
        here, there, _child = _knights_transposition()

        body = self.client.get(f'/atomicdb/explore/{here.key}/',
                               {'play': 'b1c3,g8f6'}).content.decode()

        chip = _borrowed_chip(body)
        self.assertIsNotNone(chip)
        self.assertTrue(chip.startswith('<a'))
        self.assertIn('class="backed-mark light eval-borrowed"', chip)
        jump = f'/atomicdb/borrowed-source/{here.key}/g1f3/?play=b1c3,g8f6'
        self.assertIn(f'href="{jump}"', chip)
        self.assertIn('aria-label="jump to the position whose search wrote '
                      'this number"', chip)
        self.assertIn('click to open it', chip)
        # And it goes where the chip says.
        self.assertEqual(self.client.get(jump)['Location'],
                         f'/atomicdb/explore/{there.key}/')

    def test_a_chip_without_a_jump_stays_a_mute_span(self):
        here, _there, _child = _knights_transposition()

        with mock.patch.object(views, '_borrowed_source_url',
                               return_value=''):
            body = self.client.get(
                f'/atomicdb/explore/{here.key}/').content.decode()

        chip = _borrowed_chip(body)
        self.assertIsNotNone(chip)
        self.assertTrue(chip.startswith('<span'))
        self.assertNotIn('href', chip)
        self.assertNotIn('click to open it', chip)
        self.assertNotIn('borrowed-source', body)

    def test_painting_the_link_costs_no_queries(self):
        """Finding the writer is paid on the click; the table only writes a
        string, with the visitor's route to this position as ``goto`` has
        it: an empty one travels too, and none is a bare link."""
        alias = settings.ATOMICDB_DATABASE_ALIAS
        key = _key('BW-PAINT')

        with self.assertNumQueries(0, using=alias):
            urls = [views._borrowed_source_url(key, 'g1f3', ucis)
                    for ucis in (['b1c3', 'g8f6'], [], None)]

        self.assertEqual(urls, [
            f'/atomicdb/borrowed-source/{key}/g1f3/?play=b1c3,g8f6',
            f'/atomicdb/borrowed-source/{key}/g1f3/?play=',
            f'/atomicdb/borrowed-source/{key}/g1f3/'])
