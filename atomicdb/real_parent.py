"""Real-parent pointers: the breadcrumb as a pointer walk, not a search.

THE PROPOSAL (MoKy, 24-Nov-2026).  ``views._walk_lines_to_root`` ran a reverse
BFS, one windowed ``atomicdb_edge`` query per ply, and returned the
minimum-ply line with ties broken by status and key order: often a move order
nobody plays.  The regret Dijkstra (``ingest._regret_from_root_bounded``)
already knows the better answer, the path of least cumulative loss from the
root, and only threw the predecessor away.  Now it can keep it, and this
module publishes it as one pointer per settled node and walks it back.

WRITER (``ATOMICDB_REAL_PARENT_WRITE``, default off).
  * Rides the v2 priorities pass when that pass runs (free: same Dijkstra),
    and otherwise runs as its own step of ``refresh_selector`` at most every
    ``ATOMICDB_REAL_PARENT_INTERVAL`` seconds, capped at ``MAX_SETTLED``
    settled nodes (``ATOMICDB_REAL_PARENT_MAX_SETTLED``) so a horizon that
    fails to bite cannot turn it into a graph scan.  Production runs ``--no-priorities`` with the pn selector,
    so there the standalone step is the one that writes.
  * Writes only pointers that CHANGED, in batches of ``WRITE_BATCH``, into the
    small ``RealParent`` table, closest to the root first, at most
    ``MAX_WRITES`` rows per pass.  Reading what is stored costs one primary
    key query per ``READ_BATCH`` settled nodes.

TIES AND HYSTERESIS.  The relaxation keeps every way into a node whose
regret is within ``TIE_CP`` centipawns of the best one.  Among those:
  1. the pointer already stored stays if it is among them, unless another
     candidate's parent has a proof number ``SELECTED_CHILD_HYSTERESIS``
     times smaller (the same rule, and the same integer arithmetic, as
     ``proof.sticky_index`` for ``selected_child``);
  2. otherwise the smaller parent pn wins (default campaign; a parent with no
     proof row counts as infinite), then the smaller regret, then the oldest
     edge (smallest id).
So equal-regret transpositions do not flip-flop between passes, a regret
that drifts by a few centipawns does not rewrite the row, and a strictly
better path (more than ``TIE_CP`` better) always wins.

READER (``ATOMICDB_REAL_PARENT_READ``, default off).  ``lines_to_root`` walks,
in ONE recursive query for a batch of positions, at every step the stored
pointer if its edge still exists and otherwise the oldest incoming edge
(``MIN(id)``: the edge the position was born with), up to the ply budget.  A
walk that does not reach the root, or that repeats a node, is dropped and the
caller falls back to the BFS.
"""

import logging

from django.conf import settings
from django.utils import timezone

from . import proof
from .database import connection
from .models import Edge, ProofNode, RealParent

logger = logging.getLogger(__name__)

# Ways into a node within this many centipawns of regret count as a tie.
TIE_CP = 5.0
# Hard cap on the nodes a standalone pass settles.  The cost of a pass is the
# settled set PLUS its border (every child of a settled node is loaded to
# price its gap, ~25 per node), so 20k settled is ~0.5M node rows read, in
# batches of 500.  Dijkstra order makes them the 20k lowest-regret nodes:
# the main lines, which is where breadcrumbs are read.
MAX_SETTLED = 20_000
# Hard cap on the rows one pass writes (lowest regret first).
MAX_WRITES = 20_000
WRITE_BATCH = 500
READ_BATCH = 500
# Seeds per recursive walk query: rows are at most seeds x plies.
WALK_SEED_BATCH = 200

_state = {'at': None, 'report': {}}


def writer_enabled():
    return bool(getattr(settings, 'ATOMICDB_REAL_PARENT_WRITE', False))


def reader_enabled():
    return bool(getattr(settings, 'ATOMICDB_REAL_PARENT_READ', False))


def interval_seconds():
    return float(getattr(settings, 'ATOMICDB_REAL_PARENT_INTERVAL', 600.0))


