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
        with self.assertNumQueries(8, using=settings.ATOMICDB_DATABASE_ALIAS):
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
        self.assertEqual(views._own_search(388, 0), 'own search: +388')

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

    def test_seed_lost_race_keeps_the_childs_own_analysis(self):
        """La siembra del padre llega TARDE: el analisis propio del hijo ya
        aterrizo en otro consumer y es mas fiable que la linea MultiPV."""
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        child = Edge.objects.filter(parent=root).order_by('move_uci') \
                            .first().child
        Position.objects.filter(key=child.key).update(eval_cp=731)
        self.assertIsNone(child.eval_cp)   # objeto rancio, como en la carrera
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
        """The raw-lines showcase never downgrades; knowledge always flows.

        A visitor requests MultiPV 5 and reads five lines; a later FILL
        pass with searchmoves re-touches the position with two lines at 8M
        and used to replace the whole snapshot — 275 of 400 revisited
        positions carried the clobber when Wolfram reported seeing one
        line where five were promised.  Narrow-and-shallower keeps its
        knowledge (eval and best_move still update) but not the showcase;
        DEEPER passes still replace it even when narrower, because that is
        the deliberate depth-over-width revisit policy.
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
        self.assertEqual(pos.eval_cp, 90)             # conocimiento fluye

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
            pos, [55, 45, 35, 25, 15]), 128_000_000)
        pos.refresh_from_db()
        # Un pase fresco igual de ancho sustituye tambien al anterior.
        self.assertEqual(len(pos.last_analysis), 5)
        self.assertFalse(any(l.get('prior_pass') for l in pos.last_analysis))

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
