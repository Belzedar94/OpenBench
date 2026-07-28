"""A breadcrumb always starts at the root, or says why it cannot.

The tree reached 40-90 plies the day the community brought 372 cores, and the
reverse walk to the root started running out of budget.  When it did, the
renderer kept the TAIL and dropped the head, so the home page showed
"… d4 Bc4 Bg7 d3 bxc4 e4 Nf6" — a line with no identity, which reads exactly
like a bug because it is one.
"""

from . import logic, views
from .testing import TestCase


class _Top:
    def __init__(self, fen):
        self.fen = fen


def _line(plies):
    return [{'san': f'M{index}', 'white': index % 2 == 0, 'key': str(index)}
            for index in range(plies)]


class FormatSanLineTests(TestCase):

    def setUp(self):
        self.root = _Top(logic.start_fen())
        self.deep = _Top('8/8/8/8/8/8/1k6/K6Q w - - 0 1')

    def test_a_short_line_is_numbered_from_move_one(self):
        text = views._format_san_line(self.root, _line(4))
        self.assertTrue(text.startswith('1. M0 M1 2. M2 M3'))
        self.assertNotIn('…', text)

    def test_a_long_line_keeps_its_head_AND_its_tail(self):
        text = views._format_san_line(self.root, _line(60), max_plies=16)
        self.assertTrue(text.startswith('1. M0'))     # the opening survives
        self.assertIn('…', text)
        self.assertIn('M59', text)                    # so does where we are

    def test_a_long_line_never_starts_with_the_ellipsis(self):
        """The reported bug, pinned."""
        text = views._format_san_line(self.root, _line(90), max_plies=16)
        self.assertFalse(text.startswith('…'))
        self.assertFalse(text.startswith('… '))

    def test_keep_head_still_means_head_only(self):
        text = views._format_san_line(self.root, _line(60), max_plies=10,
                                      keep_head=True)
        self.assertTrue(text.startswith('1. M0'))
        self.assertTrue(text.endswith('…'))

    def test_a_budget_exhausted_line_is_labelled_not_muted(self):
        text = views._format_san_line(
            self.deep, _line(views.LINEAGE_SEARCH_MAX_PLIES))
        self.assertIn('deep line', text)
        self.assertIn(f'ply ≥{views.LINEAGE_SEARCH_MAX_PLIES}', text)
        self.assertFalse(text.startswith('…'))

    def test_a_short_orphan_fragment_keeps_its_old_honest_shape(self):
        """Not depth: a genuinely unrooted position, and the page says so."""
        text = views._format_san_line(self.deep, _line(2))
        self.assertTrue(text.startswith('… '))
        self.assertNotIn('deep line', text)

    def test_an_empty_line_is_unchanged(self):
        self.assertEqual(views._format_san_line(self.root, []), '')
        self.assertEqual(views._format_san_line(self.deep, []), '…')

    def test_the_search_ceiling_covers_the_deep_tree(self):
        self.assertGreaterEqual(views.LINEAGE_SEARCH_MAX_PLIES, 96)


class LineboxCopyTests(TestCase):
    """What a copy carries is the text nodes, never the CSS.

    Wolfram selected a line in the explorer, pasted it, and got
    "1. Nf3f62. e4d53. Nc3Bg4" — the plies were separated by margins,
    which the clipboard does not see.  The separator must be a real
    space in the markup.
    """

    def test_a_copied_line_has_spaces_between_plies(self):
        from django.utils.html import strip_tags

        from . import ingest
        from .models import Edge

        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        nf3 = Edge.objects.get(parent=root, move_uci='g1f3').child
        ingest.expand(nf3)
        nf6 = Edge.objects.get(parent=nf3, move_uci='g8f6').child

        body = self.client.get(
            f'/atomicdb/explore/{nf6.key}/').content.decode()
        linebox = body.split('class="linebox"', 1)[1].split('</div>', 1)[0]
        text = strip_tags(linebox)

        self.assertIn('1. Nf3 Nf6', ' '.join(text.split()))


class LineboxCssTests(TestCase):

    def test_the_move_list_wraps_or_scrolls_inside_its_own_box(self):
        import pathlib

        from django.conf import settings
        css = (pathlib.Path(settings.BASE_DIR) / 'atomicdb' / 'static'
               / 'atomicdb' / 'atomicdb.css').read_text(encoding='utf-8')
        block = css.split('.linebox {', 1)[1].split('}', 1)[0]
        self.assertIn('max-width: 100%', block)
        self.assertIn('overflow-wrap: anywhere', block)
        self.assertIn('overflow-x: auto', block)
