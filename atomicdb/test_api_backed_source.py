"""``/api/backed-source``: where a value comes from, as JSON (BManBJ, 15-Nov-2026)."""

from . import ingest, logic
from .models import Edge, Position
from .testing import TestCase


class BackedSourceApiTests(TestCase):

    def _play(self, position, uci):
        child = ingest.get_or_create_position(
            logic.apply_move(position.fen, uci))
        Edge.objects.update_or_create(parent=position, move_uci=uci,
                                      defaults={'child': child})
        return child

    def test_it_returns_the_origin_and_the_moves_leading_there(self):
        start = ingest.get_or_create_position(logic.start_fen())
        mid = self._play(start, 'g1f3')
        deep = self._play(mid, 'f7f6')
        Position.objects.filter(key=start.key).update(
            eval_cp=40, backed_eval=300, backed_move='g1f3')
        Position.objects.filter(key=mid.key).update(
            eval_cp=90, backed_eval=300, backed_move='f7f6')
        Position.objects.filter(key=deep.key).update(
            eval_cp=300, backed_eval=300)

        response = self.client.get('/atomicdb/api/backed-source',
                                   {'fen': logic.start_fen()})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['moves'], ['g1f3', 'f7f6'])
        self.assertEqual(body['plies'], 2)
        self.assertEqual(body['end'], 'origin')
        self.assertEqual(body['origin']['key'], deep.key)
        self.assertEqual(body['position']['key'], start.key)
        self.assertIn('analysis', body['origin'])

    def test_a_position_that_owns_its_value_is_its_own_origin(self):
        start = ingest.get_or_create_position(logic.start_fen())
        Position.objects.filter(key=start.key).update(eval_cp=40,
                                                      backed_eval=40)

        body = self.client.get('/atomicdb/api/backed-source',
                               {'fen': logic.start_fen()}).json()

        self.assertEqual((body['moves'], body['end']), ([], 'origin'))
        self.assertEqual(body['origin']['key'], start.key)

    def test_an_unknown_or_bad_fen_is_refused(self):
        self.assertEqual(self.client.get('/atomicdb/api/backed-source',
                                         {'fen': 'nonsense'}).status_code, 400)
        self.assertEqual(self.client.get(
            '/atomicdb/api/backed-source',
            {'fen': '4k3/8/8/8/8/8/8/R3K3 w - - 0 1'}).status_code, 404)
