"""La PUERTA DOBLE de exploracion: estimador de coste, puerta y desacuerdo.

El eval del motor dice quien esta mejor; ``solve_estimate.annoyance`` dice
cuanto cuesta cerrarlo.  Un nodo merece esfuerzo de RESOLUCION cuando las dos
senales coinciden, y el cuadrante donde NO coinciden — motor optimista,
estimador pesimista — se registra en vez de descartarse.
"""

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import ingest, logic, proof, solve_estimate
from .models import DBEvent, Edge, Position, ProofCampaign, ProofNode
from .testing import TestCase

# Startpos: 32 piezas, blancas al turno.
START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
# Final pelado por encima del horizonte de tablebase (12 piezas).
BARE = '4k3/pp4pp/8/8/8/8/PP4PP/3RK2R w K - 0 1'
# El mismo, con negras al turno: nodo AND de una campana WHITE_WIN.
BARE_BLACK = '4k3/pp4pp/8/8/8/8/PP4PP/3RK2R b K - 0 1'
# Bajo el horizonte de tablebase: el veredicto es una consulta.
TABLEBASE = '8/8/8/4k3/8/8/4K3/4R3 w - - 0 1'
# Una PV de pura maniobra: el contador de 50 no se resetea ni una vez.
MANOEUVRE = ['d1d2', 'e8e7', 'd2d1', 'e7e8', 'd1d2', 'e8e7']


def leaf(fen=START, eval_cp=None, lines=None, mate_in=None):
    return solve_estimate.Leaf(fen, eval_cp, lines, mate_in)


class WeightsTests(SimpleTestCase):
    """Los pesos son una declaracion, y una declaracion se comprueba."""

    def test_the_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(solve_estimate.FEATURE_WEIGHTS.values()),
                               1.0)

    def test_every_feature_has_a_weight(self):
        self.assertEqual(set(solve_estimate.features(leaf())),
                         set(solve_estimate.FEATURE_WEIGHTS))

    def test_the_declared_constants_are_the_approved_ones(self):
        self.assertEqual(solve_estimate.MAX_FACTOR, 8)
        self.assertEqual(solve_estimate.REVERSIBLE_WEIGHT, 0.35)
        self.assertEqual(solve_estimate.NEUTRAL, 0.5)


class ReversibleFeatureTests(SimpleTestCase):

    def _lines(self, pv):
        return [{'eval_cp': 0, 'pv': pv}]

    def test_a_pv_of_manoeuvres_is_the_tedious_end(self):
        # Caballos y torres yendo y viniendo: el contador de 50 no se resetea
        # ni una vez.
        pv = ['g1f3', 'g8f6', 'f3g1', 'f6g8', 'b1c3', 'b8c6']
        self.assertEqual(
            solve_estimate.reversible_feature(leaf(lines=self._lines(pv))),
            1.0)

    def test_a_pv_of_pawn_moves_and_captures_is_the_cheap_end(self):
        pv = ['e2e4', 'd7d5', 'a2a4', 'h7h5']
        self.assertEqual(
            solve_estimate.reversible_feature(leaf(lines=self._lines(pv))),
            0.0)

    def test_a_capture_counts_even_when_no_pawn_moves(self):
        # 3 de 4: e2e4 y d7d5 son de peon, e4d5 es captura y d8d5 ya no lo es
        # porque en atomic los dos peones volaron con la captura anterior.
        pv = ['e2e4', 'd7d5', 'e4d5', 'd8d5']
        self.assertAlmostEqual(
            solve_estimate.reversible_feature(leaf(lines=self._lines(pv))),
            0.25)

    def test_the_density_is_a_fraction_not_a_count(self):
        half = ['e2e4', 'g8f6', 'd2d4', 'f6g8']
        self.assertAlmostEqual(
            solve_estimate.reversible_feature(leaf(lines=self._lines(half))),
            0.5)

    def test_no_pv_is_neutral_not_a_guess(self):
        self.assertEqual(solve_estimate.reversible_feature(leaf()),
                         solve_estimate.NEUTRAL)
        self.assertEqual(
            solve_estimate.reversible_feature(leaf(lines=[{'eval_cp': 3}])),
            solve_estimate.NEUTRAL)

    def test_a_prior_pass_line_is_not_the_current_verdict(self):
        row = leaf(lines=[{'pv': ['g1f3', 'g8f6'], 'prior_pass': True}])
        self.assertEqual(solve_estimate.reversible_feature(row),
                         solve_estimate.NEUTRAL)

    def test_the_first_ply_is_exact_deeper_ones_are_a_proxy(self):
        """El mapa es la FEN de verdad en el ply 0; luego se degrada.

        Se documenta como proxy, asi que lo que se comprueba es lo que el
        proxy PROMETE: exactitud en el primer ply y un recorrido acotado.
        """
        read, zeroing = solve_estimate.zeroing_plies(START, ['e2e4'])
        self.assertEqual((read, zeroing), (1, 1))       # peon
        read, zeroing = solve_estimate.zeroing_plies(START, ['g1f3'])
        self.assertEqual((read, zeroing), (1, 0))       # caballo a casilla libre

    def test_a_promotion_counts_as_zeroing(self):
        read, zeroing = solve_estimate.zeroing_plies(
            '8/4P3/8/8/8/8/8/4K1k1 w - - 0 1', ['e7e8q'])
        self.assertEqual((read, zeroing), (1, 1))

    def test_the_scan_is_bounded(self):
        pv = ['g1f3', 'g8f6', 'f3g1', 'f6g8'] * 40
        read, _ = solve_estimate.zeroing_plies(START, pv)
        self.assertEqual(read, solve_estimate.PV_SCAN_MAX_PLIES)

    def test_a_malformed_pv_stops_the_walk_instead_of_crashing(self):
        read, _ = solve_estimate.zeroing_plies(START, ['e2e4', None, 'd2d4'])
        self.assertEqual(read, 1)


