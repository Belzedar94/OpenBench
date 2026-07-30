"""La PUERTA DOBLE de exploracion: estimador de coste, puerta y desacuerdo.

El eval del motor dice quien esta mejor; ``solve_estimate.annoyance`` dice
cuanto cuesta cerrarlo.  Un nodo merece esfuerzo de RESOLUCION cuando las dos
senales coinciden, y el cuadrante donde NO coinciden — motor optimista,
estimador pesimista — se registra en vez de descartarse.
"""

from django.test import SimpleTestCase

from . import solve_estimate

# Startpos: 32 piezas, blancas al turno.
START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
# Final pelado por encima del horizonte de tablebase (12 piezas).
BARE = '4k3/pp4pp/8/8/8/8/PP4PP/3RK2R w K - 0 1'
# Bajo el horizonte de tablebase: el veredicto es una consulta.
TABLEBASE = '8/8/8/4k3/8/8/4K3/4R3 w - - 0 1'


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