def max_settled():
    return int(getattr(settings, 'ATOMICDB_REAL_PARENT_MAX_SETTLED',
                       MAX_SETTLED))


def reset_state():
    """Forget the last pass (tests; the next ``refresh`` runs at once)."""
    _state['at'] = None
    _state['report'] = {}


def last_report():
    return dict(_state['report'])


# ---------------- choosing among tied ways in ----------------

def choose(candidates, held, pn):
    """The ``(regret, parent, move, edge_id)`` to publish for one node.

    ``held`` is the stored ``(parent, move)`` or ``None``; ``pn`` maps a
    parent key to its proof number.  See the module docstring for the rule.
    """
    def pn_of(cand):
        return pn.get(cand[1], proof.PROOF_INFINITY)

    best = min(candidates, key=lambda c: (pn_of(c), c[0], c[3]))
    if held is None:
        return best
    for cand in candidates:
        if (cand[1], cand[2]) == tuple(held):
            if cand is best:
                return best
            if pn_of(best) * proof.SELECTED_CHILD_HYSTERESIS < pn_of(cand):
                return best
            return cand
    return best


def _proof_numbers(parent_keys):
    if not parent_keys:
        return {}
    campaign = proof.default_campaign(create=False)
    if campaign is None:
        return {}
    keys = sorted(parent_keys)
    out = {}
    for start in range(0, len(keys), READ_BATCH):
        out.update(ProofNode.objects.filter(
            campaign=campaign, position_id__in=keys[start:start + READ_BATCH])
            .values_list('position_id', 'pn'))
    return out


# ---------------- writer ----------------

def publish(done, parents, max_writes=None):
    """Write the pointers of the settled nodes that changed.

    ``done`` is ``{key: regret}`` of the settled nodes and ``parents`` the
    candidates the relaxation collected.  Returns the counters of the pass.
    """
    if max_writes is None:
        max_writes = MAX_WRITES
    items = sorted((r, key) for key, r in done.items() if parents.get(key))
    keys = [key for _r, key in items]
    stored = {}
    for start in range(0, len(keys), READ_BATCH):
        for key, parent, move in RealParent.objects.filter(
                position_key__in=keys[start:start + READ_BATCH]).values_list(
                    'position_key', 'parent_key', 'move_uci'):
            stored[key] = (parent, move)
    tied = [key for key in keys if len(parents[key]) > 1]
    pn = _proof_numbers({cand[1] for key in tied for cand in parents[key]})

    now = timezone.now()
    changed = []
    capped = False
    for _r, key in items:
        held = stored.get(key)
        cand = choose(parents[key], held, pn)
        if held == (cand[1], cand[2]):
            continue
        if len(changed) >= max_writes:
            capped = True
            break
        changed.append(RealParent(position_key=key, parent_key=cand[1],
                                  move_uci=cand[2], updated=now))
    for start in range(0, len(changed), WRITE_BATCH):
        RealParent.objects.bulk_create(
            changed[start:start + WRITE_BATCH], update_conflicts=True,
            unique_fields=['position_key'],
            update_fields=['parent_key', 'move_uci', 'updated'])
    return {'settled': len(done), 'pointers': len(items),
            'written': len(changed), 'ties': len(tied), 'capped': capped}


def _finish(report, started):
    report['seconds'] = round((timezone.now() - started).total_seconds(), 3)
    _state['at'] = timezone.now()
    _state['report'] = report
    return report


def publish_from_pass(done, parents):
    """The priorities pass hands over what it settled.  Never raises: a
    failing pointer write must not cost the priorities that pass wrote."""
    started = timezone.now()
    try:
        return _finish(publish(done, parents), started)
    except Exception:            # noqa: BLE001 - optional side output
        logger.exception('real_parent: publishing from the selector failed')
        return None


