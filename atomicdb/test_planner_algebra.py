"""The planner's proof-number algebra, virtual loss, batch stop and recompute.

What is pinned here (all in memory, no database):
  * leaf, closed, OR (min/sum) and AND (sum/min) numbers, the closed-child
    counts and the "rest" pseudo-child (k obligations at AND, one
    alternative at OR, at the MultiPV bound);
  * a pn conditioned on what was already invested grows with it;
  * virtual loss doubles a leaf's pn, sets dn to infinity, flags it, and
    lifting it restores every number exactly;
  * the batch stops when no White move is within 4x of the best pn, and an
    AND node is exhausted once every reply is in flight;
  * an incremental recompute equals a full one, bit for bit, after random
    changes to evals, closures, expansions and new transpositions;
  * repetitions: White never repeats, a repeating Black reply is a fresh
    leaf, and the descent never loops.
"""

import math
import random
import unittest

from atomicdb.planner.costmodel import CostModel, dn_leaf
from atomicdb.planner.graph import (Graph, decode_move, encode_move,
                                    NOEVAL)
from atomicdb.planner.master import INF, NODE, REST, Master
from atomicdb.planner.select import clear_batch, descend, select_batch


def K(i):
    return '%064x' % (i + 1)


class StubModel:
    """pn = table[eval] (default eval itself); dn = dn_leaf."""
    H = 1e13

    def __init__(self, table=None):
        self.table = table or {}

    def pn(self, a_cp, dog, n_branch=None, depth=None, pieces=24, mat_fav=0,
           invested=0.0):
        if a_cp is None:
            return self.H
        return float(self.table.get(a_cp, a_cp))

    def fresh_pn(self, a_cp, dog):
        return self.pn(a_cp, dog)

    def survivor(self):
        return None


def node(i, stm=0, ev=500, status='UNKNOWN', invested=1e8, expanded=True):
    return {'key': K(i), 'stm': stm, 'ev': ev, 'status': status,
            'invested': invested, 'expanded': expanded}


def build(nodes, edges, root=0, model=None, tol=0.0):
    g = Graph.from_records(nodes, {K(p): (n, kids) for p, (n, kids)
                                   in edges.items()}, K(root))
    m = Master(g, model or StubModel(), tol=tol)
    m.full_recompute()
    return g, m


def child(move, i, ev=None, status='UNKNOWN', ni=1):
    return (move, K(i), status, ev, ni)


class MoveCodecTest(unittest.TestCase):
    def test_roundtrip(self):
        for uci in ('e2e4', 'a7a8q', 'h2h1n', 'g1f3', 'b7b8k'):
            self.assertEqual(decode_move(encode_move(uci)), uci)


