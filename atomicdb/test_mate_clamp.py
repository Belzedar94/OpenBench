"""Un mate CORTO se verifica barato; uno largo sigue comprando su excavacion.

EL DESPERDICIO MEDIDO.  Una posicion cuya reclamacion conocida es un M2 se
encolaba igual que un mate en treinta: ``budget_for`` sube a 128M en cuanto
|eval| entra en la banda de mate, y el worker manda ``go nodes N`` sin parada
temprana (no hay forma de decirle "para cuando lo tengas").  El motor ve el M2
en unos miles de nodos y la maquina se pasa el resto del minuto puntuando
alternativas irrelevantes, con MultiPV alto ademas.

LA UNIDAD, que es lo primero que hay que fijar porque aqui se cruzan dos.
``Position.mate_in`` cuenta PLIES — es ``len(pv_rest)``, la longitud del
testigo guardado en ``won_line``, y un TERMINAL vale 0.  El motor habla en
JUGADAS: el ``score mate N`` de UCI, que el worker convierte a
``eval_cp = (10_000 - |N|) * signo``.  Toda la politica razona en PLIES y la
conversion vive en un solo sitio.

LO QUE ESTOS TESTS CLAVAN:

* la distancia solo se da por CONOCIDA cuando esta escrita (``mate_in``, el
  campo ``mate`` de la linea vigente, o un ``eval_cp`` por encima del recorte
  de tablebase); la banda SIN distancia conserva sus 128M, porque esa es la
  extraccion de PV de los mates largos y no se toca;
* la forma del clamp: ``max(2M, 4M x plies)`` con cap, y MultiPV 1;
* que el clamp abarata la PRIMERA mirada sin poner techo — la escalera por
  visitas recupera el mando en cuanto pide mas, asi que un nodo no puede
  quedarse en un bucle de sondas de dos millones;
* que la promocion de una tarea ya PENDING sigue siendo monotona AL ALZA;
* y que el testigo refutado sigue comprando su revisita profunda, que es el
  mecanismo que hace seguro comprar poco cuando la reclamacion resulta falsa.
"""

from unittest.mock import patch

from django.conf import settings

from . import ingest, logic
from .models import AnalysisTask, Edge, Position
from .testing import TestCase


# Blancas al turno, mate real cerca: el mismo fixture que usa
# ``test_mate_distance``.  Lo sintetico en estos tests es solo el valor
# almacenado, que es lo que la base viva tenia escrito.
CLAIM_FEN = '7k/6p1/8/8/8/8/8/3QK3 w - - 0 1'
CLAIM_FEN_BLACK = '7k/6p1/8/8/8/8/8/3QK3 b - - 0 1'


def _mate_eval(moves, winner_white=True):
    """``eval_cp`` tal y como lo escribe el worker para un ``score mate N``."""
    return (10_000 - abs(moves)) * (1 if winner_white else -1)


def _position(fen, **fields):
    fen = logic.canonical_fen(fen)
    pos, _ = Position.objects.update_or_create(
        key=logic.key_of(fen), defaults={'fen': fen, **fields})
    return pos


def _claiming(moves=2, **fields):
    """Nodo UNKNOWN que reclama mate en ``moves`` JUGADAS, al estilo worker."""
    return _position(CLAIM_FEN, eval_cp=_mate_eval(moves), **fields)


