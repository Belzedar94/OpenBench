"""Valor RESPALDADO (backed eval): negamax del subarbol, sus guardas y su
propagacion por el DAG.

Bug de comunidad que motiva esto: una posicion mostraba su eval PUNTUAL en la
cabecera (+696) mientras su propia tabla de jugadas ya ensenaba una hija a
+794 salida del analisis del nieto, y el padre pintaba la arista con el mismo
+696 obsoleto.  AtomicDB propagaba cierres (minimax de status) pero no evals
heuristicas refinadas.
"""

import hashlib
import re

from django.conf import settings
from django.test import Client

from . import ingest, logic, views
from .models import AnalysisTask, DBEvent, Edge, Position
from .testing import TestCase


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    """Posicion sintetica: solo importan el turno y los campos del backup."""
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci=None):
    uci = uci or f'a1a{1 + Edge.objects.filter(parent=parent).count()}'
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


class BackedChainTests(TestCase):

    def test_three_level_chain_propagates_upward(self):
        # P(w) -> C(b) -> G(w) -> H(b) -> L(w, +300).  Cobertura completa en
        # todos los niveles: el valor sube entero y arrastra la distancia.
        p = _pos('P', 'w', expanded=True)
        c = _pos('C', 'b', expanded=True)
        g = _pos('G', 'w', expanded=True)
        h = _pos('H', 'b', expanded=True)
        leaf = _pos('L', 'w', eval_cp=300, nodes_invested=1_000)
        _edge(p, c, 'd2d4')
        _edge(c, g)
        _edge(g, h)
        _edge(h, leaf)

        ingest.backup_backed_evals([h.key])

        for node, plies in ((h, 1), (g, 2), (c, 3), (p, 4)):
            node.refresh_from_db()
            self.assertEqual(node.backed_eval, 300, node.key)
            self.assertEqual(node.backed_plies, plies, node.key)
        p.refresh_from_db()
        self.assertEqual(p.backed_move, 'd2d4')

    def test_transposition_updates_every_parent(self):
        # Un DAG tiene varios padres: la transposicion recibe el refinamiento
        # por las dos rutas, no solo por la primera que encuentre el ascenso.
        a = _pos('A', 'w', expanded=True)
        b = _pos('B', 'w', expanded=True)
        shared = _pos('S', 'b', expanded=True)
        leaf = _pos('SL', 'w', eval_cp=-450, nodes_invested=1_000)
        _edge(a, shared, 'g1f3')
        _edge(b, shared, 'b1c3')
        _edge(shared, leaf)

        ingest.backup_backed_evals([shared.key])

        for node in (a, b, shared):
            node.refresh_from_db()
            self.assertEqual(node.backed_eval, -450, node.key)

    def test_transposition_cycle_terminates(self):
        # 1.Nf3 Nf6 2.Ng1 Ng8 ES la posicion inicial con los contadores fuera:
        # el ascenso tiene que cerrar el ciclo sin colgarse.
        a = _pos('CA', 'w', expanded=True)
        b = _pos('CB', 'b', expanded=True)
        leaf = _pos('CL', 'w', eval_cp=120, nodes_invested=1_000)
        _edge(a, b)
        _edge(b, a)          # ciclo
        _edge(b, leaf)

        changed = ingest.backup_backed_evals([b.key])

        self.assertGreater(changed, 0)
        b.refresh_from_db()
        self.assertIsNotNone(b.backed_eval)

    def test_ply_guard_is_recorded(self):
        chain = [_pos(f'D{i}', 'w' if i % 2 == 0 else 'b', expanded=True)
                 for i in range(8)]
        leaf = _pos('DL', 'w', eval_cp=700, nodes_invested=1_000)
        for upper, lower in zip(chain, chain[1:]):
            _edge(upper, lower)
        _edge(chain[-1], leaf)

        ingest.backup_backed_evals([chain[-1].key], max_plies=3)

        chain[0].refresh_from_db()
        self.assertIsNone(chain[0].backed_eval)   # el tope corto el ascenso
        self.assertTrue(DBEvent.objects.filter(kind='BACKED_GUARD').exists())


class BackedGuardTests(TestCase):
    """Cobertura parcial: el valor solo se mueve a favor del que mueve."""

    def test_or_node_never_drops_below_its_own_eval(self):
        # Blancas al turno (nodo OR).  Un unico hijo mirado, y malo: los 30
        # sin mirar pueden esconder algo mejor, asi que no baja.
        p = _pos('ORP', 'w', expanded=True, eval_cp=200, nodes_invested=1_000)
        bad = _pos('ORBAD', 'b', eval_cp=-500, nodes_invested=8_000)
        blind = _pos('ORBLIND', 'b')
        _edge(p, bad)
        _edge(p, blind)

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, 200)
        self.assertIsNone(p.backed_move)

    def test_or_node_rises_with_a_deep_child(self):
        p = _pos('ORP2', 'w', expanded=True, eval_cp=200, nodes_invested=1_000)
        good = _pos('ORGOOD', 'b', eval_cp=760, nodes_invested=8_000)
        blind = _pos('ORBLIND2', 'b')
        _edge(p, good, 'd2d4')
        _edge(p, blind)

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, 760)
        self.assertEqual(p.backed_move, 'd2d4')

    def test_and_node_ignores_an_optimistic_partial_min(self):
        # Negras al turno (nodo AND).  El min sobre cobertura parcial es
        # sistematicamente optimista para el atacante: queda vetado.
        p = _pos('ANDP', 'b', expanded=True, eval_cp=-200, nodes_invested=1_000)
        good_for_white = _pos('ANDW', 'w', eval_cp=500, nodes_invested=8_000)
        blind = _pos('ANDBLIND', 'w')
        _edge(p, good_for_white)
        _edge(p, blind)

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, -200)

    def test_and_node_accepts_a_proven_better_defence(self):
        p = _pos('ANDP2', 'b', expanded=True, eval_cp=-200,
                 nodes_invested=1_000)
        better = _pos('ANDD', 'w', eval_cp=-820, nodes_invested=8_000)
        blind = _pos('ANDBLIND2', 'w')
        _edge(p, better, 'e7e5')
        _edge(p, blind)

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, -820)
        self.assertEqual(p.backed_move, 'e7e5')

    def test_complete_coverage_replaces_the_point_eval_either_way(self):
        # Lista de jugadas COMPLETA: el negamax es el minimax de verdad y
        # puede bajar el valor del nodo OR sin pedir permiso.
        p = _pos('FULL', 'w', expanded=True, eval_cp=400, nodes_invested=1_000)
        for name, value in (('FA', -50), ('FB', -900)):
            _edge(p, _pos(name, 'b', eval_cp=value, nodes_invested=10))

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, -50)

    def test_shallow_child_cannot_override_a_deeper_search(self):
        # 128M no pisa lo que respaldo un 10B en el mismo subarbol.
        p = _pos('QP', 'w', expanded=True, eval_cp=100,
                 nodes_invested=10_000_000_000)
        shallow = _pos('QSHALLOW', 'b', eval_cp=900,
                       nodes_invested=128_000_000)
        blind = _pos('QBLIND', 'b')
        _edge(p, shallow)
        _edge(p, blind)

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, 100)

        shallow.nodes_invested = 10_000_000_000
        shallow.save(update_fields=['nodes_invested'])
        ingest.backup_backed_evals([p.key])
        p.refresh_from_db()
        self.assertEqual(p.backed_eval, 900)

    def test_solved_children_enter_with_their_truth_value(self):
        # Un hijo probado entra con mate/tablas, nunca con una eval.
        p = _pos('TP', 'w', expanded=True, eval_cp=-300, nodes_invested=1_000)
        draw = _pos('TDRAW', 'b', status='DRAW', closure='TERMINAL')
        _edge(p, draw, 'a2a3')
        _edge(p, _pos('TBAD', 'b', eval_cp=-800, nodes_invested=1_000))

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, 0)
        self.assertEqual(p.backed_move, 'a2a3')

    def test_backed_never_contradicts_a_proven_status(self):
        won = _pos('WON', 'w', status='WHITE_WIN', closure='MATE_PV',
                   best_move='h5f7', expanded=True)
        _edge(won, _pos('WONKID', 'b', eval_cp=-4_000, nodes_invested=1_000))

        ingest.backup_backed_evals([won.key])

        won.refresh_from_db()
        self.assertEqual(won.backed_eval, 10_000)
        self.assertEqual(won.backed_nodes, ingest.PROVEN_QUALITY)


