"""An unsearched claim does not hide the searched moves behind it.

Live on 4-Oct, ``2d39ae6a`` (Black to move): its own 640M search says 1207, a
reply searched at 512M says 1131, and a reply nobody searched carries a claim
of 1117.  The claim is rightly refused (no engine nodes behind it), but the
refusal used to return the node's OWN number, so the header read 1207 over a
searched row at 1131: "best move doesnt match eval".
"""

import hashlib

from . import ingest, views
from .models import AnalysisTask, Edge, Position
from .testing import TestCase


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


class VetoedClaimTests(TestCase):

    def _node(self, name, searched=1131):
        """Black to move, own search 1207; one unsearched claim at 1117 and
        one reply an engine searched.  Other replies are still unopened."""
        node = _pos(f'{name}-N', 'b', eval_cp=1207, nodes_invested=640_000_000)
        claim = _pos(f'{name}-claim', 'w', eval_cp=1117)
        _edge(node, claim, 'h7g7')
        _edge(node, _pos(f'{name}-searched', 'w', eval_cp=searched,
                         nodes_invested=512_000_000), 'b7b6')
        _edge(node, _pos(f'{name}-unopened', 'w'), 'a8d8')
        return node, claim

    def test_the_searched_reply_backs_up_past_the_refused_claim(self):
        node, claim = self._node('PAST')

        ingest.backup_backed_evals([node.key])

        node.refresh_from_db()
        self.assertEqual((node.backed_eval, node.backed_move), (1131, 'b7b6'))
        self.assertEqual(node.backed_nodes, 512_000_000)
        # The claim is still an open question, and it is still bought.
        self.assertTrue(AnalysisTask.objects.filter(position=claim).exists())

    def test_a_refused_claim_with_nothing_searched_behind_it_changes_nothing(self):
        # The veto itself is intact: when no searched reply is better for the
        # mover than the node's own measure, the node keeps its own number.
        node, claim = self._node('KEEP', searched=1300)

        ingest.backup_backed_evals([node.key])

        node.refresh_from_db()
        self.assertEqual((node.backed_eval, node.backed_move), (1207, None))
        self.assertTrue(AnalysisTask.objects.filter(position=claim).exists())


class ClaimArrowTests(TestCase):
    """The board arrow names the move the header's number comes from."""

    def test_the_arrow_follows_the_backed_move_past_the_claim(self):
        # Live on 14-Oct, 1bbe0b3e (Black to move): g4h5 carries 1179, seeded
        # by an earlier pass, and g8h8 holds 1224 from searches of its own.
        # The header read 1224 through g8h8; the arrow, the top row, g4h5.
        node = _pos('1BBE-N', 'b', eval_cp=1262, nodes_invested=640_000_000)
        _edge(node, _pos('1BBE-claim', 'w', eval_cp=1179), 'g4h5')
        _edge(node, _pos('1BBE-searched', 'w', eval_cp=1224,
                         nodes_invested=256_000_000), 'g8h8')
        _edge(node, _pos('1BBE-unopened', 'w'), 'a8d8')
        ingest.backup_backed_evals([node.key])
        node.refresh_from_db()
        rows = views._child_moves(node)

        self.assertEqual((node.backed_eval, node.backed_move), (1224, 'g8h8'))
        # The table keeps ordering by value: the claim stays on top.
        self.assertEqual([row['uci'] for row in rows[:2]], ['g4h5', 'g8h8'])
        self.assertEqual(views._arrow_move(rows, node), 'g8h8')

    def _settled(self, name, claim=None):
        """Black to move, own search 1207 through b7b6, which that pass
        seeded at the same number; f7f6 was searched and is worse for Black.
        The backup keeps the own number, with or without the claim."""
        node = _pos(f'{name}-N', 'b', eval_cp=1207, best_move='b7b6',
                    nodes_invested=640_000_000)
        _edge(node, _pos(f'{name}-own', 'w', eval_cp=1207), 'b7b6')
        _edge(node, _pos(f'{name}-searched', 'w', eval_cp=1300,
                         nodes_invested=512_000_000), 'f7f6')
        if claim is not None:
            _edge(node, _pos(f'{name}-claim', 'w', eval_cp=claim), 'h7g7')
        ingest.backup_backed_evals([node.key])
        node.refresh_from_db()
        self.assertEqual((node.backed_eval, node.backed_move), (1207, None))
        return node, views._child_moves(node)

    def test_without_a_backed_move_the_arrow_is_the_engines_move(self):
        node, rows = self._settled('OWN')

        self.assertEqual(rows[0]['uci'], 'b7b6')
        self.assertEqual(views._arrow_move(rows, node), 'b7b6')

    def test_a_vetoed_claim_with_nothing_behind_it_is_not_the_arrow(self):
        # No searched reply beats 1207 for Black, so the header keeps the own
        # number; the claim on the top row is still not the move it quotes.
        node, rows = self._settled('KEEP', claim=1117)

        self.assertEqual(rows[0]['uci'], 'h7g7')
        self.assertEqual(views._arrow_move(rows, node), 'b7b6')

    def test_a_proven_position_keeps_the_arrow_of_its_table(self):
        # Its header quotes the verdict, not a backed value, so a backed move
        # left from before the closure does not steer the arrow.
        node = _pos('PROVEN-N', 'b', status='BLACK_WIN', closure='MINIMAX',
                    best_move='b7b6', backed_eval=1117, backed_move='h7g7')
        _edge(node, _pos('PROVEN-won', 'w', status='BLACK_WIN',
                         closure='MINIMAX', mate_in=3), 'b7b6')
        _edge(node, _pos('PROVEN-claim', 'w', eval_cp=1117), 'h7g7')

        self.assertEqual(
            views._arrow_move(views._child_moves(node), node), 'b7b6')