class BranchingFeatureTests(SimpleTestCase):

    def test_the_edge_count_is_trusted_in_both_directions(self):
        self.assertLess(solve_estimate.branching_feature(leaf(), branching=3),
                        solve_estimate.branching_feature(leaf(), branching=30))
        self.assertEqual(
            solve_estimate.branching_feature(leaf(), branching=200), 1.0)

    def test_the_multipv_proxy_can_only_push_upwards(self):
        """N lineas PRUEBAN N legales; una linea no prueba que solo haya una.

        Sin esta asimetria un pase profundo de MultiPV 1 haria parecer
        estrecha — y por tanto barata — cualquier posicion que tocara.
        """
        narrow = leaf(lines=[{'pv': ['e2e4']}])
        wide = leaf(lines=[{'pv': ['e2e4']}] * 5)
        self.assertEqual(solve_estimate.branching_feature(narrow),
                         solve_estimate.NEUTRAL)
        self.assertEqual(solve_estimate.branching_feature(wide), 1.0)

    def test_no_information_at_all_is_neutral(self):
        self.assertEqual(solve_estimate.branching_feature(leaf()),
                         solve_estimate.NEUTRAL)


class MaterialFeatureTests(SimpleTestCase):

    def test_below_the_tablebase_horizon_the_answer_is_a_lookup(self):
        self.assertEqual(solve_estimate.material_feature(leaf(TABLEBASE)), 0.0)

    def test_the_peak_is_the_bare_endgame_above_that_horizon(self):
        self.assertEqual(solve_estimate.material_feature(leaf(BARE)), 1.0)

    def test_a_full_board_is_tactical_not_tedious(self):
        self.assertEqual(solve_estimate.material_feature(leaf(START)), 0.0)

    def test_the_curve_is_a_tent_not_a_ramp(self):
        bare = solve_estimate.material_feature(leaf(BARE))
        self.assertGreater(bare, solve_estimate.material_feature(leaf(START)))
        self.assertGreater(bare,
                           solve_estimate.material_feature(leaf(TABLEBASE)))


