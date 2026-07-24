"""Explorer route and opening-name integration tests."""

from unittest import mock

from . import ingest, logic
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

    def test_route_with_missing_intermediate_position_fails_closed(self):
        ucis = ['g1f3', 'f7f6', 'b1c3']
        ingest.get_or_create_position(logic.start_fen())
        fen = logic.start_fen()
        for uci in ucis:
            fen = logic.apply_move(fen, uci)
        target = ingest.get_or_create_position(fen)

        response = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {'play': ','.join(ucis)},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertContains(
            response, 'not fully materialized', status_code=409)

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

    def test_last_opening_is_retained_after_named_position(self):
        ucis = ['g1f3', 'f7f6', 'b1c3', 'a7a6']
        target = self._materialize(ucis)[-1]

        response = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {'play': ','.join(ucis)},
        )

        opening = response.context['opening']
        self.assertEqual(opening['name'], 'Two Knights Opening')
        self.assertEqual(opening['matched_ply'], 3)
        self.assertFalse(opening['exact'])
        self.assertContains(response, 'continued from ply 3')

    def test_goto_preserves_and_extends_validated_route(self):
        ucis = ['g1f3', 'f7f6']
        current = self._materialize(ucis)[-1]

        response = self.client.get(
            f'/atomicdb/goto/{current.key}/b1c3/',
            {'play': ','.join(ucis)},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn('?play=g1f3,f7f6,b1c3', response['Location'])

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
            move for move in response.context['unexplored']
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
        with mock.patch(
                'atomicdb.views.openings.match_line',
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

    def test_nested_provenance_is_human_readable(self):
        ucis = ['g1f3', 'f7f6', 'b1c3']
        target = self._materialize(ucis)[-1]

        response = self.client.get(
            f'/atomicdb/explore/{target.key}/',
            {'play': ','.join(ucis)},
        )

        self.assertContains(
            response, 'Two Knights Attack (atomix-0096)')
        self.assertNotContains(response, 'labels_differ')
