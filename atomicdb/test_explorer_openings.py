"""Explorer route and opening-name integration tests."""

from urllib.parse import parse_qs, urlsplit
from unittest import mock

from django.core import signing

from . import ingest, logic, views
from .models import Edge
from .testing import TestCase


class ExplorerOpeningRouteTests(TestCase):

    def _materialize(self, ucis, *, connect=False):
        fen = logic.start_fen()
        current = ingest.get_or_create_position(fen)
        positions = [current]
        for uci in ucis:
            fen = logic.apply_move(fen, uci)
            child = ingest.get_or_create_position(fen)
            if connect:
                Edge.objects.update_or_create(
                    parent=current,
                    move_uci=uci,
                    defaults={'child': child},
                )
            current = child
            positions.append(current)
        return positions

    @staticmethod
    def _long_villager_route(plies):
        """Ruta larga en la Villager Defense, SIN pasar dos veces por nada.

        Esta ayudante construia la longitud con un vaiven de caballos
        (``f3g1 b8c6 g1f3 c6b8``), que es una ruta que reentra en sus propias
        posiciones una y otra vez.  Desde el 6-ago eso no existe en el
        explorador: una ruta no puede reentrar (invariante 6 de
        docs/value-semantics.md), asi que el vaiven ya no es una ruta larga —
        es una ruta de tres plies y un cruce.

        Lo que estos tests miden es el desbordamiento del tope de rejuego y
        el ancla firmada que lo sustituye, y eso necesita LONGITUD, no
        repeticion.  Se camina en profundidad: de cada posicion, la primera
        jugada en orden estable que lleve a una clave nunca vista y no deje
        la linea sin continuacion.  Las dos primeras son las de siempre, para
        que la ficha de apertura siga siendo la Villager Defense.
        """
        fen = logic.start_fen()
        seen = {logic.key_of(fen)}
        moves = []
        for uci in ('g1f3', 'd7d6'):
            fen = logic.apply_move(fen, uci)
            seen.add(logic.key_of(fen))
            moves.append(uci)
        while len(moves) < plies:
            for uci in sorted(logic.legal_moves(fen)):
                nxt = logic.apply_move(fen, uci)
                key = logic.key_of(nxt)
                if key in seen or not logic.legal_moves(nxt):
                    continue
                fen = nxt
                seen.add(key)
                moves.append(uci)
                break
            else:
                raise AssertionError(
                    f'sin continuacion nueva en el ply {len(moves)}')
        return moves[:plies]

    def test_valid_play_route_is_replayed_and_propagated(self):
        ucis = ['g1f3', 'f7f6', 'b1c3']
        target = self._materialize(ucis)[-1]

        response = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {'play': ','.join(ucis)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_play'], ucis)
        self.assertTrue(response.context['line_from_root'])
        self.assertEqual(
            response.context['line'][-1]['url'],
            f'/atomicdb/explore/{target.key}/?play=g1f3,f7f6,b1c3',
        )
        self.assertEqual(
            response.context['opening']['name'], 'Two Knights Opening')
        self.assertTrue(response.context['opening']['exact'])
        self.assertContains(response, 'data-play="g1f3,f7f6,b1c3"')

    def test_route_with_missing_intermediate_lands_on_deepest_prefix(self):
        # Antes esto era un 409 "not fully materialized".  Desde el 8-ago
        # (reporte de comunidad: "it should just build a tree from the pgn")
        # un PGN legal cuyo prefijo el arbol no conoce ATERRIZA en el punto
        # mas profundo materializado, con la historia truncada ahi.  Sigue
        # sin materializarse nada: la ruta se trunca, no se acepta.
        ucis = ['g1f3', 'f7f6', 'b1c3']
        root = ingest.get_or_create_position(logic.start_fen())
        fen = logic.start_fen()
        for uci in ucis:
            fen = logic.apply_move(fen, uci)
        target = ingest.get_or_create_position(fen)

        response = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {'play': ','.join(ucis)},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertIn(root.key, response['Location'])
        self.assertNotIn('b1c3', response['Location'])

    def test_route_target_mismatch_returns_conflict_without_fallback(self):
        ucis = ['g1f3', 'f7f6', 'b1c3']
        target = self._materialize(ucis, connect=True)[-1]

        response = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {'play': 'g1f3'},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertContains(
            response, 'does not reach', status_code=409)

    def test_transposed_route_recognizes_same_opening(self):
        transposed = ['b1c3', 'f7f6', 'g1f3']
        target = self._materialize(transposed)[-1]

        response = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {'play': ','.join(transposed)},
        )

        self.assertEqual(response.context['active_play'], transposed)
        self.assertEqual(
            response.context['opening']['name'], 'Two Knights Opening')
        self.assertTrue(response.context['opening']['exact'])

    def test_explicit_play_route_wins_over_valid_opening_anchor(self):
        ucis = ['g1f3', 'f7f6', 'b1c3']
        target = self._materialize(ucis)[-1]
        villager = views.openings.match_line(['g1f3', 'd7d6'])
        anchor = views._signed_opening_anchor(
            target.key, villager, len(ucis))

        response = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {
                'play': ','.join(ucis),
                views.OPENING_ANCHOR_PARAM: anchor,
            },
        )

        self.assertEqual(response.context['active_play'], ucis)
        self.assertEqual(
            response.context['opening']['name'], 'Two Knights Opening')
        self.assertTrue(response.context['opening']['exact'])
        self.assertTrue(all(
            'play=' in link['url']
            for link in response.context['legal_move_links']
        ))

    def test_unnamed_continuation_keeps_villager_defense(self):
        ucis = ['g1f3', 'd7d6', 'f3g5']
        target = self._materialize(ucis)[-1]

        response = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {'play': ','.join(ucis)},
        )

        self.assertEqual(
            response.context['opening']['name'], 'Villager Defense')
        self.assertFalse(response.context['opening']['exact'])
        self.assertEqual(response.context['opening']['matched_ply'], 2)
        self.assertContains(response, 'Villager Defense')
        self.assertNotContains(response, 'exact position')
        self.assertNotContains(response, 'Aliases and provenance')

    def test_goto_preserves_and_extends_validated_route(self):
        ucis = ['g1f3', 'f7f6']
        current = self._materialize(ucis)[-1]

        response = self.client.get(
            f'/atomicdb/goto/{current.key}/b1c3/',
            {'play': ','.join(ucis)},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn('?play=g1f3,f7f6,b1c3', response['Location'])

    def test_signed_anchor_keeps_opening_beyond_public_replay_limit(self):
        route = self._long_villager_route(views.PLAY_ROUTE_MAX_PLIES + 2)
        current = self._materialize(
            route[:views.PLAY_ROUTE_MAX_PLIES])[-1]

        overflow = self.client.get(
            f'/atomicdb/goto/{current.key}/{route[64]}/',
            {'play': ','.join(route[:64])},
        )

        self.assertEqual(overflow.status_code, 302)
        self.assertNotIn('play=', overflow['Location'])
        query = parse_qs(urlsplit(overflow['Location']).query)
        self.assertEqual(set(query), {views.OPENING_ANCHOR_PARAM})
        anchor = query[views.OPENING_ANCHOR_PARAM][0]

        inherited = self.client.get(overflow['Location'])
        self.assertEqual(inherited.status_code, 200)
        self.assertIsNone(inherited.context['active_play'])
        # La apertura viaja HEREDADA desde el ply 2 y no vuelve a coincidir:
        # una ruta que no reentra cruza la posicion catalogada UNA vez.  (El
        # vaiven de antes la volvia a pisar cada cuatro plies, y de ahi
        # salian el 62 y el 66 que este test comprobaba.)
        self.assertEqual(
            inherited.context['opening']['name'], 'Villager Defense')
        self.assertEqual(inherited.context['opening']['matched_ply'], 2)
        self.assertFalse(inherited.context['opening']['exact'])
        board_play = inherited.context['board_play']
        self.assertTrue(
            board_play.startswith(views.OPENING_ANCHOR_PLAY_PREFIX))

        child = inherited.context['pos']
        replacement = self.client.get(
            f'/atomicdb/goto/{child.key}/{route[65]}/',
            {'play': board_play},
        )
        deeper = self.client.get(replacement['Location'])

        # Y el ancla sigue nombrando la apertura un ply mas alla del tope,
        # que es lo que este test existe para medir.
        self.assertEqual(deeper.status_code, 200)
        self.assertIsNone(deeper.context['active_play'])
        self.assertEqual(deeper.context['opening']['name'],
                         'Villager Defense')
        self.assertEqual(deeper.context['opening']['matched_ply'], 2)

    def test_signed_anchor_beats_short_transposed_canonical_route(self):
        route = self._long_villager_route(views.PLAY_ROUTE_MAX_PLIES + 2)
        # Se materializa DOS plies mas alla del tope y se arranca en el 64:
        # el test mira la tabla de jugadas de la posicion de aterrizaje, y
        # con una ruta que no reentra esa posicion es nueva — solo tiene
        # hijos si la linea sigue conectada por debajo.  (Con el vaiven de
        # antes los tenia "gratis", porque aterrizaba en una posicion que el
        # propio prefijo ya habia creado.)
        current = self._materialize(
            route[:views.PLAY_ROUTE_MAX_PLIES + 2],
            connect=True)[views.PLAY_ROUTE_MAX_PLIES]

        overflow = self.client.get(
            f'/atomicdb/goto/{current.key}/{route[64]}/',
            {'play': ','.join(route[:64])},
        )
        inherited = self.client.get(overflow['Location'])

        self.assertEqual(inherited.status_code, 200)
        self.assertIsNone(inherited.context['active_play'])
        self.assertEqual(
            inherited.context['opening']['name'], 'Villager Defense')
        self.assertEqual(inherited.context['opening']['matched_ply'], 2)
        self.assertFalse(inherited.context['opening']['exact'])
        self.assertTrue(inherited.context['moves'])
        for move in inherited.context['moves']:
            query = parse_qs(urlsplit(move['url']).query)
            self.assertNotIn('play', query)
            self.assertIn(views.OPENING_ANCHOR_PARAM, query)
        for link in inherited.context['legal_move_links']:
            query = parse_qs(urlsplit(link['url']).query)
            self.assertNotIn('play', query)
            self.assertIn(views.OPENING_ANCHOR_PARAM, query)

        anchored_breadcrumbs = [
            step for step in inherited.context['line'] if step.get('url')
        ]
        self.assertGreaterEqual(len(anchored_breadcrumbs), 2)
        for step in anchored_breadcrumbs:
            query = parse_qs(urlsplit(step['url']).query)
            self.assertNotIn('play', query)
            token = query[views.OPENING_ANCHOR_PARAM][0]
            payload = signing.loads(
                token, salt=views.OPENING_ANCHOR_SALT)
            self.assertEqual(payload['target'], step['key'])
            self.assertGreaterEqual(
                payload['route_ply'], payload['matched_ply'])
        previous = anchored_breadcrumbs[-2]
        backed_up = self.client.get(previous['url'])
        self.assertIsNone(backed_up.context['active_play'])
        self.assertEqual(
            backed_up.context['opening']['name'], 'Villager Defense')
        self.assertFalse(backed_up.context['opening']['exact'])

        continued = next(
            move for move in inherited.context['moves']
            if move['uci'] == route[65]
        )
        child = self.client.get(continued['url'])
        self.assertIsNone(child.context['active_play'])
        self.assertEqual(
            child.context['opening']['name'], 'Villager Defense')
        self.assertEqual(child.context['opening']['matched_ply'], 2)

    def test_opening_anchor_tampering_and_target_reuse_fail_closed(self):
        route = self._long_villager_route(views.PLAY_ROUTE_MAX_PLIES + 1)
        current = self._materialize(route[:64])[-1]
        overflow = self.client.get(
            f'/atomicdb/goto/{current.key}/{route[64]}/',
            {'play': ','.join(route[:64])},
        )
        query = parse_qs(urlsplit(overflow['Location']).query)
        anchor = query[views.OPENING_ANCHOR_PARAM][0]
        child = self.client.get(overflow['Location']).context['pos']
        tampered = anchor[:-1] + ('A' if anchor[-1] != 'A' else 'B')

        bad_signature = self.client.get(
            f'/atomicdb/explore/{child.key}/',
            {views.OPENING_ANCHOR_PARAM: tampered},
        )
        wrong_target = self.client.get(
            f'/atomicdb/explore/{current.key}/',
            {views.OPENING_ANCHOR_PARAM: anchor},
        )

        self.assertIsNone(bad_signature.context['opening'])
        self.assertIsNone(wrong_target.context['opening'])
        self.assertTrue(all(
            tampered not in link['url']
            for link in bad_signature.context['legal_move_links']
        ))

    def test_even_validly_signed_anchor_requires_catalogued_opening(self):
        route = self._long_villager_route(65)
        child = self._materialize(route)[-1]
        forged = signing.dumps(
            {
                'v': 1,
                'target': child.key,
                'opening': '0' * 64,
                'matched_ply': 2,
                'route_ply': 65,
            },
            salt=views.OPENING_ANCHOR_SALT,
            compress=True,
        )

        response = self.client.get(
            f'/atomicdb/explore/{child.key}/',
            {views.OPENING_ANCHOR_PARAM: forged},
        )

        self.assertIsNone(response.context['opening'])

    def test_goto_rejects_bad_explicit_route_before_writing(self):
        current = self._materialize(['g1f3'], connect=True)[-1]
        edge_count = Edge.objects.count()

        response = self.client.get(
            f'/atomicdb/goto/{current.key}/f7f6/',
            {'play': 'not-a-uci'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(Edge.objects.count(), edge_count)

    def test_illegal_and_overlong_routes_are_bad_requests(self):
        target = self._materialize(['g1f3'])[-1]
        illegal = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {'play': 'e2e5'},
        )
        overlong = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {'play': 'g1f3,' * 100},
        )

        self.assertEqual(illegal.status_code, 400)
        self.assertEqual(overlong.status_code, 400)
        self.assertEqual(illegal['Cache-Control'], 'no-store')
        self.assertEqual(overlong['Cache-Control'], 'no-store')

    def test_named_child_is_marked_as_entering_opening(self):
        ucis = ['g1f3', 'f7f6']
        current = self._materialize(ucis)[-1]

        response = self.client.get(
            f'/atomicdb/explore/{current.key}/',
            {'play': ','.join(ucis)},
        )

        child = next(
            move for move in response.context['offtree']
            if move['uci'] == 'b1c3'
        )
        self.assertEqual(child['enters_opening'], 'Two Knights Opening')
        self.assertContains(response, 'enters Two Knights Opening')

    def test_opening_text_is_escaped_and_unsafe_source_is_not_linked(self):
        root = ingest.get_or_create_position(logic.start_fen())
        malicious = {
            'position_key': root.key,
            'name': '<script>alert("opening")</script>',
            'status': 'canonical',
            'confidence': 'confirmed',
            'aliases': ['<img src=x onerror=alert(1)>'],
            'reference_line_san': '<b>1. Boom</b>',
            'matched_ply': 0,
            'current_key': root.key,
            'exact': True,
            'sources': [{
                'name': '<svg onload=alert(2)>',
                'source_kind': 'modern',
                'status': 'canonical',
                'confidence': 'confirmed',
                'line_san': '<em>line</em>',
                'evidence': [{
                    'kind': 'study',
                    'label': '<i>unsafe</i>',
                    'url': 'javascript:alert(3)',
                }],
                'issues': [],
                'provenance': {'source_row': '<u>row</u>'},
            }],
        }
        # Los dos caminos por los que una ruta puede nombrar una apertura: por
        # sus claves cuando la vista ya las tiene, y rejugandola cuando no
        # (§ views._navigation_opening).  El escapado es de la plantilla, asi
        # que da igual cual conteste, pero el test tiene que inyectar por el
        # que la vista use de verdad.
        with mock.patch(
                'atomicdb.views.openings.match_line',
                return_value=malicious), mock.patch(
                    'atomicdb.views.openings.match_line_keys',
                    return_value=malicious), mock.patch(
                    'atomicdb.views.openings.lookup_key',
                    return_value=None):
            response = self.client.get(f'/atomicdb/explore/{root.key}/')

        html = response.content.decode()
        self.assertNotIn('<script>alert("opening")</script>', html)
        self.assertNotIn('<img src=x onerror=alert(1)>', html)
        self.assertNotIn('<svg onload=alert(2)>', html)
        self.assertNotIn('href="javascript:alert(3)"', html)
        self.assertIn(
            '&lt;script&gt;alert("opening")&lt;/script&gt;',
            html,
        )

    def test_aliases_and_provenance_are_not_rendered(self):
        ucis = ['g1f3', 'f7f6', 'b1c3']
        target = self._materialize(ucis)[-1]

        response = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {'play': ','.join(ucis)},
        )

        self.assertContains(response, 'Two Knights Opening')
        self.assertNotContains(response, 'Aliases and provenance')
        self.assertNotContains(response, 'Two Knights Attack (atomix-0096)')
        self.assertNotContains(response, 'exact position')
