"""El descenso por VALOR: bajar hasta la jugada que sostiene el numero.

EL CASO QUE LO PIDIO, medido en produccion el 11-ago-2026 sobre la campana
WHITE_WIN desde startpos.  El descenso df-pn ordena por ``pn``, y en la raiz
``1.e3`` vale 119 frente a los 475 de ``g1f3`` — que es, por respaldo, la mejor
jugada del tablero (+812).  Resultado real: 26 de las ultimas 40 tareas AUTO
bajo ``1.e3``, UNA bajo ``g1f3``, y las 40 al primer peldano de la escalera de
exploracion (8M).  A esta distancia de la prueba los pn son ficcion y su minimo
degenera en la linea floja mas barata de enumerar.

Lo que clavan estos tests, y por que cada cosa:

* que con el walker encendido la raiz REAL manda por ``g1f3`` y el descenso de
  siempre sigue mandando por ``1.e3``: el mismo arbol, los dos veredictos, en
  el mismo test — es el recibo del cambio, no una promesa;
* que el paseo termina en la hoja SIN respaldo, que es la que sostiene el
  valor, y que esa hoja entra por el primer peldano de PETICIONES y no por el
  de exploracion;
* las tres formas de la revisita — cuello de botella, peldano mas hondo y
  desambiguacion de ciclo — que son lo que impide que el walker vuelva
  eternamente a la misma punta;
* que el contrato de reserva se respeta con un DESVIO y no con un duplicado;
* que una campana de la comunidad es el MISMO bucle con otra raiz: espina,
  punta, cuello y peldano se resuelven DENTRO de su subarbol, que es lo unico
  que hace que votar una linea signifique algo;
* que la cascada corta tiene cupo y marca;
* y que los clamps de presupuesto siguen mandando por encima del walker, que
  es lo que hace que este paquete no pueda encarecer nada que la prueba ya
  haya dejado de necesitar.

Todo ello con ``ATOMICDB_DESCENT`` puesto a mano: sin esa variable el codigo
sigue siendo el df-pn de siempre, y esa es la mitad del diseno que se prueba
en ``ValueDescentSwitchTests``.
"""

import hashlib
import os
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import ingest, proof
from .models import (AnalysisTask, DBEvent, Edge, Position, ProofCampaign,
                     ProofNode)
from .testing import TestCase

VALUE = {'ATOMICDB_DESCENT': 'value'}
PRIMARY_ONLY = {'primary': 1.0, 'backup': 0.0, 'explore': 0.0}
BACKUP_ONLY = {'primary': 0.0, 'backup': 1.0, 'explore': 0.0}
EXPLORE_ONLY = {'primary': 0.0, 'backup': 0.0, 'explore': 1.0}


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **fields):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **fields)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _campaign(root, name='value-test', policy=None):
    """Una campana de prueba sobre ``root``, con la de por defecto apagada.

    La campana raiz la crea una migracion de datos y esta ACTIVE: dejarla
    encendida haria que el minteo alternase entre startpos y el arbol
    sintetico de cada test, que es ruido y no cobertura.
    """
    ProofCampaign.objects.filter(active=True).update(active=False)
    return ProofCampaign.objects.create(
        name=name, root=root, goal=ProofCampaign.Goal.WHITE_WIN,
        algorithm_version=proof.ALGORITHM_VERSION,
        repertoire_policy=dict(policy or PRIMARY_ONLY))


def _spine(name, plies, tip_fields=None):
    """Una espina de ``plies`` aristas encadenadas por ``backed_move``.

    Devuelve ``(raiz, [nodos...])``.  El ultimo NO tiene respaldo: es la hoja
    cuyo eval crudo sostiene el valor de todo lo de arriba, y por tanto la
    punta que el walker tiene que encontrar.
    """
    nodes = []
    for index in range(plies + 1):
        stm = 'w' if index % 2 == 0 else 'b'
        last = index == plies
        fields = {'eval_cp': 800}
        if last:
            fields.update(tip_fields or {})
        else:
            fields.update({'backed_eval': 812, 'backed_move': f'm{index}',
                           'expanded': True})
        nodes.append(_pos(f'{name}-{index}', stm, **fields))
    for index in range(plies):
        _edge(nodes[index], nodes[index + 1], f'm{index}')
    return nodes[0], nodes


