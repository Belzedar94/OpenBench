"""The master: cost-unit proof numbers over the in-memory graph.

Goal WHITE_WIN from the root (one campaign per goal, § design 3.2):

    node                       pn                      dn
    closed for White           0                       inf
    closed against / draw      inf                     0
    leaf                       cost model (pn)         ordinal (dn_leaf)
    White to move (OR)         min of children         sum
    Black to move (AND)        sum of children         min

Children of an open, expanded node are its node children (searched
positions), its inline leaves (named but unsearched), the counts of closed
unsearched children, and the "rest": the k legal moves nobody named, priced
as k obligations at a Black node and as one alternative at a White node,
both at the MultiPV bound (the lowest eval among the named ones).

ORDER.  A full recompute walks the graph from the root (iterative DFS over
open, expanded nodes) and numbers every reached node in post-order (``po``).
An edge u -> c with ``po[c] >= po[u]`` is a back edge, a repetition: White
never needs to repeat (the edge is dropped at an OR node) and a Black reply
that repeats is priced as a fresh leaf from its own eval.  That rule is the
same in the full and the incremental pass, which is what makes them agree.

INCREMENTAL.  Changed nodes go into a heap keyed by ``po``; popping the
smallest first recomputes every dirty child before its parents.  A node
whose numbers move by no more than ``tol`` (relative) and whose flag did not
change stops the climb.  New subtrees (a leaf that was searched and
expanded, a transposition into a part of the graph nobody reached before)
get ``po`` values inside the gap between their highest ordered child and
their parent; when there is no gap, the next pass is a full one.

VIRTUAL LOSS AND FLAGS (§ design 3.3).  A leaf with a task in flight has its
pn multiplied by (1 + vl) and dn = inf, and is flagged.  An AND node is
flagged when every open child is; an OR node when no free child is within
``spread`` times the best pn.  The descent avoids flagged children and a
batch stops when the root is flagged.
"""

import heapq
import math
from array import array

import numpy as np

from .costmodel import dn_leaf
from .graph import (BLACK_WIN, DRAW, NOEVAL, UNKNOWN, WHITE_WIN)

INF = math.inf
OR_SPREAD = 4.0
NODE, LEAF, REST = 'NODE', 'LEAF', 'REST'


def _changed(old, new, tol):
    if old == new:
        return False
    if old != old or new != new:          # NaN on either side
        return True
    if old == INF or new == INF:
        return True
    if tol <= 0:
        return True
    return abs(new - old) > tol * max(abs(old), abs(new))


