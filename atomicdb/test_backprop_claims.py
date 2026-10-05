"""An unsearched claim does not hide the searched moves behind it.

Live on 4-Oct, ``2d39ae6a`` (Black to move): its own 640M search says 1207, a
reply searched at 512M says 1131, and a reply nobody searched carries a claim
of 1117.  The claim is rightly refused (no engine nodes behind it), but the
refusal used to return the node's OWN number, so the header read 1207 over a
searched row at 1131: "best move doesnt match eval".
"""

import hashlib

from . import ingest
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