class MateDistanceUnitTests(TestCase):
    """PLIES, y la conversion desde las JUGADAS del motor."""

    def test_mate_in_is_read_as_plies_not_moves(self):
        pos = _position(CLAIM_FEN, mate_in=4)
        self.assertEqual(ingest.claimed_mate_plies(pos), 4)

    def test_a_terminal_row_is_zero_plies_away(self):
        pos = _position(CLAIM_FEN, mate_in=0)
        self.assertEqual(ingest.claimed_mate_plies(pos), 0)

    def test_the_mover_mates_on_its_own_move(self):
        """M2 del bando al turno son 3 plies: suya, del rival, suya."""
        self.assertEqual(ingest._mate_moves_to_plies(2, True, True), 3)
        self.assertEqual(ingest._mate_moves_to_plies(2, False, False), 3)

    def test_the_defender_mating_costs_the_extra_ply(self):
        """Si gana el que NO esta al turno, el ply de ahora no cuenta: 2N."""
        self.assertEqual(ingest._mate_moves_to_plies(2, False, True), 4)
        self.assertEqual(ingest._mate_moves_to_plies(2, True, False), 4)

    def test_the_worker_formula_decodes_back_to_plies(self):
        self.assertEqual(ingest.claimed_mate_plies(_claiming(moves=2)), 3)
        self.assertEqual(ingest.claimed_mate_plies(_claiming(moves=1)), 1)
        # Blancas al turno pero el mate es de las negras: el ply de mas.
        pos = _position(CLAIM_FEN, eval_cp=_mate_eval(2, winner_white=False))
        self.assertEqual(ingest.claimed_mate_plies(pos), 4)
        # Y el espejo, con negras al turno reclamando ellas el mate.
        pos = _position(CLAIM_FEN_BLACK,
                        eval_cp=_mate_eval(2, winner_white=False))
        self.assertEqual(ingest.claimed_mate_plies(pos), 3)

    def test_a_tablebase_clamp_carries_no_distance(self):
        """El worker recorta los cp de TB a +-9_500 para no fingir distancia."""
        for value in (9_500, -9_500, 9_000, 9_499):
            pos = _position(CLAIM_FEN, eval_cp=value)
            self.assertIsNone(ingest.claimed_mate_plies(pos), value)

    def test_the_declared_mate_field_beats_the_decoded_eval(self):
        """La distancia ESCRITA por el motor manda sobre la decodificada."""
        pos = _position(CLAIM_FEN, eval_cp=_mate_eval(2), last_analysis=[
            {'move': 'd1d5', 'eval_cp': _mate_eval(3), 'mate': 3,
             'pv': ['d1d5']}])
        self.assertEqual(ingest.claimed_mate_plies(pos), 5)   # M3 al turno

    def test_a_line_from_a_previous_pass_does_not_speak(self):
        """``prior_pass`` es el escaparate viejo, no el veredicto vigente."""
        pos = _position(CLAIM_FEN, eval_cp=-30, last_analysis=[
            {'move': 'd1d2', 'eval_cp': -30, 'mate': None, 'pv': ['d1d2']},
            {'move': 'd1d5', 'eval_cp': _mate_eval(2), 'mate': 2,
             'pv': ['d1d5'], 'prior_pass': True}])
        self.assertIsNone(ingest.claimed_mate_plies(pos))

    def test_no_information_at_all_is_not_a_claim(self):
        self.assertIsNone(ingest.claimed_mate_plies(_position(CLAIM_FEN)))


class ShortMateClampShapeTests(TestCase):
    """La forma de lo que se compra: cuanto, y con cuantas lineas."""

    def test_four_million_per_ply_and_one_single_line(self):
        clamp = ingest._short_mate_clamp(_claiming(moves=2))    # 3 plies
        self.assertEqual(clamp, (3 * ingest.MATE_CLAMP_PER_PLY, 1))

    def test_a_tiny_distance_still_pays_the_floor(self):
        clamp = ingest._short_mate_clamp(_position(CLAIM_FEN, mate_in=0))
        self.assertEqual(clamp, (ingest.MATE_CLAMP_FLOOR, 1))

    def test_the_cap_is_a_real_guard_rail_if_the_threshold_moves(self):
        """Al umbral de hoy el producto no llega al cap; subirlo si.

        Se mide con el umbral movido a proposito: el cap existe para que
        aflojar ``MATE_CLAMP_PLIES`` no reintroduzca la excavacion por la
        puerta de atras.
        """
        pos = _position(CLAIM_FEN, mate_in=20)
        with patch.object(ingest, 'MATE_CLAMP_PLIES', 40):
            clamp = ingest._short_mate_clamp(pos)
        self.assertEqual(clamp, (ingest.MATE_CLAMP_CAP, 1))

    def test_a_long_mate_is_not_this_policy(self):
        for moves in (10, 30, 499):
            pos = _claiming(moves=moves)
            self.assertIsNone(ingest._short_mate_clamp(pos), moves)

    def test_the_band_without_a_distance_is_not_this_policy(self):
        self.assertIsNone(
            ingest._short_mate_clamp(_position(CLAIM_FEN, eval_cp=9_500)))

    def test_the_clamp_costs_no_queries_on_a_loaded_row(self):
        """Se lee de la fila que el llamante ya tiene: cero consultas.

        Importa porque esto corre dentro del selector, una vez por candidato:
        un campo diferido convertiria la politica en una consulta por nodo.
        """
        pos = _claiming()
        with self.assertNumQueries(0, using=settings.ATOMICDB_DATABASE_ALIAS):
            self.assertIsNotNone(ingest._short_mate_clamp(pos))