class BackedPropagationCostTests(TestCase):

    def _wide(self, name, width):
        parent = _pos(name, 'w', expanded=True)
        for i in range(width):
            _edge(parent, _pos(f'{name}-{i}', 'b', eval_cp=-10 * i,
                               nodes_invested=1_000))
        return parent

    def test_query_count_does_not_grow_with_the_width_of_the_tree(self):
        narrow = self._wide('NARROW', 3)
        wide = self._wide('WIDE', 60)

        # Un nivel entero cuesta lo mismo con tres hijos que con sesenta:
        # posiciones + aristas/hijos + bulk_update + padres.  Se cuenta
        # sobre la conexion del ALIAS: con la base partida las queries no
        # pasan por default.
        alias = settings.ATOMICDB_DATABASE_ALIAS
        with self.assertNumQueries(4, using=alias):
            ingest.backup_backed_evals([narrow.key])
        with self.assertNumQueries(4, using=alias):
            ingest.backup_backed_evals([wide.key])

    def test_a_second_pass_over_a_wide_level_costs_two_reads(self):
        wide = self._wide('WIDE3', 60)
        ingest.backup_backed_evals([wide.key])

        # Nada cambio: se lee el nivel y se para, sin escribir ni subir.
        with self.assertNumQueries(2, using=settings.ATOMICDB_DATABASE_ALIAS):
            ingest.backup_backed_evals([wide.key])

    def test_one_level_of_ancestors_costs_a_bounded_number_of_queries(self):
        grand = _pos('GP', 'w', expanded=True)
        parent = self._wide('WIDE2', 60)
        _edge(grand, parent)

        # Dos niveles a cuatro sentencias cada uno, sin importar la anchura.
        # La novena es el paseo de repeticion del segundo nivel: ahi el hijo ya
        # tiene ``backed_move`` y hay espina que mirar.  Cuesta UNA sentencia
        # por ply caminado y la comparten todos los hijos del nivel, asi que
        # tampoco crece con la anchura — que es lo que este test vigila.
        with self.assertNumQueries(9, using=settings.ATOMICDB_DATABASE_ALIAS):
            ingest.backup_backed_evals([parent.key])
        grand.refresh_from_db()
        self.assertIsNotNone(grand.backed_eval)

    def test_sub_epsilon_drift_does_not_climb(self):
        grand = _pos('EG', 'w', expanded=True)
        parent = _pos('EP', 'b', expanded=True)
        leaf = _pos('EL', 'w', eval_cp=-300, nodes_invested=1_000)
        _edge(grand, parent)
        _edge(parent, leaf)
        ingest.backup_backed_evals([parent.key])
        grand.refresh_from_db()
        self.assertEqual(grand.backed_eval, -300)

        leaf.eval_cp = -304          # ruido por debajo del umbral
        leaf.save(update_fields=['eval_cp'])
        ingest.backup_backed_evals([parent.key])
        grand.refresh_from_db()
        self.assertEqual(grand.backed_eval, -300)   # no subio

        leaf.eval_cp = -420          # cambio de verdad
        leaf.save(update_fields=['eval_cp'])
        ingest.backup_backed_evals([parent.key])
        grand.refresh_from_db()
        self.assertEqual(grand.backed_eval, -420)