class AlgebraTest(unittest.TestCase):
    def test_or_is_min_and_dn_is_sum(self):
        g, m = build([node(0, stm=0), node(1, stm=1, ev=700),
                      node(2, stm=1, ev=400)],
                     {0: (2, [child('a2a3', 1), child('b2b3', 2)])})
        self.assertEqual(g.pn[g.root], 400.0)
        self.assertEqual(g.dn[g.root], dn_leaf(700) + dn_leaf(400))

    def test_and_is_sum_and_dn_is_min(self):
        g, m = build([node(0, stm=1), node(1, stm=0, ev=700),
                      node(2, stm=0, ev=400)],
                     {0: (2, [child('a7a6', 1), child('b7b6', 2)])})
        self.assertEqual(g.pn[g.root], 1100.0)
        self.assertEqual(g.dn[g.root], dn_leaf(400))

    def test_closed_nodes(self):
        g, m = build([node(0, stm=1), node(1, stm=0, status='WHITE_WIN'),
                      node(2, stm=0, ev=400)],
                     {0: (2, [child('a7a6', 1), child('b7b6', 2)])})
        self.assertEqual(g.pn[1], 0.0)
        self.assertEqual(g.dn[1], INF)
        self.assertEqual(g.pn[g.root], 400.0)       # the proven reply is free

    def test_unsearched_closed_children_are_counts(self):
        # Black to move with a reply that already wins for Black: refuted.
        g, m = build([node(0, stm=1)],
                     {0: (3, [child('a7a6', 9, status='BLACK_WIN', ni=0),
                              child('b7b6', 10, ev=800, ni=0)])})
        self.assertEqual(g.n_close[0], 1)
        self.assertEqual(g.pn[0], INF)
        self.assertEqual(g.dn[0], 0.0)
        # White to move with a child already won: proven.
        g, m = build([node(0, stm=0)],
                     {0: (3, [child('a2a3', 9, status='WHITE_WIN', ni=0),
                              child('b2b3', 10, ev=800, ni=0)])})
        self.assertEqual(g.pn[0], 0.0)

    def test_inline_leaves_and_rest(self):
        # Black to move, 2 named replies (inline) + 3 nobody named.
        g, m = build([node(0, stm=1, ev=900)],
                     {0: (5, [child('a7a6', 9, ev=800, ni=0),
                              child('b7b6', 10, ev=600, ni=0)])})
        self.assertEqual(m.rest_count(0), 3)
        # rest bound = lowest named eval (600): 3 obligations at 600
        self.assertEqual(g.rest_pn[0], 3 * 600.0)
        self.assertEqual(g.pn[0], 800.0 + 600.0 + 1800.0)
        self.assertEqual(g.dn[0], dn_leaf(600))
        # White to move: the rest is ONE alternative at the bound
        g, m = build([node(0, stm=0, ev=900)],
                     {0: (5, [child('a2a3', 9, ev=800, ni=0),
                              child('b2b3', 10, ev=700, ni=0)])})
        self.assertEqual(g.rest_pn[0], 700.0)
        self.assertEqual(g.pn[0], 700.0)
        self.assertEqual(g.dn[0], dn_leaf(800) + 2 * dn_leaf(700))

    def test_unnamed_children_without_eval_are_rest(self):
        g, m = build([node(0, stm=1, ev=900)],
                     {0: (3, [child('a7a6', 9, ev=None, ni=0)])})
        self.assertEqual(g.e_len[0], 0)
        self.assertEqual(m.rest_count(0), 3)
        self.assertEqual(g.pn[0], 3 * 900.0)       # bound: own eval

    def test_conditioned_leaf_pn_grows_with_investment(self):
        cm = CostModel()
        low = cm.pn(2000, 0.0, invested=1.28e8)
        high = cm.pn(2000, 0.0, invested=1e11)
        self.assertGreater(high, low)
        self.assertGreater(cm.pn(1200, 0.0), cm.pn(3500, 0.0))
        self.assertGreater(cm.pn(2000, 1.0), cm.pn(2000, 0.0))
        self.assertGreater(cm.pn(200, 0.0), cm.H)       # below +3
        self.assertTrue(math.isfinite(cm.pn(-5000, 0.0)))


class VirtualLossTest(unittest.TestCase):
    def setUp(self):
        self.g, self.m = build(
            [node(0, stm=0), node(1, stm=1, ev=900, expanded=True),
             node(2, stm=0, ev=800)],
            {0: (2, [child('a2a3', 1), child('b2b3', 9, ev=3000, ni=0)]),
             1: (2, [child('a7a6', 2), child('b7b6', 10, ev=700, ni=0)])})

    def test_vl_doubles_pn_sets_dn_inf_and_flags(self):
        g, m = self.g, self.m
        before = (g.pn[:g.n].copy(), g.dn[:g.n].copy())
        m.set_inflight(nodes={2})
        m.propagate()
        self.assertEqual(g.pn[2], 1600.0)
        self.assertEqual(g.dn[2], INF)
        self.assertEqual(g.flag[2], 1)
        self.assertEqual(g.pn[1], 1600.0 + 700.0)
        m.set_inflight(nodes=())
        m.propagate()
        self.assertTrue((g.pn[:g.n] == before[0]).all())
        self.assertTrue((g.dn[:g.n] == before[1]).all())
        self.assertEqual(g.flag[:g.n].sum(), 0)

    def test_inline_vl(self):
        g, m = self.g, self.m
        m.set_inflight(inline={(1, encode_move('b7b6'))})
        m.propagate()
        self.assertEqual(g.pn[1], 800.0 + 1400.0)
        self.assertEqual(g.dn[1], dn_leaf(800))