class ValueDescentSwitchTests(SimpleTestCase):
    """El conmutador, cableado de settings al ENTORNO.

    Mismo patron que ``AllocationSwitchTests``: la expresion de settings se
    evalua UNA vez al arrancar el proceso, asi que la unica forma de ver el
    mapeo desde un test es reimportar el modulo con el entorno puesto.  La
    leccion de ``ATOMICDB_SELECTOR_DELTA`` es que un conmutador que no se
    puede accionar desde el despliegue no es un conmutador.
    """

    def _mapped(self, value):
        import importlib
        import sys

        with patch.dict(os.environ, {'ATOMICDB_DESCENT': value}, clear=False):
            module = importlib.reload(sys.modules['OpenSite.settings'])
            try:
                return module.ATOMICDB_DESCENT
            finally:
                importlib.reload(sys.modules['OpenSite.settings'])

    def test_the_default_is_still_the_proof_descent(self):
        self.assertEqual(proof.descent_mode(), proof.DESCENT_PROOF)

    def test_the_environment_turns_the_walker_on(self):
        self.assertEqual(self._mapped('value'), 'value')
        with override_settings(ATOMICDB_DESCENT='value'):
            self.assertEqual(proof.descent_mode(), proof.DESCENT_VALUE)

    def test_anything_else_keeps_the_proof_descent(self):
        """Un entorno mal escrito no puede estrenar motor por accidente."""
        for value in ('', 'proof', 'values', 'pn', '1', 'true'):
            with override_settings(ATOMICDB_DESCENT=value):
                self.assertEqual(proof.descent_mode(), proof.DESCENT_PROOF,
                                 msg=f'{value!r} tenia que dejar df-pn')


@override_settings(**VALUE)
class ProductionRootTests(TestCase):
    """La raiz REAL del 11-ago, con los dos descensos delante."""

    def setUp(self):
        # ``1.e3``: barata de PROBAR (pn bajo) y mediocre.  ``g1f3``: cara de
        # probar y la que sostiene el +812 de la raiz.
        self.root = _pos('PR-root', 'w', eval_cp=40, backed_eval=812,
                         backed_move='g1f3', expanded=True)
        self.e3 = _pos('PR-e3', 'b', eval_cp=20)
        self.nf3 = _pos('PR-nf3', 'b', eval_cp=812)
        _edge(self.root, self.e3, 'e2e3')
        _edge(self.root, self.nf3, 'g1f3')
        self.campaign = _campaign(self.root)
        ProofNode.objects.create(campaign=self.campaign, position=self.root,
                                 pn=94, dn=2)
        ProofNode.objects.create(campaign=self.campaign, position=self.e3,
                                 pn=119, dn=1)
        ProofNode.objects.create(campaign=self.campaign, position=self.nf3,
                                 pn=475, dn=1)

    def test_the_proof_descent_still_goes_for_the_cheap_line(self):
        """El recibo del problema: df-pn se va por el pn menor, o sea 1.e3."""
        found, _plies = proof.descend(self.campaign, counter=0)
        self.assertEqual(found.key, self.e3.key)

    def test_the_value_descent_goes_for_the_backed_move(self):
        target = proof.descend_value(self.campaign, counter=0)
        self.assertIsNotNone(target)
        self.assertEqual(target.position.key, self.nf3.key)
        self.assertEqual(target.arm, proof.VALUE_ARM_SPINE)

    def test_the_minted_task_lands_under_the_backed_move(self):
        """Y el cambio llega a la COLA, que es donde se veia el sintoma."""
        tasks = ingest._next_tasks_by_proof(1)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].position_id, self.nf3.key)
        self.assertEqual(tasks[0].budget_nodes, ingest.VALUE_SPINE_NODES)

    @override_settings(ATOMICDB_DESCENT='proof')
    def test_with_the_switch_off_the_queue_is_the_one_from_yesterday(self):
        tasks = ingest._next_tasks_by_proof(1)
        self.assertEqual(tasks[0].position_id, self.e3.key)
        self.assertEqual(tasks[0].budget_nodes, ingest.BUDGET_LADDER[0])