class BackedDisplayTests(TestCase):

    def test_move_rows_show_the_best_current_knowledge(self):
        parent = _pos('MP', 'w', expanded=True)
        backed_child = _pos('MB', 'b', eval_cp=-100, backed_eval=-794,
                            backed_plies=2, nodes_invested=1_000)
        point_child = _pos('MC', 'b', eval_cp=-50, nodes_invested=1_000)
        solved_child = _pos('MS', 'b', status='WHITE_WIN', closure='MATE_PV',
                            mate_in=3, eval_cp=120)
        blank_child = _pos('MN', 'b')
        _edge(parent, backed_child, 'd7d6')
        _edge(parent, point_child, 'e2e4')
        _edge(parent, solved_child, 'h5f7')
        _edge(parent, blank_child, 'a2a3')

        rows = {m['uci']: m for m in views._child_moves(parent)}

        self.assertEqual(rows['d7d6']['score'], -794)   # backed, no puntual
        self.assertEqual(rows['d7d6']['point'], -100)
        self.assertTrue(rows['d7d6']['backed'])
        self.assertEqual(rows['e2e4']['score'], -50)    # caida limpia
        self.assertFalse(rows['e2e4']['backed'])
        self.assertEqual(rows['h5f7']['score'], 10_000)  # probado manda
        self.assertIsNone(rows['a2a3']['score'])

    def test_a_backed_row_reports_the_raw_search_behind_it(self):
        """El numero pintado es del subarbol; el tooltip dice de donde sale."""
        parent = _pos('OWP', 'w', expanded=True)
        child = _pos('OWC', 'b', eval_cp=388, backed_eval=794,
                     backed_plies=2, nodes_invested=128_000_000)
        _edge(parent, child, 'd7d6')

        row = next(m for m in views._child_moves(parent))

        self.assertTrue(row['backed'])
        self.assertEqual(row['own_search'], 'own search: +388 @ 128.0M nodes')

    def test_the_tooltip_is_in_the_mover_s_perspective_like_the_cell(self):
        """Un solo flip por ply pintado: la celda y su tooltip, o ninguno."""
        parent = _pos('OWBP', 'b', expanded=True)
        child = _pos('OWBC', 'w', eval_cp=-388, backed_eval=-794,
                     backed_plies=1, nodes_invested=2_400_000_000)
        _edge(parent, child, 'd7d6')

        row = next(m for m in views._child_moves(parent))

        self.assertEqual(row['score'], 794)
        self.assertEqual(row['own_search'], 'own search: +388 @ 2.40B nodes')

    def test_a_purely_inherited_row_says_so_instead_of_inventing_a_zero(self):
        parent = _pos('OWIP', 'w', expanded=True)
        child = _pos('OWIC', 'b', backed_eval=-500, backed_plies=3)
        _edge(parent, child, 'd7d6')

        row = next(m for m in views._child_moves(parent))

        self.assertTrue(row['backed'])
        self.assertEqual(row['own_search'], 'no direct search yet')

    def test_an_eval_with_no_nodes_behind_it_claims_no_support(self):
        # Cero nodos no es una busqueda propia flaca: es que no la hay. El
        # numero esta sembrado de la linea del padre y la frase lo dice — y
        # dice ademas que la siembra se repite pase tras pase, que es lo que
        # explica una tabla con mas filas sembradas que lineas tiene un pase.
        self.assertEqual(views._own_search(388, 0),
                         '+388 from an engine line (passes seed their top '
                         'moves over time), no direct search yet')

    def test_the_page_paints_the_tooltip_on_the_badge(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).first().child
        Position.objects.filter(key=child.key).update(
            eval_cp=388, backed_eval=794, backed_plies=2,
            nodes_invested=128_000_000)

        body = Client().get(f'/atomicdb/explore/{root.key}/').content.decode()

        self.assertIn('backed-mark', body)
        self.assertIn('own search: +388 @ 128.0M nodes', body)

    def test_black_to_move_flips_the_sign_exactly_once(self):
        parent = _pos('BM', 'b', expanded=True)
        _edge(parent, _pos('BMK', 'w', eval_cp=-794, backed_eval=-794,
                           backed_plies=1), 'd7d6')

        rows = {m['uci']: m for m in views._child_moves(parent)}

        self.assertEqual(rows['d7d6']['score'], 794)

    def test_explore_header_uses_the_backed_value(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        Position.objects.filter(key=pos.key).update(
            eval_cp=30, backed_eval=94, backed_plies=3, expanded=True)

        page = Client().get(f'/atomicdb/explore/{pos.key}/')

        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertIn('best line +94cp', body)
        self.assertIn('backed', body)
        self.assertIn('+30cp', body)          # la puntual sigue visible

    def test_explore_header_falls_back_to_the_point_eval(self):
        # Con busqueda propia detras: sin respaldo no hay chip que pintar, y
        # el numero es del motor mirando AQUI (§ views._walked_value), asi que
        # tampoco lleva la marca de caminado.
        pos = ingest.get_or_create_position(logic.start_fen())
        Position.objects.filter(key=pos.key).update(
            eval_cp=30, nodes_invested=128_000_000, expanded=True)

        body = Client().get(f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn('best line +30cp', body)
        self.assertNotIn('backed-mark', body)

    def test_api_query_reports_both_values(self):
        pos = ingest.get_or_create_position(logic.start_fen())
        Position.objects.filter(key=pos.key).update(
            eval_cp=30, backed_eval=94, backed_plies=3, expanded=True)

        payload = Client().get('/atomicdb/api/query',
                               {'fen': pos.fen}).json()

        self.assertEqual(payload['score'], 94)
        self.assertEqual(payload['point'], 30)
        self.assertEqual(payload['backed_plies'], 3)


class WalkedOrderTests(TestCase):
    """Un valor CAMINADO no puede encabezar la tabla (§ views._child_moves).

    Reporte de comunidad: "after d4 e6 c3 displayed as top move despite the
    fact that there is no engine analysis in that position and just someone
    did Be7 move and analyzed it".  Y no era una tonteria academica: tras
    Qh4-e4-c2 las blancas pierden material, o sea que el primer puesto lo
    ocupaba una jugada que nadie habia mirado y que ademas es mala.
    """

    def test_a_walked_value_does_not_outrank_what_the_engine_looked_at(self):
        parent = _pos('WKP', 'w', expanded=True)
        # +9000 caminados: espina respaldada, cero busqueda en ninguna parte.
        walked = _pos('WKW', 'b', backed_eval=9_000, backed_plies=4)
        # +20 de verdad, con 128M detras.  Modesto, pero comprobado.
        searched = _pos('WKS', 'b', eval_cp=20, nodes_invested=128_000_000)
        _edge(parent, walked, 'e7e6')
        _edge(parent, searched, 'd7d5')

        rows = views._child_moves(parent)

        self.assertEqual([row['uci'] for row in rows], ['d7d5', 'e7e6'])
        self.assertEqual([row['tier'] for row in rows], [3, 2])
        # Y el numero caminado sigue en su fila, entero y con su chip: baja de
        # puesto, no se esconde.
        self.assertTrue(rows[1]['backed_light'])
        self.assertEqual(rows[1]['score'], 9_000)

    def test_a_walked_value_still_beats_a_row_nobody_has_touched(self):
        """Caminado es poco, pero es mas que nada: sigue encima de lo vacio."""
        parent = _pos('WBP', 'w', expanded=True)
        walked = _pos('WBW', 'b', backed_eval=-400, backed_plies=2)
        blank = _pos('WBN', 'b')
        _edge(parent, walked, 'e7e6')
        _edge(parent, blank, 'd7d5')

        rows = views._child_moves(parent)

        self.assertEqual([row['uci'] for row in rows], ['e7e6', 'd7d5'])
        self.assertEqual([row['tier'] for row in rows], [2, 1])

    def test_the_proven_ends_of_the_table_do_not_move(self):
        parent = _pos('WEP', 'w', expanded=True)
        won = _pos('WEW', 'b', status='WHITE_WIN', closure='TERMINAL')
        lost = _pos('WEL', 'b', status='BLACK_WIN', closure='TERMINAL')
        walked = _pos('WEK', 'b', backed_eval=9_000, backed_plies=4)
        searched = _pos('WES', 'b', eval_cp=20, nodes_invested=128_000_000)
        blank = _pos('WEN', 'b')
        _edge(parent, won, 'h5f7')
        _edge(parent, lost, 'a7a6')
        _edge(parent, walked, 'e7e6')
        _edge(parent, searched, 'd7d5')
        _edge(parent, blank, 'b7b6')

        rows = views._child_moves(parent)

        self.assertEqual([row['uci'] for row in rows],
                         ['h5f7', 'd7d5', 'e7e6', 'b7b6', 'a7a6'])
        self.assertEqual([row['tier'] for row in rows], [4, 3, 2, 1, 0])

    def test_two_walked_rows_keep_their_own_order(self):
        """Dentro del tier manda el score de siempre: el tier no lo aplana."""
        parent = _pos('WWP', 'w', expanded=True)
        better = _pos('WWB', 'b', backed_eval=300, backed_plies=2)
        worse = _pos('WWO', 'b', backed_eval=-300, backed_plies=6)
        _edge(parent, worse, 'e7e6')
        _edge(parent, better, 'd7d5')

        rows = views._child_moves(parent)

        self.assertEqual([row['uci'] for row in rows], ['d7d5', 'e7e6'])
        self.assertEqual([row['tier'] for row in rows], [2, 2])
        self.assertEqual([row['score'] for row in rows], [300, -300])


class BackedIngestTests(TestCase):
    """La propagacion viaja DENTRO del flujo de submit, sobre el arbol real."""

    def _lines(self, pos, values):
        return [{'move': edge.move_uci, 'eval_cp': value,
                 'pv': [edge.move_uci]}
                for edge, value in zip(
                    Edge.objects.filter(parent=pos).order_by('move_uci'),
                    values)]

    def test_analysis_of_a_grandchild_reaches_the_root(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).order_by('move_uci') \
                            .first().child
        ingest.expand(child)
        grand = Edge.objects.filter(parent=child).order_by('move_uci') \
                            .first().child
        ingest.expand(grand)

        ingest.ingest_analysis(child.key, self._lines(
            child, [-60, -10, -10, -10, -10]), 1_000)
        summary = ingest.ingest_analysis(grand.key, self._lines(
            grand, [-900] * 5), 5_000_000)

        self.assertIn('backed_evals', summary)
        child.refresh_from_db()
        root.refresh_from_db()
        self.assertEqual(grand.__class__.objects.get(key=grand.key)
                         .backed_eval, -900)
        self.assertEqual(child.backed_eval, -900)   # negras eligen la mejor
        self.assertEqual(root.backed_eval, -900)    # y sube hasta la raiz

    def test_seed_lost_race_keeps_the_childs_own_analysis(self):
        """La siembra del padre llega TARDE: el analisis propio del hijo ya
        aterrizo en otro consumer y es mas fiable que la linea MultiPV.

        Un analisis propio llega SIEMPRE con sus nodos — eval_cp y
        nodes_invested se guardan en la misma llamada — y son los nodos los que
        lo hacen intocable para la siembra.
        """
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).order_by('move_uci') \
                            .first().child
        Position.objects.filter(key=child.key).update(
            eval_cp=731, nodes_invested=8_000_000)
        self.assertIsNone(child.eval_cp)   # objeto rancio, como en la carrera
        self.assertFalse(ingest._seed_child_eval(child, 865))
        child.refresh_from_db()
        self.assertEqual(child.eval_cp, 731)

    def test_seed_never_overwrites_a_value_the_subtree_backs(self):
        """El respaldo con peso de busqueda debajo tambien es conocimiento de
        motor sobre este hijo, y la linea del padre no lo arbitra."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).order_by('move_uci') \
                            .first().child
        Position.objects.filter(key=child.key).update(
            eval_cp=731, backed_eval=742, backed_plies=4,
            backed_nodes=128_000_000)

        self.assertFalse(ingest._seed_child_eval(child, 865))

        child.refresh_from_db()
        self.assertEqual(child.eval_cp, 731)

    def test_seed_fresh_child_still_takes_the_parents_line_eval(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).order_by('move_uci') \
                            .first().child
        self.assertTrue(ingest._seed_child_eval(child, 123))
        child.refresh_from_db()
        self.assertEqual(child.eval_cp, 123)

    def test_a_seed_refreshes_an_older_seed_because_neither_is_a_search(self):
        """Una siembra no es una medida: la sustituye la reclamacion nueva."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).order_by('move_uci') \
                            .first().child
        self.assertTrue(ingest._seed_child_eval(child, 123))

        self.assertTrue(ingest._seed_child_eval(child, -456))

        child.refresh_from_db()
        self.assertEqual(child.eval_cp, -456)

    def test_a_new_pass_refreshes_the_seed_the_old_one_left_behind(self):
        """La fila y la linea vigente dejan de contar dos numeros distintos.

        Reporte de comunidad, literal: "walked lines are not ordered correctly
        together with actual lines".  Con el filtro viejo — sembrar solo sobre
        eval_cp NULL — el hijo se quedaba con el PRIMER numero que se sembrara
        jamas: el escaparate de arriba pasaba a decir 500 y la fila de abajo
        seguia diciendo 50, que es ademas por donde ordenaba.
        """
        pos = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(pos)
        top = Edge.objects.filter(parent=pos).order_by('move_uci').first()

        ingest.ingest_analysis(pos.key, self._lines(
            pos, [50, 40, 30, 20, 10]), 128_000_000)
        seeded = Position.objects.get(key=top.child_id)
        self.assertEqual(seeded.eval_cp, 50)

        ingest.ingest_analysis(pos.key, self._lines(
            pos, [500, 400, 300, 200, 100]), 512_000_000)

        seeded.refresh_from_db()
        self.assertEqual(seeded.eval_cp, 500)
        pos.refresh_from_db()
        self.assertEqual(pos.last_analysis[0]['eval_cp'], 500)

    def test_the_arrow_follows_the_table_not_the_stale_search(self):
        """Board arrow and moves table must move TOGETHER.

        The arrow used pos.best_move (the last own search's pick) while the
        table ranks by best knowledge including backed values.  When a
        backed child overtook, the table promoted it and the arrow kept
        pointing at the old move — the board contradicted its own column
        (reported 29-jul).  One source of truth: the arrow is the table's
        top row.
        """
        pos = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(pos)
        Position.objects.filter(key=pos.key).update(
            eval_cp=100, expanded=True, best_move='a2a3')
        edge = Edge.objects.get(parent=pos, move_uci='e2e3')
        Position.objects.filter(key=edge.child_id).update(
            backed_eval=425, backed_nodes=1, backed_plies=1)

        body = Client().get(f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn('data-best-move="e2e3"', body)

    def test_a_narrow_shallow_pass_does_not_clobber_the_wide_snapshot(self):
        """The raw-lines showcase never downgrades, in width or in depth.

        A visitor requests MultiPV 5 and reads five lines; a later FILL
        pass with searchmoves re-touches the position with two lines at 8M
        and used to replace the whole snapshot — 275 of 400 revisited
        positions carried the clobber when Wolfram reported seeing one
        line where five were promised.  A shallower pass now keeps its
        accounting but arbitrates nothing: neither the showcase nor the
        number.  DEEPER passes still replace it even when narrower, because
        that is the deliberate depth-over-width revisit policy, and at
        EQUAL depth width decides exactly as before.
        """
        pos = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(pos)

        ingest.ingest_analysis(pos.key, self._lines(
            pos, [50, 40, 30, 20, 10]), 128_000_000)
        pos.refresh_from_db()
        self.assertEqual(len(pos.last_analysis), 5)

        ingest.ingest_analysis(pos.key, self._lines(pos, [90, 80]), 8_000_000)
        pos.refresh_from_db()
        self.assertEqual(len(pos.last_analysis), 5)   # escaparate intacto
        self.assertEqual(pos.eval_cp, 50)             # y el numero, tambien

        ingest.ingest_analysis(pos.key, self._lines(pos, [70, 60]),
                               512_000_000)
        pos.refresh_from_db()
        current = [l for l in pos.last_analysis if not l.get('prior_pass')]
        prior = [l for l in pos.last_analysis if l.get('prior_pass')]
        # Mas profundo SI pisa el pase vigente...
        self.assertEqual(len(current), 2)
        # ...pero las lineas del pase ancho no se tiran: viajan como pase
        # anterior (Wolfram: "it should probably preserve lines from lower
        # depths then").
        self.assertEqual(len(prior), 5)

        ingest.ingest_analysis(pos.key, self._lines(
            pos, [55, 45, 35, 25, 15]), 512_000_000)
        pos.refresh_from_db()
        # A la MISMA hondura, un pase fresco igual de ancho sustituye tambien
        # al anterior.
        self.assertEqual(len(pos.last_analysis), 5)
        self.assertFalse(any(l.get('prior_pass') for l in pos.last_analysis))

    def test_a_shallow_repeat_never_undoes_a_deeper_search(self):
        """640M invertidos y mandando el pase de 128M: computo tirado.

        La otra mitad de lo que reporto la comunidad (30-jul).  Un click de
        Verify PV en el padre encargaba el suelo de peticion sobre un nodo que
        ya tenia 512M, y ese pase — igual de ancho, mucho mas superficial — se
        llevaba por delante escaparate, eval y best_move: "moreover, after
        that deeper analysis was overwritten by more shallow one".  Ahora el
        trabajo se cobra igual (visitas y nodos siguen sumando) pero no
        arbitra nada.
        """
        pos = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(pos)
        deep_move = Edge.objects.filter(parent=pos) \
                        .order_by('move_uci')[4].move_uci

        ingest.ingest_analysis(pos.key, self._lines(
            pos, [10, 20, 30, 40, 50]), 512_000_000)
        ingest.ingest_analysis(pos.key, self._lines(
            pos, [90, 80, 70, 60, 55]), 128_000_000)
        pos.refresh_from_db()

        self.assertEqual([l['eval_cp'] for l in pos.last_analysis],
                         [10, 20, 30, 40, 50])
        self.assertEqual(pos.eval_cp, 50)
        self.assertEqual(pos.best_move, deep_move)
        # El pase superficial no manda, pero se hizo: 512M + 128M donados.
        self.assertEqual(pos.nodes_invested, 640_000_000)
        self.assertEqual(pos.visits, 2)

    def test_at_the_same_budget_the_width_rule_still_decides(self):
        """Empatar hondura le devuelve la palabra a la anchura, como antes."""
        pos = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(pos)

        ingest.ingest_analysis(pos.key, self._lines(
            pos, [50, 40, 30, 20, 10]), 128_000_000)
        ingest.ingest_analysis(pos.key, self._lines(
            pos, [60, 45, 35, 25, 15]), 128_000_000)
        pos.refresh_from_db()

        self.assertEqual([l['eval_cp'] for l in pos.last_analysis],
                         [60, 45, 35, 25, 15])
        self.assertEqual(pos.eval_cp, 60)

        # Y a esa misma hondura uno mas ESTRECHO sigue sin tocar el
        # escaparate, con su conocimiento entrando igual: la regla de anchura
        # no se ha movido de donde estaba.
        ingest.ingest_analysis(pos.key, self._lines(pos, [95, 85]),
                               128_000_000)
        pos.refresh_from_db()
        current = [l for l in pos.last_analysis if not l.get('prior_pass')]
        self.assertEqual(len(current), 5)
        self.assertEqual(pos.eval_cp, 95)

    def test_navigating_onto_a_terminal_closes_the_parent_at_once(self):
        """A goto can DISCOVER a mate — the cascade must fire right there.

        Bxe7 exploding the king is WHITE_WIN TERMINAL the instant the edge
        exists, but goto created position and edge without any cascade, so
        the parent kept saying UNSOLVED +302 with a mate-in-one sitting in
        its own moves table until some unrelated analysis touched the
        family.  The community read the delay as a bug, because it was one.
        """
        parent = ingest.get_or_create_position('k7/pp6/1P6/8/8/8/8/K7 w - - 0 1')
        self.assertEqual(parent.status, 'UNKNOWN')

        response = Client().get(f'/atomicdb/goto/{parent.key}/b6a7/')

        self.assertEqual(response.status_code, 302)
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'WHITE_WIN')

    def test_a_walked_mate_line_loses_proof_weight_at_partial_nodes(self):
        """A proof's authority ends where the unproven alternatives begin.

        A visitor WALKED a line to a terminal mate without requesting a
        single analysis.  The terminal is genuinely proven, but every walked
        node above it has no eval of its own, so the directional guard had
        no anchor and the mate-band value climbed the whole chain carrying
        PROVEN quality — the explorer painted 9994 BACKED over territory no
        engine ever looked at (Wolfram, 28-jul).  The value may climb (it is
        the best knowledge), but past a partial-coverage node it carries
        only that node's own search support: the first evaluated ancestor
        blocks it, and the convergence purchase sends the ENGINE down the
        line the human explored.
        """
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        Position.objects.filter(key=root.key).update(
            eval_cp=120, nodes_invested=128_000_000)
        walked = Edge.objects.filter(parent=root).order_by('move_uci') \
                             .first().child
        # The walked node: an edge exists, but no eval and no expansion —
        # exactly what /goto/ navigation leaves behind.
        deep_fen = logic.apply_move(walked.fen,
                                    logic.legal_moves(walked.fen)[0])
        deep = ingest.get_or_create_position(deep_fen)
        Edge.objects.get_or_create(
            parent=walked, move_uci=logic.legal_moves(walked.fen)[0],
            defaults={'child': deep})
        Position.objects.filter(key=deep.key).update(
            status='WHITE_WIN', closure='TERMINAL')

        ingest.backup_backed_evals([deep.key, walked.key, root.key])

        walked.refresh_from_db()
        root.refresh_from_db()
        # The walked node itself may honestly carry the value...
        self.assertIsNotNone(walked.backed_eval)
        # ...but WITHOUT proof weight,
        self.assertLess(walked.backed_nodes, ingest.PROVEN_QUALITY)
        # so the evaluated ancestor keeps its own knowledge,
        self.assertEqual(root.backed_eval, 120)
        self.assertIsNone(root.backed_move)
        # and the discrepancy buys the walked line an engine analysis.
        self.assertTrue(AnalysisTask.objects.filter(
            position=walked, source=AnalysisTask.Source.FILL).exists())

    def test_a_new_analysis_of_the_child_retests_the_blocked_parent(self):
        """The purchase's delivery route, end to end through submit.

        A blocked parent stores a self-echo and the echo never changes, so
        propagation from below never reached it again: the convergence
        purchase analysed the child, the child's backed value stayed put
        (value unchanged, no propagation), and the parent stayed blocked on
        stale quality forever.  The apply now seeds the analysed position's
        PARENTS too, because a fresh analysis changes ``nodes_invested``
        even when it changes nothing else — and that is precisely what the
        parents' quality guards are waiting on.
        """
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        Position.objects.filter(key=parent.key).update(
            eval_cp=369, nodes_invested=2_000_000_000)
        edge = Edge.objects.filter(parent=parent).order_by('move_uci').first()
        child = edge.child
        ingest.expand(child)

        # A shallow pass over the child: informative, mover-favourable at
        # the parent (416 > 369 for White), but 8M against 2B — blocked.
        ingest.ingest_analysis(child.key, self._lines(
            child, [416] * 5), 8_000_000)
        parent.refresh_from_db()
        self.assertEqual(parent.backed_eval, 369)   # the self-echo
        self.assertIsNone(parent.backed_move)

        # The convergence purchase lands: same values, real depth.
        ingest.ingest_analysis(child.key, self._lines(
            child, [416] * 5), 2_000_000_000)

        parent.refresh_from_db()
        self.assertEqual(parent.backed_eval, 416)
        self.assertEqual(parent.backed_move, edge.move_uci)

    def test_partially_expanded_lineage_still_backs_up(self):
        """El hueco real que dejaba la cascada heredada.

        ``backup_cascade`` se niega, a proposito, a hacer minimax de evals
        sobre una expansion PARCIAL (``expanded=False``, tipica de una rama
        abierta navegando con /goto/): ahi el valor deja de subir y el padre
        se queda pintando una eval vieja.  El respaldo si atraviesa ese nodo,
        con las guardas de cobertura parcial puestas.
        """
        root = ingest.get_or_create_position(logic.start_fen())
        first = logic.legal_moves(root.fen)[0]
        middle = ingest.get_or_create_position(
            logic.apply_move(root.fen, first))
        Edge.objects.get_or_create(parent=root, move_uci=first,
                                   defaults={'child': middle})
        second = logic.legal_moves(middle.fen)[0]
        deep = ingest.get_or_create_position(
            logic.apply_move(middle.fen, second))
        Edge.objects.get_or_create(parent=middle, move_uci=second,
                                   defaults={'child': deep})
        Position.objects.filter(key=deep.key).update(
            eval_cp=-880, nodes_invested=10_000_000)

        self.assertFalse(middle.expanded)
        ingest.backup_cascade([deep.key])
        middle.refresh_from_db()
        self.assertIsNone(middle.eval_cp)          # la cascada no baja aqui

        ingest.backup_backed_evals([middle.key])

        middle.refresh_from_db()
        root.refresh_from_db()
        self.assertEqual(middle.backed_eval, -880)
        self.assertEqual(root.backed_eval, -880)


class BackedRelicTests(TestCase):
    """Reliquias: respaldos escritos con guardas VIEJAS no se recomputan
    solos.  El caso Wolfram (30-jul): un ancla con eval propio conservaba el
    9994 que una espina caminada le subio antes de la guarda direccional; la
    regla vigente calcula el eval propio, pero nada habia vuelto a tocar el
    nodo.  ``recascade_backed`` existe para ese barrido."""

    def _relic(self):
        # Ancla negra con eval propio 671 y UNA sola respuesta mirada que
        # reclama mate blanco: la guarda direccional vigente devuelve 671,
        # pero la fila guarda el 9994 reliquia de la era pre-guarda.
        anchor = _pos('RELIC-A', 'b', eval_cp=671, nodes_invested=0,
                      backed_eval=9994, backed_move='c8g4', backed_plies=6,
                      backed_nodes=0)
        spine = _pos('RELIC-S', 'w')
        _edge(anchor, spine, 'c8g4')
        leaf = _pos('RELIC-L', 'b', eval_cp=9994, nodes_invested=128_000_000)
        _edge(spine, leaf, 'c2c3')
        return anchor, spine

    def test_current_rules_would_not_regenerate_the_relic(self):
        # Sembrar la ESPINA: la hoja no cambia su propio respaldo (no tiene
        # hijos) y sin cambio no hay ascenso; la espina si cambia (adopta el
        # 9994 favorable a su bando) y el ascenso recomputa el ancla, donde
        # la guarda direccional vigente devuelve el eval propio.
        anchor, spine = self._relic()

        ingest.backup_backed_evals([spine.key])

        anchor.refresh_from_db()
        self.assertEqual(anchor.backed_eval, 671)   # la guarda manda hoy
        self.assertIsNone(anchor.backed_move)

    def test_recascade_command_sweeps_the_relic(self):
        from django.core.management import call_command
        anchor, _leaf = self._relic()

        call_command('recascade_backed', '--chunk', '10')

        anchor.refresh_from_db()
        self.assertEqual(anchor.backed_eval, 671)


class BackedRepetitionTests(TestCase):
    """Un hijo que vuelve a su PROPIO padre por su espina aporta TABLAS.

    Dos reportes de la comunidad, un solo defecto.  "Move repetition
    creates illusion that line is solved as +9, while it is not the case ...
    due to the loop it uses circular reasoning to justify this eval and is
    unsound".  Y el ciclo envenenado: "if engine erroneously evaluated
    something as +4 because it missed a tactics (and in fact it is a draw),
    and made a loop from +4, and you already know a refutation by deeper
    analysis, it is not possible to 'cleanse' that loop in the database by
    backpropagating evaluation from outside this loop".

    PERSPECTIVA: como todo el modulo, estos valores son de las BLANCAS.  El
    "+4" de un reporte escrito desde el bando que mueve es -400 aqui cuando el
    que mueve son las negras, y por eso cada fixture dice de quien es el turno.
    """

    def _ring(self, length, value=640):
        """Anillo: un ``top`` y una cadena de ``length`` que vuelve a el.

        Todos los eslabones se prestan el mismo valor por ``backed_move``, que
        es lo unico que el paseo mira.
        """
        top = _pos('RING-TOP', 'b', expanded=True)
        chain = [_pos(f'RING-{i}', 'w' if i % 2 == 0 else 'b', eval_cp=i,
                      backed_eval=value, backed_move='a1a1',
                      backed_plies=length - i, backed_nodes=1_000)
                 for i in range(length)]
        _edge(top, chain[0], 'a1a1')
        for upper, lower in zip(chain, chain[1:]):
            _edge(upper, lower, 'a1a1')
        _edge(chain[-1], top, 'a1a1')     # el anillo se cierra
        return top, chain

    def test_a_two_move_loop_cannot_justify_its_own_value(self):
        # A <-> B y nada mas: cada uno se presta el +9 del otro y ninguno lo
        # sostiene.  Sin alternativa, lo que vale el nodo es lo que vale poder
        # repetir: tablas.
        a = _pos('LOOP-A', 'w', expanded=True, backed_eval=900,
                 backed_move='g1f3', backed_plies=4, backed_nodes=1_000)
        b = _pos('LOOP-B', 'b', expanded=True, backed_eval=900,
                 backed_move='g8f6', backed_plies=3, backed_nodes=1_000)
        _edge(a, b, 'g1f3')
        _edge(b, a, 'g8f6')          # 1.Nf3 Nf6 2.Ng1 Ng8 otra vez aqui

        ingest.backup_backed_evals([a.key])

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.backed_eval, 0)
        self.assertEqual(a.backed_move, 'g1f3')
        self.assertEqual(b.backed_eval, 0)
        # Y es PUNTO FIJO: las tablas nacen en la arista, asi que la distancia
        # no crece pasada tras pasada y ``recascade_backed`` puede terminar.
        self.assertEqual(a.backed_plies, 1)
        self.assertEqual(ingest.backup_backed_evals([a.key]), 0)

    def test_a_real_alternative_beats_the_loop_that_outranked_it(self):
        # El caso de math_god: el ciclo publicaba +9.30 y con eso tapaba una
        # jugada que de verdad vale +9.14.  Con la repeticion valorada a 0 la
        # de verdad gana el max, y ``backed_move`` la senala.
        x = _pos('ALT-X', 'w', expanded=True)
        loop = _pos('ALT-LOOP', 'b', expanded=True, backed_eval=930,
                    backed_move='e8d7', backed_plies=3, backed_nodes=5_000)
        real = _pos('ALT-REAL', 'b', eval_cp=914, nodes_invested=9_000)
        _edge(x, loop, 'd1d2')
        _edge(x, real, 'd7e8')
        _edge(loop, x, 'e8d7')       # el brazo del ciclo vuelve a X

        ingest.backup_backed_evals([x.key])

        x.refresh_from_db()
        self.assertEqual(x.backed_eval, 914)
        self.assertEqual(x.backed_move, 'd7e8')

    def test_an_outside_refutation_finally_reaches_a_poisoned_loop(self):
        # Mueven NEGRAS, asi que el "+4" del ciclo es -400 en esta
        # perspectiva.  La refutacion honesta viene de una busqueda mucho mas
        # honda FUERA del ciclo y dice -200: peor para las negras que la
        # ilusion, y ahora alcanzable, que es justo lo que el reporte decia
        # que era imposible.
        x = _pos('POISON-X', 'b', expanded=True)
        loop = _pos('POISON-LOOP', 'w', expanded=True, backed_eval=-400,
                    backed_move='b1c3', backed_plies=2, backed_nodes=8_000)
        refutation = _pos('POISON-REF', 'w', eval_cp=-200,
                          nodes_invested=128_000_000)
        _edge(x, loop, 'b8c6')
        _edge(x, refutation, 'e7e5')
        _edge(loop, x, 'b1c3')

        ingest.backup_backed_evals([x.key])

        x.refresh_from_db()
        self.assertEqual(x.backed_eval, -200)
        self.assertEqual(x.backed_move, 'e7e5')

    def test_a_transposition_that_does_not_come_back_keeps_its_value(self):
        # Rombo A->B->D, A->C->D.  La transposicion es REAL y no pasa por A:
        # nadie se justifica a si mismo y nadie pierde su valor.  Es el falso
        # positivo que la regla no puede permitirse, porque transponer es lo
        # que este grafo hace todo el rato.
        a = _pos('DIA-A', 'w', expanded=True)
        b = _pos('DIA-B', 'b', expanded=True)
        c = _pos('DIA-C', 'b', expanded=True)
        d = _pos('DIA-D', 'w', eval_cp=-260, nodes_invested=1_000)
        _edge(a, b, 'g1f3')
        _edge(a, c, 'b1c3')
        _edge(b, d, 'b1c3')
        _edge(c, d, 'g1f3')

        ingest.backup_backed_evals([b.key, c.key])

        for node in (b, c, a):
            node.refresh_from_db()
            self.assertEqual(node.backed_eval, -260, node.key)
        self.assertIn(a.backed_move, ('g1f3', 'b1c3'))

    def test_a_cycle_beyond_the_guard_is_not_claimed(self):
        # El tope del paseo es cordura, no verdad: una espina mas larga que el
        # sale SIN reclamar repeticion y con su valor intacto.  Equivocarse
        # hacia "no hay ciclo" deja las cosas como estaban; hacia "hay ciclo"
        # inventaria unas tablas que nadie ha demostrado.
        top, _chain = self._ring(ingest.BACKED_CYCLE_MAX_PLIES + 4)

        children = ingest._backed_children_by_parent([top.key])
        ingest._draw_cycling_children(children, ingest._SpineCache())

        self.assertEqual([child.value for child in children[top.key]], [640])

    def test_the_spine_walk_reports_the_repetition_to_the_explorer(self):
        # Mitad de PANTALLA: la espina de un ciclo no tiene origen que ensenar.
        # El paseo se corta, lo dice, y el destino lo etiqueta en vez de dejar
        # al visitante creyendo que aterrizo en la fuente del numero.
        # Las jugadas son LEGALES en el fen sintetico: esta pagina pinta
        # tambien la migaja de pan, y esa si replica la linea de verdad.
        a = _pos('SPINE-A', 'w', eval_cp=1, backed_eval=120,
                 backed_move='e1e2')
        b = _pos('SPINE-B', 'b', eval_cp=2, backed_eval=120,
                 backed_move='e8e7')
        _edge(a, b, 'e1e2')
        _edge(b, a, 'e8e7')

        origin, walked, repetition = views._walk_backed_spine(a)

        self.assertEqual(origin.key, b.key)
        self.assertEqual(walked, ['e1e2'])
        self.assertTrue(repetition)

        jump = self.client.get(f'/atomicdb/backed-source/{a.key}/')
        self.assertEqual(jump['Location'],
                         f'/atomicdb/explore/{b.key}/?repetition=1')
        body = self.client.get(jump['Location']).content.decode()
        self.assertIn('>repetition</span>', body)

    def test_a_spine_that_does_not_repeat_is_left_alone(self):
        a = _pos('OPEN-A', 'w', eval_cp=1, backed_eval=500,
                 backed_move='e1e2')
        b = _pos('OPEN-B', 'b', eval_cp=2, backed_eval=500,
                 backed_move='e8e7')
        origin_node = _pos('OPEN-C', 'w', eval_cp=500, backed_eval=500)
        _edge(a, b, 'e1e2')
        _edge(b, origin_node, 'e8e7')

        node, walked, repetition = views._walk_backed_spine(a)

        self.assertEqual(node.key, origin_node.key)
        self.assertEqual(walked, ['e1e2', 'e8e7'])
        self.assertFalse(repetition)

        jump = self.client.get(f'/atomicdb/backed-source/{a.key}/')
        self.assertEqual(jump['Location'],
                         f'/atomicdb/explore/{origin_node.key}/')
        body = self.client.get(jump['Location']).content.decode()
        self.assertNotIn('>repetition</span>', body)


