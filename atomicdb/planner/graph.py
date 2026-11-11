"""In-memory graph of the planner: searched positions and their named moves.

WHAT IS A NODE.  Every position with a search of its own (``nodes_invested >
0``), about 3.6M today.  Positions nobody searched are NOT nodes: when an
engine named them (a seeded MultiPV eval) they live INLINE on the parent's
edge, as a leaf with its eval; when they are closed they are a COUNT on the
parent (``n_cwin`` for White wins, ``n_close`` for Black wins and draws); the
rest of the legal moves is one more count, the "rest" pseudo-child of the
design (§ docs/atomicdb-planner-design.md 3.4), derived as ``n_legal`` minus
everything named.

WHY ARRAYS.  With one Python object per node the graph is 6 to 10 GB; with
numpy columns it is well under one (§ design 3.8).  Hot loops read the
columns through ``memoryview``s, which hand back plain Python numbers without
creating numpy scalars.

KEYS.  A node's key is kept as its 32 raw bytes.  Lookup goes through a
sorted array of the first 8 bytes (an int64) plus a small dict for nodes
added since the last rebuild; candidates are confirmed against the full key,
so a prefix collision costs a comparison, never a wrong answer.

EDGES.  A node's named children are a contiguous block of three parallel
columns (child node index or -1 for an inline leaf, encoded move, inline
eval).  Replacing a node's children appends a new block and abandons the old
one; ``compact`` reclaims the garbage.  Parents of node children are kept as
a CSR built by ``rebuild_index`` plus an overflow dict for links made since.

NOTHING HERE WRITES.  The loader reads the AtomicDB database inside a
read-only transaction (PostgreSQL) and never issues anything but SELECTs.
"""

import contextlib
import math
import time
from array import array

import numpy as np

UNKNOWN, WHITE_WIN, BLACK_WIN, DRAW = 0, 1, 2, 3
STATUS_CODE = {'UNKNOWN': UNKNOWN, 'WHITE_WIN': WHITE_WIN,
               'BLACK_WIN': BLACK_WIN, 'DRAW': DRAW}
STATUS_NAME = {v: k for k, v in STATUS_CODE.items()}
NOEVAL = -32768                   # int16 sentinel: no eval known
EVAL_CLIP = 32767
NO_CHILD = -1                     # e_child of an inline leaf

PROMO = ' nbrqk'
VAL = {'p': 1, 'n': 3, 'b': 3, 'r': 5, 'q': 9, 'k': 0}

# name, dtype, default
NODE_FIELDS = (
    ('key', 'S32', b''),
    ('stm', 'i1', 0),             # 0 White to move, 1 Black
    ('status', 'i1', UNKNOWN),
    ('ev', 'i2', NOEVAL),         # own eval, White's point of view
    ('invested', 'f8', 0.0),      # engine nodes of its own searches
    ('pieces', 'i1', 0),
    ('mat', 'i1', 0),             # material White minus Black
    ('expanded', 'i1', 0),
    ('n_legal', 'i4', 0),         # every edge in the database
    ('n_cwin', 'i4', 0),          # unsearched children closed WHITE_WIN
    ('n_close', 'i4', 0),         # unsearched children closed BLACK_WIN/DRAW
    ('e_off', 'i8', 0),
    ('e_len', 'i4', 0),
    # derived by the master
    ('pn', 'f8', math.nan),
    ('dn', 'f8', math.nan),
    ('po', 'f8', math.nan),       # post-order rank; NaN = not ordered
    ('rest_pn', 'f8', math.inf),
    ('rest_dn', 'f8', math.inf),
    ('flag', 'i1', 0),
    ('vl', 'i1', 0),              # leaf with a task in flight
)


def encode_move(uci):
    f = (ord(uci[0]) - 97) + 8 * (ord(uci[1]) - 49)
    t = (ord(uci[2]) - 97) + 8 * (ord(uci[3]) - 49)
    p = PROMO.index(uci[4]) if len(uci) > 4 else 0
    return f | (t << 6) | (p << 12)