class EvalBandFeatureTests(SimpleTestCase):

    def test_seen_mate_is_the_fast_lane(self):
        self.assertEqual(
            solve_estimate.eval_band_feature(leaf(eval_cp=9_900)), 0.0)
        self.assertEqual(
            solve_estimate.eval_band_feature(leaf(mate_in=7)), 0.0)
        self.assertEqual(
            solve_estimate.eval_band_feature(
                leaf(eval_cp=40, lines=[{'mate': 5, 'pv': ['e2e4']}])),
            0.0)

    def test_the_band_is_ordinal_and_monotone(self):
        values = [solve_estimate.eval_band_feature(leaf(eval_cp=score))
                  for score in (9_500, 1_000, 500, 20)]
        self.assertEqual(values, sorted(values))

    def test_the_sign_does_not_matter(self):
        self.assertEqual(solve_estimate.eval_band_feature(leaf(eval_cp=1_000)),
                         solve_estimate.eval_band_feature(leaf(eval_cp=-1_000)))

    def test_no_eval_is_neutral(self):
        self.assertEqual(solve_estimate.eval_band_feature(leaf()),
                         solve_estimate.NEUTRAL)


class AnnoyanceTests(SimpleTestCase):

    def test_the_value_is_always_a_fraction(self):
        for row in (leaf(), leaf(BARE, -30_000), leaf(TABLEBASE, 12),
                    leaf(START, 0, [{'pv': ['g1f3'] * 30}])):
            value = solve_estimate.annoyance(row)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_the_tedious_endgame_beats_the_promising_mate(self):
        """El caso que motiva la puerta, escrito como test.

        Los dos nodos tienen un eval enorme para el mismo bando.  Uno es un
        final pelado con una PV de maniobra; el otro es un mate visto.  El
        motor no los distingue; el estimador si.
        """
        tedious = leaf(BARE, 1_200,
                       [{'eval_cp': 1_200, 'pv': ['d1d2', 'e8e7'] * 6}])
        promising = leaf(START, 1_200, [{'mate': 4, 'pv': ['e2e4']}])
        self.assertGreater(solve_estimate.annoyance(tedious),
                           solve_estimate.annoyance(promising))

    def test_it_is_pure_same_row_same_number(self):
        row = leaf(BARE, 700, [{'eval_cp': 700, 'pv': ['d1d2', 'e8e7']}])
        self.assertEqual(solve_estimate.annoyance(row),
                         solve_estimate.annoyance(row))

    def test_an_empty_row_lands_on_the_neutral_middle(self):
        """Sin PV, sin ancho y sin eval solo queda el material del FEN."""
        value = solve_estimate.annoyance(leaf())
        self.assertAlmostEqual(
            value, solve_estimate.NEUTRAL * (1 - solve_estimate.MATERIAL_WEIGHT))


class GateFactorTests(SimpleTestCase):

    def test_no_annoyance_costs_exactly_what_it_used_to(self):
        self.assertEqual(solve_estimate.gate_factor(0.0), 1.0)

    def test_full_annoyance_costs_the_declared_maximum(self):
        self.assertEqual(solve_estimate.gate_factor(1.0),
                         float(solve_estimate.MAX_FACTOR))

    def test_it_is_monotone_and_never_below_one(self):
        previous = 0.0
        for step in range(11):
            factor = solve_estimate.gate_factor(step / 10.0)
            self.assertGreaterEqual(factor, 1.0)
            self.assertGreater(factor, previous)
            previous = factor

    def test_out_of_range_input_is_clamped_not_trusted(self):
        self.assertEqual(solve_estimate.gate_factor(-5.0), 1.0)
        self.assertEqual(solve_estimate.gate_factor(99.0),
                         float(solve_estimate.MAX_FACTOR))


class GateOffIsTheHistoricBehaviourTests(SimpleTestCase):
    """Con el flag apagado, la hoja vale lo que valia. Byte a byte."""

    def test_the_flag_defaults_to_off(self):
        self.assertFalse(proof.solve_gate_enabled())

    def test_no_annoyance_is_computed_at_all_when_it_is_off(self):
        """Apagado no debe costar ni el estimador."""
        self.assertIsNone(proof.gate_annoyance(leaf(BARE, 1_200)))
        self.assertIsNone(proof.shallow_annoyance(BARE, 1_200))

    def test_the_numbers_are_the_documented_ones(self):
        self.assertEqual(
            proof.leaf_numbers(START, 'UNKNOWN', None, 'WHITE_WIN'), (1, 1))
        self.assertEqual(
            proof.leaf_numbers(BARE_BLACK, 'UNKNOWN', 1_200, 'WHITE_WIN',
                               legal_moves=10),
            (20, 16))

    def test_passing_no_annoyance_is_the_same_as_the_old_signature(self):
        for score in (None, -900, 0, 400, 1_200, 30_000):
            self.assertEqual(
                proof.leaf_numbers(BARE_BLACK, 'UNKNOWN', score, 'WHITE_WIN',
                                   legal_moves=8),
                proof.leaf_numbers(BARE_BLACK, 'UNKNOWN', score, 'WHITE_WIN',
                                   legal_moves=8, annoyance=None))

    def test_the_maintenance_only_stays_narrow(self):
        self.assertEqual(proof.maintenance_fields(),
                         ('key', 'fen', 'status', 'eval_cp'))


