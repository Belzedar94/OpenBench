"""recascade_backed in key slices: the repair pass that can be stopped.

The one-query sweep enumerates every position with edges through a hash
aggregate over the whole edge table, which needs gigabytes of temp space
before the first row comes back.  In slices every range is an index scan.
"""

import hashlib
from io import StringIO

from django.core.management import call_command

from . import ingest
from .management.commands.recascade_backed import Command
from .models import Edge, Position
from .testing import TestCase


def _pos(key, stm='w', **kw):
    return Position.objects.create(
        key=key, fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _relic(prefix):
    """An open node whose backed value no rule in force would write: its own
    search says 671 and a walked spine once pushed 9994 into it.  The three
    positions share ``prefix`` so that one slice holds the whole family."""
    tail = hashlib.sha256(prefix.encode()).hexdigest()[:61]
    anchor = _pos(prefix + '0' + tail, 'b', eval_cp=671, backed_eval=9994,
                  backed_move='c8g4', backed_plies=6)
    spine = _pos(prefix + '1' + tail, 'w')
    _edge(anchor, spine, 'c8g4')
    _edge(spine, _pos(prefix + '2' + tail, 'b', eval_cp=9994,
                      nodes_invested=128_000_000), 'c2c3')
    return anchor


class SliceTests(TestCase):

    def test_the_slices_cover_the_key_space_once_and_in_order(self):
        slices = list(Command._slices(2, '', ''))

        self.assertEqual(len(slices), 256)
        self.assertEqual(slices[0], ('00', '01'))
        self.assertEqual(slices[-1], ('ff', ''))
        for (_lo, hi), (lo, _hi) in zip(slices, slices[1:]):
            self.assertEqual(hi, lo)

    def test_a_range_clips_the_slices_at_both_ends(self):
        self.assertEqual(list(Command._slices(2, '7a3', '7c')),
                         [('7a3', '7b'), ('7b', '7c')])
        self.assertEqual(list(Command._slices(0, '7a3', '7c')),
                         [('7a3', '7c')])

    def test_a_sliced_sweep_heals_what_the_whole_table_sweep_heals(self):
        low, high = _relic('1a'), _relic('e4')

        call_command('recascade_backed', '--slice-chars', '2', '--open-only',
                     stdout=StringIO())

        for anchor in (low, high):
            anchor.refresh_from_db()
            self.assertEqual(anchor.backed_eval, 671)

    def test_a_sweep_resumes_where_it_was_stopped(self):
        low, high = _relic('1a'), _relic('e4')

        call_command('recascade_backed', '--slice-chars', '2',
                     '--key-from', '80', stdout=StringIO())

        low.refresh_from_db()
        high.refresh_from_db()
        self.assertEqual(low.backed_eval, 9994)      # before the resume point
        self.assertEqual(high.backed_eval, 671)

    def test_keys_heal_one_family_and_nothing_else(self):
        low, high = _relic('1a'), _relic('e4')
        out = StringIO()

        call_command('recascade_backed', '--keys', low.key, stdout=out)

        low.refresh_from_db()
        high.refresh_from_db()
        self.assertEqual(ingest.best_known_eval(low), 671)
        self.assertEqual(high.backed_eval, 9994)
        self.assertIn('1 claves', out.getvalue())

    def test_open_only_leaves_proven_positions_alone(self):
        # A proven position headlines its status; its parents read the status
        # too.  Its backed columns are bookkeeping nobody reads.
        proven = _pos('2b' + '0' * 62, 'w', status='WHITE_WIN',
                      closure='MINIMAX', best_move='a1a2', backed_eval=300,
                      backed_move='a1a2', backed_plies=3)
        _edge(proven, _pos('2c' + '0' * 62, 'b', status='WHITE_WIN',
                           closure='TERMINAL'), 'a1a2')

        call_command('recascade_backed', '--open-only', stdout=StringIO())
        proven.refresh_from_db()
        self.assertEqual(proven.backed_eval, 300)

        call_command('recascade_backed', stdout=StringIO())
        proven.refresh_from_db()
        self.assertEqual(proven.backed_eval, 10_000)
