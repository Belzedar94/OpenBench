"""The planner in SHADOW mode: compute, pick, record, serve nothing.

One process (``run_planner --mode shadow``) keeps the graph in memory and,
every few minutes:

  1. reads what moved in the database since the last cycle (the ``updated``
     index) and folds it into the graph, incrementally;
  2. puts virtual loss on the work already queued or leased (so the batch
     it would serve does not duplicate it);
  3. selects the batch it WOULD serve (``select_batch``) and lifts the
     batch's virtual loss again;
  4. records the batch and the cycle's metrics in ``PlannerShadowCycle`` /
     ``PlannerShadowPick``: root expected cost (also in fleet-days), open
     obligations per ply of its strategy, the overlap of the previous
     batch with what the fleet was actually given, timings and memory.

The ONLY writes are to those two tables (and the pruning of their old rows).
Positions, edges and tasks are read, never written; that is the contract the
tests pin (``test_planner_shadow``).
"""

import logging
import math
import os
import time
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .costmodel import load_model
from .graph import Graph, Loader, decode_move
from .master import Master
from .select import LEAF, NODE, REST, clear_batch, select_batch

logger = logging.getLogger(__name__)

try:
    import psutil
except ImportError:          # pragma: no cover
    psutil = None
try:
    import resource
except ImportError:          # pragma: no cover - Windows
    resource = None


def rss_mb():
    if psutil is None:
        return None
    try:
        return psutil.Process(os.getpid()).memory_info().rss / 2 ** 20
    except Exception:        # noqa: BLE001
        return None