class GateArithmeticTests(SimpleTestCase):
    """El factor: que encarece, que no, y hasta donde."""

    def _leaf(self, score, annoyance, moves=10, fen=BARE_BLACK):
        return proof.leaf_numbers(fen, 'UNKNOWN', score, 'WHITE_WIN',
                                  legal_moves=moves, annoyance=annoyance)

    def test_annoyance_multiplies_the_pn(self):
        plain = self._leaf(1_200, None)
        gated = self._leaf(1_200, 1.0)
        self.assertEqual(gated[0], plain[0] * solve_estimate.MAX_FACTOR)

    def test_zero_annoyance_changes_nothing(self):
        self.assertEqual(self._leaf(1_200, 0.0), self._leaf(1_200, None))

    def test_dn_is_never_touched(self):
        """Refutar una posicion tediosa no es mas caro por ser tediosa."""
        for annoyance in (0.0, 0.3, 1.0):
            self.assertEqual(self._leaf(1_200, annoyance)[1],
                             self._leaf(1_200, None)[1])

    def test_the_mate_band_is_exempt(self):
        """Ya es la via rapida: encarecerla solo alejaria al descenso."""
        self.assertEqual(self._leaf(30_000, 1.0), self._leaf(30_000, None))
        self.assertEqual(self._leaf(9_000, 1.0), self._leaf(9_000, None))
        # Justo por debajo de la banda si entra.
        self.assertGreater(self._leaf(8_999, 1.0)[0],
                           self._leaf(8_999, None)[0])

    def test_the_classic_one_one_is_exempt(self):
        """Sin informacion tiene que seguir siendo lo mas demostrador."""
        self.assertEqual(
            proof.leaf_numbers(START, 'UNKNOWN', None, 'WHITE_WIN',
                               annoyance=1.0),
            (1, 1))

    def test_a_closed_leaf_keeps_its_truth_value(self):
        for status, numbers in (('WHITE_WIN', (0, proof.PROOF_INFINITY)),
                                ('BLACK_WIN', (proof.PROOF_INFINITY, 0)),
                                ('DRAW', (proof.PROOF_INFINITY, 0))):
            self.assertEqual(
                proof.leaf_numbers(START, status, 1_200, 'WHITE_WIN',
                                   annoyance=1.0),
                numbers)

    def test_a_gated_leaf_still_never_claims_infinity(self):
        for score in (-30_000, -1_000, 0, 1_000):
            pn, dn = proof.leaf_numbers(START, 'UNKNOWN', score, 'WHITE_WIN',
                                        legal_moves=200, annoyance=1.0)
            self.assertLessEqual(pn, proof.PROOF_MAX_LEAF)
            self.assertLess(pn, proof.PROOF_INFINITY)
            self.assertLess(dn, proof.PROOF_INFINITY)
            self.assertGreaterEqual(min(pn, dn), 1)

    def test_the_gate_is_monotone_in_the_annoyance(self):
        previous = 0
        for step in range(11):
            pn = self._leaf(1_200, step / 10.0)[0]
            self.assertGreaterEqual(pn, previous)
            previous = pn