class BudgetForCarveOutTests(TestCase):
    """El salto de banda cede SOLO ante una distancia corta y conocida."""

    def test_a_short_claim_replaces_the_band_jump(self):
        pos = _claiming(moves=2)
        self.assertEqual(ingest.budget_for(pos), 3 * ingest.MATE_CLAMP_PER_PLY)
        self.assertLess(ingest.budget_for(pos), ingest.BUDGET_LADDER[2])

    def test_a_long_claim_keeps_the_band_jump(self):
        pos = _claiming(moves=30)
        self.assertEqual(ingest.budget_for(pos), ingest.BUDGET_LADDER[2])

    def test_the_band_without_distance_keeps_the_band_jump(self):
        pos = _position(CLAIM_FEN, eval_cp=9_500)
        self.assertEqual(ingest.budget_for(pos), ingest.BUDGET_LADDER[2])

    def test_the_visit_ladder_takes_over_when_it_asks_for_more(self):
        """Dos sondas baratas que no cierran el nodo escalan solas."""
        pos = _claiming(moves=2, visits=3)
        self.assertEqual(ingest.budget_for(pos), ingest.BUDGET_LADDER[3])

    def test_nothing_changes_outside_the_band(self):
        pos = _position(CLAIM_FEN, eval_cp=300, visits=1)
        self.assertEqual(ingest.budget_for(pos), ingest.BUDGET_LADDER[1])


class MultipvPolicyTests(TestCase):
    """MultiPV 1 exactamente cuando lo que se compra es la verificacion."""

    def test_the_verification_asks_for_one_line(self):
        clamp = (12_000_000, 1)
        self.assertEqual(ingest.multipv_for(0, 12_000_000, clamp=clamp), 1)

    def test_a_bigger_budget_goes_back_to_the_house_policy(self):
        clamp = (12_000_000, 1)
        self.assertEqual(ingest.multipv_for(0, 128_000_000, clamp=clamp), 5)
        self.assertEqual(ingest.multipv_for(4, 512_000_000, clamp=clamp),
                         ingest.DEPTH_MULTIPV)

    def test_seeding_still_wins_over_everything(self):
        clamp = (12_000_000, 1)
        self.assertEqual(
            ingest.multipv_for(0, 12_000_000, seeding=True, clamp=clamp), 5)

    def test_the_calls_without_a_clamp_are_untouched(self):
        self.assertEqual(ingest.multipv_for(0), 5)
        self.assertEqual(ingest.multipv_for(3), 3)
        self.assertEqual(ingest.multipv_for(0, 128_000_000), 5)


class SelectorTaskTests(TestCase):
    """La cola autonoma: que tarea sale para una reclamacion corta."""

    def _queued(self, **fields):
        pos = ingest.get_or_create_position(logic.start_fen())
        for name, value in fields.items():
            setattr(pos, name, value)
        pos.save()
        ingest._priority_refresh_cache['at'] = 0.0
        ingest.refresh_priorities()
        tasks = ingest.next_tasks(1)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].position_id, pos.key)
        return tasks[0]

    def test_a_short_mate_claim_buys_a_cheap_single_line_check(self):
        task = self._queued(eval_cp=_mate_eval(2))

        self.assertEqual(task.budget_nodes, 3 * ingest.MATE_CLAMP_PER_PLY)
        self.assertEqual(task.multipv, 1)

    def test_the_band_without_distance_still_digs_wide(self):
        task = self._queued(eval_cp=9_500)

        self.assertEqual(task.budget_nodes, ingest.BUDGET_LADDER[2])
        self.assertEqual(task.multipv, 5)

    def test_a_long_mate_still_digs_for_the_whole_pv(self):
        task = self._queued(eval_cp=_mate_eval(30))

        self.assertEqual(task.budget_nodes, ingest.BUDGET_LADDER[2])
        self.assertEqual(task.multipv, 5)


