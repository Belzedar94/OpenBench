"""The copy button and the lichess link of the explorer's FEN box."""

import html
import re

from django.test import Client, SimpleTestCase

from . import ingest, logic
from .templatetags.atomicdb_links import lichess_analysis
from .testing import TestCase


class LichessLinkFormatTests(SimpleTestCase):

    def test_spaces_become_underscores_and_the_ranks_keep_their_slashes(self):
        self.assertEqual(
            lichess_analysis(logic.start_fen()),
            'https://lichess.org/analysis/atomic/'
            'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR_w_KQkq_-_0_1')

    def test_anything_else_is_escaped(self):
        self.assertEqual(lichess_analysis(' 8/8/8/8/8/8/8/K6k w - - 0 1?#'),
                         'https://lichess.org/analysis/atomic/'
                         '8/8/8/8/8/8/8/K6k_w_-_-_0_1%3F%23')


class FenBoxTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.body = Client().get(
            f'/atomicdb/explore/{self.root.key}/').content.decode()
        start = self.body.index('fenbox-here')
        self.box = self.body[start:self.body.index('</form>', start)]

    def test_the_box_still_goes_there(self):
        self.assertIn('action="/atomicdb/fen/"', self.body)
        self.assertIn('>Go</button>', self.box)

    def test_the_box_has_a_copy_button(self):
        button = re.search(r'<button[^>]*id="fen-copy"[^>]*>(<svg)?',
                           self.box)
        self.assertIn('aria-label="Copy this FEN"', button.group(0))
        self.assertEqual(button.group(1), '<svg')
        self.assertRegex(self.box, r'<button[^>]*type="button"[^>]*id="fen-copy"'
                         r'|<button[^>]*id="fen-copy"[^>]*type="button"')
        self.assertIn('navigator.clipboard.writeText', self.body)

    def test_the_lichess_link_opens_this_position_in_a_new_tab(self):
        link = re.search(r'<a[^>]*id="fen-lichess"[^>]*>', self.box).group(0)

        href = html.unescape(re.search(r'href="([^"]+)"', link).group(1))
        self.assertEqual(href, lichess_analysis(self.root.fen))
        self.assertIn('target="_blank"', link)
        self.assertIn('rel="noopener"', link)
        self.assertIn('aria-label="Open this position on lichess"', link)