@override_settings(ATOMICDB_SOLVE_GATE=True)
class GateOnTests(TestCase):
    """La puerta encendida, por el camino de siempre y sobre el arbol real."""

    def setUp(self):
        proof.reset_solve_gate_log()
        self.campaign = ProofCampaign.objects.get(
            name=proof.DEFAULT_CAMPAIGN_NAME)
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)

    def _tedious_child(self):
        """Un hijo con eval de ganado y una PV de pura maniobra."""
        edge = Edge.objects.filter(parent=self.root).order_by('id').first()
        Position.objects.filter(key=edge.child_id).update(
            eval_cp=1_200,
            last_analysis=[{'eval_cp': 1_200, 'pv': MANOEUVRE}])
        return Position.objects.get(key=edge.child_id)

    def test_the_flag_turns_it_on(self):
        self.assertTrue(proof.solve_gate_enabled())

    def test_the_maintenance_asks_for_the_pv_it_is_going_to_read(self):
        """Leerlo diferido costaria un SELECT por fila."""
        self.assertIn('last_analysis', proof.maintenance_fields())
        self.assertIn('mate_in', proof.maintenance_fields())

    def test_a_tedious_frontier_leaf_gets_more_expensive(self):
        child = self._tedious_child()
        proof.refresh_proof_numbers([child.key], max_plies=1)
        gated = ProofNode.objects.get(campaign=self.campaign,
                                      position=child).pn

        with override_settings(ATOMICDB_SOLVE_GATE=False):
            ProofNode.objects.filter(position=child).delete()
            proof.refresh_proof_numbers([child.key], max_plies=1)
            plain = ProofNode.objects.get(campaign=self.campaign,
                                          position=child).pn

        self.assertGreater(gated, plain)

    def test_a_level_still_costs_a_fixed_number_of_statements(self):
        """La puerta no puede convertir una pasada en una tormenta.

        Cuatro sentencias, las mismas que sin puerta: campanas activas,
        posiciones del nivel, aristas con sus hijos y filas de prueba.  El
        estimador lee ``last_analysis`` de la fila que YA vino, nunca de una
        consulta suya.

        Es el coste de REGIMEN: el evento de desacuerdo lo emite la primera
        pasada y el rate limit lo suprime en las siguientes, que es
        exactamente por lo que el log lleva rate limit.
        """
        child = self._tedious_child()
        proof.refresh_proof_numbers([child.key], max_plies=1)   # calienta

        with self.assertNumQueries(4, using=settings.ATOMICDB_DATABASE_ALIAS):
            proof.refresh_proof_numbers([child.key], max_plies=1)

    def test_the_descent_ranks_the_tedious_child_last(self):
        """Dos hijos con el MISMO eval de ganado; el gordo va primero.

        Es el caso que la puerta gobierna: nodo OR (se ordena por pn), dos
        candidatos que el motor ve igual de bien y un estimador que no.
        """
        children = [('a1a2', 'key-bare', 'UNKNOWN', 1_200, BARE_BLACK),
                    ('b1b2', 'key-rich', 'UNKNOWN', 1_200,
                     logic.apply_move(logic.start_fen(), 'e2e4'))]
        ranked = proof._ranked_children(self.campaign, self.root, children, {})
        self.assertEqual([item[2] for item in ranked],
                         ['key-rich', 'key-bare'])

    def test_turning_it_off_again_restores_the_numbers(self):
        """Sin residuo: los pn de hoja se recalculan en la pasada siguiente."""
        child = self._tedious_child()
        proof.refresh_proof_numbers([child.key], max_plies=1)
        gated = ProofNode.objects.get(campaign=self.campaign,
                                      position=child).pn

        with override_settings(ATOMICDB_SOLVE_GATE=False):
            proof.refresh_proof_numbers([child.key], max_plies=1)
            restored = ProofNode.objects.get(campaign=self.campaign,
                                             position=child).pn

        self.assertNotEqual(gated, restored)
        self.assertEqual(restored, proof.leaf_numbers(
            child.fen, 'UNKNOWN', 1_200, self.campaign.goal)[0])