def peak_mb():
    if resource is None:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def finite(x):
    """JSON-safe number: inf/NaN become None (PostgreSQL jsonb refuses them)."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def clean(obj):
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, float):
        return finite(obj)
    return obj


class ShadowRunner:
    def __init__(self, batch=512, retention_days=7, full_every_hours=6.0,
                 vl=1.0, spread=4.0, tol=1e-3, strategy_cap=200000,
                 load_batch=1000, model=None, log=None):
        self.batch = batch
        self.retention = timedelta(days=retention_days)
        self.full_every = full_every_hours * 3600.0
        self.strategy_cap = strategy_cap
        self.model = model or load_model()
        self.alt = self.model.survivor()
        self.log = log or logger.info
        self.graph = Graph()
        self.loader = Loader(self.graph, batch=load_batch, log=self.log)
        self.master = Master(self.graph, self.model, vl_factor=vl,
                             spread=spread, tol=tol)
        self.prev = None          # (started, set of picked position keys)
        self.last_full = 0.0
        self.last_prune = 0.0
        self.inflight_keys = frozenset()
        self.load_report = {}

    # ------------------------------------------------------------ startup
    def start(self):
        from atomicdb import ingest
        t0 = time.monotonic()
        stats = self.loader.load_full(ingest.root_key())
        t1 = time.monotonic()
        ordered = self.master.full_recompute()
        t2 = time.monotonic()
        self.last_full = t2
        self.load_report = {
            'load_seconds': round(t1 - t0, 2),
            'full_recompute_seconds': round(t2 - t1, 2),
            'ordered': ordered, 'rss_mb': rss_mb(), 'peak_mb': peak_mb(),
            **stats}
        self.log('planner start: %s' % self.load_report)
        return self.load_report

    # ------------------------------------------------------------ helpers
    def _inflight(self):
        """Virtual loss for every task already queued or leased."""
        from atomicdb.models import AnalysisTask
        keys = set(AnalysisTask.objects.filter(
            state__in=('PENDING', 'LEASED')).values_list(
                'position_id', flat=True))
        g, m = self.graph, self.master
        nodes, unsearched = set(), []
        for k in keys:
            u = g.lookup(k)
            if u >= 0:
                if not m.is_internal(u):
                    nodes.add(u)
            else:
                unsearched.append(k)
        inline = set()
        for pairs in self.loader.inline_owners(unsearched).values():
            for u, code in pairs:
                inline.add((u, code))
        m.set_inflight(nodes=nodes, inline=inline, rest=())
        self.inflight_keys = frozenset(keys)
        return {'tasks': len(keys), 'on_nodes': len(nodes),
                'on_inline': len(inline)}

    def _served(self, since):
        from atomicdb.models import AnalysisTask
        rows = list(AnalysisTask.objects.filter(
            state='COMPLETED', completed__gt=since).values_list(
                'position_id', 'source'))
        rows += list(AnalysisTask.objects.filter(
            state='LEASED', leased_at__gt=since).values_list(
                'position_id', 'source'))
        return rows

    def _fleet_nodes_per_day(self):
        from django.db.models import Sum
        from atomicdb.models import AnalysisTask
        v = AnalysisTask.objects.filter(
            state='COMPLETED',
            completed__gt=timezone.now() - timedelta(days=1)).aggregate(
                s=Sum('nodes_searched'))['s']
        return float(v) if v else None

    def _pick_rows(self, picks):
        g = self.graph
        leaf_pairs = [(p.node, p.move) for p in picks if p.kind == LEAF]
        resolved = self.loader.resolve_children(leaf_pairs) if leaf_pairs \
            else {}
        rows, seen = [], set()
        for p in picks:
            node_key = g.key_hex(p.node)
            if p.kind == LEAF:
                pos = resolved.get((node_key, p.move), '')
                parent, move = node_key, p.move
            else:
                pos, parent, move = node_key, '', ''
            ident = (pos, p.kind, move)
            if not pos or ident in seen:   # a transposed leaf picked twice
                continue
            seen.add(ident)
            rows.append({'rank': len(rows), 'kind': p.kind,
                         'position_key': pos, 'parent_key': parent,
                         'move_uci': move, 'ply': p.ply, 'pn': finite(p.pn),
                         'dn': finite(p.dn),
                         'eval_cp': None if p.eval_cp is None
                         else int(p.eval_cp)})
        return rows

    # ------------------------------------------------------------ one cycle
    def cycle(self, record=True):
        from atomicdb.models import PlannerShadowCycle, PlannerShadowPick
        from atomicdb.database import current_alias
        started = timezone.now()
        t0 = time.monotonic()
        g, m = self.graph, self.master
        touched, structural, created = self.loader.refresh()
        if g.garbage > max(1_000_000, g.ne // 2):
            g.compact()
        t1 = time.monotonic()
        m.apply_changes(touched, structural, created)
        full = m.needs_full or (t1 - self.last_full) > self.full_every
        if full:
            if len(g._extra_parents) > 100_000:
                g.rebuild_index()
            m.full_recompute()
            self.last_full = time.monotonic()
            recomputed = m.last.get('ordered', 0)
        else:
            recomputed = m.propagate()
        t2 = time.monotonic()
        inflight = self._inflight()
        m.propagate()
        t3 = time.monotonic()
        picks = select_batch(m, self.batch)
        t4 = time.monotonic()
        rows = self._pick_rows(picks)
        clear_batch(m)
        t5 = time.monotonic()
        root = g.root
        root_pn = m.PN[root] if root >= 0 else None
        root_dn = m.DN[root] if root >= 0 else None
        strat = m.strategy(max_nodes=self.strategy_cap, alt=self.alt)
        t6 = time.monotonic()
        fleet = self._fleet_nodes_per_day()
        overlap = {}
        if self.prev is not None:
            since, prev_keys = self.prev
            served = self._served(since)
            served_keys = {k for k, _s in served}
            by_source = {}
            hit_by_source = {}
            for k, src in served:
                by_source[src] = by_source.get(src, 0) + 1
                if k in prev_keys:
                    hit_by_source[src] = hit_by_source.get(src, 0) + 1
            overlap = {
                'window_seconds': round((started - since).total_seconds(), 1),
                'served_tasks': len(served),
                'served_by_source': by_source,
                'prev_picks': len(prev_keys),
                'prev_picks_served': len(prev_keys & served_keys),
                'served_on_prev_picks_by_source': hit_by_source,
            }
        kinds = {}
        for r in rows:
            kinds[r['kind']] = kinds.get(r['kind'], 0) + 1
        metrics = {
            'root_expected_cost_nodes': finite(root_pn),
            'root_expected_cost_fleet_days': finite(
                root_pn / fleet) if fleet and root_pn is not None else None,
            'strategy': {
                'obligations_by_ply': strat['obligations_by_ply'],
                'obligations': sum(strat['obligations_by_ply'].values()),
                'cost_main': strat['cost_main'],
                'cost_survivor_bracket': strat['cost_alt'],
                'visited': strat['visited'], 'truncated': strat['truncated']},
            'picks_by_kind': kinds,
            'inflight': inflight,
            'overlap_prev': overlap,
            'refresh': self.loader.stats,
            'recompute': {'mode': 'full' if full else 'incremental',
                          'nodes': recomputed},
            'graph': {'nodes': g.n, 'edges_live': int(g.e_len[:g.n].sum()),
                      'edge_slots': g.ne, 'bytes': g.nbytes()},
            'seconds': {'refresh': round(t1 - t0, 3),
                        'recompute': round(t2 - t1, 3),
                        'inflight': round(t3 - t2, 3),
                        'select': round(t4 - t3, 3),
                        'resolve_clear': round(t5 - t4, 3),
                        'strategy': round(t6 - t5, 3)},
            'rss_mb': rss_mb(), 'peak_mb': peak_mb(),
            'load': self.load_report if self.prev is None else None,
        }
        metrics = clean(metrics)
        self.prev = (started, {r['position_key'] for r in rows})
        report = {'started': started.isoformat(), 'picks': len(rows),
                  'root_pn': finite(root_pn), 'model': self.model.version,
                  **metrics}
        if not record:
            return report
        alias = current_alias()
        with transaction.atomic(using=alias):
            cyc = PlannerShadowCycle.objects.using(alias).create(
                started=started, finished=timezone.now(), mode='shadow',
                model_version=self.model.version[:64],
                root_key=g.key_hex(root) if root >= 0 else '',
                root_pn=finite(root_pn), root_dn=finite(root_dn),
                fleet_nodes_per_day=fleet, picks=len(rows), metrics=metrics)
            PlannerShadowPick.objects.using(alias).bulk_create(
                [PlannerShadowPick(cycle=cyc, **r) for r in rows],
                batch_size=1000)
        report['cycle_id'] = cyc.pk
        if time.monotonic() - self.last_prune > 3600:
            self.last_prune = time.monotonic()
            PlannerShadowCycle.objects.using(alias).filter(
                started__lt=timezone.now() - self.retention).delete()
        return report


def describe_pick(graph, pick):
    """Human-readable one-liner of a pick (management output)."""
    if pick.kind == NODE:
        return 'NODE %s' % graph.key_hex(pick.node)[:12]
    if pick.kind == REST:
        return 'REST %s' % graph.key_hex(pick.node)[:12]
    return 'LEAF %s %s' % (graph.key_hex(pick.node)[:12], pick.move)


__all__ = ['ShadowRunner', 'describe_pick', 'decode_move', 'rss_mb']