@override_settings(**VALUE)
class SpineWalkTests(TestCase):
    """La espina completa: se baja entera y se compra en la punta."""

    def test_the_walk_ends_on_the_leaf_without_backing(self):
        root, nodes = _spine('SW', 4)
        campaign = _campaign(root)

        target = proof.descend_value(campaign, counter=0)

        self.assertEqual(target.position.key, nodes[-1].key)
        self.assertEqual(target.plies, 4)
        self.assertEqual(target.arm, proof.VALUE_ARM_SPINE)

    def test_the_tip_is_bought_at_the_request_rung_not_the_explore_one(self):
        """8M en la jugada que sostiene un +812 no mueve el numero."""
        root, nodes = _spine('SB', 4)
        campaign = _campaign(root)

        target = proof.descend_value(campaign, counter=0)

        self.assertEqual(ingest.value_budget(target),
                         ingest.REQUEST_BUDGET_LADDER[0])
        self.assertGreater(ingest.value_budget(target),
                           ingest.BUDGET_LADDER[0])

    def test_a_closed_root_yields_nothing(self):
        root, _nodes = _spine('SC', 2)
        Position.objects.filter(key=root.key).update(status='WHITE_WIN',
                                                     closure='MINIMAX')
        campaign = _campaign(root)
        campaign.refresh_from_db()

        self.assertIsNone(proof.descend_value(campaign, counter=0))


@override_settings(**VALUE)
class SaturatedTipTests(TestCase):
    """Una punta ya comprada no se recompra: o el cuello, o mas hondo."""

    def test_the_target_climbs_to_the_node_with_unjudged_replies(self):
        root, nodes = _spine(
            'BT', 2, tip_fields={'nodes_invested': ingest.VALUE_SPINE_NODES})
        # Una respuesta del nodo intermedio que nadie ha juzgado: ni status,
        # ni eval de ninguna procedencia, ni respaldo.  Ese es el cuello.
        spare = _pos('BT-spare', 'w')
        _edge(nodes[1], spare, 'z7z7')
        campaign = _campaign(root)

        target = proof.descend_value(campaign, counter=0)

        self.assertEqual(target.position.key, nodes[1].key)
        self.assertEqual(target.arm, proof.VALUE_ARM_BOTTLENECK)
        self.assertEqual(ingest.value_budget(target), ingest.VALUE_SPINE_NODES)

    def test_a_spine_without_bottlenecks_buys_the_next_rung(self):
        root, nodes = _spine(
            'NR', 2, tip_fields={'nodes_invested': ingest.VALUE_SPINE_NODES})
        campaign = _campaign(root)

        target = proof.descend_value(campaign, counter=0)

        self.assertEqual(target.position.key, nodes[-1].key)
        self.assertEqual(target.arm, proof.VALUE_ARM_RUNG)
        self.assertEqual(ingest.value_budget(target),
                         ingest.REQUEST_BUDGET_LADDER[1])


@override_settings(**VALUE)
class CyclingSpineTests(TestCase):
    """1.Nf3 Nf6 2.Ng1 Ng8 ES startpos: la espina puede morderse la cola."""

    def test_the_pre_cycle_node_buys_a_deeper_pass(self):
        root = _pos('CY-root', 'w', eval_cp=800, backed_eval=812,
                    backed_move='m0', expanded=True,
                    nodes_invested=ingest.BUDGET_LADDER[0])
        mid = _pos('CY-mid', 'b', eval_cp=800, backed_eval=812,
                   backed_move='m1', expanded=True)
        _edge(root, mid, 'm0')
        _edge(mid, root, 'm1')          # la espina vuelve sobre la raiz
        campaign = _campaign(root)

        target = proof.descend_value(campaign, counter=0)

        self.assertEqual(target.position.key, mid.key)
        self.assertEqual(target.arm, proof.VALUE_ARM_CYCLE)
        # Repetir el peldano del hijo que cierra el bucle reproduciria la
        # misma espina; el siguiente re-ordena de verdad.
        self.assertGreater(ingest.value_budget(target),
                           ingest.BUDGET_LADDER[0])
        self.assertEqual(ingest.value_budget(target), ingest.BUDGET_LADDER[1])


