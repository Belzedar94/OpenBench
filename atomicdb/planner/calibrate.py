"""Calibration of the planner's cost model (read-only on the database).

A port of ``tools/planner-proto/calib_export.py`` (export) and
``fit_costs.py`` (fit), § docs/atomicdb-planner-design.md section 2.

EXPORT.  Sample the searched positions (``TABLESAMPLE SYSTEM`` with a fixed
seed on PostgreSQL), classify each by its own search's verdict (the side the
engine prefers is to move: ``fav``; the other: ``dog``) and by band of
|eval|, then, for a stratified subset (every proven one up to ``event_cap``
per stratum, ``per_stratum`` open ones), walk the explored subgraph below
it and add up the engine nodes invested there.  Proven positions give the
COST (an event); open ones give only a lower bound (right-censored at the
exposure so far).  Every query runs in a read-only session with a statement
timeout; nothing is written.

FIT.  Weighted maximum likelihood of the censored lognormal

    ln C = x . beta + sigma * eps

over a few variants of x, picked by AIC; the weights undo the stratified
plan.  The survivor-only fit (events alone, audit table 2.2) is kept as the
optimistic end of the bracket.  Weighted Kaplan-Meier brackets per (role,
band) go to the report.
"""

import collections
import contextlib
import math
import re
import time
from datetime import datetime, timezone

from .costmodel import (DEFAULT_HORIZON, NAMES, SCHEMA, features,
                        restricted_mean)

RX = re.compile(r'depth (\d+)')
VAL = {'p': 1, 'n': 3, 'b': 3, 'r': 5, 'q': 9, 'k': 0}
EDGES = [0, 300, 600, 1000, 1300, 1600, 2500, 5000, 9000]
BAND_NAMES = ['0-3', '3-6', '6-10', '10-13', '13-16', '16-25', '25-50',
              '50-90', '>=90']
FIT_BANDS = ['3-6', '6-10', '10-13', '13-16', '16-25', '25-50', '50-90']


def band(a):
    for i in range(len(EDGES) - 1, -1, -1):
        if a >= EDGES[i]:
            return BAND_NAMES[i]
    return BAND_NAMES[0]


def material(board):
    w = b = n = 0
    for ch in board:
        if ch.isalpha():
            n += 1
            if ch.isupper():
                w += VAL.get(ch.lower(), 0)
            else:
                b += VAL.get(ch, 0)
    return w, b, n


@contextlib.contextmanager
def read_only_session(cursor, connection, timeout_ms=240000):
    """Session-level read-only + statement timeout on PostgreSQL."""
    pg = connection.vendor == 'postgresql'
    if pg:
        cursor.execute('SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY')
        cursor.execute('SET statement_timeout = %s' % int(timeout_ms))
    try:
        yield
    finally:
        if pg:
            cursor.execute('SET SESSION CHARACTERISTICS AS TRANSACTION '
                           'READ WRITE')
            cursor.execute('RESET statement_timeout')


def _ph(n):
    return ','.join(['%s'] * n)


