"""Repeticion en los numeros de prueba (invariante 6): la arista cuya espina
primaria vuelve a su propio nodo puntua (INF, 0), no el numero del ciclo.

El caso que motiva esto es la lanzadera de Eclipsia (6-ago): un componente de
ocho posiciones donde el alfil da jaques en circulo, la suma de dn se
realimentaba a traves del ciclo pasada tras pasada — los ``ProofNode``
persisten, asi que cada refresco releia lo que el anterior escribio — y en
unas decenas de pasadas seis nodos UNSOLVED quedaron clavados en
``dn = 2^62`` con ``pn = 1``: un estado imposible (dn infinito significa
probado, y probado es pn = 0).  La regla de estos tests es la gemela de
``ingest._draw_cycling_children``, que cerro el mismo agujero en la columna
de los backed el 3-ago.
"""

import hashlib
from io import StringIO

from django.core.management import call_command

from . import proof
from .models import Edge, Position, ProofCampaign, ProofNode
from .testing import TestCase

INF = proof.PROOF_INFINITY


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    """Posicion sintetica: al calculo de prueba le bastan turno y status."""
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


class ShuttleFixture(TestCase):
    """Lanzadera minima: A(OR) -> B(AND) -> C(OR) -> D(AND) -> A.

    A tiene ademas la desviacion honesta E (una hoja con eval), B y D tienen
    su otra respuesta ya PROBADA (como en el caso real: los reyes que no
    bailan pierden), y C tiene una alternativa tranquila Q.  El veneno de la
    fixture es el estado real observado: numeros (1, INF) y los primarios
    trazando el ciclo.
    """

    def setUp(self):
        self.campaign = ProofCampaign.objects.get(
            name=proof.DEFAULT_CAMPAIGN_NAME)
        self.a = _pos('CYC-A', 'w', expanded=True, eval_cp=900)
        self.b = _pos('CYC-B', 'b', expanded=True, eval_cp=950)
        self.c = _pos('CYC-C', 'w', expanded=True, eval_cp=950)
        self.d = _pos('CYC-D', 'b', expanded=True, eval_cp=900)
        self.e = _pos('CYC-E', 'b', eval_cp=400)          # desviacion honesta
        self.q = _pos('CYC-Q', 'b', eval_cp=-300)         # alternativa de C
        self.p1 = _pos('CYC-P1', 'w', status='WHITE_WIN')
        self.p2 = _pos('CYC-P2', 'w', status='WHITE_WIN')
        _edge(self.a, self.e, 'd2d4')
        _edge(self.a, self.b, 'e8f7')
        _edge(self.b, self.p1, 'e6f6')
        _edge(self.b, self.c, 'e6d7')
        _edge(self.c, self.q, 'a2a3')
        _edge(self.c, self.d, 'f7e8')
        _edge(self.d, self.p2, 'd7d8')
        _edge(self.d, self.a, 'd7e6')

    def _poison(self):
        """El estado observado en produccion: saturados y el ciclo trazado."""
        rows = [(self.a, 'e8f7'), (self.b, 'e6d7'),
                (self.c, 'f7e8'), (self.d, 'd7e6')]
        for node, selected in rows:
            ProofNode.objects.create(
                campaign=self.campaign, position_id=node.key, pn=1, dn=INF,
                expanded_in_proof=True, selected_child=selected)

    def _numbers(self, position):
        row = ProofNode.objects.get(campaign=self.campaign,
                                    position_id=position.key)
        return row.pn, row.dn, row.selected_child