class SelectionTest(unittest.TestCase):
    def _two_moves(self, pn_b):
        model = StubModel({1000: 1.0, 900: float(pn_b)})
        return build([node(0, stm=0)],
                     {0: (2, [child('a2a3', 9, ev=1000, ni=0),
                              child('b2b3', 10, ev=900, ni=0)])},
                     model=model)

    def test_stop_when_no_white_move_within_4x(self):
        g, m = self._two_moves(9)
        picks = select_batch(m, 10)
        self.assertEqual([p.move for p in picks], ['a2a3'])
        self.assertEqual(g.flag[g.root], 1)
        clear_batch(m)
        self.assertEqual(g.flag[g.root], 0)
        self.assertEqual(g.pn[g.root], 1.0)

    def test_second_move_bought_when_within_4x(self):
        g, m = self._two_moves(5)
        picks = select_batch(m, 10)
        self.assertEqual([p.move for p in picks], ['a2a3', 'b2b3'])

    def test_and_node_exhausted_when_every_reply_in_flight(self):
        g, m = build([node(0, stm=1, ev=900)],
                     {0: (3, [child('a7a6', 9, ev=800, ni=0),
                              child('b7b6', 10, ev=600, ni=0),
                              child('c7c6', 11, ev=700, ni=0)])})
        picks = select_batch(m, 10)
        # most dangerous first: smallest dn = lowest attacker eval
        self.assertEqual([p.move for p in picks], ['b7b6', 'c7c6', 'a7a6'])
        self.assertEqual(len({(p.node, p.move) for p in picks}), 3)

    def test_rest_and_node_picks(self):
        g, m = build([node(0, stm=0, ev=900), node(1, stm=1, ev=650,
                                                    expanded=False)],
                     {0: (4, [child('a2a3', 1)])})
        # the searched leaf (650) and the rest (3 moves, one alternative at
        # the bound 650) tie; the leaf comes first, then the rest, then the
        # root is flagged
        picks = select_batch(m, 4)
        self.assertEqual([p.kind for p in picks], [NODE, REST])
        self.assertEqual(g.flag[g.root], 1)

    def test_proven_root_selects_nothing(self):
        g, m = build([node(0, stm=0), node(1, stm=1, status='WHITE_WIN')],
                     {0: (1, [child('a2a3', 1)])})
        self.assertEqual(select_batch(m, 5), [])


def random_dag(seed, n=300):
    """A layered random DAG with transpositions, inline leaves and rests."""
    rnd = random.Random(seed)
    layers = [[0]]
    nid = 1
    while nid < n:
        layer = []
        for _ in range(rnd.randint(3, 12)):
            if nid >= n:
                break
            layer.append(nid)
            nid += 1
        layers.append(layer)
    nodes, edges = [], {}
    stm = {}
    for d, layer in enumerate(layers):
        for i in layer:
            stm[i] = d % 2
            closed = 'UNKNOWN'
            if d > 1 and rnd.random() < 0.08:
                closed = rnd.choice(['WHITE_WIN', 'WHITE_WIN', 'BLACK_WIN'])
            nodes.append(node(i, stm=stm[i], ev=rnd.randint(-200, 4000),
                              status=closed,
                              invested=rnd.choice([1.28e8, 5e8, 3e9]),
                              expanded=d < len(layers) - 1))
    extra = 10 ** 6
    for d, layer in enumerate(layers[:-1]):
        nxt = layers[d + 1]
        for i in layer:
            kids = []
            moves = iter(['%s%d%s%d' % (chr(97 + a), b, chr(97 + c), e)
                          for a in range(8) for b in (2, 7)
                          for c in range(8) for e in (3, 6)])
            for c in rnd.sample(nxt, min(len(nxt), rnd.randint(1, 3))):
                kids.append(child(next(moves), c))
            for _ in range(rnd.randint(0, 3)):
                extra += 1
                kids.append(child(next(moves), extra,
                                  ev=rnd.randint(-100, 4000), ni=0))
            if rnd.random() < 0.1:
                extra += 1
                kids.append(child(next(moves), extra, status='WHITE_WIN',
                                  ni=0))
            edges[i] = (len(kids) + rnd.randint(0, 4), kids)
    return nodes, edges, layers, stm


