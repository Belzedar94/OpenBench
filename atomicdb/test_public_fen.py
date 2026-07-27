"""The FEN box must read what people actually paste.

A FEN copied out of a GUI or a chat often arrives with the halfmove counter
and no fullmove number.  The shape check demanded both or neither, so the
owner's five-field FEN was rejected as malformed — and the rejection page said
"Position not in the tree (yet)", which is a different statement and sent
everyone looking for the wrong bug.
"""

from django.test import SimpleTestCase

from . import logic
from .testing import TestCase
from .views import PublicFenError, _parse_public_fen

OWNER_FEN = '2r1B3/r1P5/8/3pp1pp/1B1PP1PP/8/7K/2N2bkR w - - 19'


class PublicFenParserTests(SimpleTestCase):

    def test_four_five_and_six_fields_all_parse_to_one_identity(self):
        board = '2r1B3/r1P5/8/3pp1pp/1B1PP1PP/8/7K/2N2bkR w - -'
        parsed = {_parse_public_fen(board),
                  _parse_public_fen(board + ' 19'),
                  _parse_public_fen(board + ' 19 42')}
        self.assertEqual(len(parsed), 1)
        # The canonical identity zeroes the counters, so the missing field
        # was never information in the first place.
        self.assertTrue(parsed.pop().endswith(' 0 1'))

    def test_the_owners_exact_fen_is_accepted(self):
        self.assertEqual(_parse_public_fen(OWNER_FEN),
                         logic.canonical_fen(OWNER_FEN + ' 1'))

    def test_underscores_still_work_as_separators(self):
        self.assertEqual(
            _parse_public_fen(OWNER_FEN.replace(' ', '_')),
            _parse_public_fen(OWNER_FEN))

    def test_each_refusal_says_which_one_it_is(self):
        cases = {
            '': 'empty',
            'rubbish': 'malformed',
            '8/8/8/8/8/8/8/8 w - -': 'kings',
            '4k3/8/8/8/8/8/8/4K3 x - -': 'malformed',
            '4k4/8/8/8/8/8/8/4K3 w - -': 'ranks',
        }
        for raw, reason in cases.items():
            with self.assertRaises(PublicFenError) as caught:
                _parse_public_fen(raw)
            self.assertEqual(caught.exception.reason, reason, raw)

    def test_an_oversized_string_is_refused_before_anything_else(self):
        with self.assertRaises(PublicFenError) as caught:
            _parse_public_fen('8/' * 200)
        self.assertEqual(caught.exception.reason, 'malformed')


class FenJumpTests(TestCase):

    def test_a_valid_fen_lands_on_its_position(self):
        response = self.client.post('/atomicdb/fen/', {'fen': OWNER_FEN})
        self.assertEqual(response.status_code, 302)
        self.assertIn(logic.key_of(_parse_public_fen(OWNER_FEN)),
                      response['Location'])

    def test_a_bad_fen_says_what_is_wrong_with_it(self):
        response = self.client.post('/atomicdb/fen/', {'fen': 'rubbish'})
        self.assertEqual(response.status_code, 400)
        body = response.content.decode()
        self.assertIn('was not accepted', body)
        self.assertNotIn('not in the tree', body)