class CycleRuleTests(ShuttleFixture):

    def test_cycling_edge_is_detected_along_value_carriers(self):
        self._poison()
        cache = proof._CarrierCache()
        node_rows = proof._node_rows(
            self.campaign, [self.a.key, self.b.key, self.c.key, self.d.key])
        children = proof._children_by_parent([self.a.key])
        cycling = proof._cycling_edges(
            self.campaign, [self.a.key], children, node_rows, cache)
        # B -> C -> D -> A: el valor del hijo vuelve al padre que lo evalua.
        self.assertEqual(cycling, {(self.a.key, 'e8f7')})

    def test_the_walk_follows_values_not_the_sticky_primary(self):
        """Por que el paseo NO sigue a ``selected_child``: la histeresis puede
        dejar al primario clavado FUERA del ciclo mientras la suma sigue
        dando la vuelta.  Con los primarios apuntando a las salidas honestas,
        la arista del ciclo tiene que seguir detectandose."""
        self._poison()
        ProofNode.objects.filter(
            campaign=self.campaign, position_id=self.a.key).update(
            selected_child='d2d4')
        ProofNode.objects.filter(
            campaign=self.campaign, position_id=self.c.key).update(
            selected_child='a2a3')
        cache = proof._CarrierCache()
        node_rows = proof._node_rows(
            self.campaign, [self.a.key, self.b.key, self.c.key, self.d.key])
        children = proof._children_by_parent([self.a.key])
        cycling = proof._cycling_edges(
            self.campaign, [self.a.key], children, node_rows, cache)
        self.assertEqual(cycling, {(self.a.key, 'e8f7')})

    def test_cycling_contribution_is_a_refutation_not_the_loop_number(self):
        self._poison()
        children = proof._children_by_parent([self.a.key])[self.a.key]
        pn, dn, _expanded, selected = proof.compute_numbers(
            self.campaign, self.a, children, {}, previous='e8f7',
            cycling={(self.a.key, 'e8f7')})
        # La arista del ciclo vale (INF, 0): el min de pn la ignora, la suma
        # de dn no la infla, y el primario clavado en ella queda destronado
        # por la aritmetica de la histeresis de siempre (3*finito < INF).
        self.assertLess(pn, INF)
        self.assertLess(dn, INF)
        self.assertEqual(selected, 'd2d4')

    def _refresh_until_fixpoint(self, seeds, cap=16):
        """Pasadas hasta que una no escriba nada; el veneno drena un salto de
        espina por pasada, asi que la convergencia es gradual a proposito."""
        for _ in range(cap):
            if proof.refresh_proof_numbers(seeds) == 0:
                return True
        return False

    def test_poisoned_shuttle_drains_to_a_fixpoint(self):
        """El trinquete muere, y cada bando acaba donde le toca.

        Lo que se arregla es la REALIMENTACION, no el veredicto: los dos
        nodos OR (mueve el atacante) vuelven a numeros finitos apoyados en su
        desviacion honesta, y los dos nodos AND — donde la unica respuesta
        del defensor que no pierde es la repeticion — quedan REFUTADOS
        (INF, 0), que es exactamente lo que significa poder repetir.  Un
        ``pn`` infinito no es el sintoma: el sintoma era el ``dn`` creciendo
        sin tope pasada tras pasada.
        """
        self._poison()
        seeds = [self.a.key, self.b.key, self.c.key, self.d.key]
        self.assertTrue(self._refresh_until_fixpoint(seeds),
                        'el trinquete no llego a punto fijo')
        for node in (self.a, self.c):          # OR: mueve el atacante
            pn, dn, _sel = self._numbers(node)
            self.assertLess(pn, INF, node.key)
            self.assertLess(dn, INF, node.key)
        for node in (self.b, self.d):          # AND: el defensor repite
            pn, dn, _sel = self._numbers(node)
            self.assertEqual((pn, dn), (INF, 0), node.key)
        # La desviacion honesta manda: el primario de A ya no es el ciclo.
        self.assertEqual(self._numbers(self.a)[2], 'd2d4')
        # Y el punto fijo es punto fijo: otra pasada no escribe nada.
        self.assertEqual(proof.refresh_proof_numbers(seeds), 0)

    def test_the_dn_ratchet_is_dead(self):
        """La regresion exacta del caso: antes de la regla, el dn del nodo
        de entrada crecia en CADA pasada (+48 medido en esta misma fixture) y
        no paraba hasta saturar.  Ahora se queda quieto."""
        self._poison()
        seeds = [self.a.key, self.b.key, self.c.key, self.d.key]
        self._refresh_until_fixpoint(seeds)
        before = self._numbers(self.a)[1]
        for _ in range(6):
            proof.refresh_proof_numbers(seeds)
        self.assertEqual(self._numbers(self.a)[1], before)

    def test_fresh_component_does_not_ratchet(self):
        """La regresion del caso: sin veneno previo, el componente converge y
        se QUEDA quieto — antes de la regla, cada pasada doblaba el dn y no
        habia punto fijo que alcanzar."""
        seeds = [self.d.key, self.c.key, self.b.key, self.a.key]
        self.assertTrue(self._refresh_until_fixpoint(seeds),
                        'el componente fresco no converge')
        settled = {node.key: self._numbers(node)[:2]
                   for node in (self.a, self.b, self.c, self.d)}
        for _ in range(3):
            proof.refresh_proof_numbers(seeds)
        for node in (self.a, self.b, self.c, self.d):
            self.assertEqual(self._numbers(node)[:2], settled[node.key],
                             node.key)
            self.assertLess(self._numbers(node)[1], INF, node.key)

    def test_long_cycles_beyond_the_cap_claim_nothing(self):
        """Equivocarse hacia "no hay ciclo" es el error seguro: un ciclo mas
        largo que el tope se deja pasar sin refutar la arista."""
        parent = _pos('CAP-P', 'w', expanded=True)
        chain = [_pos(f'CAP-{i}', 'b' if i % 2 == 0 else 'w')
                 for i in range(proof.PROOF_CYCLE_MAX_PLIES + 2)]
        _edge(parent, chain[0], 'a1a2')
        # El padre tiene fila, como en produccion: sin un valor previo
        # persistido no hay nada que pueda volver a entrar en si mismo.
        ProofNode.objects.create(
            campaign=self.campaign, position_id=parent.key, pn=1, dn=1,
            selected_child='a1a2')
        ProofNode.objects.create(
            campaign=self.campaign, position_id=chain[0].key, pn=1, dn=1,
            selected_child='b1b2')
        for i in range(len(chain) - 1):
            _edge(chain[i], chain[i + 1], 'b1b2')
            ProofNode.objects.create(
                campaign=self.campaign, position_id=chain[i + 1].key,
                pn=1, dn=1, selected_child='b1b2')
        _edge(chain[-1], parent, 'b1b2')
        cache = proof._CarrierCache()
        node_rows = proof._node_rows(self.campaign, [chain[0].key])
        children = proof._children_by_parent([parent.key])
        cycling = proof._cycling_edges(
            self.campaign, [parent.key], children, node_rows, cache)
        self.assertEqual(cycling, set())

    def test_a_cycle_within_the_cap_is_claimed(self):
        """La otra mitad del tope: el mismo montaje, con el ciclo corto,
        SI se corta — asi el test de arriba mide el tope y no un bug."""
        parent = _pos('SHORT-P', 'w', expanded=True)
        chain = [_pos(f'SHORT-{i}', 'b' if i % 2 == 0 else 'w')
                 for i in range(4)]
        _edge(parent, chain[0], 'a1a2')
        ProofNode.objects.create(
            campaign=self.campaign, position_id=parent.key, pn=1, dn=1,
            selected_child='a1a2')
        ProofNode.objects.create(
            campaign=self.campaign, position_id=chain[0].key, pn=1, dn=1,
            selected_child='b1b2')
        for i in range(len(chain) - 1):
            _edge(chain[i], chain[i + 1], 'b1b2')
            ProofNode.objects.create(
                campaign=self.campaign, position_id=chain[i + 1].key,
                pn=1, dn=1, selected_child='b1b2')
        _edge(chain[-1], parent, 'b1b2')
        cache = proof._CarrierCache()
        node_rows = proof._node_rows(self.campaign, [chain[0].key])
        children = proof._children_by_parent([parent.key])
        cycling = proof._cycling_edges(
            self.campaign, [parent.key], children, node_rows, cache)
        self.assertEqual(cycling, {(parent.key, 'a1a2')})


