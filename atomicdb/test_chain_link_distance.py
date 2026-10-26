"""A position closed by a mate line takes the faster mates already below it.

Live on 26-Oct: a node shown as "≤M13" while its child Kh1 already mated
faster; a later update through it brought it to "≤M11".  The ingest closed
the child a search's line enters, and every link of the line it then
materialised, with the length of that position's part of the line as its
distance, and the cascade that followed visited their parents only.  A link
that already had another proven child with a shorter mate never compared
itself with it, and nothing ever came back to it.

Two halves are pinned here: new closures are re-derived at once (the
cascade is seeded with what the pass closed), and ``backfill_mate_distance``
repairs the rows written before, by default only the distances that are too
long and what derives from them.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from . import ingest, logic
from .management.commands import backfill_mate_distance
from .models import DBEvent, Edge, Position
from .testing import TestCase


def _after(*moves):
    """Canonical FEN after those moves from the start position."""
    fen = logic.start_fen()
    for uci in moves:
        fen = logic.apply_move(fen, uci)
    return fen


def _node(*moves):
    return ingest.get_or_create_position(_after(*moves))


def _close(pos, **fields):
    """Write a closure exactly as given (the live rows were written so)."""
    fields.setdefault('clock_slack', 90)
    Position.objects.filter(key=pos.key).update(**fields)
    pos.refresh_from_db()
    return pos


def _edge(parent, uci, child):
    return Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                      defaults={'child': child})[0]


# A legal line from the start position.  The ingest takes the proof it is
# handed (the queue hands it prepared), so the line only has to be legal for
# the chain to materialise; what is measured is the distance bookkeeping.
LINE = ['e2e4', 'e7e5', 'g1f3', 'b8c6', 'f1c4', 'd7d6']


class NewClosureTests(TestCase):
    """The closures an ingest writes are re-derived in the same cascade."""

    def test_a_chain_link_takes_a_faster_child_it_already_had(self):
        root = _node()
        link = _node(*LINE[:2])            # White to move: an OR link
        faster = _node(*LINE[:2], 'd2d4')
        _close(faster, status='WHITE_WIN', closure='MATE_PV', proof='ENGINE',
               won_line='a7a6', mate_in=1, best_move='a7a6')
        _edge(link, 'd2d4', faster)
        lines = [{'move': LINE[0], 'eval_cp': 9990, 'mate': 3, 'pv': LINE}]

        ingest.ingest_analysis(
            root.key, lines, 1_000_000,
            mate_proofs={0: (True, LINE[1:], 'INCONCLUSIVE', None)})

        link.refresh_from_db()
        top = Edge.objects.get(parent=root, move_uci=LINE[0]).child
        root.refresh_from_db()
        # The link was closed by the line with its 4 plies, and d2d4 mates
        # in 1 below it: 1 + 1, through d2d4.
        self.assertEqual((link.status, link.closure), ('WHITE_WIN', 'MATE_PV'))
        self.assertEqual(link.won_line, ' '.join(LINE[2:]))
        self.assertEqual((link.mate_in, link.best_move), (2, 'd2d4'))
        # And the change climbs: the top of the chain (Black to move, its
        # only reply is the link) and the analysed position above it.
        self.assertEqual(top.mate_in, 3)
        self.assertEqual((root.status, root.closure), ('WHITE_WIN', 'MINIMAX'))
        self.assertEqual(root.mate_in, 4)

    def test_the_closed_child_itself_takes_a_faster_child(self):
        # Black to move and mated: its best defence enters a position where
        # White already had a faster mate than the one the line plays.
        parent = _node('g1f3')
        line = ['b8c6', 'e2e4', 'g8f6', 'd2d4']
        top = _node('g1f3', 'b8c6')
        faster = _node('g1f3', 'b8c6', 'a2a3')
        _close(faster, status='WHITE_WIN', closure='MATE_PV', proof='ENGINE',
               won_line='a7a6', mate_in=1, best_move='a7a6')
        _edge(top, 'a2a3', faster)
        lines = [{'move': line[0], 'eval_cp': 9990, 'mate': 2, 'pv': line}]

        ingest.ingest_analysis(
            parent.key, lines, 1_000_000,
            mate_proofs={0: (True, line[1:], 'INCONCLUSIVE', None)})

        top.refresh_from_db()
        self.assertEqual((top.status, top.closure), ('WHITE_WIN', 'MATE_PV'))
        self.assertEqual(top.won_line, ' '.join(line[1:]))
        self.assertEqual((top.mate_in, top.best_move), (2, 'a2a3'))

    def test_without_a_faster_child_the_line_keeps_its_length(self):
        root = _node()
        lines = [{'move': LINE[0], 'eval_cp': 9990, 'mate': 3, 'pv': LINE}]

        ingest.ingest_analysis(
            root.key, lines, 1_000_000,
            mate_proofs={0: (True, LINE[1:], 'INCONCLUSIVE', None)})

        link = Position.objects.get(key=logic.key_of(_after(*LINE[:2])))
        self.assertEqual((link.mate_in, link.best_move), (4, LINE[2]))


class _StaleChain:
    """What the old ingest left behind, written as it was on the live base.

    ``top`` (Black to move) and ``link`` (White to move) are links of one
    winning line, each closed with the length of its part of it; ``link``
    had another child, ``faster``, already proven at 1 ply.  ``other`` is an
    unrelated row whose distance would have to GROW: Wolfram's shape, a
    MINIMAX parent at 3 plies over its only winning child at 4.
    """

    def build(self):
        self.top = _node(LINE[0])
        self.link = _node(*LINE[:2])
        self.next = _node(*LINE[:3])
        self.faster = _node(*LINE[:2], 'd2d4')
        _close(self.top, status='WHITE_WIN', closure='MATE_PV',
               proof='ENGINE', won_line=' '.join(LINE[1:]), mate_in=5,
               best_move=LINE[1])
        _close(self.link, status='WHITE_WIN', closure='MATE_PV',
               proof='ENGINE', won_line=' '.join(LINE[2:]), mate_in=4,
               best_move=LINE[2])
        _close(self.next, status='WHITE_WIN', closure='MATE_PV',
               proof='ENGINE', won_line=' '.join(LINE[3:]), mate_in=3,
               best_move=LINE[3])
        _close(self.faster, status='WHITE_WIN', closure='MATE_PV',
               proof='ENGINE', won_line='a7a6', mate_in=1, best_move='a7a6')
        _edge(self.top, LINE[1], self.link)
        _edge(self.link, LINE[2], self.next)
        _edge(self.link, 'd2d4', self.faster)

        self.other = _close(_node('g1f3', 'd7d5'), status='WHITE_WIN',
                            closure='MINIMAX', proof='ENGINE', mate_in=3,
                            best_move='f3e5')
        longer = _close(_node('g1f3', 'd7d5', 'f3e5'), status='WHITE_WIN',
                        closure='MATE_PV', proof='ENGINE',
                        won_line='f7f6 e5d7 e7e6 d7f8', mate_in=4,
                        best_move='f7f6')
        _edge(self.other, 'f3e5', longer)
        return self

    def refreshed(self):
        for row in (self.top, self.link, self.other):
            row.refresh_from_db()
        return self


def _backfill(*args, **kwargs):
    out = StringIO()
    call_command('backfill_mate_distance', *args, stdout=out, stderr=out,
                 **kwargs)
    return out.getvalue()


class BackfillDefaultScopeTests(TestCase):
    """By default the backfill repairs exactly the distances that are too long,
    and what derives from them."""

    def test_it_shortens_the_link_and_what_stands_on_it(self):
        chain = _StaleChain().build()

        report = _backfill()

        chain.refreshed()
        self.assertEqual((chain.link.mate_in, chain.link.best_move),
                         (2, 'd2d4'))
        self.assertEqual(chain.top.mate_in, 3)          # 1 + its only reply
        self.assertIn('lowered=2', report)
        self.assertIn('rows_changed=2', report)
        self.assertIn('written=2', report)
        self.assertIn('scope=too-long', report)
        self.assertIn('rows to change by closure: MATE_PV=2', report)
        self.assertTrue(DBEvent.objects.filter(
            kind='MATE_DISTANCE_BACKFILL', payload__scope='too-long').exists())

    def test_a_distance_that_must_grow_is_not_this_repair(self):
        chain = _StaleChain().build()

        _backfill()

        chain.refreshed()
        self.assertEqual(chain.other.mate_in, 3)

    def test_all_repairs_every_row(self):
        chain = _StaleChain().build()

        report = _backfill('--all')

        chain.refreshed()
        self.assertEqual(chain.link.mate_in, 2)
        self.assertEqual(chain.top.mate_in, 3)
        self.assertEqual(chain.other.mate_in, 5)
        self.assertIn('raised=1', report)
        self.assertIn('rows_changed=3', report)
        self.assertIn('scope=all', report)

    def test_the_dry_run_writes_nothing_and_says_how_many(self):
        chain = _StaleChain().build()

        report = _backfill('--dry-run')

        chain.refreshed()
        self.assertEqual((chain.link.mate_in, chain.top.mate_in), (4, 5))
        self.assertIn('dry run, 2 row(s) would change; nothing was written',
                      report)
        self.assertIn('written=0', report)
        self.assertFalse(
            DBEvent.objects.filter(kind='MATE_DISTANCE_BACKFILL').exists())

    def test_it_is_idempotent(self):
        _StaleChain().build()
        _backfill()

        again = _backfill()

        self.assertIn('rows_changed=0', again)
        self.assertIn('written=0', again)


class BackfillOnALiveServerTests(TestCase):
    """Short transactions and room for the site between them."""

    def test_the_writes_go_in_short_transactions(self):
        _StaleChain().build()
        opened = []
        real = backfill_mate_distance.atomic

        def counting():
            opened.append(1)
            return real()

        with patch.object(backfill_mate_distance, 'atomic', counting):
            report = _backfill('--write-batch', '1')

        self.assertIn('written=2', report)
        self.assertEqual(len(opened), 2)

    def test_the_pause_is_taken_between_batches(self):
        _StaleChain().build()

        with patch.object(backfill_mate_distance.time, 'sleep') as slept:
            _backfill('--pause', '0.25', '--write-batch', '1')

        self.assertTrue(slept.called)
        self.assertEqual({call.args[0] for call in slept.call_args_list},
                         {0.25})

    def test_the_orphan_count_is_opt_in(self):
        stray = _node('h2h3')
        Position.objects.filter(key=stray.key).update(mate_in=7)

        quiet = _backfill()
        counted = _backfill('--count-orphans')

        self.assertNotIn('still carry a mate_in', quiet)
        self.assertIn('1 UNKNOWN row(s) still carry a mate_in', counted)


class EquivalentWitnessTests(TestCase):
    """Two witnesses of the same rank are one witness: nothing to write."""

    def test_a_tie_between_verified_witnesses_is_left_alone(self):
        node = _close(_node('g1f3', 'g8f6'), status='WHITE_WIN',
                      closure='MINIMAX', proof='ANDOR', mate_in=1,
                      best_move='b1c3')
        first = _close(_node('g1f3', 'g8f6', 'a2a3'), status='WHITE_WIN',
                       closure='TERMINAL', mate_in=0)
        second = _close(_node('g1f3', 'g8f6', 'b1c3'), status='WHITE_WIN',
                        closure='TERMINAL', mate_in=0)
        _edge(node, 'a2a3', first)
        _edge(node, 'b1c3', second)
        # The recurrence alone takes the first edge it is handed.
        edges = [Edge.objects.select_related('child').get(parent=node,
                                                          move_uci=uci)
                 for uci in ('a2a3', 'b1c3')]
        self.assertEqual(ingest.mate_distance_refresh(node, edges),
                         (1, 'a2a3'))

        report = _backfill('--all')

        node.refresh_from_db()
        self.assertEqual(node.best_move, 'b1c3')
        self.assertIn('rows_changed=0', report)
