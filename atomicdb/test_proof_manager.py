"""Proof manager v1: pn/dn per CAMPAIGN, not per position (P1a).

The recurrences, the documented leaf initialisation, the incremental
maintenance shape, the soft-repertoire descent and the selector flag.
"""

from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from . import ingest, logic, proof
from .models import (AnalysisTask, DBEvent, Edge, Position, ProofCampaign,
                     ProofNode)
from .testing import TestCase, worker_account

WHITE_TO_MOVE = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
BLACK_TO_MOVE = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1'
INF = proof.PROOF_INFINITY


class RecurrenceTests(SimpleTestCase):

    def test_or_node_takes_min_pn_and_sum_dn(self):
        numbers = proof.internal_numbers(
            WHITE_TO_MOVE, 'WHITE_WIN', [(3, 10), (7, 4), (5, 6)])
        self.assertEqual(numbers, (3, 20))

    def test_and_node_takes_sum_pn_and_min_dn(self):
        numbers = proof.internal_numbers(
            BLACK_TO_MOVE, 'WHITE_WIN', [(3, 10), (7, 4), (5, 6)])
        self.assertEqual(numbers, (15, 4))

    def test_goal_reversal_swaps_the_roles(self):
        """For BLACK_WIN it is Black to move that is the OR node."""
        self.assertEqual(
            proof.internal_numbers(BLACK_TO_MOVE, 'BLACK_WIN',
                                   [(3, 10), (7, 4)]),
            (3, 14))
        self.assertEqual(
            proof.internal_numbers(WHITE_TO_MOVE, 'BLACK_WIN',
                                   [(3, 10), (7, 4)]),
            (10, 4))

    def test_saturation_never_overflows(self):
        self.assertEqual(proof.saturating_sum([INF, INF, INF]), INF)
        self.assertEqual(proof.saturating_sum([INF - 1, 5]), INF)
        self.assertEqual(proof.saturating_min([]), INF)
        or_numbers = proof.internal_numbers(
            WHITE_TO_MOVE, 'WHITE_WIN', [(INF, 0), (INF, 0)])
        self.assertEqual(or_numbers, (INF, 0))
        and_numbers = proof.internal_numbers(
            BLACK_TO_MOVE, 'WHITE_WIN', [(INF, 0), (2, 9)])
        self.assertEqual(and_numbers, (INF, 0))

    def test_proved_node_is_a_leaf_with_its_truth_value(self):
        self.assertEqual(
            proof.leaf_numbers(WHITE_TO_MOVE, 'WHITE_WIN', None, 'WHITE_WIN'),
            (0, INF))
        self.assertEqual(
            proof.leaf_numbers(WHITE_TO_MOVE, 'BLACK_WIN', None, 'WHITE_WIN'),
            (INF, 0))
        # A draw REFUTES a win proposition; PNS is binary on purpose.
        self.assertEqual(
            proof.leaf_numbers(WHITE_TO_MOVE, 'DRAW', None, 'WHITE_WIN'),
            (INF, 0))


class LeafInitialisationTests(SimpleTestCase):

    def test_no_information_falls_back_to_classic_one_one(self):
        self.assertEqual(
            proof.leaf_numbers(WHITE_TO_MOVE, 'UNKNOWN', None, 'WHITE_WIN'),
            (1, 1))

    def test_attacker_advantage_makes_proving_look_cheaper(self):
        winning = proof.leaf_numbers(
            WHITE_TO_MOVE, 'UNKNOWN', 900, 'WHITE_WIN')
        losing = proof.leaf_numbers(
            WHITE_TO_MOVE, 'UNKNOWN', -900, 'WHITE_WIN')
        self.assertLess(winning[0], losing[0])
        self.assertGreater(winning[1], losing[1])

    def test_eval_is_read_from_the_attacker_side(self):
        """The stored eval is White-POV; only this function flips it."""
        white_goal = proof.leaf_numbers(
            WHITE_TO_MOVE, 'UNKNOWN', 900, 'WHITE_WIN')
        black_goal = proof.leaf_numbers(
            BLACK_TO_MOVE, 'UNKNOWN', -900, 'BLACK_WIN')
        self.assertEqual(white_goal, black_goal)

    def test_branching_pushes_the_side_that_must_answer_everything(self):
        # Defender to move: refuting takes ONE good reply, proving takes all.
        few = proof.leaf_numbers(BLACK_TO_MOVE, 'UNKNOWN', 0, 'WHITE_WIN',
                                 legal_moves=3)
        many = proof.leaf_numbers(BLACK_TO_MOVE, 'UNKNOWN', 0, 'WHITE_WIN',
                                  legal_moves=40)
        self.assertLess(few[0], many[0])
        self.assertEqual(few[1], many[1])
        # Attacker to move: the mirror image.
        few_or = proof.leaf_numbers(WHITE_TO_MOVE, 'UNKNOWN', 0, 'WHITE_WIN',
                                    legal_moves=3)
        many_or = proof.leaf_numbers(WHITE_TO_MOVE, 'UNKNOWN', 0, 'WHITE_WIN',
                                     legal_moves=40)
        self.assertEqual(few_or[0], many_or[0])
        self.assertLess(few_or[1], many_or[1])

    def test_a_leaf_never_claims_infinity(self):
        for score in (-30_000, -1_000, 0, 1_000, 30_000):
            pn, dn = proof.leaf_numbers(
                WHITE_TO_MOVE, 'UNKNOWN', score, 'WHITE_WIN',
                legal_moves=200)
            self.assertLess(pn, INF)
            self.assertLess(dn, INF)
            self.assertGreaterEqual(min(pn, dn), 1)


