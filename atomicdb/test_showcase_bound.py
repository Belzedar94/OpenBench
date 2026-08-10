"""La cota del escaparate multipv en el respaldo, y la guarda que dejo de vetar.

El caso de comunidad que motiva ambos (8-ago-2026): una cabecera decia +1051
con 2B detras mientras el unico hijo re-analizado decia +589; los otros 33
movimientos, sin mirar, mantenian la cobertura parcial y las guardas dejaban
el +1051 de titular indefinidamente.  La resolucion del propietario, con el
racional de Wolfram: si el motor corrio multipv, la peor linea del escaparate
ACOTA a todo lo que quedo fuera ("all other moves are not better than 5 in
multipv"), y en propagacion no hay razon para el pesimismo porque el solver
tiene su PN/DN aparte.
"""

import hashlib
from types import SimpleNamespace

from . import ingest
from .models import AnalysisTask, Edge, Position
from .testing import TestCase


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci=None):
    uci = uci or f'a1a{1 + Edge.objects.filter(parent=parent).count()}'
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _showcase(*evals):
    """Un escaparate vigente sintetico, en el orden en que lo emite el motor."""
    return [{'move': f'm{i}', 'eval_cp': ev, 'pv': [f'm{i}']}
            for i, ev in enumerate(evals)]


class ShowcaseBoundTests(TestCase):

    def test_bound_completes_coverage_and_lets_value_drop(self):
        # El caso Eclipsia entero: padre 2B a +1051 con escaparate de cinco
        # lineas (1051, 69, 28, 7, 1), un hijo re-analizado a +589 con 128M y
        # seis aristas de las que el arbol no sabe nada.  La peor linea (+1)
        # acota a las seis: la cobertura queda completa-con-cota y el negamax
        # baja la cabecera al +589 del unico testigo real.
        p = _pos('SB-P', 'w', eval_cp=1051, nodes_invested=2_000_000_000,
                 expanded=True, last_analysis=_showcase(1051, 69, 28, 7, 1))
        best = _pos('SB-C', 'b', eval_cp=589, nodes_invested=128_000_000)
        _edge(p, best, 'c1f4')
        for i in range(6):
            _edge(p, _pos(f'SB-U{i}', 'b'))

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, 589)
        self.assertEqual(p.backed_move, 'c1f4')
        self.assertEqual(p.backed_nodes, 128_000_000)

    def test_single_line_showcase_gives_no_bound(self):
        # Con una sola linea vigente no hay cota (el PV1 es la eval propia):
        # la guarda direccional sigue mandando y la cabecera no baja mientras
        # queden movimientos sin mirar.
        p = _pos('SL-P', 'w', eval_cp=1051, nodes_invested=2_000_000_000,
                 expanded=True, last_analysis=_showcase(1051))
        _edge(p, _pos('SL-C', 'b', eval_cp=589, nodes_invested=128_000_000),
              'c1f4')
        _edge(p, _pos('SL-U', 'b'))

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, 1051)
        self.assertIsNone(p.backed_move)

    def test_bound_alone_never_backs_without_a_real_witness(self):
        # Una cota sin un solo hijo informado no respalda nada: el escaparate
        # afirma "nada supera esto", pero sin testigo no hay valor que elegir.
        p = _pos('NW-P', 'w', eval_cp=400, nodes_invested=512_000_000,
                 expanded=True, last_analysis=_showcase(400, 120))
        for i in range(3):
            _edge(p, _pos(f'NW-U{i}', 'b'))

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertIsNone(p.backed_eval)

    def test_quality_guard_propagates_and_still_buys_convergence(self):
        # La guarda de calidad ya no veta: un hijo FAVORABLE con soporte
        # debil (1M contra los 2B propios) desplaza la cabecera YA... y sigue
        # comprando el analisis que confirme o refute, que era la mitad del
        # sistema que si funcionaba.
        p = _pos('QG-P', 'w', eval_cp=1000, nodes_invested=2_000_000_000,
                 expanded=True)
        weak = _pos('QG-C', 'b', eval_cp=1200, nodes_invested=1_000_000)
        _edge(p, weak, 'd2d4')
        _edge(p, _pos('QG-U', 'b'))

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, 1200)
        self.assertEqual(p.backed_move, 'd2d4')
        bought = AnalysisTask.objects.filter(
            position=weak, arm=ingest.QUALITY_ARM)
        self.assertEqual(bought.count(), 1)

    def test_unfavourable_without_bound_still_holds(self):
        # Sin escaparate y con cobertura parcial, bajar sigue vetado: los
        # movimientos sin mirar pueden esconder algo mejor, y eso no es
        # pesimismo sino la aritmetica del max.
        p = _pos('DF-P', 'w', eval_cp=1000, nodes_invested=512_000_000,
                 expanded=True)
        _edge(p, _pos('DF-C', 'b', eval_cp=300, nodes_invested=512_000_000),
              'd2d4')
        _edge(p, _pos('DF-U', 'b'))

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, 1000)

    def test_proof_authority_does_not_cross_the_bound(self):
        # Un hijo PROBADO detras de una cota: el VALOR sube (es el mejor
        # conocimiento) pero la calidad de prueba no atraviesa el escaparate
        # — los movimientos acotados no estan probados, estan buscados.
        p = _pos('PA-P', 'w', eval_cp=400, nodes_invested=5_000,
                 expanded=True, last_analysis=_showcase(400, 50))
        won = _pos('PA-C', 'b', status='WHITE_WIN')
        _edge(p, won, 'd2d4')
        _edge(p, _pos('PA-U', 'b'))

        ingest.backup_backed_evals([p.key])

        p.refresh_from_db()
        self.assertEqual(p.backed_eval, 10_000)
        self.assertEqual(p.backed_nodes, 5_000)


class WalkedUnexploredTests(TestCase):

    def test_seeded_eval_counts_as_unexplored(self):
        # Una siembra no es una medida: el hijo con eval heredada de la linea
        # del padre y cero busqueda propia vuelve a ser comprable por el
        # boton de anchura ("include all walked nodes" — comunidad, 8-ago).
        walked = SimpleNamespace(status='UNKNOWN', eval_cp=42,
                                 nodes_invested=0, backed_nodes=0,
                                 backed_eval=None)
        self.assertTrue(ingest.is_unexplored(walked))

    def test_own_search_or_backing_is_explored(self):
        searched = SimpleNamespace(status='UNKNOWN', eval_cp=42,
                                   nodes_invested=128_000_000,
                                   backed_nodes=0, backed_eval=None)
        backed = SimpleNamespace(status='UNKNOWN', eval_cp=None,
                                 nodes_invested=0, backed_nodes=64_000_000,
                                 backed_eval=17)
        self.assertFalse(ingest.is_unexplored(searched))
        self.assertFalse(ingest.is_unexplored(backed))