def refresh(force=False):
    """Standalone pointer pass, for when the priorities pass is not run.

    No-op with the writer off, and inside ``interval_seconds()`` of the last
    pass (whoever ran it) unless ``force``.
    """
    if not writer_enabled():
        return None
    last = _state['at']
    if (not force and last is not None and
            (timezone.now() - last).total_seconds() < interval_seconds()):
        return None
    from . import ingest
    started = timezone.now()
    parents = {}
    done = ingest._regret_from_root_bounded(parents=parents,
                                            max_settled=max_settled(),
                                            tie_cp=TIE_CP)
    return _finish(publish(done, parents), started)


# ---------------- reader ----------------

def _walk_sql(seed_count):
    q = connection.ops.quote_name
    edge = q(Edge._meta.db_table)
    pointer = q(RealParent._meta.db_table)
    seeds = ', '.join(['(%s)'] * seed_count)
    # At each step: the stored pointer if its edge still exists, else the
    # oldest edge into the node.  ``e.id + 0`` keeps the planner on the
    # ``child_id`` index (a bare ``ORDER BY id LIMIT 1`` invites a walk down
    # the primary key of a table with hundreds of millions of rows).
    return f'''
WITH RECURSIVE seeds(seed) AS (VALUES {seeds}),
walk(seed, depth, node, move, below, via_pointer) AS (
    SELECT CAST(seed AS varchar(64)), 0, CAST(seed AS varchar(64)),
           CAST(NULL AS varchar(8)), CAST(NULL AS varchar(64)), 0
    FROM seeds
    UNION ALL
    SELECT w.seed, w.depth + 1, CAST(nx.parent_id AS varchar(64)),
           CAST(nx.move_uci AS varchar(8)), CAST(nx.child_id AS varchar(64)),
           CASE WHEN pe.id IS NULL THEN 0 ELSE 1 END
    FROM walk w
    LEFT JOIN {pointer} rp ON rp.position_key = w.node
    LEFT JOIN {edge} pe ON pe.parent_id = rp.parent_key
                       AND pe.move_uci = rp.move_uci
                       AND pe.child_id = w.node
    JOIN {edge} nx ON nx.id = COALESCE(pe.id, (
        SELECT e.id FROM {edge} e WHERE e.child_id = w.node
        ORDER BY e.id + 0 LIMIT 1))
    WHERE w.depth < %s AND w.node <> %s
)
SELECT seed, depth, node, move, below, via_pointer FROM walk
WHERE depth > 0
'''


def lines_to_root(keys, start_key, max_plies):
    """``{key: (ordered, pointer_hops)}`` for the keys whose walk reaches
    ``start_key`` within ``max_plies``.

    ``ordered`` is ``[(move_uci, child_key), ...]`` from the root down to the
    key, the shape the breadcrumb renders.  One query per
    ``WALK_SEED_BATCH`` keys; a key missing from the answer is one the walk
    could not root (no edges, a cycle, or out of plies).
    """
    keys = list(dict.fromkeys(key for key in keys if key))
    out = {}
    if start_key in keys:
        out[start_key] = ([], 0)
    seeds = [key for key in keys if key != start_key]
    for start in range(0, len(seeds), WALK_SEED_BATCH):
        chunk = seeds[start:start + WALK_SEED_BATCH]
        steps = {}
        with connection.cursor() as cursor:
            cursor.execute(_walk_sql(len(chunk)),
                           [*chunk, int(max_plies), start_key])
            for seed, depth, node, move, below, via in cursor.fetchall():
                steps.setdefault(seed, {})[int(depth)] = (node, move, below,
                                                          int(via))
        for seed in chunk:
            path = steps.get(seed)
            if not path:
                continue
            seen = {seed}
            upward = []
            hops = 0
            rooted = False
            for depth in range(1, len(path) + 1):
                step = path.get(depth)
                if step is None:
                    break
                node, move, below, via = step
                if node in seen:
                    break                 # a cycle: let the BFS answer
                seen.add(node)
                upward.append((move, below))
                hops += via
                if node == start_key:
                    rooted = True
                    break
            if rooted:
                out[seed] = (list(reversed(upward)), hops)
    return out
