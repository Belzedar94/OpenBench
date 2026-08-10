"""Repeticiones DESACTIVADAS en el explorador (invariante 6, 6-ago).

Tres superficies, una regla: una linea no puede reentrar en una posicion por
la que ya paso.  La PV mostrada se corta en su primer cruce consigo misma con
el chip de repeticion de siempre; una jugada de la tabla cuyo hijo ya esta en
la linea actual pierde el enlace y viste el mismo chip; una ruta ``?play``
entrante que contenga una reentrada se trunca en el primer cruce; y ``goto``
— la guardia del servidor detras del bloqueo — rebobina a la posicion de la
linea en vez de reentrar, sin escribir nada.

La fixture es el bucle real mas barato del arbol: 1.Nf3 Nf6 2.Ng1 y la vuelta
Ng8 devuelve la partida a la posicion inicial exacta (los contadores no
existen en la identidad canonica, § logic.canonical_fen).
"""

from django.test import Client

from . import logic
from .models import Edge, Position
from .testing import TestCase


class KnightLoopFixture(TestCase):

    def setUp(self):
        self.client = Client()
        self.start_fen = logic.start_fen()
        self.start_key = logic.key_of(self.start_fen)
        self.start = Position.objects.get(key=self.start_key)
        fen = self.start_fen
        self.route = ['g1f3', 'g8f6', 'f3g1']
        self.nodes = []
        parent = self.start
        for uci in self.route:
            fen = logic.apply_move(fen, uci)
            child, _ = Position.objects.get_or_create(
                key=logic.key_of(fen), defaults={'fen': fen})
            Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                       defaults={'child': child})
            self.nodes.append(child)
            parent = child
        self.p3 = self.nodes[-1]
        # La premisa de la fixture, comprobada y no supuesta: cerrar el bucle
        # devuelve EXACTAMENTE a la clave inicial.
        self.assertEqual(logic.key_of(logic.apply_move(fen, 'f6g8')),
                         self.start_key)
        Edge.objects.get_or_create(parent=self.p3, move_uci='f6g8',
                                   defaults={'child': self.start})

    def _explore(self, key, play=None):
        url = f'/atomicdb/explore/{key}/'
        if play is not None:
            url += f'?play={play}'
        return self.client.get(url)


class BlockedMoveTests(KnightLoopFixture):

    def test_move_returning_into_the_line_is_disabled(self):
        response = self._explore(self.p3.key, play=','.join(self.route))
        self.assertEqual(response.status_code, 200)
        row = next(m for m in response.context['moves']
                   if m['uci'] == 'f6g8')
        self.assertTrue(row['blocked'])
        self.assertIsNone(row['url'])
        self.assertIsNone(row['backed_url'])
        self.assertContains(response, 'Repetitions are disabled')
        # El tablero obedece el mismo bloqueo: la jugada no es clicable.
        self.assertNotIn('f6g8', response.context['legal_ucis'])
        self.assertNotIn('f6g8', [m['uci'] for m in
                                  response.context['legal_move_links']])

    def test_canonical_lineage_blocks_the_same_way(self):
        """Sin ``?play`` la miga de pan enseña el linaje canonico replayable,
        y ESA es la linea actual: la vuelta al inicio queda bloqueada igual."""
        response = self._explore(self.p3.key)
        self.assertEqual(response.status_code, 200)
        if response.context['active_play'] is None:
            self.skipTest('canonical lineage did not reach the root')
        row = next(m for m in response.context['moves']
                   if m['uci'] == 'f6g8')
        self.assertTrue(row['blocked'])

    def test_offtree_reply_into_the_line_is_disabled_too(self):
        """Sin arista materializada la jugada seria un goto que CREA el
        bucle: bloqueada igual que sus hermanas con arista."""
        Edge.objects.filter(parent=self.p3, move_uci='f6g8').delete()
        response = self._explore(self.p3.key, play=','.join(self.route))
        self.assertEqual(response.status_code, 200)
        row = next(u for u in response.context['offtree']
                   if u['uci'] == 'f6g8')
        self.assertTrue(row['blocked'])
        self.assertIsNone(row['url'])
        self.assertNotIn('f6g8', response.context['legal_ucis'])

    def test_moves_off_the_line_keep_their_links(self):
        response = self._explore(self.p3.key, play=','.join(self.route))
        others = [m for m in response.context['moves']
                  if m['uci'] != 'f6g8']
        offtree = response.context['offtree']
        self.assertTrue(all(not m['blocked'] for m in others))
        self.assertTrue(all(not u['blocked'] for u in offtree))


