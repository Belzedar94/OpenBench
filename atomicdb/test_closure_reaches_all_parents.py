"""A closure reaches EVERY parent of the position it closes.

The DAG transposes, so one position hangs from several parents.  A closure
written by an analysis (the child closed by a verified mate PV, and the
links of its won line) used to climb only through the analysed position: the
child's other parents stayed UNKNOWN with a proven winning child beside
them.  Live on 14-Oct-2026, eight in a 0.2% sample of the open expanded
positions; ``ce66aee07655`` stayed open while ``5fd985a2e279``, closed by the
same child ``a976cfb1e5f8`` fourteen seconds later, did not.
"""

import json
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command

from . import ingest, logic
from .models import DBEvent, Edge
from .test_proofs import FORCED_MATE_FEN, FORCED_MATE_PV
from .testing import TestCase

# The child of FORCED_MATE_FEN + b1g6, reached with the queen from c2.
TRANSPOSED_FEN = '4p3/8/8/7k/n7/Kp2n3/2Qp4/8 w - - 0 1'
TRANSPOSED_MOVE = 'c2g6'
# Black to move with two replies: g5h4 lands on the middle link of the won
# line (FORCED_MATE_FEN + b1g6 h5h4), g5f4 somewhere else.
FORK_FEN = '4p3/8/6Q1/6k1/n7/Kp2n3/3p4/8 b - - 0 1'
FORK_LINK_MOVE = 'g5h4'
FORK_OTHER_MOVE = 'g5f4'

MATE_LINES = [{'move': FORCED_MATE_PV[0], 'eval_cp': 9_998, 'mate': 2,
               'pv': FORCED_MATE_PV}]


def _expanded(fen):
    pos = ingest.get_or_create_position(fen)
    ingest.expand(pos)
    return pos


def _child(parent, uci):
    return Edge.objects.get(parent=parent, move_uci=uci).child


def _close_by_hand(pos, proof='ANDOR', won_line=None, mate_in=1, slack=90):
    """A white win written straight into the row: no cascade runs."""
    pos.status, pos.closure, pos.proof = 'WHITE_WIN', 'MATE_PV', proof
    pos.won_line, pos.mate_in, pos.clock_slack = won_line, mate_in, slack
    if won_line:
        pos.best_move = won_line.split()[0]
    pos.save()
    return pos


def _fork_with_one_open_child():
    """An AND node whose only open child is the middle link of the line."""
    fork = _expanded(FORK_FEN)
    _close_by_hand(_child(fork, FORK_OTHER_MOVE), proof='ENGINE', mate_in=3)
    return fork, _child(fork, FORK_LINK_MOVE)


def _witness_closed_by_hand():
    """The child of the mate, closed with its line not materialised yet."""
    child = ingest.get_or_create_position(
        logic.apply_move(FORCED_MATE_FEN, FORCED_MATE_PV[0]))
    return _close_by_hand(child, won_line=' '.join(FORCED_MATE_PV[1:]),
                          mate_in=2)


class IngestClosureReachesEveryParentTests(TestCase):

    def test_a_mate_closure_reaches_the_other_parent_of_the_child(self):
        other = _expanded(TRANSPOSED_FEN)
        child = _child(other, TRANSPOSED_MOVE)
        analysed = ingest.get_or_create_position(FORCED_MATE_FEN)

        ingest.ingest_analysis(analysed.key, MATE_LINES, nodes_budget=1_000)

        child.refresh_from_db()
        analysed.refresh_from_db()
        other.refresh_from_db()
        # One child, two parents: a transposition.
        self.assertEqual(_child(analysed, FORCED_MATE_PV[0]).key, child.key)
        self.assertEqual((child.status, child.closure),
                         ('WHITE_WIN', 'MATE_PV'))
        self.assertEqual(analysed.status, 'WHITE_WIN')
        self.assertEqual((other.status, other.closure, other.best_move),
                         ('WHITE_WIN', 'MINIMAX', TRANSPOSED_MOVE))
        self.assertEqual(other.mate_in, child.mate_in + 1)
        self.assertTrue(DBEvent.objects.filter(
            kind='NODE_CLOSED', payload__key=other.key).exists())

    def test_a_closed_link_of_the_won_line_reaches_its_other_parents(self):
        """The links materialised under the child count as closures too,
        and the AND rule (every child proven) is the one that decides here."""
        fork, link = _fork_with_one_open_child()
        analysed = ingest.get_or_create_position(FORCED_MATE_FEN)

        ingest.ingest_analysis(analysed.key, MATE_LINES, nodes_budget=1_000)

        link.refresh_from_db()
        fork.refresh_from_db()
        self.assertEqual((link.status, link.closure), ('WHITE_WIN', 'MATE_PV'))
        self.assertEqual((fork.status, fork.closure), ('WHITE_WIN', 'MINIMAX'))

    def test_a_certified_line_upgrades_every_parent_standing_on_it(self):
        other = _expanded(TRANSPOSED_FEN)
        child = _child(other, TRANSPOSED_MOVE)
        _close_by_hand(child, proof='ENGINE',
                       won_line=' '.join(FORCED_MATE_PV[1:]), mate_in=2)
        ingest.backup_cascade([child.key])
        other.refresh_from_db()
        self.assertEqual((other.status, other.proof), ('WHITE_WIN', 'ENGINE'))
        # The certificate arrives through a parent that is still open.
        analysed = _expanded(FORCED_MATE_FEN)
        proofs = ingest.prepare_mate_proofs(analysed.fen, MATE_LINES,
                                            deadline_seconds=None)
        self.assertEqual(proofs[0][2], 'PROVEN')

        ingest.ingest_analysis(analysed.key, MATE_LINES, nodes_budget=1_000,
                               mate_proofs=proofs)

        child.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(child.proof, 'ANDOR')
        self.assertEqual(other.proof, 'ANDOR')