def export(pct=4.0, per_stratum=200, event_cap=1500, node_cap=2500,
           time_limit=2700.0, seed=20261111, log=None):
    """(summary, records) for the fit.  Read-only."""
    from atomicdb.database import connection
    log = log or (lambda *a: None)
    t0 = time.monotonic()
    pg = connection.vendor == 'postgresql'
    with connection.cursor() as cur, read_only_session(cur, connection):
        if pg:
            sample = ('TABLESAMPLE SYSTEM (%s) REPEATABLE (%d)'
                      % (float(pct), int(seed)))
            own = "(last_analysis->0->>'eval_cp')::int"
            raw = "last_analysis->0->>'raw'"
        else:                                    # tests: every row
            sample = ''
            own = "CAST(json_extract(last_analysis, '$[0].eval_cp') AS INTEGER)"
            raw = "json_extract(last_analysis, '$[0].raw')"
        cur.execute(
            'SELECT key, fen, status, eval_cp, nodes_invested, %s, %s '
            'FROM atomicdb_position %s WHERE nodes_invested > 0'
            % (own, raw, sample))
        rows = cur.fetchall()
        strata = collections.defaultdict(list)
        tab = collections.defaultdict(collections.Counter)
        for key, fen, status, ev, nodes, la_ev, rawtxt in rows:
            o = la_ev if la_ev is not None else ev
            if o is None or o == 0:
                continue
            fav_white = o > 0
            stm = fen.split()[1]
            role = 'fav' if (stm == 'w') == fav_white else 'dog'
            a = abs(o)
            if status == ('WHITE_WIN' if fav_white else 'BLACK_WIN'):
                res = 'proven'
            elif status == ('BLACK_WIN' if fav_white else 'WHITE_WIN'):
                res = 'against'
            elif status == 'DRAW':
                res = 'draw'
            else:
                res = 'open'
            b = band(a)
            tab[(role, b)][res] += 1
            if a >= 9000 or a < 300 or res in ('against', 'draw'):
                continue
            m = RX.search(rawtxt or '')
            wm, bm, npieces = material(fen.split()[0])
            strata[(role, b, res == 'proven')].append(dict(
                key=key, stm=stm, role=role, a=a, band=b, res=res,
                own_nodes=nodes, depth=int(m.group(1)) if m else None,
                mat_fav=(wm - bm) * (1 if fav_white else -1),
                pieces=npieces))

        def edges_of(parents):
            out = collections.defaultdict(list)
            for i in range(0, len(parents), 400):
                chunk = parents[i:i + 400]
                cur.execute(
                    'SELECT e.parent_id, c.key, c.nodes_invested FROM '
                    'atomicdb_edge e JOIN atomicdb_position c ON c.key = '
                    'e.child_id WHERE e.parent_id IN (%s)' % _ph(len(chunk)),
                    chunk)
                for p, c, ni in cur.fetchall():
                    out[p].append((c, ni))
            return out

        def walk_all(root):
            seen = {root}
            level = [root]
            nodes = expanded = 0
            trunc = False
            while level:
                kids = edges_of(level)
                nxt = []
                for p in level:
                    expanded += int(p in kids)
                    for c, ni in kids.get(p, ()):
                        if c in seen:
                            continue
                        seen.add(c)
                        if ni:
                            nodes += ni
                        nxt.append(c)
                if expanded > node_cap:
                    trunc = True
                    break
                level = nxt
            return nodes, trunc

        summary = {'searched_rows': len(rows),
                   'table': {'%s|%s' % k: dict(v) for k, v in
                             sorted(tab.items())},
                   'strata_sizes': {'%s|%s|%s' % k: len(v)
                                    for k, v in strata.items()}}
        order = [(k, v[:(event_cap if k[2] else per_stratum)])
                 for k, v in sorted(strata.items())]
        recs = []
        i = 0
        stopped = 'done'
        while True:
            progressed = False
            for k, v in order:
                if i >= len(v):
                    continue
                progressed = True
                if time.monotonic() - t0 > time_limit:
                    stopped = 'time'
                    break
                rec = v[i]
                kids = edges_of([rec['key']]).get(rec['key'], [])
                rec['n_children'] = len(kids)
                rec['desc_nodes'], rec['desc_trunc'] = walk_all(rec['key'])
                recs.append(rec)
            if stopped == 'time' or not progressed:
                break
            i += 1
            log('calibrate: %d walked, %.0fs' % (len(recs),
                                                 time.monotonic() - t0))
        summary['stopped'] = stopped
        summary['seconds'] = round(time.monotonic() - t0, 1)
        return summary, recs


# ---------------------------------------------------------------- fit
VARIANTS = {
    'band_role': [0, 1, 2, 3],
    'band_role_branch': [0, 1, 2, 3, 4, 5],
    'band_role_branch_depth': [0, 1, 2, 3, 4, 5, 6],
    'full_material_aware': list(range(len(NAMES))),
}


def cost_of(r):
    return float(max(r['desc_nodes'] or 0, r['own_nodes'] or 0, 1))


def stratum(r):
    return '%s|%s|%s' % (r['role'], r['band'], r['res'] == 'proven')