class RouteSanitationTests(KnightLoopFixture):

    def test_incoming_route_with_a_reentry_truncates_at_first_crossing(self):
        response = self._explore(self.start_key,
                                 play='g1f3,g8f6,f3g1,f6g8')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            f'/atomicdb/explore/{self.p3.key}/?play=g1f3,g8f6,f3g1')
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_goto_that_would_reenter_rewinds_instead(self):
        before = Edge.objects.count()
        response = self.client.get(
            f'/atomicdb/goto/{self.p3.key}/f6g8/?play=' + ','.join(
                self.route))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{self.start_key}/')
        # Rebobinar no escribe: ni arista nueva ni lapida revivida.
        self.assertEqual(Edge.objects.count(), before)

    def test_clean_routes_still_validate_as_always(self):
        response = self._explore(self.p3.key, play=','.join(self.route))
        self.assertEqual(response.status_code, 200)


class PvCutTests(KnightLoopFixture):

    LOOP_RAW = ('info depth 10 seldepth 12 multipv 1 score cp 500 nodes 9 '
                'pv f6g8 g1f3 g8f6 f3g1 d2d4 d7d5')

    def setUp(self):
        super().setUp()
        self.p3.last_analysis = [
            {'eval_cp': 500, 'raw': self.LOOP_RAW,
             'pv': ['f6g8', 'g1f3', 'g8f6', 'f3g1', 'd2d4', 'd7d5']},
            {'eval_cp': 480,
             'pv': ['d2d4', 'd7d5', 'c2c4', 'e7e6']},
        ]
        self.p3.save(update_fields=['last_analysis'])

    def test_pv_is_cut_at_its_first_self_crossing(self):
        response = self._explore(self.p3.key)
        shown = response.context['analysis_lines']
        # El corte INCLUYE la jugada que cierra el bucle (f3g1 devuelve a la
        # posicion de la pagina) y tira lo de detras.
        self.assertEqual(shown[0]['pv'], ['f6g8', 'g1f3', 'g8f6', 'f3g1'])
        self.assertTrue(shown[0]['pv_repetition'])
        self.assertEqual(
            shown[0]['raw'],
            'info depth 10 seldepth 12 multipv 1 score cp 500 nodes 9 '
            'pv f6g8 g1f3 g8f6 f3g1')
        self.assertContains(response, 'line cut at a repetition')

    def test_lines_without_a_crossing_are_untouched(self):
        response = self._explore(self.p3.key)
        shown = response.context['analysis_lines']
        self.assertEqual(shown[1]['pv'], ['d2d4', 'd7d5', 'c2c4', 'e7e6'])
        self.assertNotIn('pv_repetition', shown[1])

    def test_stored_json_is_never_touched(self):
        self._explore(self.p3.key)
        self.p3.refresh_from_db()
        self.assertEqual(self.p3.last_analysis[0]['raw'], self.LOOP_RAW)
        self.assertEqual(len(self.p3.last_analysis[0]['pv']), 6)

    def test_broken_pv_is_shown_as_is(self):
        self.p3.last_analysis = [
            {'eval_cp': 10, 'pv': ['f6g8', 'z9z9', 'g8f6', 'f3g1', 'a2a3']}]
        self.p3.save(update_fields=['last_analysis'])
        response = self._explore(self.p3.key)
        shown = response.context['analysis_lines']
        self.assertEqual(len(shown[0]['pv']), 5)
        self.assertNotIn('pv_repetition', shown[0])


class ArrowTests(KnightLoopFixture):

    def test_arrow_never_points_at_a_blocked_move(self):
        # El ``best_move`` almacenado apunta justo a la jugada bloqueada: la
        # flecha calla (o señala otra fila) antes que contradecir al bloqueo.
        self.p3.best_move = 'f6g8'
        self.p3.save(update_fields=['best_move'])
        response = self._explore(self.p3.key, play=','.join(self.route))
        self.assertNotEqual(response.context['best_move'], 'f6g8')