def decode_move(code):
    f, t, p = code & 63, (code >> 6) & 63, (code >> 12) & 7
    s = '%s%d%s%d' % (chr(97 + f % 8), f // 8 + 1, chr(97 + t % 8), t // 8 + 1)
    return s + (PROMO[p] if p else '')


def clip_eval(v):
    if v is None:
        return NOEVAL
    v = int(v)
    return max(-EVAL_CLIP, min(EVAL_CLIP, v))


def fen_features(fen):
    """(stm, pieces, material White minus Black) of a FEN."""
    parts = fen.split(' ', 2)
    w = b = n = 0
    for ch in parts[0]:
        if ch.isalpha():
            n += 1
            lo = ch.lower()
            if ch.isupper():
                w += VAL.get(lo, 0)
            else:
                b += VAL.get(lo, 0)
    stm = 1 if len(parts) > 1 and parts[1] == 'b' else 0
    return stm, min(n, 127), max(-127, min(127, w - b))


def _prefix(kb):
    return int.from_bytes(kb[:8], 'big', signed=True)


class Graph:
    def __init__(self, capacity=1024, edge_capacity=4096):
        self.version = 0               # bumped on every array reallocation
        self.n = 0
        self.cap = 0
        self.cols = {}
        for name, dt, _d in NODE_FIELDS:
            self.cols[name] = np.empty(0, dtype=dt)
        self._grow_nodes(max(16, capacity))
        self.ne = 0                    # edge slots used (incl. garbage)
        self.ecap = 0
        self.e_child = np.empty(0, dtype=np.int32)
        self.e_move = np.empty(0, dtype=np.uint16)
        self.e_ev = np.empty(0, dtype=np.int16)
        self._grow_edges(max(64, edge_capacity))
        self.garbage = 0
        # key index
        self._sorted_prefix = np.empty(0, dtype=np.int64)
        self._sorted_idx = np.empty(0, dtype=np.int32)
        self._new_keys = {}            # bytes -> idx, since the last rebuild
        # parents
        self._p_off = np.zeros(1, dtype=np.int64)
        self._p_idx = np.empty(0, dtype=np.int32)
        self._p_built = 0              # nodes covered by the CSR
        self._extra_parents = {}       # child -> set(parents) since rebuild
        self.root = -1
        # Off during a bulk load: the CSR is rebuilt at the end anyway, and
        # millions of overflow sets would double the peak memory.
        self.track_parents = True

    # ------------------------------------------------------------ storage
    def _grow_nodes(self, need):
        if need <= self.cap:
            return
        # 1/8 headroom: at 3.5M nodes a 1.5x step would cost ~300 MB
        cap = max(need, self.cap + max(16, self.cap // 8))
        for name, dt, d in NODE_FIELDS:
            old = self.cols[name]
            new = np.empty(cap, dtype=dt)
            new[:self.n] = old[:self.n]
            new[self.n:] = d
            self.cols[name] = new
            setattr(self, name, new)
        self.cap = cap
        self.version += 1

    def _grow_edges(self, need):
        if need <= self.ecap:
            return
        cap = max(need, self.ecap + max(64, self.ecap // 8))
        for name, dt in (('e_child', np.int32), ('e_move', np.uint16),
                         ('e_ev', np.int16)):
            old = getattr(self, name)
            new = np.empty(cap, dtype=dt)
            new[:self.ne] = old[:self.ne]
            setattr(self, name, new)
        self.ecap = cap
        self.version += 1

    def nbytes(self):
        total = sum(c.nbytes for c in self.cols.values())
        total += self.e_child.nbytes + self.e_move.nbytes + self.e_ev.nbytes
        total += self._sorted_prefix.nbytes + self._sorted_idx.nbytes
        total += self._p_off.nbytes + self._p_idx.nbytes
        return total

    # ------------------------------------------------------------ keys
    def lookup(self, key):
        """Node index of a key (hex str or 32 bytes), or -1."""
        kb = bytes.fromhex(key) if isinstance(key, str) else key
        i = self._new_keys.get(kb)
        if i is not None:
            return i
        if not len(self._sorted_prefix):
            return -1
        p = _prefix(kb)
        lo = int(np.searchsorted(self._sorted_prefix, p, 'left'))
        hi = len(self._sorted_prefix)
        keys = self.key
        stripped = kb.rstrip(b'\x00')     # numpy 'S' drops trailing NULs
        while lo < hi and self._sorted_prefix[lo] == p:
            j = int(self._sorted_idx[lo])
            if keys[j] == stripped:
                return j
            lo += 1
        return -1

    def key_hex(self, i):
        return bytes(self.key[i]).ljust(32, b'\x00').hex()

    def rebuild_index(self):
        """Sorted key prefixes and the parents CSR, from scratch."""
        n = self.n
        pref = self.key[:n].view(np.uint8).reshape(n, 32)[:, :8] if n else \
            np.empty((0, 8), np.uint8)
        pref = np.ascontiguousarray(pref).view('>i8').reshape(n).astype(np.int64) \
            if n else np.empty(0, np.int64)
        order = np.argsort(pref, kind='stable').astype(np.int32)
        self._sorted_prefix = pref[order]
        self._sorted_idx = order
        self._new_keys = {}
        # parents CSR over live edge blocks only
        lens = self.e_len[:n].astype(np.int64)
        offs = self.e_off[:n]
        live = lens > 0
        if live.any():
            owners = np.repeat(np.arange(n, dtype=np.int32)[live], lens[live])
            starts = np.repeat(offs[live], lens[live])
            within = np.arange(len(owners), dtype=np.int64) - np.repeat(
                np.cumsum(lens[live]) - lens[live], lens[live])
            slots = starts + within
            kids = self.e_child[slots]
            mask = kids >= 0
            kids, owners = kids[mask], owners[mask]
            order = np.argsort(kids, kind='stable')
            kids, owners = kids[order], owners[order]
            counts = np.bincount(kids, minlength=n)
        else:
            owners = np.empty(0, np.int32)
            counts = np.zeros(n, np.int64)
        self._p_off = np.zeros(n + 1, dtype=np.int64)
        np.cumsum(counts, out=self._p_off[1:])
        self._p_idx = owners.astype(np.int32)
        self._p_built = n
        self._extra_parents = {}
        self.version += 1

    def parents(self, c):
        out = []
        if c < self._p_built:
            a, b = int(self._p_off[c]), int(self._p_off[c + 1])
            if b > a:
                out = self._p_idx[a:b].tolist()
        extra = self._extra_parents.get(c)
        if extra:
            out = list(set(out) | extra)
        return out

    # ------------------------------------------------------------ mutation
    def upsert_node(self, key, stm, status, ev, invested, pieces, mat,
                    expanded):
        """Insert or update a node; returns (idx, created, changed)."""
        kb = bytes.fromhex(key) if isinstance(key, str) else key
        i = self.lookup(kb)
        created = i < 0
        if created:
            self._grow_nodes(self.n + 1)
            i = self.n
            self.n += 1
            self.key[i] = kb
            self._new_keys[kb] = i
            changed = True
        else:
            changed = (int(self.status[i]) != status or int(self.ev[i]) != ev
                       or float(self.invested[i]) != float(invested)
                       or int(self.expanded[i]) != int(expanded))
        self.stm[i] = stm
        self.status[i] = status
        self.ev[i] = ev
        self.invested[i] = invested
        self.pieces[i] = pieces
        self.mat[i] = mat
        self.expanded[i] = 1 if expanded else 0
        return i, created, changed

    def append_node(self, key, stm, status, ev, invested, pieces, mat,
                    expanded):
        """Bulk-load insert: the caller guarantees the key is new.

        No overflow dict entry: ``rebuild_index`` must run before lookups.
        """
        if self.n >= self.cap:
            self._grow_nodes(self.n + 1)
        i = self.n
        self.n += 1
        self.key[i] = bytes.fromhex(key) if isinstance(key, str) else key
        self.stm[i] = stm
        self.status[i] = status
        self.ev[i] = ev
        self.invested[i] = invested
        self.pieces[i] = pieces
        self.mat[i] = mat
        self.expanded[i] = 1 if expanded else 0
        return i

    def children(self, u):
        a = int(self.e_off[u])
        b = a + int(self.e_len[u])
        return a, b

    def child_nodes(self, u):
        a, b = self.children(u)
        return [c for c in self.e_child[a:b].tolist() if c >= 0]

    def set_children(self, u, n_legal, kids):
        """Replace the named children of ``u``.

        ``kids``: iterable of (move_uci, child_key, status_name, eval_cp,
        nodes_invested) for every child that is NAMED (closed, seeded or
        searched).  Returns the set of node children (for order checks).
        """
        node_kids, inline, cwin, close = [], [], 0, 0
        seen = set()
        for mv, ckey, st, ev, ni in kids:
            if mv in seen:
                continue
            seen.add(mv)
            code = STATUS_CODE.get(st, UNKNOWN)
            c = self.lookup(ckey) if ni and ni > 0 else -1
            if c >= 0:
                node_kids.append((encode_move(mv), c))
            elif code == WHITE_WIN:
                cwin += 1
            elif code != UNKNOWN:
                close += 1
            elif ev is not None:
                inline.append((encode_move(mv), clip_eval(ev)))
            # an unsearched, unclosed child without eval is not named: rest
        m = len(node_kids) + len(inline)
        old_len = int(self.e_len[u])
        if m <= old_len:
            start = int(self.e_off[u])           # reuse the block in place
            self.garbage += old_len - m
        else:
            self._grow_edges(self.ne + m)
            start = self.ne
            self.ne += m
            self.garbage += old_len
        j = start
        for code, c in node_kids:
            self.e_child[j] = c
            self.e_move[j] = code
            self.e_ev[j] = NOEVAL
            j += 1
        for code, ev in inline:
            self.e_child[j] = NO_CHILD
            self.e_move[j] = code
            self.e_ev[j] = ev
            j += 1
        self.e_off[u] = start
        self.e_len[u] = m
        self.n_legal[u] = max(int(n_legal), m + cwin + close)
        self.n_cwin[u] = cwin
        self.n_close[u] = close
        out = set()
        for _code, c in node_kids:
            out.add(c)
            if not self.track_parents:
                continue
            if not (c < self._p_built and u in self._csr_parents(c)):
                self._extra_parents.setdefault(c, set()).add(u)
        return out

    def _csr_parents(self, c):
        a, b = int(self._p_off[c]), int(self._p_off[c + 1])
        return self._p_idx[a:b].tolist() if b > a else ()

    def compact(self):
        """Rewrite the edge columns without the abandoned blocks."""
        n = self.n
        lens = self.e_len[:n].astype(np.int64)
        total = int(lens.sum())
        new_off = np.zeros(n, dtype=np.int64)
        if n:
            new_off[1:] = np.cumsum(lens)[:-1]
        live = lens > 0
        if total:
            starts = np.repeat(self.e_off[:n][live], lens[live])
            within = np.arange(total, dtype=np.int64) - np.repeat(
                np.cumsum(lens[live]) - lens[live], lens[live])
            slots = starts + within
            ec, em, ee = (self.e_child[slots], self.e_move[slots],
                          self.e_ev[slots])
        else:
            ec = np.empty(0, np.int32)
            em = np.empty(0, np.uint16)
            ee = np.empty(0, np.int16)
        cap = max(64, int(total * 1.1) + 64)
        self.e_child = np.empty(cap, np.int32)
        self.e_move = np.empty(cap, np.uint16)
        self.e_ev = np.empty(cap, np.int16)
        self.e_child[:total] = ec
        self.e_move[:total] = em
        self.e_ev[:total] = ee
        self.ecap = cap
        self.ne = total
        self.e_off[:n] = new_off
        self.garbage = 0
        self.version += 1

    def trim(self):
        """Drop spare capacity (after a full load)."""
        self.compact()
        keep = self.n + max(16, self.n // 50)    # room for a day of growth
        if self.cap > keep:
            for name, dt, _d in NODE_FIELDS:
                arr = np.ascontiguousarray(self.cols[name][:keep]).copy()
                self.cols[name] = arr
                setattr(self, name, arr)
            self.cap = keep
            self.version += 1

    # ------------------------------------------------------------ building
    @classmethod
    def from_records(cls, nodes, edges, root_key):
        """Build a graph from plain records (tests, synthetic graphs).

        ``nodes``: dicts with key, fen or stm, status, ev, invested, expanded,
        and optional pieces/mat.  ``edges``: {parent_key: (n_legal, [(move,
        child_key, status, eval_cp, nodes_invested), ...])}.
        """
        g = cls(capacity=len(nodes) + 16)
        for d in nodes:
            if 'fen' in d:
                stm, pieces, mat = fen_features(d['fen'])
            else:
                stm, pieces, mat = d.get('stm', 0), d.get('pieces', 24), \
                    d.get('mat', 0)
            g.upsert_node(d['key'], stm, STATUS_CODE[d.get('status', 'UNKNOWN')],
                          clip_eval(d.get('ev')), float(d.get('invested', 1)),
                          pieces, mat, d.get('expanded', True))
        for pk, (n_legal, kids) in edges.items():
            u = g.lookup(pk)
            g.set_children(u, n_legal, kids)
        g.rebuild_index()
        g.root = g.lookup(root_key)
        return g


# ---------------------------------------------------------------- database
def _db():
    from atomicdb.database import connection, current_alias
    return connection, current_alias()


def _is_pg(connection):
    return connection.vendor == 'postgresql'


def _own_eval_sql(connection):
    if _is_pg(connection):
        return "(last_analysis->0->>'eval_cp')::int"
    return "CAST(json_extract(last_analysis, '$[0].eval_cp') AS INTEGER)"


def _ph(connection, n):
    return ','.join(['%s'] * n)


@contextlib.contextmanager
def read_tx():
    """A read-only transaction for the loader's reads.

    In autocommit (the service) it opens a transaction, makes it READ ONLY
    and gives it a statement timeout on PostgreSQL; inside a transaction
    somebody else opened (tests) it just runs there, since READ ONLY can
    only be the first statement of a transaction.
    """
    from django.db import transaction
    connection, alias = _db()
    if connection.in_atomic_block:
        with connection.cursor() as cur:
            yield cur
        return
    with transaction.atomic(using=alias):
        with connection.cursor() as cur:
            if _is_pg(connection):
                cur.execute('SET TRANSACTION READ ONLY')
                cur.execute("SET LOCAL statement_timeout = '30min'")
            yield cur


class Loader:
    """Fills a Graph from the AtomicDB database, read-only.

    ``load_full`` streams every searched position once (a sequential scan
    on PostgreSQL through a server-side cursor), then the named children of
    the open, expanded ones in batches.  ``refresh`` reads what moved since
    the watermark through the ``updated`` index.
    """

    NODE_COLS = ('key, fen, status, eval_cp, {own}, nodes_invested, expanded, '
                 'updated')

    def __init__(self, graph, batch=1000, log=None, overlap_seconds=300):
        self.g = graph
        self.batch = batch
        self.log = log or (lambda *a: None)
        self.watermark = None
        self.overlap = overlap_seconds
        self.stats = {}

    # -- helpers
    def _node_row(self, row):
        key, fen, status, ev, own, ni, expanded, updated = row
        stm, pieces, mat = fen_features(fen)
        e = own if own is not None else ev
        i, created, changed = self.g.upsert_node(
            key, stm, STATUS_CODE.get(status, UNKNOWN), clip_eval(e),
            float(ni or 0), pieces, mat, bool(expanded))
        if updated is not None and (self.watermark is None
                                    or updated > self.watermark):
            self.watermark = updated
        return i, created, changed

    def _append_row(self, row):
        key, fen, status, ev, own, ni, expanded, updated = row
        stm, pieces, mat = fen_features(fen)
        e = own if own is not None else ev
        if updated is not None and (self.watermark is None
                                    or updated > self.watermark):
            self.watermark = updated
        return self.g.append_node(
            key, stm, STATUS_CODE.get(status, UNKNOWN), clip_eval(e),
            float(ni or 0), pieces, mat, bool(expanded))

    def _load_children(self, cursor, connection, parents):
        """(Re)load the named children of node indices ``parents``.

        Returns {u: (old_node_children, new_node_children)}.
        """
        g = self.g
        out = {}
        for s in range(0, len(parents), self.batch):
            chunk = parents[s:s + self.batch]
            keys = [g.key_hex(u) for u in chunk]
            ph = _ph(connection, len(keys))
            cursor.execute('SELECT parent_id, count(*) FROM atomicdb_edge '
                           'WHERE parent_id IN (%s) GROUP BY parent_id' % ph,
                           keys)
            counts = dict(cursor.fetchall())
            cursor.execute(
                'SELECT e.parent_id, e.move_uci, c.key, c.status, c.eval_cp, '
                'c.nodes_invested FROM atomicdb_edge e JOIN atomicdb_position '
                'c ON c.key = e.child_id WHERE e.parent_id IN (%s) AND '
                "(c.status <> 'UNKNOWN' OR c.eval_cp IS NOT NULL OR "
                'c.nodes_invested > 0)' % ph, keys)
            kids = {}
            for p, mv, ck, st, ev, ni in cursor.fetchall():
                kids.setdefault(p, []).append((mv, ck, st, ev, ni))
            for u, k in zip(chunk, keys):
                old = set(g.child_nodes(u))
                new = g.set_children(u, counts.get(k, 0), kids.get(k, ()))
                out[u] = (old, new)
        return out

    # -- full load
    def load_full(self, root_key):
        connection, alias = _db()
        g = self.g
        t0 = time.monotonic()
        sql = ('SELECT ' + self.NODE_COLS.format(own=_own_eval_sql(connection))
               + ' FROM atomicdb_position WHERE nodes_invested > 0')
        open_expanded = array('i')       # ~2.6M ints: 10 MB, not 100
        # A server-side cursor on PostgreSQL (``chunked_cursor`` inside a
        # transaction): the 3.6M rows stream instead of landing in memory.
        with read_tx():
            with connection.chunked_cursor() as cur:
                cur.execute(sql)
                while True:
                    rows = cur.fetchmany(20000)
                    if not rows:
                        break
                    for row in rows:
                        i = self._append_row(row)
                        if row[2] == 'UNKNOWN' and row[6]:
                            open_expanded.append(i)
                    self.log('planner load: %d nodes, %.0fs'
                             % (g.n, time.monotonic() - t0))
        g.rebuild_index()
        g.track_parents = False
        t1 = time.monotonic()
        done = 0
        for s in range(0, len(open_expanded), 50 * self.batch):
            chunk = open_expanded[s:s + 50 * self.batch].tolist()
            with read_tx() as cur:
                self._load_children(cur, connection, chunk)
            done += len(chunk)
            self.log('planner load: children of %d/%d open nodes, %.0fs'
                     % (done, len(open_expanded), time.monotonic() - t1))
        g.trim()
        g.rebuild_index()
        g.track_parents = True
        g.root = g.lookup(root_key)
        self.stats = {'nodes': g.n, 'open_expanded': len(open_expanded),
                      'edges': int(g.e_len[:g.n].sum()),
                      'node_seconds': round(t1 - t0, 1),
                      'edge_seconds': round(time.monotonic() - t1, 1),
                      'bytes': g.nbytes()}
        return self.stats

    # -- incremental refresh
    def refresh(self, limit=200000):
        """Apply what moved since the watermark.

        Returns (touched, structural, created): node indices whose own data
        changed, {u: (old_kids, new_kids)} for reloaded children, and the
        nodes that did not exist before.
        """
        from datetime import timedelta
        connection, alias = _db()
        g = self.g
        since = self.watermark
        if since is None:
            return set(), {}, set()
        since = since - timedelta(seconds=self.overlap)
        touched, created = set(), set()
        reload = set()
        with read_tx() as cur:
            cur.execute(
                'SELECT ' + self.NODE_COLS.format(
                    own=_own_eval_sql(connection))
                + ' FROM atomicdb_position WHERE updated > %s '
                'ORDER BY updated LIMIT %s', [since, limit])
            rows = cur.fetchall()
            moved = []
            for row in rows:
                moved.append(row[0])
                if row[5] and row[5] > 0:
                    i, was_new, changed = self._node_row(row)
                    if was_new:
                        created.add(i)
                    if changed or was_new:
                        touched.add(i)
                    if row[2] == 'UNKNOWN' and row[6]:
                        reload.add(i)
                elif self.watermark is None or row[7] > self.watermark:
                    self.watermark = row[7]
            # parents in memory of everything that moved
            for s in range(0, len(moved), self.batch):
                chunk = moved[s:s + self.batch]
                cur.execute('SELECT DISTINCT parent_id FROM atomicdb_edge '
                            'WHERE child_id IN (%s)'
                            % _ph(connection, len(chunk)), chunk)
                for (pk,) in cur.fetchall():
                    u = g.lookup(pk)
                    if u >= 0 and g.status[u] == UNKNOWN and g.expanded[u]:
                        reload.add(u)
            structural = self._load_children(cur, connection,
                                             sorted(reload))
        self.stats = {'moved': len(rows), 'touched': len(touched),
                      'created': len(created), 'reloaded': len(structural)}
        return touched, structural, created

    def inline_owners(self, keys):
        """(parent node, move code) pairs under which unsearched ``keys`` hang."""
        connection, alias = _db()
        g = self.g
        out = {}
        if not keys:
            return out
        with read_tx() as cur:
            for s in range(0, len(keys), self.batch):
                chunk = keys[s:s + self.batch]
                cur.execute('SELECT child_id, parent_id, move_uci FROM '
                            'atomicdb_edge WHERE child_id IN (%s)'
                            % _ph(connection, len(chunk)), chunk)
                for ck, pk, mv in cur.fetchall():
                    u = g.lookup(pk)
                    if u >= 0:
                        out.setdefault(ck, []).append((u, encode_move(mv)))
        return out

    def resolve_children(self, pairs):
        """Child keys of (parent node, move uci) pairs."""
        connection, alias = _db()
        g = self.g
        want = {}
        for u, mv in pairs:
            want.setdefault(g.key_hex(u), set()).add(mv)
        out = {}
        if not want:
            return out
        parents = list(want)
        with read_tx() as cur:
            for s in range(0, len(parents), self.batch):
                chunk = parents[s:s + self.batch]
                cur.execute('SELECT parent_id, move_uci, child_id FROM '
                            'atomicdb_edge WHERE parent_id IN (%s)'
                            % _ph(connection, len(chunk)), chunk)
                for pk, mv, ck in cur.fetchall():
                    if mv in want[pk]:
                        out[(pk, mv)] = ck
        return out