class MaterialisedLinksReachEveryParentTests(TestCase):
    """The two other doors that materialise a won line."""

    def test_the_proven_line_jump(self):
        fork, link = _fork_with_one_open_child()
        child = _witness_closed_by_hand()

        response = self.client.get(f'/atomicdb/proven-line-end/{child.key}/')

        self.assertEqual(response.status_code, 302)
        link.refresh_from_db()
        fork.refresh_from_db()
        self.assertEqual(link.status, 'WHITE_WIN')
        self.assertEqual((fork.status, fork.closure), ('WHITE_WIN', 'MINIMAX'))

    def test_the_backfill_command(self):
        fork, link = _fork_with_one_open_child()
        _witness_closed_by_hand()

        call_command('materialise_mate_lines', json=True, stdout=StringIO())

        link.refresh_from_db()
        fork.refresh_from_db()
        self.assertEqual(link.status, 'WHITE_WIN')
        self.assertEqual((fork.status, fork.closure), ('WHITE_WIN', 'MINIMAX'))


class RecascadeStatusCommandTests(TestCase):
    """The repair of what the old seeding left behind."""

    def _stuck(self, slack=90):
        """Two open parents beside a child closed without any cascade."""
        analysed = _expanded(FORCED_MATE_FEN)
        other = _expanded(TRANSPOSED_FEN)
        child = _close_by_hand(_child(other, TRANSPOSED_MOVE),
                               won_line=' '.join(FORCED_MATE_PV[1:]),
                               mate_in=2, slack=slack)
        return analysed, other, child

    def _run(self, **options):
        out = StringIO()
        call_command('recascade_status', json=True, sleep=0, stdout=out,
                     **options)
        return json.loads(out.getvalue())

    def test_the_dry_run_finds_the_open_parents_and_writes_nothing(self):
        analysed, other, _child_row = self._stuck()

        summary = self._run()

        self.assertEqual(summary['mode'], 'dry-run')
        self.assertEqual(summary['decided'], 2)
        self.assertEqual(summary['by_status'], {'WHITE_WIN': 2})
        self.assertEqual(
            {example['key']: example['move']
             for example in summary['examples']},
            {analysed.key: FORCED_MATE_PV[0], other.key: TRANSPOSED_MOVE})
        for pos in (analysed, other):
            pos.refresh_from_db()
            self.assertEqual(pos.status, 'UNKNOWN')
        self.assertFalse(DBEvent.objects.filter(
            kind__in=('NODE_CLOSED', 'STATUS_RECASCADE')).exists())

    def test_apply_closes_them_and_a_second_pass_finds_nothing(self):
        analysed, other, _child_row = self._stuck()

        summary = self._run(apply=True)

        self.assertEqual(summary['closed'], 2)
        for pos in (analysed, other):
            pos.refresh_from_db()
            self.assertEqual((pos.status, pos.closure),
                             ('WHITE_WIN', 'MINIMAX'))
        self.assertTrue(DBEvent.objects.filter(
            kind='STATUS_RECASCADE').exists())
        self.assertEqual(self._run()['decided'], 0)

    def test_a_quiet_edge_without_clock_slack_is_not_counted(self):
        """Found from the child, refused by the cascade's own rule."""
        self._stuck(slack=None)

        summary = self._run()

        self.assertEqual(summary['one_winning_child'], 2)
        self.assertEqual(summary['decided'], 0)

    def test_an_open_position_with_every_child_closed_is_found(self):
        fork = _expanded(FORK_FEN)
        for uci in (FORK_LINK_MOVE, FORK_OTHER_MOVE):
            _close_by_hand(_child(fork, uci))

        summary = self._run()

        self.assertEqual(summary['all_children_closed'], 1)
        self.assertEqual(summary['decided'], 1)
        self.assertEqual(summary['examples'][0]['key'], fork.key)

    def test_the_orm_pass_finds_the_same(self):
        """What a database without block ranges (SQLite) runs."""
        self._stuck()
        target = 'atomicdb.management.commands.recascade_status.connection'

        with mock.patch(target, SimpleNamespace(vendor='sqlite')):
            summary = self._run()

        self.assertEqual(summary['decided'], 2)
        self.assertNotIn('next_block', summary)