class RequestedTaskTests(TestCase):
    """El click humano: verificacion barata, y escalada cuando toca."""

    def _pos(self, **fields):
        pos = ingest.get_or_create_position(logic.start_fen())
        for name, value in fields.items():
            setattr(pos, name, value)
        pos.save()
        return pos

    def test_a_click_on_a_short_mate_verifies_instead_of_digging(self):
        pos = self._pos(eval_cp=_mate_eval(2))

        ingest.request_analysis(pos)

        task = AnalysisTask.objects.get(position=pos)
        self.assertEqual(task.budget_nodes, 3 * ingest.MATE_CLAMP_PER_PLY)
        self.assertEqual(task.multipv, 1)
        self.assertEqual(task.source, 'USER')

    def test_a_click_on_the_band_without_distance_keeps_the_floor(self):
        pos = self._pos(eval_cp=9_500)

        ingest.request_analysis(pos)

        task = AnalysisTask.objects.get(position=pos)
        self.assertEqual(task.budget_nodes, ingest.REQUEST_BUDGET_LADDER[0])
        self.assertEqual(task.multipv, 5)

    def test_a_pending_request_for_more_is_never_lowered(self):
        """La promocion de presupuesto sigue siendo monotona AL ALZA."""
        pos = self._pos(eval_cp=_mate_eval(2))
        AnalysisTask.objects.create(
            position=pos, generation=0, multipv=2,
            budget_nodes=ingest.REQUEST_BUDGET_LADDER[1], source='AUTO')

        ingest.request_analysis(pos)

        task = AnalysisTask.objects.get(position=pos)
        self.assertEqual(task.budget_nodes, ingest.REQUEST_BUDGET_LADDER[1])
        self.assertEqual(task.multipv, 2)        # tampoco se estrecha
        self.assertEqual(task.source, 'USER')

    def test_a_cheap_seed_does_not_force_the_request_floor(self):
        """Con 8M gastados, la verificacion sigue siendo lo que falta."""
        pos = self._pos(eval_cp=_mate_eval(2), visits=1)
        AnalysisTask.objects.create(
            position=pos, generation=0, multipv=2, state='COMPLETED',
            budget_nodes=ingest.COVERAGE_SEED_NODES, source='FILL')

        ingest.request_analysis(pos)

        task = AnalysisTask.objects.get(position=pos, state='PENDING')
        self.assertLess(task.budget_nodes, ingest.REQUEST_BUDGET_LADDER[0])

    def test_the_ladder_recovers_control_once_the_check_ran(self):
        """El clamp abarata la primera mirada; no es un techo permanente."""
        pos = self._pos(eval_cp=_mate_eval(2), visits=1)
        AnalysisTask.objects.create(
            position=pos, generation=0, multipv=1, state='COMPLETED',
            budget_nodes=3 * ingest.MATE_CLAMP_PER_PLY, source='USER')

        ingest.request_analysis(pos)

        task = AnalysisTask.objects.get(position=pos, state='PENDING')
        self.assertEqual(task.budget_nodes, ingest.REQUEST_BUDGET_LADDER[0])
        self.assertEqual(task.multipv, 5)


class UnexploredChildrenTests(TestCase):
    """El boton masivo sigue comprando sondas de grado peticion."""

    def test_children_without_information_keep_the_request_floor(self):
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        children = ingest.unexplored_children(parent)
        self.assertTrue(children)

        queued = ingest.enqueue_unexplored_children(parent)

        self.assertEqual(queued, len(children))
        tasks = AnalysisTask.objects.filter(
            position_id__in=[child.key for child in children])
        self.assertEqual(tasks.count(), len(children))
        for task in tasks:
            self.assertEqual(task.budget_nodes,
                             ingest.REQUEST_BUDGET_LADDER[0])
            self.assertEqual(task.multipv, 5)

    def test_a_child_that_already_claims_a_short_mate_is_verified(self):
        """Guarda estructural: si llega distancia, no se compra excavacion.

        Hoy ``unexplored_children`` exige hijos sin eval ni respaldo, asi que
        la distancia entra solo por ``mate_in``; el punto del test es que la
        politica no dependa de ese filtro.
        """
        parent = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(parent)
        edge = Edge.objects.select_related('child').filter(
            parent=parent).order_by('id').first()
        child = edge.child
        child.mate_in = 3
        child.save(update_fields=['mate_in'])

        ingest.enqueue_unexplored_children(parent)

        task = AnalysisTask.objects.get(position=child)
        self.assertEqual(task.budget_nodes, 3 * ingest.MATE_CLAMP_PER_PLY)
        self.assertEqual(task.multipv, 1)


class WitnessRefutedTests(TestCase):
    """Comprar poco es seguro porque la refutacion re-arma la excavacion."""

    def test_a_refuted_witness_still_buys_the_deep_revisit(self):
        pos = _claiming(moves=2)

        task = ingest._queue_disputed_reanalysis(pos)

        self.assertEqual(task.budget_nodes, ingest.BUDGET_LADDER[-1])
        self.assertEqual(task.multipv, ingest.DEPTH_MULTIPV)

    def test_it_promotes_a_clamped_task_upwards(self):
        pos = _claiming(moves=2)
        clamped = AnalysisTask.objects.create(
            position=pos, generation=0, source='AUTO',
            budget_nodes=ingest._short_mate_clamp(pos)[0], multipv=1)

        task = ingest._queue_disputed_reanalysis(pos)

        self.assertEqual(task.pk, clamped.pk)
        self.assertEqual(task.budget_nodes, ingest.BUDGET_LADDER[-1])
        self.assertEqual(task.multipv, ingest.DEPTH_MULTIPV)
