"""Un PGN legal hacia territorio sin materializar aterriza, no rompe.

El reporte de comunidad (7-ago-2026): "when u jump to an unknown position
with pgn, it doesn't show the lineage, even tho you just gave it the pgn?".
Antes: destino sin materializar = 404 seco; prefijo sin materializar con
destino existente = pagina de error en texto plano.  Ahora ambos aterrizan
en el prefijo mas profundo que el arbol conoce, con la historia truncada
ahi — sin dejar que un GET materialice rutas, que sigue vetado a proposito.
"""

from django.test import Client

from . import ingest, logic
from .testing import TestCase


class PlayLandingTests(TestCase):

    def test_unknown_target_lands_on_deepest_materialized_prefix(self):
        root = ingest.get_or_create_position(logic.start_fen())
        first = logic.legal_moves(root.fen)[0]
        child_fen = logic.apply_move(root.fen, first)
        child = ingest.get_or_create_position(child_fen)
        second = logic.legal_moves(child_fen)[0]
        grand_key = logic.key_of(logic.apply_move(child_fen, second))

        response = Client().get(
            f'/atomicdb/explore/{grand_key}/?play={first},{second}')

        self.assertEqual(response.status_code, 302)
        self.assertIn(child.key, response['Location'])
        self.assertIn(f'play={first}', response['Location'])
        self.assertNotIn(second, response['Location'])

    def test_a_route_no_engine_would_accept_still_404s(self):
        response = Client().get(
            f'/atomicdb/explore/{"0" * 64}/?play=zz99')
        self.assertEqual(response.status_code, 404)

    def test_no_route_keeps_the_plain_404(self):
        response = Client().get(f'/atomicdb/explore/{"0" * 64}/')
        self.assertEqual(response.status_code, 404)
