"""Batch selection: descent with virtual loss and JL-PNS flagging (§ design 3.3).

From the root: at a White node the child with the smallest pn, at a Black
node the child with the smallest dn (the most dangerous reply).  The leaf
reached is the task.  The fleet asks in bursts, so the master prepares
batches: each pick puts virtual loss on its leaf (pn x (1 + vl), dn = inf,
flagged) and re-evaluates the path, so the next descent spreads instead of
piling up.  The batch stops when

  * the root is flagged: no White move left within ``spread`` (4x) of the
    best pn, every Black reply under the best one already in flight;
  * the descent gets stuck (nothing selectable below an internal node), or
    lands on work already in flight or already proven.

The stop rule is what keeps a batch from buying a clearly worse White move
only because the good one is busy.
"""

from dataclasses import dataclass

from .graph import decode_move
from .master import INF, LEAF, NODE, REST


@dataclass
class Pick:
    rank: int
    kind: str          # NODE (re-search a searched leaf), LEAF, REST
    node: int          # the node searched (NODE) or the parent (LEAF/REST)
    move: str          # LEAF: the move from ``node``; '' otherwise
    ply: int
    pn: float
    dn: float
    eval_cp: object
    path: tuple


def descend(master, start=None):
    """One descent; returns (path, (kind, node, slot)) or (path, None)."""
    g = master.g
    u = g.root if start is None else start
    path = [u]
    seen = {u}
    while master.is_internal(u):
        white = master.STM[u] == 0
        best = None
        best_key = None
        for kind, ref, pn, dn, flagged in master.children(u):
            if flagged or not (0 < pn < INF):
                continue
            key = (pn, -dn) if white else (dn, pn)
            if best_key is None or key < best_key:
                best, best_key = (kind, ref), key
        if best is None:
            return path, None
        kind, ref = best
        if kind == NODE:
            if ref in seen:
                return path, None
            seen.add(ref)
            u = ref
            path.append(u)
            continue
        return path, (kind, u, ref)
    return path, (NODE, u, None)


def select_batch(master, n):
    """Up to ``n`` picks; the batch's virtual loss stays on (see clear)."""
    g = master.g
    master._sync()
    picks = []
    batch = master.batch = getattr(master, 'batch', None) or []
    for _ in range(n):
        root = g.root
        if root < 0 or master.FL[root] == 1:
            break
        rpn = master.PN[root]
        if not (0 < rpn < INF):
            break
        path, leaf = descend(master)
        if leaf is None:
            break
        kind, u, ref = leaf
        if kind == NODE:
            if master.VL[u] or master.is_internal(u) or master.PN[u] == 0:
                break
            pn, dn = master.PN[u], master.DN[u]
            ev = master.white_eval(u)
            move = ''
            master.VL[u] = 1
        elif kind == LEAF:
            code = master.EM[ref]
            if (u, code) in master.vl_inline:
                break
            pn, dn = master.inline_numbers(u, ref)
            ev = master.EE[ref]
            move = decode_move(code)
            master.vl_inline.add((u, code))
        else:
            if u in master.vl_rest:
                break
            pn, dn = master.RP[u], master.RD[u]
            ev = None
            move = ''
            master.vl_rest.add(u)
        batch.append((kind, u, ref))
        picks.append(Pick(rank=len(picks), kind=kind, node=u, move=move,
                          ply=len(path) - (1 if kind == NODE else 0),
                          pn=pn, dn=dn, eval_cp=ev, path=tuple(path)))
        # re-evaluate along the path only; other parents are refreshed
        # when the batch is cleared (a full heap propagation)
        for x in reversed(path):
            master.update(x)
    return picks


def clear_batch(master):
    """Lift the batch's virtual loss and propagate properly."""
    master._sync()
    for kind, u, ref in getattr(master, 'batch', None) or []:
        if kind == NODE:
            master.VL[u] = 0
        elif kind == LEAF:
            master.vl_inline.discard((u, master.EM[ref]))
        else:
            master.vl_rest.discard(u)
        master.touch(u)
    master.batch = []
    master.propagate()


__all__ = ['Pick', 'descend', 'select_batch', 'clear_batch', 'NODE', 'LEAF',
           'REST']