class IncrementalEqualsFullTest(unittest.TestCase):
    def _snapshot(self, g):
        idx = [i for i in range(g.n) if g.po[i] == g.po[i]]
        return {g.key_hex(i): (g.pn[i], g.dn[i]) for i in idx}

    def _check(self, seed, model):
        rnd = random.Random(seed * 7 + 1)
        nodes, edges, layers, stm = random_dag(seed)
        g, m = build(nodes, edges, model=model, tol=0.0)
        nid = 10 ** 7
        for step in range(25):
            touched, structural, created = set(), {}, set()
            kind = rnd.random()
            if kind < 0.35:                         # an eval / investment moves
                u = rnd.randrange(g.n)
                g.ev[u] = rnd.randint(-200, 4000)
                g.invested[u] = g.invested[u] * 4
                touched.add(u)
            elif kind < 0.5:                        # a node closes
                u = rnd.randrange(1, g.n)
                g.status[u] = rnd.choice([1, 2])
                touched.add(u)
            elif kind < 0.75:                       # a leaf gets searched
                cands = [u for u in range(g.n) if g.e_len[u] > 0]
                p = rnd.choice(cands)
                a = int(g.e_off[p])
                inl = [j for j in range(a, a + int(g.e_len[p]))
                       if g.e_child[j] < 0]
                if not inl:
                    continue
                j = rnd.choice(inl)
                nid += 1
                c, was_new, _ = g.upsert_node(K(nid), 1 - int(g.stm[p]), 0,
                                              int(g.e_ev[j]), 1.28e8, 24, 0,
                                              True)
                created.add(c)
                # the new node's own children: inline + one transposition
                anc = self._ancestors(g, p)
                deeper = [x for x in range(g.n) if g.stm[x] == g.stm[p]
                          and x != p and x != c and x not in anc]
                kids = [('a7a6', K(nid + 10 ** 5), 'UNKNOWN',
                         rnd.randint(0, 4000), 0)]
                if deeper and rnd.random() < 0.7:
                    t = rnd.choice(deeper)
                    kids.append(('h7h6', g.key_hex(t), 'UNKNOWN', None, 1))
                g.set_children(c, len(kids) + 1, kids)
                structural[c] = (set(), set(g.child_nodes(c)))
                # reload the parent: the inline child is now a node child
                old = set(g.child_nodes(p))
                kids_p = []
                for jj in range(a, a + int(g.e_len[p])):
                    cc = int(g.e_child[jj])
                    m_ = decode_move(int(g.e_move[jj]))
                    if jj == j:
                        kids_p.append((m_, K(nid), 'UNKNOWN', None, 1))
                    elif cc >= 0:
                        kids_p.append((m_, g.key_hex(cc), 'UNKNOWN', None, 1))
                    else:
                        kids_p.append((m_, K(10 ** 8 + jj), 'UNKNOWN',
                                       int(g.e_ev[jj]), 0))
                new = g.set_children(p, int(g.n_legal[p]), kids_p)
                structural[p] = (old, new)
            else:                                   # a new transposition edge
                p = rnd.randrange(g.n)
                if not (g.status[p] == 0 and g.expanded[p]):
                    continue
                opp = [x for x in range(g.n) if g.stm[x] != g.stm[p]
                       and x != g.root]
                t = rnd.choice(opp)
                if t in self._ancestors(g, p) or t == p:
                    continue                        # keep it a DAG
                old = set(g.child_nodes(p))
                a = int(g.e_off[p])
                kids_p = []
                for jj in range(a, a + int(g.e_len[p])):
                    cc = int(g.e_child[jj])
                    m_ = decode_move(int(g.e_move[jj]))
                    if cc >= 0:
                        kids_p.append((m_, g.key_hex(cc), 'UNKNOWN', None, 1))
                    else:
                        kids_p.append((m_, K(10 ** 8 + jj), 'UNKNOWN',
                                       int(g.e_ev[jj]), 0))
                kids_p.append(('g8h6', g.key_hex(t), 'UNKNOWN', None, 1))
                new = g.set_children(p, int(g.n_legal[p]) + 1, kids_p)
                structural[p] = (old, new)
            m.apply_changes(touched, structural, created)
            m.propagate()
            inc = self._snapshot(g)
            m.full_recompute()
            full = self._snapshot(g)
            # nodes cut off since stay ordered in the incremental pass; only
            # what the root still reaches has to agree
            self.assertLessEqual(set(full), set(inc), 'step %d' % step)
            for k in full:
                self.assertEqual(inc[k], full[k], 'step %d key %s' % (step,
                                                                      k[-6:]))
        return m.counters

    def _ancestors(self, g, u):
        out, stack = set(), [u]
        while stack:
            x = stack.pop()
            for p in g.parents(x):
                if p not in out:
                    out.add(p)
                    stack.append(p)
        return out

    def test_incremental_equals_full_stub(self):
        totals = {}
        for seed in range(6):
            for k, v in self._check(seed, StubModel()).items():
                totals[k] = totals.get(k, 0) + v
        # the order was kept by insertion and repair, not by falling back
        self.assertGreater(totals['inserted'], 0)
        self.assertGreater(totals['repaired'], 0)
        self.assertEqual(totals['fallbacks'], 0)

    def test_incremental_equals_full_cost_model(self):
        for seed in range(3):
            self._check(100 + seed, CostModel())

    def test_tolerance_keeps_root_close(self):
        nodes, edges, layers, stm = random_dag(42)
        g, m = build(nodes, edges, model=CostModel(), tol=1e-3)
        rnd = random.Random(5)
        for _ in range(40):
            u = rnd.randrange(g.n)
            g.ev[u] = rnd.randint(300, 4000)
            m.touch(u, data=True)
            m.propagate()
        inc = g.pn[g.root]
        m.full_recompute()
        self.assertLess(abs(inc - g.pn[g.root]) / g.pn[g.root], 0.05)


