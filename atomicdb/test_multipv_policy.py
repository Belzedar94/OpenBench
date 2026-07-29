"""MultiPV is a spending decision, not a constant.

Measured on the position the community reported: with the SAME node budget,
MultiPV 5 reached depth 18 and said Black holds (-89); MultiPV 1 reached 23
and said Black is lost (-901).  Past a certain rung, width stops buying move
ordering and starts buying expensive noise.
"""

from . import ingest, logic
from .models import AnalysisTask, Position
from .testing import TestCase


class MultipvPolicyTests(TestCase):

    def test_seeding_keeps_the_full_width(self):
        self.assertEqual(ingest.multipv_for(0, 2_000_000_000, seeding=True), 5)

    def test_a_high_budget_spends_on_depth(self):
        for budget in (512_000_000, 2_000_000_000, 10_000_000_000):
            self.assertEqual(ingest.multipv_for(4, budget), 2, budget)

    def test_a_low_rung_keeps_the_visit_policy(self):
        self.assertEqual(ingest.multipv_for(0, 128_000_000), 5)
        self.assertEqual(ingest.multipv_for(4, 128_000_000), 3)

    def test_two_not_one_so_ordering_keeps_a_second_opinion(self):
        self.assertEqual(ingest.DEPTH_MULTIPV, 2)

    def test_the_old_single_argument_call_is_unchanged(self):
        self.assertEqual(ingest.multipv_for(0), 5)
        self.assertEqual(ingest.multipv_for(3), 3)


class MultipvAtCreationSitesTests(TestCase):

    def _pos(self):
        return ingest.get_or_create_position(logic.start_fen())

    def test_a_visitor_request_at_the_floor_stays_wide(self):
        pos = self._pos()
        self.client.post(f'/atomicdb/request/{pos.key}/')
        task = AnalysisTask.objects.get(position=pos)
        self.assertEqual(task.budget_nodes, ingest.REQUEST_BUDGET_LADDER[0])
        self.assertEqual(task.multipv, 5)

    def test_a_visitor_request_on_a_high_rung_goes_deep(self):
        pos = self._pos()
        AnalysisTask.objects.create(
            position=pos, generation=0, budget_nodes=128_000_000,
            state='COMPLETED', source='USER')
        pos.visits = 1
        pos.save(update_fields=['visits'])

        ingest.request_analysis(pos)

        task = AnalysisTask.objects.get(position=pos, state='PENDING')
        self.assertGreaterEqual(task.budget_nodes,
                                ingest.DEPTH_BUDGET_THRESHOLD)
        self.assertEqual(task.multipv, 2)

    def test_the_auto_selector_follows_the_budget_it_chose(self):
        pos = self._pos()
        pos.visits = 4            # ladder rung 4 = 2B nodes
        pos.eval_cp = 300
        pos.save()
        ingest._priority_refresh_cache['at'] = 0.0
        ingest.refresh_priorities()

        tasks = ingest.next_tasks(1)

        self.assertEqual(len(tasks), 1)
        self.assertGreaterEqual(tasks[0].budget_nodes,
                                ingest.DEPTH_BUDGET_THRESHOLD)
        self.assertEqual(tasks[0].multipv, 2)

    def test_a_disputed_reanalysis_asks_for_depth(self):
        pos = self._pos()
        task = ingest._queue_disputed_reanalysis(pos)
        self.assertEqual(task.budget_nodes, ingest.BUDGET_LADDER[-1])
        self.assertEqual(task.multipv, 2)

    def test_bootstrapping_the_root_stays_wide(self):
        ingest.bootstrap_root()
        for task in AnalysisTask.objects.all():
            self.assertEqual(task.multipv, 5)


class RestrictedPassSemanticsTests(TestCase):
    """Que SIGNIFICA el numero que vuelve de un pase con ``searchmoves``.

    El servidor acota la busqueda a las jugadas sin resolver para no gastar
    nodos re-derivando defensas ya demostradas.  El precio es que la mejor
    linea de ese pase es la mejor ENTRE LO QUE QUEDABA: con la jugada buena ya
    cerrada y fuera de la lista, el numero es PEOR que la posicion, y de
    ``eval_cp`` comen budget_for, el breadth-swap, witness-refuted y la
    incertidumbre.  Un pase asi puede mejorar la eval — eso lo ha demostrado —
    pero no puede empeorarla, que es justo lo que no ha mirado.
    """

    def setUp(self):
        self.pos = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.pos)
        Position.objects.filter(key=self.pos.key).update(eval_cp=300)

    def _ingest(self, eval_cp, restricted):
        ingest.ingest_analysis(
            self.pos.key, [{'move': 'g1f3', 'eval_cp': eval_cp,
                            'mate': None, 'pv': ['g1f3']}],
            8_000_000, restricted=restricted)
        return Position.objects.get(key=self.pos.key)

    def test_a_restricted_pass_does_not_talk_down_the_position(self):
        fresh = self._ingest(-50, restricted=True)

        self.assertEqual(fresh.eval_cp, 300)

    def test_a_restricted_pass_may_still_talk_it_up(self):
        fresh = self._ingest(900, restricted=True)

        self.assertEqual(fresh.eval_cp, 900)
        self.assertEqual(fresh.best_move, 'g1f3')

    def test_a_complete_pass_is_believed_in_both_directions(self):
        fresh = self._ingest(-50, restricted=False)

        self.assertEqual(fresh.eval_cp, -50)

    def test_black_to_move_reads_improvement_the_other_way(self):
        black = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'g1f3'))
        ingest.expand(black)
        Position.objects.filter(key=black.key).update(eval_cp=-300)

        ingest.ingest_analysis(
            black.key, [{'move': 'g8f6', 'eval_cp': 100, 'mate': None,
                         'pv': ['g8f6']}], 8_000_000, restricted=True)

        # +100 en perspectiva blanca es PEOR para las negras, que son quienes
        # mueven: el pase restringido no puede afirmarlo.
        self.assertEqual(Position.objects.get(key=black.key).eval_cp, -300)

    def test_a_position_without_an_eval_yet_takes_what_it_can_get(self):
        # Sin nada almacenado, "no empeorar" no tiene contra que medirse y la
        # unica alternativa es seguir sin saber nada.
        blank = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        ingest.expand(blank)

        ingest.ingest_analysis(
            blank.key, [{'move': 'e7e5', 'eval_cp': -40, 'mate': None,
                         'pv': ['e7e5']}], 8_000_000, restricted=True)

        self.assertEqual(Position.objects.get(key=blank.key).eval_cp, -40)