class CampaignSeedTests(TestCase):

    def test_migration_created_the_default_campaign(self):
        campaign = ProofCampaign.objects.get(name=proof.DEFAULT_CAMPAIGN_NAME)
        self.assertEqual(campaign.goal, 'WHITE_WIN')
        self.assertTrue(campaign.active)
        self.assertEqual(campaign.root_id, logic.key_of(logic.start_fen()))
        self.assertEqual(campaign.root.fen, logic.start_fen())

    def test_policy_defaults_to_eighty_fifteen_five(self):
        campaign = ProofCampaign.objects.get(name=proof.DEFAULT_CAMPAIGN_NAME)
        self.assertEqual(proof.normalized_policy(campaign),
                         {'primary': 0.8, 'backup': 0.15, 'explore': 0.05})

    def test_malformed_policy_is_normalised_not_trusted(self):
        campaign = ProofCampaign.objects.get(name=proof.DEFAULT_CAMPAIGN_NAME)
        campaign.repertoire_policy = {'primary': 6, 'backup': 3,
                                      'explore': 'nonsense'}
        policy = proof.normalized_policy(campaign)
        self.assertAlmostEqual(sum(policy.values()), 1.0)
        self.assertGreater(policy['primary'], policy['backup'])


class IncrementalMaintenanceTests(TestCase):

    def setUp(self):
        self.campaign = ProofCampaign.objects.get(
            name=proof.DEFAULT_CAMPAIGN_NAME)
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)

    def test_numbers_are_written_for_the_touched_cone(self):
        child = Edge.objects.filter(parent=self.root).first().child
        proof.refresh_proof_numbers([child.key])

        node = ProofNode.objects.get(campaign=self.campaign,
                                     position=self.root)
        self.assertTrue(node.expanded_in_proof)
        self.assertIsNotNone(node.selected_child)
        self.assertGreaterEqual(node.pn, 1)

    def test_one_level_costs_a_fixed_number_of_statements(self):
        """Width must not buy statements: a level is a handful either way.

        Four: the active campaigns, the level's positions, the level's edges
        joined to their children, and the existing proof rows.  The start
        position has twenty legal moves; a node with sixty would cost exactly
        the same four.
        """
        child = Edge.objects.filter(parent=self.root).first().child
        proof.refresh_proof_numbers([child.key])   # warm: rows now exist

        with self.assertNumQueries(4, using=settings.ATOMICDB_DATABASE_ALIAS):
            proof.refresh_proof_numbers([child.key], max_plies=1)

    def test_an_idempotent_refresh_stops_at_the_first_level(self):
        child = Edge.objects.filter(parent=self.root).first().child
        proof.refresh_proof_numbers([child.key])
        before = ProofNode.objects.count()

        self.assertEqual(proof.refresh_proof_numbers([child.key]), 0)
        self.assertEqual(ProofNode.objects.count(), before)

    def test_a_transposition_with_two_parents_updates_both(self):
        shared = ingest.get_or_create_position(
            '8/8/8/8/8/8/1k6/K6Q w - - 0 1')
        first = ingest.get_or_create_position(
            '8/8/8/8/8/8/1k6/K5Q1 b - - 0 1')
        second = ingest.get_or_create_position(
            '8/8/8/8/8/8/2k5/K5Q1 b - - 0 1')
        Edge.objects.create(parent=first, move_uci='g1h1', child=shared)
        Edge.objects.create(parent=second, move_uci='g1h1', child=shared)

        proof.refresh_proof_numbers([shared.key])

        self.assertTrue(ProofNode.objects.filter(
            campaign=self.campaign, position=first).exists())
        self.assertTrue(ProofNode.objects.filter(
            campaign=self.campaign, position=second).exists())

    def test_guard_emits_an_event_and_stops(self):
        child = Edge.objects.filter(parent=self.root).first().child
        original = proof.PROOF_MAX_NODES
        proof.PROOF_MAX_NODES = 1
        try:
            proof.refresh_proof_numbers([child.key])
        finally:
            proof.PROOF_MAX_NODES = original
        event = DBEvent.objects.filter(kind='PROOF_GUARD').first()
        self.assertIsNotNone(event)
        self.assertEqual(event.payload['reason'], 'node-budget')

    def test_a_proved_child_drives_the_parent_to_zero_pn(self):
        edge = Edge.objects.filter(parent=self.root).first()
        child = edge.child
        child.status = 'WHITE_WIN'
        child.closure = 'MATE_PV'
        child.proof = 'ANDOR'
        child.save()

        proof.refresh_proof_numbers([child.key])

        node = ProofNode.objects.get(campaign=self.campaign,
                                     position=self.root)
        self.assertEqual(node.pn, 0)     # OR node: one winning child suffices
        self.assertEqual(node.selected_child, edge.move_uci)

    def test_backup_cascade_maintains_the_numbers(self):
        edge = Edge.objects.filter(parent=self.root).first()
        child = edge.child
        child.status = 'WHITE_WIN'
        child.closure = 'MATE_PV'
        child.proof = 'ANDOR'
        child.mate_in = 1
        child.save()

        ingest.backup_cascade([child.key])

        self.assertEqual(ProofNode.objects.get(
            campaign=self.campaign, position=self.root).pn, 0)