@override_settings(**VALUE)
class ReservationTests(TestCase):
    """La reserva del lote se respeta con un DESVIO, no con un duplicado."""

    def setUp(self):
        self.root = _pos('RS-root', 'w', eval_cp=800, backed_eval=800,
                         backed_move='m0', expanded=True)
        self.tip = _pos('RS-tip', 'b', eval_cp=800)
        self.alt = _pos('RS-alt', 'b', eval_cp=700)      # a 100cp: compite
        self.far = _pos('RS-far', 'b', eval_cp=-400)     # a 1200cp: no compite
        _edge(self.root, self.tip, 'm0')
        _edge(self.root, self.alt, 'm1')
        _edge(self.root, self.far, 'm2')
        self.campaign = _campaign(self.root)

    def test_a_live_task_on_the_tip_diverts_to_the_alternative(self):
        AnalysisTask.objects.create(position=self.tip, generation=0,
                                    budget_nodes=ingest.VALUE_SPINE_NODES)

        target = proof.descend_value(self.campaign, counter=0)

        self.assertEqual(target.position.key, self.alt.key)

    def test_the_batch_reservation_moves_the_walk_too(self):
        target = proof.descend_value(self.campaign, counter=0,
                                     avoid={self.tip.key})
        self.assertEqual(target.position.key, self.alt.key)

    def test_a_walk_with_nothing_left_to_offer_comes_back_empty(self):
        """Sin alternativa competitiva libre no se entrega un duplicado."""
        target = proof.descend_value(
            self.campaign, counter=0,
            avoid={self.root.key, self.tip.key, self.alt.key})
        self.assertIsNone(target)

    def test_the_pool_never_hands_the_same_position_twice(self):
        tasks = ingest._next_tasks_by_proof(3)
        keys = [task.position_id for task in tasks]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn(self.tip.key, keys)

    def test_the_backup_bucket_spends_on_the_alternative(self):
        """El 15% re-significado: el desvio competitivo, a precio de desvio."""
        self.campaign.repertoire_policy = dict(BACKUP_ONLY)
        self.campaign.save(update_fields=['repertoire_policy'])

        target = proof.descend_value(self.campaign, counter=0)

        self.assertEqual(target.position.key, self.alt.key)
        self.assertEqual(target.arm, proof.VALUE_ARM_BACKUP)
        self.assertEqual(ingest.value_budget(target),
                         ingest.VALUE_BACKUP_NODES)

    def test_the_explore_bucket_never_buys_the_worst_child(self):
        """El 5% iba a ``ranked[-1]`` — el PEOR hijo — y ese era el defecto.

        Aqui los tres hijos estan sin explorar, asi que el brazo tiene donde
        elegir: elige el MEJOR para el que mueve, que es lo contrario de lo
        que hacia el descenso de siempre.  El -400 no se compra por ser el
        peor; se descarta por serlo.
        """
        self.campaign.repertoire_policy = dict(EXPLORE_ONLY)
        self.campaign.save(update_fields=['repertoire_policy'])

        target = proof.descend_value(self.campaign, counter=0)

        self.assertNotEqual(target.position.key, self.far.key)
        self.assertEqual(target.position.key, self.tip.key)

    def test_the_explore_bucket_buys_the_best_seeded_virgin(self):
        virgin = _pos('RS-virgin', 'w', eval_cp=-50)     # sembrado, sin medir
        better = _pos('RS-better', 'w', eval_cp=-900)
        _edge(self.tip, virgin, 'v1v1')
        _edge(self.tip, better, 'v2v2')
        self.campaign.repertoire_policy = dict(EXPLORE_ONLY)
        self.campaign.save(update_fields=['repertoire_policy'])

        target = proof.descend_value(self.campaign, counter=0)

        # En ``tip`` mueven negras: el mejor virgen PARA ELLAS es el -900.
        self.assertEqual(target.position.key, better.key)
        self.assertEqual(target.arm, proof.VALUE_ARM_EXPLORE)
        self.assertEqual(ingest.value_budget(target),
                         ingest.VALUE_EXPLORE_NODES)


@override_settings(**VALUE)
class PoolVarietyTests(TestCase):
    """Una espina y sesenta y cuatro huecos de colchon.

    Concentrar el gasto en la linea que decide es el objetivo entero de este
    paquete, pero un colchon a medio llenar deja a la flota mirando al techo,
    y eso es peor que la dispersion que venimos a quitar.  El desvio
    competitivo es lo que resuelve las dos cosas a la vez: variedad SIN salirse
    del entorno de la espina.
    """

    def test_a_batch_fills_with_distinct_positions_around_the_spine(self):
        root, nodes = _spine('PV', 6)
        for index, node in enumerate(nodes[:-1]):
            rival = _pos(f'PV-alt-{index}', 'b' if index % 2 == 0 else 'w',
                         eval_cp=750)          # a menos de peon y medio
            _edge(node, rival, f'a{index}')
        _campaign(root)

        tasks = ingest._next_tasks_by_proof(6)

        keys = [task.position_id for task in tasks]
        self.assertEqual(len(keys), 6)
        self.assertEqual(len(set(keys)), 6)
        self.assertEqual({task.budget_nodes for task in tasks},
                         {ingest.VALUE_SPINE_NODES})

    def test_a_far_alternative_is_not_variety_it_is_dispersion(self):
        """Lo que esta a mil cp de la primaria no compite: no se compra.

        Y "peor" se mide SIEMPRE para el que mueve: en un nodo de negras el
        hijo que no compite es el que les va horrible, o sea el de eval blanca
        alta.  Una alternativa que fuese MEJOR que la primaria si compite —
        entra por la misma desigualdad, con la diferencia negativa — y eso es
        deliberado: significa que el respaldo todavia no la ha alcanzado.
        """
        root, nodes = _spine('PD', 3)
        for index, node in enumerate(nodes[:-1]):
            white_moves = index % 2 == 0
            _edge(node, _pos(f'PD-far-{index}', 'b' if white_moves else 'w',
                             eval_cp=-400 if white_moves else 2_500),
                  f'f{index}')
        _campaign(root)

        tasks = ingest._next_tasks_by_proof(4)

        self.assertEqual([task.position_id for task in tasks],
                         [nodes[-1].key])