class SaturationVisibilityTests(ShuttleFixture):

    def test_open_saturated_nodes_are_counted_apart(self):
        self.assertEqual(proof.saturated_open_count(self.campaign), 0)
        self._poison()
        self.assertEqual(proof.saturated_open_count(self.campaign), 4)
        self.assertEqual(proof.saturated_open_count(), 4)
        # Un PROBADO con dn infinito es la firma legitima de un cierre y no
        # cuenta: el contador vigila lo imposible, no lo probado.
        ProofNode.objects.create(
            campaign=self.campaign, position_id=self.p1.key,
            pn=0, dn=INF)
        self.assertEqual(proof.saturated_open_count(self.campaign), 4)

    def test_the_two_columns_are_counted_separately(self):
        """dn saturado en un abierto es imposible (el sintoma); pn saturado
        es corriente y honesto.  Sumarlos seria llorar por la regla nueva."""
        self._poison()          # los cuatro llevan dn = INF, pn = 1
        self.assertEqual(
            proof.saturated_open_count(self.campaign, column='dn'), 4)
        self.assertEqual(
            proof.saturated_open_count(self.campaign, column='pn'), 0)
        ProofNode.objects.filter(
            campaign=self.campaign, position_id=self.b.key).update(
            pn=INF, dn=0)
        self.assertEqual(
            proof.saturated_open_count(self.campaign, column='dn'), 3)
        self.assertEqual(
            proof.saturated_open_count(self.campaign, column='pn'), 1)

    def test_frontier_stats_carry_the_alarming_count(self):
        self._poison()
        stats = proof.frontier_dn_stats(self.campaign, floor=2)
        # La cifra publicada es la que alarma: el dn imposible.
        self.assertEqual(stats['saturated'], 4)
        # Y los saturados siguen FUERA de la mediana, como siempre.
        self.assertEqual(stats['and_nodes'], 0)


class RecascadeProofCommandTests(ShuttleFixture):

    def test_command_drains_the_dn_ratchet_and_reports_both_columns(self):
        self._poison()
        out = StringIO()
        call_command('recascade_proof', '--max-passes', '8', stdout=out)
        text = out.getvalue()
        self.assertIn('abiertos saturados antes: dn=4', text)
        self.assertIn('punto fijo', text)
        # Lo que TIENE que irse es el dn imposible.  El pn que queda es la
        # refutacion por repeticion de los nodos AND, que es el resultado
        # correcto y no un residuo (§ CycleRuleTests).
        self.assertEqual(proof.saturated_open_count(column='dn'), 0)
        self.assertEqual(proof.saturated_open_count(column='pn'), 2)

    def test_command_with_nothing_to_do_says_so(self):
        out = StringIO()
        call_command('recascade_proof', stdout=out)
        self.assertIn('nada saturado que sembrar', out.getvalue())