class WalkedChipTests(TestCase):
    """El chip de la tabla distingue respaldo VERIFICADO de valor de linea
    caminada (backed_nodes == 0): 'backed' contra 'walked'."""

    def test_weightless_backed_renders_as_walked(self):
        parent = _pos('CHIP-P', 'w', expanded=True)
        walked = _pos('CHIP-W', 'b', backed_eval=9994, backed_plies=5,
                      backed_nodes=0)
        verified = _pos('CHIP-V', 'b', eval_cp=100, backed_eval=250,
                        backed_plies=2, backed_nodes=5_000_000)
        _edge(parent, walked, 'g2g4')
        _edge(parent, verified, 'e2e4')

        body = Client().get(f'/atomicdb/explore/{parent.key}/') \
                       .content.decode()

        # El chip ES el enlace al origen, y lleva sus plies dentro.
        self.assertIn('walked ·5</a>', body)
        self.assertIn('backed ·2</a>', body)
        self.assertIsNotNone(
            re.search(r'<a\b[^>]*class="backed-mark light"[^>]*>walked ·5</a>',
                      body))
        self.assertIsNotNone(
            re.search(r'<a\b[^>]*class="backed-mark"[^>]*>backed ·2</a>',
                      body))
        self.assertIn(f'href="/atomicdb/backed-source/{walked.key}/"', body)
        self.assertIn(f'href="/atomicdb/backed-source/{verified.key}/"', body)