@override_settings(**VALUE)
class CampaignRootTests(TestCase):
    """Una campana de la comunidad es el MISMO bucle, con otra raiz.

    El principio del propietario, entero: si el descenso arranca en la raiz de
    una campana, el walker tiene que comportarse exactamente igual que desde
    startpos pero con ESA posicion como raiz del bucle — seguir su espina
    hasta la hoja que sostiene SU evaluacion, comprarla, y en el descenso
    siguiente releer la espina fresca desde la misma raiz para encontrar el
    cuello nuevo.  Lo que refina una campana es el numero de su raiz.

    Lo que este bloque impide es la version rota de eso: un walker que
    arranca donde le dicen pero razona sobre la espina GLOBAL acabaria
    comprando fuera del subarbol votado, que es exactamente el "no sirve de
    nada" que las campanas vienen a arreglar.
    """

    def setUp(self):
        # La espina GLOBAL, con un cuello propio bien visible: si el walker se
        # saliera del subarbol de la campana, aterrizaria aqui.
        self.root = _pos('CR-root', 'w', eval_cp=40, backed_eval=812,
                         backed_move='g0', expanded=True)
        self.global_mid = _pos('CR-gmid', 'b', eval_cp=812, backed_eval=812,
                               backed_move='g1', expanded=True)
        self.global_tip = _pos('CR-gtip', 'w', eval_cp=812)
        self.global_spare = _pos('CR-gspare', 'b')          # sin juzgar
        _edge(self.root, self.global_mid, 'g0')
        _edge(self.global_mid, self.global_tip, 'g1')
        _edge(self.root, self.global_spare, 'g9')
        # La raiz de la campana cuelga del mismo arbol y tiene SU espina.
        self.camp = _pos('CR-camp', 'b', eval_cp=500, backed_eval=520,
                         backed_move='c1', expanded=True)
        self.camp_mid = _pos('CR-cmid', 'w', eval_cp=520, backed_eval=520,
                             backed_move='c2', expanded=True)
        self.camp_tip = _pos('CR-ctip', 'b', eval_cp=520)
        _edge(self.root, self.camp, 'c0')
        _edge(self.camp, self.camp_mid, 'c1')
        _edge(self.camp_mid, self.camp_tip, 'c2')
        self.campaign = _campaign(self.root)

    def test_the_walk_follows_the_spine_of_that_root(self):
        target = proof.descend_value(self.campaign, counter=0,
                                     start=self.camp)

        self.assertEqual(target.position.key, self.camp_tip.key)
        self.assertEqual(target.arm, proof.VALUE_ARM_SPINE)
        self.assertEqual(target.plies, 2)
        self.assertEqual(ingest.value_budget(target), ingest.VALUE_SPINE_NODES)

    def test_without_a_start_the_same_campaign_walks_the_global_spine(self):
        """El contraste que hace util al test de arriba."""
        target = proof.descend_value(self.campaign, counter=0)

        self.assertEqual(target.position.key, self.global_tip.key)

    def test_a_saturated_tip_climbs_inside_the_campaign_subtree(self):
        Position.objects.filter(key=self.camp_tip.key).update(
            nodes_invested=ingest.VALUE_SPINE_NODES)
        spare = _pos('CR-cspare', 'b')      # el cuello, dentro de la campana
        _edge(self.camp_mid, spare, 'c9')

        target = proof.descend_value(self.campaign, counter=0,
                                     start=self.camp)

        self.assertEqual(target.position.key, self.camp_mid.key)
        self.assertEqual(target.arm, proof.VALUE_ARM_BOTTLENECK)
        self.assertNotIn(target.position.key,
                         {self.root.key, self.global_mid.key,
                          self.global_tip.key})

    def test_a_campaign_spine_without_bottlenecks_deepens_its_own_tip(self):
        """Y sin cuello propio profundiza SU punta, sin salir a buscar otra."""
        Position.objects.filter(key=self.camp_tip.key).update(
            nodes_invested=ingest.VALUE_SPINE_NODES)

        target = proof.descend_value(self.campaign, counter=0,
                                     start=self.camp)

        self.assertEqual(target.position.key, self.camp_tip.key)
        self.assertEqual(target.arm, proof.VALUE_ARM_RUNG)
        self.assertEqual(ingest.value_budget(target),
                         ingest.REQUEST_BUDGET_LADDER[1])


