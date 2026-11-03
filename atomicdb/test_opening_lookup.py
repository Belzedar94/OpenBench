"""An opening name in the Go box (cz_eclipsia, 3-Nov-2026).

"The best way to navigate to an opening is to go the roots page and ctrl+f
it": the box that takes a FEN or a PGN takes a name too.
"""

from unittest import mock

from django.test import Client

from . import logic, openings
from .models import Position
from .testing import TestCase


def _by_name(name):
    return next(o for o in openings.load_catalog().values() if o.name == name)


class OpeningLookupTests(TestCase):

    def _go(self, text):
        return Client().post('/atomicdb/fen/', {'fen': text})

    def test_a_unique_name_jumps_to_its_position(self):
        charon = _by_name('Charon Gambit')

        response = self._go('  charon   GAMBIT ')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{charon.position_key}/')
        self.assertTrue(Position.objects.filter(
            key=charon.position_key).exists())

    def test_several_matches_are_listed(self):
        response = self._go('gambit')

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Openings matching', body)
        self.assertIn(
            f'/atomicdb/explore/{_by_name("Charon Gambit").position_key}/',
            body)

    def test_a_match_by_another_name_says_which(self):
        opening = next(o for o in openings.load_catalog().values()
                       if o.aliases and o.aliases[0].lower()
                       not in o.name.lower())
        alias = opening.aliases[0]

        response = self._go(alias)

        if response.status_code == 302:
            self.assertIn(opening.position_key, response['Location'])
        else:
            self.assertIn(f'(also &ldquo;{alias}&rdquo;)',
                          response.content.decode())

    def test_an_exact_name_beats_the_longer_ones_containing_it(self):
        atomic = _by_name('Atomic Attack')

        response = self._go('Atomic Attack')

        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{atomic.position_key}/')

    def test_an_approved_community_name_is_found(self):
        key = logic.key_of(logic.start_fen())
        names = {key: {'name': 'Belfast Opening', 'kind': 'NEW',
                       'corrects': '', 'approved_by': 'x',
                       'approved_at': None}}
        with mock.patch('atomicdb.community_names.approved_map',
                        return_value=names):
            response = self._go('belfast opening')

        self.assertEqual(response['Location'], f'/atomicdb/explore/{key}/')

    def test_a_pgn_and_a_fen_still_take_their_paths(self):
        self.assertEqual(self._go('1. Nf3 f6').status_code, 302)
        fen = logic.start_fen()
        self.assertEqual(self._go(fen)['Location'],
                         f'/atomicdb/explore/{logic.key_of(fen)}/')

    def test_no_match_says_no_opening_is_called_that(self):
        response = self._go('zzqqxx')

        self.assertEqual(response.status_code, 400)
        self.assertIn('No opening is called', response.content.decode())

    def test_a_broken_fen_keeps_the_fen_answer(self):
        response = self._go('rnbqkbnr/pppppppp w')

        self.assertEqual(response.status_code, 400)
        self.assertIn('That FEN was not accepted', response.content.decode())