class SeededValueTests(TestCase):
    """La eval SEMBRADA tambien es caminada (§ views._walked_value).

    Reporte de comunidad, literal: "walking eval still not fixed ... long line
    without any request analysis ... and pretends to have accurate eval ... not
    even marked as walked".  El respaldo caminado ya bajaba de tier y llevaba
    su chip; esta otra no llevaba nada, porque el numero no viene de un
    respaldo sino de la linea MultiPV del padre, que el ingest siembra en el
    hijo al aterrizar un pase (§ ingest._seed_child_eval).  Para quien mira la
    pagina es la misma afirmacion sin comprobar, asi que se dice igual.
    """

    def test_a_seeded_child_does_not_outrank_what_the_engine_looked_at(self):
        parent = _pos('SEED-P', 'w', expanded=True)
        # +900 sembrados de la linea del padre: nadie ha buscado nunca aqui.
        seeded = _pos('SEED-S', 'b', eval_cp=900)
        # +20 con 128M detras.  Modesto, pero mirado.
        searched = _pos('SEED-E', 'b', eval_cp=20, nodes_invested=128_000_000)
        _edge(parent, seeded, 'e7e6')
        _edge(parent, searched, 'd7d5')

        rows = views._child_moves(parent)

        self.assertEqual([row['uci'] for row in rows], ['d7d5', 'e7e6'])
        self.assertEqual([row['tier'] for row in rows], [3, 2])
        # La fila sembrada se ensena entera: baja de puesto y se marca, no se
        # esconde.  Y no lleva chip de respaldo, porque respaldo no tiene.
        self.assertTrue(rows[1]['walked'])
        self.assertFalse(rows[1]['backed'])
        self.assertFalse(rows[1]['backed_light'])
        self.assertEqual(rows[1]['score'], 900)
        # La fila buscada no se toca: sigue siendo conocimiento de motor.
        self.assertFalse(rows[0]['walked'])

    def test_backing_with_search_weight_is_still_engine_knowledge(self):
        """El predicado mira NODOS, no de donde vino el numero: un respaldo con
        busqueda de verdad debajo sigue en el tier de motor aunque el nodo no
        tenga busqueda propia."""
        parent = _pos('SEED-WP', 'w', expanded=True)
        heavy = _pos('SEED-WB', 'b', eval_cp=10, backed_eval=400,
                     backed_plies=3, backed_nodes=5_000_000)
        _edge(parent, heavy, 'e7e6')

        row = next(iter(views._child_moves(parent)))

        self.assertEqual(row['tier'], 3)
        self.assertFalse(row['walked'])
        self.assertTrue(row['backed'])
        self.assertFalse(row['backed_light'])

    def test_the_seeded_row_wears_the_walked_mark(self):
        parent = _pos('SEED-RP', 'w', expanded=True)
        _edge(parent, _pos('SEED-RS', 'b', eval_cp=900), 'e7e6')

        body = Client().get(f'/atomicdb/explore/{parent.key}/') \
                       .content.decode()

        self.assertIn('>walked</span>', body)
        self.assertIn('nothing has searched this position itself yet', body)

    def test_the_header_of_a_seeded_position_says_walked(self):
        pos = _pos('SEED-HS', 'w', eval_cp=180, expanded=True)

        body = Client().get(f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn('best line +180cp', body)
        self.assertIn('>walked</span>', body)

    def test_the_header_of_a_searched_position_carries_no_mark(self):
        pos = _pos('SEED-HE', 'w', eval_cp=180, nodes_invested=128_000_000,
                   expanded=True)

        body = Client().get(f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn('best line +180cp', body)
        # ``walked`` a secas sale tambien del JS del boton de frontera; lo que
        # no puede existir es el chip.
        self.assertNotIn('>walked</span>', body)
        self.assertNotIn('backed-mark', body)
