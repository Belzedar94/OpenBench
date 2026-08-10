#!/bin/python3

import os
import sys
import re
import unittest

# Needed to include from ../Client/*.py
PARENT = os.path.join(os.path.dirname(__file__), os.path.pardir)
sys.path.append(os.path.abspath(os.path.join(PARENT, 'Client')))

from pgn_util import pgn_iterator, pgn_strip_headers, pgn_strip_movelist
from pgn_util import REGEX_COMMENT_COMPACT, REGEX_COMMENT_VERBOSE

def verify_stripped_move_list(move_list, compact):

    special_comments = [ 'book', 'unknown' ]
    comment_regex = re.compile(REGEX_COMMENT_COMPACT if compact else REGEX_COMMENT_VERBOSE)

    for move, comment in re.findall(r'([a-zA-Z0-9+=#-]+)\s\{([^}]*)\}', move_list):
        assert comment in special_comments or comment_regex.match(comment)


class PgnHeaderTests(unittest.TestCase):

    def test_termination_survives_compact_and_verbose_uploads(self):
        headers = {
            'Event': '?',
            'Site': '?',
            'Date': '2026.08.09',
            'Round': '1',
            'White': 'dev',
            'Black': 'base',
            'Result': '0-1',
            'Termination': 'time forfeit',
        }

        for compact in (True, False):
            with self.subTest(compact=compact):
                stripped = pgn_strip_headers(headers, compact)
                self.assertIn('[Termination "time forfeit"]', stripped)

if __name__ == '__main__':
    for example_pgn in [ 'example1.pgn', 'example2.pgn', 'example3.pgn', ]:
        for headers, move_list in pgn_iterator(example_pgn):
            for compact in [ True, False ]:
                verify_stripped_move_list(pgn_strip_movelist(move_list, compact), compact)