class SelectedChildHysteresisTests(TestCase):
    """El primario de un nodo OR no cambia por un parpadeo de pn.

    Peticion de la comunidad: el descenso alternaba entre dos jugadas blancas
    igual de prometedoras y ninguna acumulaba la profundidad que la cerraria.
    ``pn`` es una estimacion de coste, asi que "algo mejor" esta dentro del
    ruido; solo ``K`` veces mejor es informacion.
    """

    def setUp(self):
        self.campaign = ProofCampaign.objects.get(
            name=proof.DEFAULT_CAMPAIGN_NAME)
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.moves = [edge.move_uci for edge in
                      Edge.objects.filter(parent=self.root).order_by('id')]

    def _selected(self, numbers, previous):
        """selected_child de la raiz con esos pn/dn por hijo."""
        children = [(move, f'child-{move}', 'UNKNOWN', None, BLACK_TO_MOVE)
                    for move in self.moves[:len(numbers)]]
        nodes = {f'child-{move}': ProofNode(
            campaign=self.campaign, position_id=f'child-{move}',
            pn=pn, dn=dn)
            for move, (pn, dn) in zip(self.moves, numbers)}
        _pn, _dn, _expanded, selected = proof.compute_numbers(
            self.campaign, self.root, children, nodes, previous=previous)
        return selected

    def test_the_declared_constant_is_the_approved_one(self):
        self.assertEqual(proof.SELECTED_CHILD_HYSTERESIS, 3)

    def test_a_flicker_does_not_move_the_primary(self):
        # 30 es mejor que 60, pero no TRES VECES mejor: eso es ruido.
        self.assertEqual(
            self._selected([(60, 5), (30, 5)], previous=self.moves[0]),
            self.moves[0])

    def test_crossing_the_threshold_does_move_it(self):
        # 19 * 3 < 60: el retador ya no esta discutiendo dentro del ruido.
        self.assertEqual(
            self._selected([(60, 5), (19, 5)], previous=self.moves[0]),
            self.moves[1])

    def test_the_boundary_itself_is_not_a_change(self):
        """Exactamente K veces mejor no basta: el umbral es estricto."""
        self.assertEqual(
            self._selected([(60, 5), (20, 5)], previous=self.moves[0]),
            self.moves[0])

    def test_a_proved_primary_is_never_dethroned(self):
        """pn = 0 esta probado: nada puede ser menor que cero.

        El caso que muerde no es un retador mejor (imposible), es un EMPATE:
        una hermana que tambien cierra y que el desempate por indice habria
        puesto delante.
        """
        self.assertEqual(
            self._selected([(0, INF), (0, INF)], previous=self.moves[1]),
            self.moves[1])
        self.assertEqual(
            self._selected([(0, INF), (1, 5)], previous=self.moves[0]),
            self.moves[0])

    def test_a_refuted_primary_hands_the_seat_over(self):
        """Pegajoso no es inamovible: un pn infinito lo pierde igual."""
        self.assertEqual(
            self._selected([(INF, 0), (7, 5)], previous=self.moves[0]),
            self.moves[1])

    def test_with_no_primary_yet_the_best_pn_takes_it(self):
        self.assertEqual(
            self._selected([(60, 5), (30, 5)], previous=None),
            self.moves[1])

    def test_a_primary_that_is_no_longer_a_child_is_not_honoured(self):
        self.assertEqual(
            self._selected([(60, 5), (30, 5)], previous='z9z9'),
            self.moves[1])

    def test_an_and_node_is_not_sticky(self):
        """El primario AND es la defensa mas dura; sostenerla contra
        evidencia nueva retrasaria la refutacion que la prueba necesita."""
        black = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        ingest.expand(black)
        replies = [edge.move_uci for edge in
                   Edge.objects.filter(parent=black).order_by('id')]
        children = [(move, f'r-{move}', 'UNKNOWN', None, WHITE_TO_MOVE)
                    for move in replies[:2]]
        nodes = {f'r-{replies[0]}': ProofNode(campaign=self.campaign,
                                              position_id='a', pn=5, dn=60),
                 f'r-{replies[1]}': ProofNode(campaign=self.campaign,
                                              position_id='b', pn=5, dn=30)}

        _pn, _dn, _expanded, selected = proof.compute_numbers(
            self.campaign, black, children, nodes, previous=replies[0])

        self.assertEqual(selected, replies[1])

    def test_the_stored_primary_survives_a_refresh(self):
        """Sobre el arbol real y por el camino de siempre, no en laboratorio.

        La primaria se gana en un indice ALTO a proposito: asi el empate que
        viene despues la habria destronado por puro orden de arista, que es
        justo el baile del que se quejo la comunidad.
        """
        edges = list(Edge.objects.filter(parent=self.root).order_by('id'))
        for edge in edges:
            Position.objects.filter(key=edge.child_id).update(eval_cp=-400)
        Position.objects.filter(key=edges[9].child_id).update(eval_cp=3_000)
        proof.refresh_proof_numbers([edge.child_id for edge in edges])
        node = ProofNode.objects.get(campaign=self.campaign,
                                     position=self.root)
        self.assertEqual(node.selected_child, edges[9].move_uci)

        # Una hermana alcanza la MISMA banda de eval: empata en pn, y un
        # empate se rompe por indice, no por informacion.
        Position.objects.filter(key=edges[2].child_id).update(eval_cp=3_000)
        proof.refresh_proof_numbers([edges[2].child_id])

        node.refresh_from_db()
        self.assertEqual(node.selected_child, edges[9].move_uci)

    def test_the_descent_follows_the_sticky_primary(self):
        """La histeresis solo sirve si el descenso la respeta."""
        self.campaign.repertoire_policy = {'primary': 1.0, 'backup': 0.0,
                                           'explore': 0.0}
        self.campaign.save(update_fields=['repertoire_policy'])
        edges = list(Edge.objects.filter(parent=self.root).order_by('id'))
        for edge in edges:
            Position.objects.filter(key=edge.child_id).update(eval_cp=-400)
        held = edges[9]
        Position.objects.filter(key=held.child_id).update(eval_cp=3_000)
        proof.refresh_proof_numbers([edge.child_id for edge in edges])
        # Una hermana empata en banda: por puro orden pn el descenso se iria
        # con la de indice menor.
        Position.objects.filter(key=edges[2].child_id).update(eval_cp=3_000)
        proof.refresh_proof_numbers([edges[2].child_id])

        found, _plies = proof.descend(self.campaign, counter=0)

        self.assertEqual(found.key, held.child_id)

    def test_the_backup_bucket_still_gets_an_alternative(self):
        """La histeresis elige QUIEN es primaria; el 80/15/5 sigue repartiendo.

        Con la primaria fuera del primer puesto, un ``ranked[1]`` a secas
        podria ser la primaria otra vez y el 15% se gastaria en la misma
        jugada que el 80%.
        """
        self.campaign.repertoire_policy = {'primary': 0.0, 'backup': 1.0,
                                           'explore': 0.0}
        self.campaign.save(update_fields=['repertoire_policy'])
        edges = list(Edge.objects.filter(parent=self.root).order_by('id'))
        for edge in edges:
            Position.objects.filter(key=edge.child_id).update(eval_cp=-400)
        held = edges[9]
        Position.objects.filter(key=held.child_id).update(eval_cp=3_000)
        proof.refresh_proof_numbers([edge.child_id for edge in edges])
        Position.objects.filter(key=edges[2].child_id).update(eval_cp=3_000)
        proof.refresh_proof_numbers([edges[2].child_id])

        found, _plies = proof.descend(self.campaign, counter=0)

        self.assertIsNotNone(found)
        self.assertNotEqual(found.key, held.child_id)