class Master:
    def __init__(self, graph, model, vl_factor=1.0, spread=OR_SPREAD,
                 tol=1e-3):
        self.g = graph
        self.m = model
        self.vl = float(vl_factor)
        self.spread = float(spread)
        self.tol = float(tol)
        self.vl_inline = set()         # (u, move code) of inline leaves in flight
        self.vl_rest = set()           # u whose rest pseudo-child is in flight
        self._heap = []
        self._queued = set()
        self.needs_full = True
        self.last = {}
        self._ver = None
        # how the order was kept: local insertions, Pearce-Kelly repairs,
        # repetitions left as back edges, fallbacks to a full pass
        self.counters = {'inserted': 0, 'repaired': 0, 'cycles': 0,
                         'fallbacks': 0}

    # ------------------------------------------------------------ binding
    def _sync(self):
        g = self.g
        if self._ver == g.version:
            return
        c = g.cols
        self.S = memoryview(c['status'])
        self.STM = memoryview(c['stm'])
        self.EV = memoryview(c['ev'])
        self.INV = memoryview(c['invested'])
        self.PIE = memoryview(c['pieces'])
        self.MAT = memoryview(c['mat'])
        self.EXP = memoryview(c['expanded'])
        self.NL = memoryview(c['n_legal'])
        self.NW = memoryview(c['n_cwin'])
        self.NC = memoryview(c['n_close'])
        self.EO = memoryview(c['e_off'])
        self.EL = memoryview(c['e_len'])
        self.PN = memoryview(c['pn'])
        self.DN = memoryview(c['dn'])
        self.PO = memoryview(c['po'])
        self.RP = memoryview(c['rest_pn'])
        self.RD = memoryview(c['rest_dn'])
        self.FL = memoryview(c['flag'])
        self.VL = memoryview(c['vl'])
        self.EC = memoryview(g.e_child)
        self.EM = memoryview(g.e_move)
        self.EE = memoryview(g.e_ev)
        self._ver = g.version

    # ------------------------------------------------------------ structure
    def is_internal(self, u):
        self._sync()
        return self.S[u] == UNKNOWN and self.NL[u] > 0 and self.EXP[u] == 1

    def white_eval(self, c):
        s = self.S[c]
        if s == WHITE_WIN:
            return 10000
        if s == BLACK_WIN:
            return -10000
        if s == DRAW:
            return 0
        e = self.EV[c]
        return None if e == NOEVAL else e

    def static_leaf(self, u, model=None):
        """Leaf numbers of node ``u`` from its own eval, no virtual loss."""
        m = model or self.m
        s = self.S[u]
        if s == WHITE_WIN:
            return 0.0, INF
        if s == BLACK_WIN or s == DRAW:
            return INF, 0.0
        e = self.EV[u]
        e = None if e == NOEVAL else e
        nl = self.NL[u]
        pn = m.pn(e, float(self.STM[u]), nl or None, None, self.PIE[u],
                  self.MAT[u], self.INV[u])
        return pn, dn_leaf(e)

    def leaf_numbers(self, u):
        pn, dn = self.static_leaf(u)
        if self.VL[u] and 0 < pn < INF:
            return pn * (1.0 + self.vl), INF
        return pn, dn

    def inline_numbers(self, u, j, model=None):
        """Numbers of the inline leaf in edge slot ``j`` of node ``u``."""
        m = model or self.m
        e = self.EE[j]
        if e == NOEVAL:
            e = self.EV[u]
            e = None if e == NOEVAL else e
        dog = 1.0 if self.STM[u] == 0 else 0.0
        pn = m.fresh_pn(e, dog) if e is not None else m.H
        dn = dn_leaf(e)
        if model is None and (u, self.EM[j]) in self.vl_inline and 0 < pn < INF:
            return pn * (1.0 + self.vl), INF
        return pn, dn

    def rest_count(self, u):
        return max(0, self.NL[u] - self.EL[u] - self.NW[u] - self.NC[u])

    def rest_numbers(self, u, k, evals, model=None):
        m = model or self.m
        if evals:
            bound = min(evals)
        else:
            e = self.EV[u]
            bound = None if e == NOEVAL else e
        white = self.STM[u] == 0
        dog = 1.0 if white else 0.0
        one = m.fresh_pn(bound, dog) if bound is not None else m.H
        pn = one if white else min(INF, k * one)
        dn = dn_leaf(bound)
        if model is None and u in self.vl_rest and 0 < pn < INF:
            return pn * (1.0 + self.vl), INF
        return pn, dn

    # ------------------------------------------------------------ one node
    def _compute(self, u):
        """(pn, dn, rest_pn, rest_dn, flag) of ``u`` from its children.

        The flag is computed in the same pass over the children: ``best`` is
        the smallest pn among the open selectable children, ``best_free``
        the smallest among those not flagged.
        """
        S, NL, EXP = self.S, self.NL, self.EXP
        if not (S[u] == UNKNOWN and NL[u] > 0 and EXP[u] == 1):
            pn, dn = self.leaf_numbers(u)
            return pn, dn, INF, INF, (1 if self.VL[u] else 0)
        white = self.STM[u] == 0
        if white and self.NW[u] > 0:
            return 0.0, INF, INF, INF, 1
        if not white and self.NC[u] > 0:
            return INF, 0.0, INF, INF, 1
        PO, PN, DN, EC, FL = self.PO, self.PN, self.DN, self.EC, self.FL
        vl_inline = self.vl_inline
        po_u = PO[u]
        a = self.EO[u]
        b = a + self.EL[u]
        evals = []
        if white:
            pn, dn = INF, 0.0
        else:
            pn, dn = 0.0, INF
        best = best_free = INF
        any_open = False
        any_child = False
        for j in range(a, b):
            c = EC[j]
            selectable = True
            if c >= 0:
                po_c = PO[c]
                if po_c >= po_u or po_c != po_c:
                    if white:
                        continue
                    cpn, cdn = self.static_leaf(c)
                    selectable = False
                else:
                    cpn, cdn = PN[c], DN[c]
                    if cpn != cpn:
                        cpn, cdn = self.static_leaf(c)
                    flagged = FL[c] == 1
                e = self.white_eval(c)
            else:
                cpn, cdn = self.inline_numbers(u, j)
                flagged = (u, self.EM[j]) in vl_inline
                e = self.EE[j]
                e = None if e == NOEVAL else e
            any_child = True
            if e is not None:
                evals.append(e)
            if selectable and 0 < cpn < INF:
                any_open = True
                if cpn < best:
                    best = cpn
                if not flagged and cpn < best_free:
                    best_free = cpn
            if white:
                if cpn < pn:
                    pn = cpn
                dn += cdn
            else:
                pn += cpn
                if cdn < dn:
                    dn = cdn
        k = self.rest_count(u)
        if k > 0:
            rpn, rdn = self.rest_numbers(u, k, evals)
            any_child = True
            if 0 < rpn < INF:
                any_open = True
                if rpn < best:
                    best = rpn
                if u not in self.vl_rest and rpn < best_free:
                    best_free = rpn
            if white:
                if rpn < pn:
                    pn = rpn
                dn += rdn
            else:
                pn += rpn
                if rdn < dn:
                    dn = rdn
        else:
            rpn, rdn = INF, INF
        if not any_child:
            pn, dn = self.leaf_numbers(u)
            return pn, dn, rpn, rdn, (1 if self.VL[u] else 0)
        if pn > INF or pn != pn:
            pn = INF
        if dn != dn:
            dn = INF
        if not any_open or best_free == INF:
            flag = 1
        elif white:
            flag = 1 if best_free > self.spread * best else 0
        else:
            flag = 0
        return pn, dn, rpn, rdn, flag

    def children(self, u):
        """Selectable children of an internal node: (kind, ref, pn, dn, flagged).

        ``kind`` NODE (ref = node index), LEAF (ref = edge slot) or REST
        (ref = u).  Back edges and closed counts are not selectable.
        """
        self._sync()
        out = []
        po_u = self.PO[u]
        a = self.EO[u]
        for j in range(a, a + self.EL[u]):
            c = self.EC[j]
            if c >= 0:
                po_c = self.PO[c]
                if po_c >= po_u or po_c != po_c:
                    continue
                out.append((NODE, c, self.PN[c], self.DN[c], self.FL[c] == 1))
            else:
                pn, dn = self.inline_numbers(u, j)
                out.append((LEAF, j, pn, dn,
                            (u, self.EM[j]) in self.vl_inline))
        if self.rest_count(u) > 0:
            out.append((REST, u, self.RP[u], self.RD[u], u in self.vl_rest))
        return out

    def update(self, u):
        """Recompute ``u``; True when the change is worth climbing."""
        pn, dn, rpn, rdn, fl = self._compute(u)
        PN, DN = self.PN, self.DN
        changed = _changed(PN[u], pn, self.tol) or _changed(DN[u], dn, self.tol)
        PN[u] = pn
        DN[u] = dn
        self.RP[u] = rpn
        self.RD[u] = rdn
        if fl != self.FL[u]:
            changed = True
            self.FL[u] = fl
        return changed

    # ------------------------------------------------------------ full pass
    def _post_order(self):
        self._sync()
        g = self.g
        n = g.n
        root = g.root
        order = array('i')
        if root < 0:
            return order
        S, NL, EXP, EO, EL, EC = (self.S, self.NL, self.EXP, self.EO, self.EL,
                                  self.EC)
        state = bytearray(n)
        stack_u = [root]
        stack_j = [EO[root]]
        state[root] = 1
        while stack_u:
            u = stack_u[-1]
            j = stack_j[-1]
            if S[u] == UNKNOWN and NL[u] > 0 and EXP[u] == 1:
                end = EO[u] + EL[u]
            else:
                end = j
            nxt = -1
            while j < end:
                c = EC[j]
                j += 1
                if c >= 0 and state[c] == 0:
                    nxt = c
                    break
            if nxt >= 0:
                stack_j[-1] = j
                state[nxt] = 1
                stack_u.append(nxt)
                stack_j.append(EO[nxt])
            else:
                stack_u.pop()
                stack_j.pop()
                state[u] = 2
                order.append(u)
        return order

    def full_recompute(self):
        self._sync()
        g = self.g
        g.po[:g.n] = np.nan
        order = self._post_order()
        if len(order):
            idx = np.frombuffer(order, dtype=np.int32)
            g.po[idx] = np.arange(len(idx), dtype=np.float64)
        update = self.update
        for u in order:
            update(u)
        self._heap = []
        self._queued = set()
        self.needs_full = False
        self.last = {'mode': 'full', 'ordered': len(order)}
        return len(order)

    # ------------------------------------------------------------ incremental
    def touch(self, u, data=False):
        """Mark ``u`` dirty; with ``data`` its back-edge parents too."""
        self._sync()
        po = self.PO[u]
        if po == po and u not in self._queued:
            self._queued.add(u)
            heapq.heappush(self._heap, (po, u))
        if data:
            for p in self.g.parents(u):
                pp = self.PO[p]
                if pp == pp and p not in self._queued:
                    self._queued.add(p)
                    heapq.heappush(self._heap, (pp, p))

    def propagate(self):
        self._sync()
        if self.needs_full:
            return self.full_recompute()
        heap, queued, PO = self._heap, self._queued, self.PO
        g = self.g
        done = 0
        while heap:
            po_u, u = heapq.heappop(heap)
            if u not in queued:
                continue
            queued.discard(u)
            if PO[u] != po_u:            # re-numbered since it was queued
                if PO[u] == PO[u]:
                    queued.add(u)
                    heapq.heappush(heap, (PO[u], u))
                continue
            done += 1
            if self.update(u):
                for p in g.parents(u):
                    pp = PO[p]
                    if pp == pp and pp > po_u and p not in queued:
                        queued.add(p)
                        heapq.heappush(heap, (pp, p))
        self.last = {'mode': 'incremental', 'recomputed': done}
        return done

    def _local_order(self, start):
        """Post-order of the not-yet-ordered nodes reachable from ``start``."""
        S, NL, EXP, EO, EL, EC, PO = (self.S, self.NL, self.EXP, self.EO,
                                      self.EL, self.EC, self.PO)
        order = []
        seen = {start}
        stack_u = [start]
        stack_j = [EO[start]]
        while stack_u:
            u = stack_u[-1]
            j = stack_j[-1]
            end = (EO[u] + EL[u]) if (S[u] == UNKNOWN and NL[u] > 0
                                      and EXP[u] == 1) else j
            nxt = -1
            while j < end:
                c = EC[j]
                j += 1
                if c >= 0 and c not in seen and PO[c] != PO[c]:
                    nxt = c
                    break
            if nxt >= 0:
                stack_j[-1] = j
                seen.add(nxt)
                stack_u.append(nxt)
                stack_j.append(EO[nxt])
            else:
                stack_u.pop()
                stack_j.pop()
                order.append(u)
        return order

    def _forward_kids(self, n):
        """Node children of ``n`` along forward (DAG) edges."""
        if not (self.S[n] == UNKNOWN and self.NL[n] > 0 and self.EXP[n] == 1):
            return ()
        po_n = self.PO[n]
        a = self.EO[n]
        out = []
        for j in range(a, a + self.EL[n]):
            c = self.EC[j]
            if c >= 0:
                pc = self.PO[c]
                if pc == pc and pc < po_n:
                    out.append(c)
        return out

    def _repair(self, x, y, cap=200000):
        """Restore ``po[y] < po[x]`` for the edge x -> y (Pearce-Kelly).

        Descendants of y ranked above x (``F``) and ancestors of x ranked
        below y (``B``) swap places inside the pool of their own ranks: F
        moves down, B moves up, each keeping its internal order.  Returns
        'ok', 'cycle' (x is below y: the edge closes a repetition and stays a
        back edge) or 'cap' (region too large: do a full pass).
        """
        PO = self.PO
        lb, ub = PO[x], PO[y]
        F, seen, stack = [], {y}, [y]
        while stack:
            n = stack.pop()
            F.append(n)
            if len(F) > cap:
                return 'cap'
            for c in self._forward_kids(n):
                if PO[c] > lb:
                    if c == x:
                        self.counters['cycles'] += 1
                        return 'cycle'
                    if c not in seen:
                        seen.add(c)
                        stack.append(c)
        B, seen_b, stack = [], {x}, [x]
        while stack:
            n = stack.pop()
            B.append(n)
            if len(B) > cap:
                return 'cap'
            po_n = PO[n]
            for p in self.g.parents(n):
                pp = PO[p]
                if p in seen_b or not (pp == pp and po_n < pp < ub):
                    continue
                if n in self._forward_kids(p):      # skip stale links
                    seen_b.add(p)
                    stack.append(p)
        F.sort(key=lambda n: PO[n])
        B.sort(key=lambda n: PO[n])
        pool = sorted(PO[n] for n in F + B)
        g = self.g
        for n, r in zip(F + B, pool):
            g.po[n] = r
        for n in F + B:
            self.touch(n, data=True)
        self.counters['repaired'] += 1
        return 'ok'

    def apply_changes(self, touched=(), structural=None, created=()):
        """Fold one refresh of the loader into the order and the heap."""
        self._sync()
        if self.needs_full:
            return
        PO = self.PO
        g = self.g
        structural = structural or {}
        repairs = []                      # edges x -> y to put in order
        for u, (old, new) in structural.items():
            po_u = PO[u]
            if po_u != po_u:
                continue                  # not reached yet
            for c in new - old:
                if PO[c] == PO[c]:
                    if PO[c] >= po_u:
                        repairs.append((u, c))
                    continue
                local = self._local_order(c)
                local_set = set(local)
                lo, hi = -INF, po_u
                for x in local:
                    for p in g.parents(x):
                        if p not in local_set:
                            pp = PO[p]
                            if pp == pp and pp < hi:
                                hi = pp
                for x in local:
                    a = self.EO[x]
                    for j in range(a, a + self.EL[x]):
                        y = self.EC[j]
                        if y >= 0 and y not in local_set:
                            py = PO[y]
                            if py == py and py < hi and py > lo:
                                lo = py
                if lo == -INF:
                    lo = hi - len(local) - 1.0
                step = (hi - lo) / (len(local) + 1)
                if step <= 1e-9 * max(1.0, abs(hi)):
                    self.needs_full = True
                    self.counters['fallbacks'] += 1
                    return
                self.counters['inserted'] += len(local)
                for i, x in enumerate(local):
                    g.po[x] = lo + (i + 1) * step
                for x in local:
                    self.touch(x)
                    a = self.EO[x]
                    for j in range(a, a + self.EL[x]):
                        y = self.EC[j]
                        if y >= 0 and y not in local_set and PO[y] >= PO[x]:
                            repairs.append((x, y))
            self.touch(u)
        for x, y in repairs:
            if PO[y] < PO[x]:
                continue                  # an earlier repair fixed it
            if self._repair(x, y) == 'cap':
                self.needs_full = True
                self.counters['fallbacks'] += 1
                return
        for u in touched:
            self.touch(u, data=True)
        for u in created:
            self.touch(u, data=True)

    # ------------------------------------------------------------ in flight
    def set_inflight(self, nodes=(), inline=(), rest=()):
        """Replace the set of work in flight (virtual loss), dirtying owners."""
        self._sync()
        g = self.g
        cur_nodes = set(np.flatnonzero(g.vl[:g.n]).tolist())
        nodes = set(nodes)
        for u in cur_nodes ^ nodes:
            self.VL[u] = 1 if u in nodes else 0
            self.touch(u)
        inline = set(inline)
        for u, _code in self.vl_inline ^ inline:
            self.touch(u)
        self.vl_inline = inline
        rest = set(rest)
        for u in self.vl_rest ^ rest:
            self.touch(u)
        self.vl_rest = rest

    # ------------------------------------------------------------ strategy
    def strategy(self, max_nodes=200000, alt=None):
        """Walk the planner's strategy: OR -> cheapest child, AND -> all.

        Returns per-ply counts of open obligations, the summed cost of the
        obligations under the main model and, when ``alt`` is given, under
        that model (same strategy), and whether the walk was truncated.
        """
        self._sync()
        g = self.g
        root = g.root
        res = {'obligations_by_ply': {}, 'cost_main': 0.0, 'cost_alt': None,
               'visited': 0, 'truncated': False}
        if root < 0 or self.PN[root] != self.PN[root]:
            return res
        by_ply = {}
        cost_main = 0.0
        cost_alt = 0.0 if alt is not None else None
        seen = {root}
        frontier = [root]
        ply = 0

        def oblige(ply, n, main, other):
            nonlocal cost_main, cost_alt
            by_ply[ply] = by_ply.get(ply, 0) + n
            cost_main += main
            if cost_alt is not None:
                cost_alt += other

        while frontier:
            nxt = []
            for u in frontier:
                res['visited'] += 1
                if res['visited'] > max_nodes:
                    res['truncated'] = True
                    nxt = []
                    break
                if not self.is_internal(u):
                    pn = self.PN[u]
                    if 0 < pn < INF:
                        oblige(ply, 1, pn, self.static_leaf(u, alt)[0]
                               if alt is not None else 0.0)
                    continue
                ch = [x for x in self.children(u) if x[2] > 0]
                if self.STM[u] == 0:
                    ch = [x for x in ch if x[2] < INF]
                    if not ch:
                        continue
                    ch = [min(ch, key=lambda x: (x[2], -x[3]))]
                for kind, ref, pn, _dn, _f in ch:
                    if kind == NODE:
                        if ref not in seen:
                            seen.add(ref)
                            nxt.append(ref)
                    elif kind == LEAF:
                        oblige(ply + 1, 1, pn, self.inline_numbers(u, ref, alt)[0]
                               if alt is not None else 0.0)
                    else:
                        k = self.rest_count(u) if self.STM[u] == 1 else 1
                        other = 0.0
                        if alt is not None:
                            evals = self._named_evals(u)
                            other = self.rest_numbers(u, self.rest_count(u),
                                                      evals, alt)[0]
                        oblige(ply + 1, k, pn, other)
            frontier = nxt
            ply += 1
        res['obligations_by_ply'] = dict(sorted(by_ply.items()))
        res['cost_main'] = cost_main
        res['cost_alt'] = cost_alt
        return res

    def _named_evals(self, u):
        evals = []
        a = self.EO[u]
        po_u = self.PO[u]
        white = self.STM[u] == 0
        for j in range(a, a + self.EL[u]):
            c = self.EC[j]
            if c >= 0:
                po_c = self.PO[c]
                if white and (po_c >= po_u or po_c != po_c):
                    continue
                e = self.white_eval(c)
            else:
                e = self.EE[j]
                e = None if e == NOEVAL else e
            if e is not None:
                evals.append(e)
        return evals