class ValueCascadeTests(TestCase):
    """La cascada corta: mirar barato las respuestas que nadie ha juzgado."""

    def setUp(self):
        self.node = _pos('VC-node', 'w', eval_cp=300, expanded=True)
        self.children = [_pos(f'VC-child-{index}', 'b')
                         for index in range(ingest.VALUE_CASCADE_CAP + 4)]
        for index, child in enumerate(self.children):
            _edge(self.node, child, f'c{index}')

    @override_settings(**VALUE)
    def test_the_cascade_is_capped_and_cheap_and_marked(self):
        made = ingest._queue_value_cascade(self.node)

        self.assertEqual(made, ingest.VALUE_CASCADE_CAP)
        tasks = AnalysisTask.objects.filter(arm=ingest.VALUE_CASCADE_ARM)
        self.assertEqual(tasks.count(), ingest.VALUE_CASCADE_CAP)
        self.assertEqual({task.budget_nodes for task in tasks},
                         {ingest.VALUE_CASCADE_NODES})
        self.assertTrue(DBEvent.objects.filter(kind='VALUE_CASCADE').exists())

    @override_settings(**VALUE)
    def test_the_quota_is_its_own_and_does_not_refill_while_it_is_full(self):
        ingest._queue_value_cascade(self.node)

        self.assertEqual(ingest._queue_value_cascade(self.node), 0)

    @override_settings(**VALUE)
    def test_a_seeded_reply_is_not_bought_again(self):
        """``is_unjudged``, no ``is_unexplored``: una siembra ya ordena."""
        Position.objects.filter(
            key__in=[child.key for child in self.children]).update(eval_cp=-20)

        self.assertEqual(ingest._queue_value_cascade(self.node), 0)

    def test_the_proof_descent_does_not_cascade(self):
        self.assertEqual(ingest._queue_value_cascade(self.node), 0)
        self.assertFalse(AnalysisTask.objects.exists())


@override_settings(**VALUE)
class ClampsStillWinTests(TestCase):
    """Los clamps de presupuesto mandan POR ENCIMA del objetivo primario."""

    def test_a_short_mate_claim_buys_its_verification_not_the_excavation(self):
        root = _pos('MC-root', 'w', eval_cp=9_900, backed_eval=9_900,
                    backed_move='m0', expanded=True)
        # Reclama M2 (3 plies): eso se verifica barato, no se excava.
        tip = _pos('MC-tip', 'b', eval_cp=9_900, mate_in=3)
        _edge(root, tip, 'm0')
        _campaign(root)

        tasks = ingest._next_tasks_by_proof(1)

        self.assertEqual(tasks[0].position_id, tip.key)
        self.assertLess(tasks[0].budget_nodes, ingest.VALUE_SPINE_NODES)
        self.assertEqual(tasks[0].budget_nodes,
                         ingest._short_mate_clamp(tip)[0])
        self.assertEqual(tasks[0].multipv, ingest.MATE_CLAMP_MULTIPV)

    def test_the_proved_or_clamp_still_cheapens_a_settled_sibling(self):
        """Un hermano de un ganador ya PROBADO no le debe nada a la prueba."""
        root = _pos('OC-root', 'w', eval_cp=800, backed_eval=812,
                    backed_move='m0', expanded=True)
        tip = _pos('OC-tip', 'b', eval_cp=812)
        won = _pos('OC-won', 'b', status='WHITE_WIN', closure='MINIMAX')
        _edge(root, tip, 'm0')
        _edge(root, won, 'm1')
        _campaign(root)

        tasks = ingest._next_tasks_by_proof(1)

        self.assertEqual(tasks[0].position_id, tip.key)
        self.assertEqual(tasks[0].budget_nodes, proof.OR_CLAMP_NODES)
