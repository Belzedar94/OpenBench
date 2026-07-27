"""Valor RESPALDADO (backed eval): negamax del subarbol, sus guardas y su
propagacion por el DAG.

Bug de comunidad que motiva esto: una posicion mostraba su eval PUNTUAL en la
cabecera (+696) mientras su propia tabla de jugadas ya ensenaba una hija a
+794 salida del analisis del nieto, y el padre pintaba la arista con el mismo
+696 obsoleto.  AtomicDB propagaba cierres (minimax de status) pero no evals
heuristicas refinadas.
"""

import hashlib

from django.test import Client

from . import ingest, logic, views
from .models import DBEvent, Edge, Position
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
        # posiciones + aristas/hijos + bulk_update + padres.
        with self.assertNumQueries(4):
            ingest.backup_backed_evals([narrow.key])
        with self.assertNumQueries(4):
            ingest.backup_backed_evals([wide.key])

    def test_a_second_pass_over_a_wide_level_costs_two_reads(self):
        wide = self._wide('WIDE3', 60)
        ingest.backup_backed_evals([wide.key])

        # Nada cambio: se lee el nivel y se para, sin escribir ni subir.
        with self.assertNumQueries(2):
            ingest.backup_backed_evals([wide.key])

    def test_one_level_of_ancestors_costs_a_bounded_number_of_queries(self):
        grand = _pos('GP', 'w', expanded=True)
        parent = self._wide('WIDE2', 60)
        _edge(grand, parent)

        # Dos niveles a cuatro sentencias cada uno, sin importar la anchura.
        with self.assertNumQueries(8):
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
        pos = ingest.get_or_create_position(logic.start_fen())
        Position.objects.filter(key=pos.key).update(eval_cp=30, expanded=True)

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
