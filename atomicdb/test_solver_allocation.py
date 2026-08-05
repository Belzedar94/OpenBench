"""Politica de asignacion del solver: campanas bajo ``pn`` y clamp de OR.

Las dos reglas que fija este fichero salen del mismo principio del
propietario — "si se establece una campana, el auto deberia ir por esos
arboles" — y del mismo hallazgo: con ``ATOMICDB_SELECTOR=pn`` el trabajo lo
reparte el descenso df-pn, que no lee ``Position.priority``, que es donde
vivia el unico peso de las campanas.

Lo que se comprueba aqui, y por que cada cosa:

* que una campana ACTIVE recibe SU CUOTA de descensos (estadistico sobre
  contadores fijos: el sorteo es determinista a proposito, asi que esto no es
  un test con suerte sino una cuenta reproducible);
* que sin campanas ACTIVE la cola es la de siempre, arranque a arranque —
  el "no cambia nada si no lo enciendes" escrito como test y no como promesa;
* que el clamp de nodos OR abarata a los hermanos de un hijo ya PROBADO y NO
  toca un nodo AND, donde hay que refutar todas las respuestas;
* que los dos conmutadores estan cableados al ENTORNO y no solo a settings:
  la leccion de ``ATOMICDB_SELECTOR_DELTA`` es que un kill-switch que no se
  puede accionar no es un kill-switch.

El diseno completo, con los numeros de la auditoria del 5-ago, en
docs/solver-allocation.md.
"""

import hashlib
import os
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import ingest, logic, proof
from .models import AnalysisTask, Campaign, Edge, Position, ProofCampaign
from .testing import TestCase

COUNTERS = 4_000        # muestra del sorteo determinista
SHARE_DELTA = 0.02      # tolerancia estadistica sobre esa muestra


def _line(*ucis):
    """Materializa una linea desde la raiz y devuelve su ultima posicion."""
    parent = ingest.get_or_create_position(logic.start_fen())
    for uci in ucis:
        child = ingest.get_or_create_position(
            logic.apply_move(parent.fen, uci))
        Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                   defaults={'child': child})
        parent = child
    return parent


def _campaign_on(position, state=Campaign.CState.ACTIVE, votes=0, name=None):
    """Una campana ya montada sobre ``position``, sin pasar por la vista."""
    return Campaign.objects.create(
        name=name or 'campana %s' % position.key[:8], root=position,
        line_san='1. Nf3', state=state, votes=votes,
        active=state == Campaign.CState.ACTIVE)


def _share(roots=None):
    """Fraccion de los contadores de la muestra que arranca en una campana."""
    started = sum(1 for counter in range(COUNTERS)
                  if proof.campaign_start(counter, roots) is not None)
    return started / COUNTERS


def _share_of(root, roots=None):
    """La misma fraccion, pero de UNA raiz concreta."""
    started = 0
    for counter in range(COUNTERS):
        start = proof.campaign_start(counter, roots)
        if start is not None and start.key == root.key:
            started += 1
    return started / COUNTERS


class AllocationSwitchTests(SimpleTestCase):
    """Los dos conmutadores, cableados de settings al ENTORNO.

    Mismo patron que ``SelectorDeltaSwitchTests``: la expresion de settings se
    evalua UNA vez al arrancar el proceso, asi que la unica forma de ver el
    mapeo desde un test es reimportar el modulo con el entorno puesto.
    """

    def _mapped(self, name, value):
        import importlib
        import sys

        with patch.dict(os.environ, {name: value}, clear=False):
            module = importlib.reload(sys.modules['OpenSite.settings'])
            try:
                return getattr(module, name)
            finally:
                importlib.reload(sys.modules['OpenSite.settings'])

    def test_both_default_to_on(self):
        self.assertTrue(self._mapped('ATOMICDB_CAMPAIGN_DESCENT', ''))
        self.assertTrue(self._mapped('ATOMICDB_OR_CLAMP', ''))
        self.assertTrue(proof.campaign_descent_enabled())
        self.assertTrue(proof.or_clamp_enabled())

    def test_the_environment_can_turn_them_off(self):
        for value in ('0', 'false', 'no', 'FALSE', 'No'):
            self.assertFalse(self._mapped('ATOMICDB_CAMPAIGN_DESCENT', value),
                             msg='{!r} tenia que apagarlo'.format(value))
            self.assertFalse(self._mapped('ATOMICDB_OR_CLAMP', value),
                             msg='{!r} tenia que apagarlo'.format(value))

    def test_anything_that_is_not_a_no_keeps_them_on(self):
        """Un valor raro no puede apagar lo que esta desplegado."""
        for value in ('1', 'true', 'yes', 'si', 'campaign'):
            self.assertTrue(self._mapped('ATOMICDB_CAMPAIGN_DESCENT', value))
            self.assertTrue(self._mapped('ATOMICDB_OR_CLAMP', value))