class RepetitionTest(unittest.TestCase):
    def test_white_never_repeats_and_black_repeat_is_a_fresh_leaf(self):
        # 0 (W) -> 1 (B) -> 2 (W) -> back to 1 (repetition, W drops it)
        #                  1 (B) -> 0 (repetition: fresh leaf from own eval)
        g, m = build([node(0, stm=0, ev=900), node(1, stm=1, ev=800),
                      node(2, stm=0, ev=700)],
                     {0: (1, [child('a2a3', 1)]),
                      1: (2, [child('a7a6', 2), child('a6a7', 0)]),
                      2: (2, [child('a3a2', 1),
                              child('b2b3', 9, ev=650, ni=0)])})
        self.assertEqual(g.pn[2], 650.0)            # the repetition is dropped
        self.assertEqual(g.pn[1], 650.0 + 900.0)    # 1->0 as a fresh leaf
        self.assertEqual(g.pn[0], g.pn[1])
        path, leaf = descend(m)
        self.assertEqual(len(set(path)), len(path))
        picks = select_batch(m, 20)
        self.assertTrue(picks)
        self.assertLessEqual(len(picks), 20)


class NoEvalTest(unittest.TestCase):
    def test_missing_eval_is_priced_at_the_horizon(self):
        g, m = build([node(0, stm=0, ev=None)], {0: (2, [])},
                     model=CostModel())
        self.assertEqual(g.ev[0], NOEVAL)
        self.assertEqual(g.pn[0], CostModel().H)


if __name__ == '__main__':
    unittest.main()
