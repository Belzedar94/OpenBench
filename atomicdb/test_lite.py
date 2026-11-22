"""The lite explorer: one position on a page that needs no JavaScript.

It is for e-readers, so the page must render with no script at all, every
move must be a plain link to the lite page of the position it leads to, and
a bad FEN or an unknown position must get a message, never an error page.
"""

from . import ingest, logic
from .lite import PATH_MAX, lite_url
from .models import Edge, Position
from .testing import TestCase

URL = '/atomicdb/lite/'


def _child(parent, uci, **fields):
    child = ingest.get_or_create_position(logic.apply_move(parent.fen, uci))
    if fields:
        Position.objects.filter(key=child.key).update(**fields)
    Edge.objects.create(parent=parent, move_uci=uci, child=child)
    return child


class LitePageTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        Position.objects.filter(key=self.root.key).update(
            eval_cp=120, nodes_invested=128_000_000,
            last_analysis=[{'move': 'g1f3', 'eval_cp': 120, 'mate': None,
                            'pv': ['g1f3', 'f7f6']}])
        self.root.refresh_from_db()
        self.nf3 = _child(self.root, 'g1f3', eval_cp=120,
                          nodes_invested=128_000_000)
        self.e4 = _child(self.root, 'e2e4', eval_cp=40,
                         nodes_invested=128_000_000)

    def _body(self, query=''):
        response = self.client.get(URL + query)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_start_position_is_the_default(self):
        body = self._body()
        self.assertIn('White to move', body)
        self.assertIn('<table class="board"', body)
        # Engine line and best move, in SAN, and the header value.
        self.assertIn('Best move: <strong>Nf3</strong>', body)
        self.assertIn('Nf3 f6', body)
        self.assertIn('+120cp', body)

    def test_there_is_no_script_anywhere(self):
        for query in ('', f'?key={self.nf3.key}', '?fen=rubbish',
                      '?letters=1'):
            with self.subTest(query=query):
                self.assertNotIn('<script', self._body(query).lower())

    def test_each_move_links_to_the_lite_page_of_its_position(self):
        body = self._body()
        for child, san in ((self.nf3, 'Nf3'), (self.e4, 'e4')):
            href = lite_url(child.key, [self.root.key]).replace('&', '&amp;')
            self.assertIn(f'<a href="{href}">{san}</a>', body)

    def test_a_child_page_goes_back_to_its_parent(self):
        body = self._body(f'?key={self.nf3.key}&path={self.root.key}')
        self.assertIn('Black to move', body)
        self.assertIn(f'href="/atomicdb/lite/?key={self.root.key}">'
                      '\u00ab Back</a>', body)
        self.assertIn(f'href="/atomicdb/explore/{self.nf3.key}/"', body)

    def test_a_fen_finds_the_same_position_as_its_key(self):
        fen = logic.apply_move(logic.start_fen(), 'e2e4')
        by_fen = self._body('?fen=' + fen.replace(' ', '+'))
        self.assertIn(f'/atomicdb/explore/{self.e4.key}/', by_fen)

    def test_a_bad_fen_gets_a_message_and_a_200(self):
        body = self._body('?fen=rubbish/8+w')
        self.assertIn('That FEN was not accepted', body)
        self.assertIn('name="fen"', body)

    def test_an_unknown_position_gets_a_message_and_is_not_created(self):
        fen = '4k3/8/8/8/8/8/8/R3K3 w - - 0 1'
        before = Position.objects.count()
        body = self._body('?fen=' + fen.replace(' ', '+'))
        self.assertIn('not in AtomicDB yet', body)
        # The board of a legal position is still drawn.
        self.assertIn('<table class="board"', body)
        self.assertEqual(Position.objects.count(), before)

    def test_a_malformed_key_or_path_is_harmless(self):
        self.assertIn('not valid', self._body('?key=<b>nope</b>'))
        body = self._body(f'?key={self.nf3.key}&path=zzz,{self.root.key}')
        self.assertNotIn('Back</a>', body)

    def test_the_back_history_is_capped(self):
        path = [self.root.key] * (PATH_MAX + 5)
        url = lite_url(self.nf3.key, path)
        self.assertEqual(url.count(self.root.key), PATH_MAX)

    def test_a_proven_position_shows_its_verdict_and_mate_distance(self):
        Position.objects.filter(key=self.e4.key).update(
            status='WHITE_WIN', closure='MINIMAX', mate_in=4)
        body = self._body()
        # The row counts its own move: 1 + 4 plies is mate in 3.  The page of
        # the position itself says what the explorer's verdict says.
        self.assertIn('M3', body)
        page = self._body(f'?key={self.e4.key}')
        self.assertIn('<strong>White wins</strong> \u00b7 MINIMAX', page)
        self.assertIn('<strong>M2</strong>', page)

    def test_squares_are_coloured_with_a1_dark(self):
        body = self._body('?letters=1')
        rank1 = body[body.index('<td class="lbl">1</td>'):]
        rank1 = rank1[:rank1.index('</tr>')]
        cells = rank1.split('<td class="')[2:]
        self.assertTrue(cells[0].startswith('d'))     # a1
        self.assertTrue(cells[7].startswith('l'))     # h1

    def test_the_full_explorer_links_here(self):
        body = self.client.get(
            f'/atomicdb/explore/{self.root.key}/').content.decode()
        self.assertIn(f'/atomicdb/lite/?key={self.root.key}', body)