class DeterministicDrawTests(SimpleTestCase):
    """El sorteo: reproducible, con dominios separados, y sin tocar el 80/15/5."""

    def test_the_historic_hash_of_the_repertoire_is_untouched(self):
        """El reparto blando tiene que salir del MISMO byte que ayer."""
        for counter in range(200):
            digest = hashlib.sha256(str(counter).encode()).digest()
            self.assertEqual(
                proof._point(counter),
                int.from_bytes(digest[:8], 'big') / float(1 << 64))

    def test_the_campaign_draw_lives_in_another_domain(self):
        """Correlacionados, el 35% caeria siempre sobre los mismos descensos."""
        for counter in range(200):
            self.assertNotEqual(proof._point(counter),
                                proof._point(counter, 'campaign:'))

    def test_the_draw_is_deterministic_not_random(self):
        first = [proof._point(i, 'campaign:') for i in range(50)]
        second = [proof._point(i, 'campaign:') for i in range(50)]
        self.assertEqual(first, second)


class CampaignShareTests(TestCase):
    """Cuanto le toca a una campana, y a quien no le toca nada."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.line = _line('g1f3', 'f7f6')

    def test_without_active_campaigns_every_descent_starts_at_the_root(self):
        """El caso "no has encendido nada": cero cambios, contador a contador."""
        _campaign_on(self.line, state=Campaign.CState.PROPOSED, votes=9)
        _campaign_on(_line('e2e4'), state=Campaign.CState.PAUSED, votes=9,
                     name='pausada')
        self.assertEqual(proof.campaign_roots(), [])
        for counter in range(COUNTERS):
            self.assertIsNone(proof.campaign_start(counter))

    def test_an_active_campaign_takes_the_capped_share(self):
        _campaign_on(self.line, votes=4)
        self.assertAlmostEqual(_share(), proof.CAMPAIGN_DESCENT_SHARE,
                               delta=SHARE_DELTA)

    def test_an_active_campaign_without_votes_is_not_inert(self):
        """Activar es el voto del propietario: ``ln(1+0)`` no puede valer cero."""
        _campaign_on(self.line, votes=0)
        self.assertAlmostEqual(_share(), proof.CAMPAIGN_DESCENT_SHARE,
                               delta=SHARE_DELTA)

    def test_two_campaigns_split_the_cap_by_log_votes(self):
        import math

        loud = _campaign_on(self.line, votes=20, name='muy votada')
        quiet = _campaign_on(_line('e2e4'), votes=1, name='poco votada')
        total = math.log1p(20) + math.log1p(1)
        roots = proof.campaign_roots()

        self.assertAlmostEqual(_share(roots), proof.CAMPAIGN_DESCENT_SHARE,
                               delta=SHARE_DELTA)
        self.assertAlmostEqual(
            _share_of(loud.root, roots),
            proof.CAMPAIGN_DESCENT_SHARE * math.log1p(20) / total,
            delta=SHARE_DELTA)
        self.assertAlmostEqual(
            _share_of(quiet.root, roots),
            proof.CAMPAIGN_DESCENT_SHARE * math.log1p(1) / total,
            delta=SHARE_DELTA)

    def test_the_cap_holds_with_many_campaigns(self):
        """Tres campanas muy votadas no pueden quedarse con el solver."""
        for index, uci in enumerate(('e2e4', 'd2d4', 'c2c4')):
            _campaign_on(_line(uci), votes=50 + index, name='campana %d' % index)
        self.assertAlmostEqual(_share(), proof.CAMPAIGN_DESCENT_SHARE,
                               delta=SHARE_DELTA)

    def test_a_closed_campaign_root_is_not_a_starting_point(self):
        campaign = _campaign_on(self.line, votes=4)
        Position.objects.filter(key=campaign.root_id).update(
            status='WHITE_WIN', closure='MINIMAX')
        self.assertEqual(proof.campaign_roots(), [])
        self.assertEqual(_share(), 0.0)

    @override_settings(ATOMICDB_CAMPAIGN_DESCENT=False)
    def test_the_switch_brings_the_global_root_back(self):
        _campaign_on(self.line, votes=4)
        self.assertFalse(proof.campaign_descent_enabled())
        self.assertEqual(proof.campaign_roots(), [])
        self.assertEqual(_share(), 0.0)

    def test_a_descent_that_starts_at_a_campaign_stays_under_it(self):
        campaign = ProofCampaign.objects.get(name=proof.DEFAULT_CAMPAIGN_NAME)
        found, _plies = proof.descend(campaign, counter=0, start=self.line)
        self.assertIsNotNone(found)
        self.assertEqual(found.key, self.line.key)


@override_settings(ATOMICDB_SELECTOR='pn')
class CampaignMintingTests(TestCase):
    """El cableado de punta a punta: de donde arranca el descenso que mintea."""

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.line = _line('g1f3', 'f7f6')

    def _starts(self, wanted=4):
        """Los ``start`` con los que ``_next_tasks_by_proof`` llamo al descenso."""
        seen = []
        original = proof.descend

        def spy(campaign, **kwargs):
            seen.append(kwargs.get('start'))
            return original(campaign, **kwargs)

        with patch.object(proof, 'descend', side_effect=spy):
            ingest.next_tasks(wanted)
        return seen

    def test_with_no_campaigns_every_descent_starts_at_the_global_root(self):
        starts = self._starts()
        self.assertTrue(starts)
        self.assertEqual(starts, [None] * len(starts))
        self.assertFalse(AnalysisTask.objects.exclude(arm='').exists())

    def test_a_campaign_descent_marks_its_tasks(self):
        _campaign_on(self.line, votes=4)
        with patch.object(proof, 'campaign_start', return_value=self.line):
            tasks = ingest.next_tasks(1)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].position_id, self.line.key)
        self.assertEqual(tasks[0].arm, ingest.CAMPAIGN_ARM)

    def test_a_normal_descent_is_not_marked(self):
        _campaign_on(self.line, votes=4)
        with patch.object(proof, 'campaign_start', return_value=None):
            tasks = ingest.next_tasks(1)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].arm, '')


class OrClampTests(TestCase):
    """El presupuesto minimo de los hermanos, y donde NO se aplica.

    La raiz (blancas al turno) es un nodo OR de ``root-white-win``; sus hijos
    (negras al turno) son nodos AND.  Con eso el fixture tiene los dos casos
    sin inventar ninguna posicion.
    """

    def setUp(self):
        self.campaign = ProofCampaign.objects.get(
            name=proof.DEFAULT_CAMPAIGN_NAME)
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        edges = list(Edge.objects.filter(parent=self.root).order_by('id'))
        self.winner = edges[0].child
        self.sibling = edges[1].child

    def _prove(self, position, status='WHITE_WIN'):
        Position.objects.filter(key=position.key).update(
            status=status, closure='MINIMAX')

    def _clamp(self, position, campaigns=None):
        return proof.proved_or_clamp(
            position.key, [self.campaign] if campaigns is None else campaigns)

    def test_without_a_proven_child_nothing_is_clamped(self):
        self.assertIsNone(self._clamp(self.sibling))

    def test_a_sibling_of_a_proven_child_falls_to_the_minimum(self):
        self._prove(self.winner)
        self.assertEqual(self._clamp(self.sibling),
                         (proof.OR_CLAMP_NODES, proof.OR_CLAMP_MULTIPV))

    def test_the_minimum_is_far_below_the_first_rung(self):
        """La constante existe para ser MUCHO mas barata, no un poco."""
        self.assertLess(proof.OR_CLAMP_NODES * 4, ingest.BUDGET_LADDER[0])

    def test_a_refuted_child_does_not_clamp_anybody(self):
        """Que un hermano PIERDA no prueba nada en un nodo OR."""
        self._prove(self.winner, status='BLACK_WIN')
        self.assertIsNone(self._clamp(self.sibling))

    def test_an_and_node_is_not_touched(self):
        """En un nodo AND hay que refutar TODAS las respuestas del defensor."""
        ingest.expand(self.winner)
        answers = list(Edge.objects.filter(parent=self.winner).order_by('id'))
        self._prove(answers[0].child)

        self.assertIsNone(self._clamp(answers[1].child))

    def test_a_parent_that_still_needs_it_keeps_the_budget(self):
        """El DAG transpone: basta un padre vivo para pagar el peldano."""
        self._prove(self.winner)
        Edge.objects.create(parent=self.winner, move_uci='a7a6',
                            child=self.sibling)
        Position.objects.filter(key=self.winner.key).update(status='UNKNOWN',
                                                            closure=None)

        self.assertIsNone(self._clamp(self.sibling))

    def test_a_closed_parent_does_not_keep_it_alive(self):
        """Un padre cerrado ya no influye hacia arriba (§ _still_reachable)."""
        self._prove(self.winner)
        other = _line('e2e4', 'e7e5')
        Edge.objects.create(parent=other, move_uci='a7a6', child=self.sibling)
        Position.objects.filter(key=other.key).update(status='DRAW',
                                                      closure='MINIMAX')

        self.assertEqual(self._clamp(self.sibling),
                         (proof.OR_CLAMP_NODES, proof.OR_CLAMP_MULTIPV))

    def test_another_campaign_that_still_needs_it_keeps_the_budget(self):
        self._prove(self.winner)
        other = ProofCampaign.objects.create(
            name='root-black-win', root=self.root,
            goal=ProofCampaign.Goal.BLACK_WIN)

        self.assertIsNone(self._clamp(self.sibling,
                                      campaigns=[self.campaign, other]))

    @override_settings(ATOMICDB_OR_CLAMP=False)
    def test_the_switch_gives_the_ladder_back(self):
        self._prove(self.winner)
        self.assertIsNone(self._clamp(self.sibling))


@override_settings(ATOMICDB_SELECTOR='pn')
class OrClampMintingTests(TestCase):
    """Lo que acaba comprando el worker, que es lo unico que gasta nodos."""

    def setUp(self):
        self.campaign = ProofCampaign.objects.get(
            name=proof.DEFAULT_CAMPAIGN_NAME)
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        edges = list(Edge.objects.filter(parent=self.root).order_by('id'))
        self.winner = edges[0].child
        self.sibling = edges[1].child

    def _mint(self):
        with patch.object(proof, 'descend', return_value=(self.sibling, 1)):
            return ingest.next_tasks(1)

    def test_the_minted_task_carries_the_minimum(self):
        Position.objects.filter(key=self.winner.key).update(
            status='WHITE_WIN', closure='MINIMAX')

        tasks = self._mint()

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].budget_nodes, proof.OR_CLAMP_NODES)
        self.assertEqual(tasks[0].multipv, proof.OR_CLAMP_MULTIPV)

    def test_the_clamp_beats_the_ladder_and_the_mate_carve_out(self):
        """Ni las visitas acumuladas ni un mate reclamado lo reabren.

        El carve-out de mate corto abarata la VERIFICACION de un nodo que a la
        prueba todavia le importa; aqui a la prueba ya no le importa el nodo.
        """
        Position.objects.filter(key=self.winner.key).update(
            status='WHITE_WIN', closure='MINIMAX')
        Position.objects.filter(key=self.sibling.key).update(visits=3,
                                                             mate_in=3)
        self.sibling.refresh_from_db()
        self.assertGreater(ingest.budget_for(self.sibling),
                           proof.OR_CLAMP_NODES)

        tasks = self._mint()

        self.assertEqual(tasks[0].budget_nodes, proof.OR_CLAMP_NODES)

    def test_without_a_proven_sibling_the_ladder_still_rules(self):
        tasks = self._mint()

        self.assertEqual(tasks[0].budget_nodes, ingest.BUDGET_LADDER[0])
        self.assertEqual(tasks[0].multipv, 5)