def fit(summary, recs, horizon=DEFAULT_HORIZON, variants=None):
    """Weighted censored-lognormal MLE; returns (model dict, report dict)."""
    import numpy as np
    from scipy import optimize, stats

    if not recs:
        raise ValueError('no walked positions to fit')
    ev = np.array([r['res'] == 'proven' for r in recs])
    if not ev.any():
        raise ValueError('no proven positions in the sample')
    nch = [r['n_children'] for r in recs if r.get('n_children')]
    dep = [r['depth'] for r in recs if r.get('depth')]
    med = {'n_children': float(np.median(nch)) if nch else 28.0,
           'depth': float(np.median(dep)) if dep else 19.0}
    sizes = summary['strata_sizes']
    taken = collections.Counter(stratum(r) for r in recs)
    w = np.array([sizes.get(stratum(r), 1) / taken[stratum(r)] for r in recs],
                 dtype=float)
    w = w / w.mean()
    y = np.log(np.array([cost_of(r) for r in recs]))

    def X_of(cols, idx=None):
        rows = []
        for r in recs:
            x = features(r['a'], 1.0 if r['role'] == 'dog' else 0.0,
                         r.get('n_children'), r.get('depth'), r['pieces'],
                         r['mat_fav'], med)
            rows.append([x[i] for i in cols])
        return np.array(rows)

    def nll(theta, X, y, ev, w):
        beta, ls = theta[:-1], theta[-1]
        s = math.exp(ls)
        z = (y - X @ beta) / s
        ll = np.where(ev, stats.norm.logpdf(z) - ls, stats.norm.logsf(z))
        return -(w * ll).sum()

    def run(X, y, ev, w):
        t0 = np.zeros(X.shape[1] + 1)
        t0[0] = float(np.average(y[ev], weights=w[ev]))
        t0[-1] = math.log(2.0)
        return optimize.minimize(nll, t0, args=(X, y, ev, w),
                                 method='L-BFGS-B')

    variants = variants or VARIANTS
    fits, aic = {}, {}
    for name, cols in variants.items():
        res = run(X_of(cols), y, ev, w)
        fits[name] = (cols, res)
        aic[name] = round(2 * res.fun + 2 * (len(cols) + 1), 1)
    best = min(aic, key=aic.get)
    cols, res = fits[best]
    beta = [0.0] * len(NAMES)
    for c, b in zip(cols, res.x[:-1]):
        beta[c] = float(b)
    sigma = math.exp(res.x[-1])
    X = X_of(cols)
    res_sv = run(X[ev], y[ev], np.ones(int(ev.sum()), bool), w[ev])
    beta_sv = [0.0] * len(NAMES)
    for c, b in zip(cols, res_sv.x[:-1]):
        beta_sv[c] = float(b)

    km = {}
    by = collections.defaultdict(list)
    for i, r in enumerate(recs):
        by[(r['role'], r['band'])].append(i)
    for (role, b), idx in sorted(by.items()):
        cs = np.array([cost_of(recs[i]) for i in idx])
        es = np.array([bool(ev[i]) for i in idx])
        ws = np.array([w[i] for i in idx])
        km['%s|%s' % (role, b)] = {
            'walked': len(idx), 'events': int(es.sum()),
            'P_closed_by_KM_upper': _km(cs, es, ws),
            'P_closed_by_never_lower': {
                '%.0e' % g: round(float(ws[es & (cs <= g)].sum() / ws.sum()), 4)
                for g in (1e9, 1e10, 1e11, 1e12)}}
    pred = {}
    for role in ('fav', 'dog'):
        for a in (400, 800, 1150, 1450, 2000, 3500, 7000):
            x = features(a, 1.0 if role == 'dog' else 0.0, None, None, 22, 3,
                         med)
            mu = sum(b_ * v for b_, v in zip(beta, x))
            pred['%s|%+d' % (role, a)] = {
                'median_Gn': round(math.exp(mu) / 1e9, 2),
                'restricted_mean_Gn': round(
                    restricted_mean(mu, sigma, 0, horizon) / 1e9, 1)}
    now = datetime.now(timezone.utc)
    model = {
        'schema': SCHEMA,
        'version': 'cal-' + now.strftime('%Y%m%dT%H%MZ'),
        'created': now.isoformat(),
        'source': 'calibrate_planner',
        'names': list(NAMES), 'beta': beta, 'sigma': sigma,
        'median_fill': med, 'horizon': horizon, 'variant': best,
        'walked': len(recs), 'events': int(ev.sum()),
        'aic': aic, 'converged': bool(res.success),
        'survivor_beta': beta_sv,
        'survivor_sigma': math.exp(res_sv.x[-1]),
        'sample': {'searched_rows': summary.get('searched_rows'),
                   'stopped': summary.get('stopped')},
    }
    report = {'model': model, 'km': km, 'pred': pred,
              'sample_table': summary.get('table')}
    return model, report


def _km(costs, events, weights, grid=(1e9, 1e10, 1e11, 1e12)):
    import numpy as np
    order = np.argsort(costs)
    c, e, ww = costs[order], events[order], weights[order]
    at_risk = ww.sum()
    s = 1.0
    steps = []
    i = 0
    while i < len(c):
        j = i
        d = tot = 0.0
        while j < len(c) and c[j] == c[i]:
            d += ww[j] * e[j]
            tot += ww[j]
            j += 1
        if at_risk > 0:
            s *= 1 - d / at_risk
        steps.append((c[i], s))
        at_risk -= tot
        i = j
    out = {}
    for g in grid:
        val = 1.0
        for ci, si in steps:
            if ci <= g:
                val = si
            else:
                break
        out['%.0e' % g] = (round(float(1 - val), 4)
                           if len(c) and g <= c.max() else None)
    return out