@override_settings(ATOMICDB_SOLVE_GATE=True)
class DisagreementQuadrantTests(TestCase):
    """El log del cuadrante: motor optimista, estimador pesimista.

    No es telemetria decorativa.  Es (a) el detector de callejones sin salida
    — el sitio donde el motor promete y el estimador cobra — y (b) el conjunto
    de entrenamiento del estimador aprendido que sustituira al hand-crafted.
    """

    def setUp(self):
        proof.reset_solve_gate_log()
        self.campaign = ProofCampaign.objects.get(
            name=proof.DEFAULT_CAMPAIGN_NAME)
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)

    def _events(self):
        return list(DBEvent.objects.filter(kind='SOLVE_GATE_DISAGREE'))

    def _gate(self, score, annoyance=1.0, key='k1'):
        return proof.leaf_numbers(BARE_BLACK, 'UNKNOWN', score, 'WHITE_WIN',
                                  legal_moves=10, annoyance=annoyance, key=key)

    def test_an_optimistic_eval_that_the_gate_taxes_is_logged(self):
        self._gate(1_200)
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].payload,
            {'key': 'k1', 'eval_cp': 1_200, 'annoyance': 1.0, 'factor': 8.0})

    def test_the_eval_in_the_payload_is_white_pov_like_the_column(self):
        """Objetivo BLACK_WIN: el atacante es optimista con eval_cp = -1_200."""
        proof.leaf_numbers(BARE, 'UNKNOWN', -1_200, 'BLACK_WIN',
                           legal_moves=10, annoyance=1.0, key='k-black')
        self.assertEqual(self._events()[0].payload['eval_cp'], -1_200)

    def test_a_quiet_eval_is_not_a_disagreement(self):
        """Sin promesa del motor no hay contradiccion que registrar."""
        self._gate(120)
        self._gate(-2_000, key='k2')
        self.assertEqual(self._events(), [])

    def test_the_mate_band_never_disagrees(self):
        """Esta exenta de la puerta, asi que no hay nada que contradecir."""
        self._gate(30_000)
        self.assertEqual(self._events(), [])

    def test_a_leaf_the_gate_did_not_tax_is_not_logged(self):
        self._gate(1_200, annoyance=0.0)
        self.assertEqual(self._events(), [])

    def test_the_same_leaf_is_not_logged_once_per_pass(self):
        for _ in range(20):
            self._gate(1_200)
        self.assertEqual(len(self._events()), 1)

    def test_different_leaves_are_all_logged(self):
        self._gate(1_200, key='a')
        self._gate(1_200, key='b')
        self.assertEqual(len(self._events()), 2)

    def test_the_degraded_edge_view_does_not_write_the_dataset(self):
        """Sin clave no hay log: dos de las cuatro features estan en neutral."""
        proof.leaf_numbers(BARE_BLACK, 'UNKNOWN', 1_200, 'WHITE_WIN',
                           legal_moves=10, annoyance=1.0)
        self.assertEqual(self._events(), [])

    def test_the_rate_limit_cache_can_be_reset(self):
        self._gate(1_200)
        proof.reset_solve_gate_log()
        self._gate(1_200)
        self.assertEqual(len(self._events()), 2)

    def test_the_cache_does_not_grow_without_bound(self):
        original = proof.SOLVE_GATE_LOG_MAX
        proof.SOLVE_GATE_LOG_MAX = 8
        try:
            for index in range(40):
                self._gate(1_200, key=f'k{index}')
        finally:
            proof.SOLVE_GATE_LOG_MAX = original
        self.assertLessEqual(len(proof._solve_gate_logged),
                             proof.SOLVE_GATE_LOG_MAX)

    def test_the_real_maintenance_pass_logs_the_frontier_leaf(self):
        """El camino de produccion, no el laboratorio."""
        edge = Edge.objects.filter(parent=self.root).order_by('id').first()
        Position.objects.filter(key=edge.child_id).update(
            eval_cp=1_500,
            last_analysis=[{'eval_cp': 1_500, 'pv': MANOEUVRE}])

        proof.refresh_proof_numbers([edge.child_id], max_plies=1)

        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload['key'], edge.child_id)
        self.assertEqual(events[0].payload['eval_cp'], 1_500)
        self.assertGreater(events[0].payload['factor'], 1.0)

    def test_nothing_is_logged_with_the_gate_off(self):
        with override_settings(ATOMICDB_SOLVE_GATE=False):
            edge = Edge.objects.filter(parent=self.root).order_by('id').first()
            Position.objects.filter(key=edge.child_id).update(
                eval_cp=1_500,
                last_analysis=[{'eval_cp': 1_500, 'pv': MANOEUVRE}])
            proof.refresh_proof_numbers([edge.child_id], max_plies=1)
        self.assertEqual(self._events(), [])