class DescentTests(TestCase):

    def setUp(self):
        self.campaign = ProofCampaign.objects.get(
            name=proof.DEFAULT_CAMPAIGN_NAME)
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        proof.refresh_proof_numbers([self.root.key])

    def test_descent_reaches_an_unexpanded_frontier_node(self):
        found, plies = proof.descend(self.campaign, counter=0)
        self.assertIsNotNone(found)
        self.assertEqual(found.status, 'UNKNOWN')
        self.assertEqual(plies, 1)
        self.assertTrue(Edge.objects.filter(
            parent=self.root, child=found).exists())

    def _pin_primary(self):
        """Force the pure best-first descent: no backup, no exploration."""
        self.campaign.repertoire_policy = {'primary': 1.0, 'backup': 0.0,
                                           'explore': 0.0}
        self.campaign.save(update_fields=['repertoire_policy'])

    def test_or_descent_follows_the_smallest_pn(self):
        """Among INFORMED siblings, the descent takes the cheapest to prove.

        A sibling with no information at all keeps the classic 1/1 and is
        therefore always attractive — that is PNS going for the shallowest
        unknown, and it is deliberate — so this pins the ordering where the
        evals actually exist.
        """
        self._pin_primary()
        edges = list(Edge.objects.filter(parent=self.root).order_by('id'))
        for edge in edges:
            child = edge.child
            child.eval_cp = -400
            child.save(update_fields=['eval_cp'])
        target = edges[5].child
        target.eval_cp = 3_000          # much better for the attacker
        target.save(update_fields=['eval_cp'])
        proof.refresh_proof_numbers([edge.child.key for edge in edges])

        found, _ = proof.descend(self.campaign, counter=0)

        self.assertEqual(found.key, target.key)

    def test_an_unexplored_sibling_keeps_the_classic_one_one(self):
        """Documented on purpose: no information at all is most-proving."""
        self.assertEqual(
            proof.leaf_numbers(BLACK_TO_MOVE, 'UNKNOWN', None, 'WHITE_WIN'),
            (1, 1))
        informed = proof.leaf_numbers(
            BLACK_TO_MOVE, 'UNKNOWN', 3_000, 'WHITE_WIN')
        self.assertGreater(informed[0], 1)

    def test_a_reserved_node_is_not_handed_out_twice(self):
        self._pin_primary()
        first, _ = proof.descend(self.campaign, counter=0)
        second, _ = proof.descend(self.campaign, counter=0,
                                  avoid={first.key})
        self.assertIsNotNone(second)
        self.assertNotEqual(second.key, first.key)

    def test_soft_repertoire_spends_roughly_eighty_fifteen_five(self):
        policy = proof.normalized_policy(self.campaign)
        buckets = {'primary': 0, 'backup': 0, 'explore': 0}
        for counter in range(2000):
            buckets[proof._bucket(counter, policy)] += 1
        self.assertAlmostEqual(buckets['primary'] / 2000, 0.80, delta=0.03)
        self.assertAlmostEqual(buckets['backup'] / 2000, 0.15, delta=0.03)
        self.assertAlmostEqual(buckets['explore'] / 2000, 0.05, delta=0.02)

    def test_allocation_is_deterministic_not_random(self):
        policy = proof.normalized_policy(self.campaign)
        first = [proof._bucket(i, policy) for i in range(50)]
        second = [proof._bucket(i, policy) for i in range(50)]
        self.assertEqual(first, second)

    def test_descent_never_loops_through_a_transposition(self):
        """1.Nf3 Nf6 2.Ng1 Ng8 IS startpos once the counters are stripped."""
        found, plies = proof.descend(self.campaign, counter=7, max_plies=200)
        self.assertLessEqual(plies, 200)
        self.assertIsNotNone(found)

    def test_a_closed_root_yields_nothing(self):
        Position.objects.filter(key=self.root.key).update(
            status='WHITE_WIN', closure='MINIMAX')
        self.campaign.refresh_from_db()
        found, _ = proof.descend(self.campaign, counter=0)
        self.assertIsNone(found)


class SelectorFlagTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)

    def test_default_selector_is_still_regret(self):
        self.assertEqual(proof.selector_mode(), 'regret')

    @override_settings(ATOMICDB_SELECTOR='pn')
    def test_pn_selector_produces_tasks_from_the_descent(self):
        tasks = ingest.next_tasks(3)
        self.assertEqual(len(tasks), 3)
        for task in tasks:
            self.assertTrue(Edge.objects.filter(
                parent=self.root, child=task.position).exists())
        self.assertEqual(AnalysisTask.objects.filter(
            state='PENDING').count(), 3)

    @override_settings(ATOMICDB_SELECTOR='pn')
    def test_pn_selector_does_not_run_the_global_dijkstra(self):
        from unittest.mock import patch
        with patch('atomicdb.ingest._regret_from_root') as dijkstra:
            ingest.next_tasks(2)
        dijkstra.assert_not_called()

    @override_settings(ATOMICDB_SELECTOR='pn')
    def test_pn_selector_without_campaigns_returns_nothing(self):
        ProofCampaign.objects.update(active=False)
        self.assertEqual(ingest.next_tasks(2), [])


class ProofStatusCommandTests(TestCase):

    def test_command_reports_root_numbers_and_frontier(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        proof.refresh_proof_numbers([root.key])
        out = StringIO()

        call_command('proof_status', stdout=out)
        text = out.getvalue()

        self.assertIn('campaign root-white-win', text)
        self.assertIn('root pn', text)
        self.assertIn('root dn', text)
        self.assertIn('most-proving frontier', text)
        self.assertIn('oldest open AND obligations', text)
        self.assertIn('primary=80%', text)

    def test_saturated_numbers_print_as_inf(self):
        root = ingest.get_or_create_position(logic.start_fen())
        campaign = ProofCampaign.objects.get(
            name=proof.DEFAULT_CAMPAIGN_NAME)
        ProofNode.objects.create(campaign=campaign, position=root,
                                 pn=proof.PROOF_INFINITY, dn=0)
        out = StringIO()

        call_command('proof_status', stdout=out)

        self.assertIn('root pn    INF', out.getvalue())

    def test_unknown_campaign_is_reported_not_crashed(self):
        out = StringIO()
        call_command('proof_status', campaign='does-not-exist', stdout=out)
        self.assertIn('no matching proof campaign', out.getvalue())


class SubmitProvenanceTests(TestCase):
    """The engine/net sha are additive: old workers keep working."""

    def setUp(self):
        worker_account('worker', 'pw')
        self.pos = ingest.get_or_create_position(logic.start_fen())
        self.task = AnalysisTask.objects.create(
            position=self.pos, budget_nodes=1_000, generation=0,
            state='LEASED', machine='m1')

    def _submit(self, **extra):
        return self.client.post('/atomicdb/api/submit', dict({
            'username': 'worker', 'password': 'pw', 'machine': 'm1',
            'task_id': self.task.id, 'lines': '[]', 'nodes': '1000',
            'elapsed': '1.0'}, **extra))

    def test_provenance_is_persisted_when_present(self):
        sha = 'a' * 64
        response = self._submit(engine_sha=sha, net_sha='b' * 64)
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.engine_sha, sha)
        self.assertEqual(self.task.net_sha, 'b' * 64)

    def test_an_old_worker_submits_exactly_as_before(self):
        response = self._submit()
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.engine_sha, '')
        self.assertEqual(self.task.net_sha, '')

    def test_garbage_provenance_is_dropped_not_stored(self):
        response = self._submit(engine_sha='not-a-sha',
                                net_sha='Z' * 64)
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.engine_sha, '')
        self.assertEqual(self.task.net_sha, '')
