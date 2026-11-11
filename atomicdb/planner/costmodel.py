"""Cost model of the planner: expected engine nodes still missing to close a leaf.

The model is a censored lognormal fitted by ``calibrate_planner``
(§ atomicdb.planner.calibrate, a port of ``tools/planner-proto/fit_costs.py``):

    ln C = x(leaf) . beta + sigma * eps,      eps ~ N(0, 1)

where C is the engine nodes invested in a leaf's explored subgraph until it
closes and x holds ln(attacker eval / 100), whether the defender is to move
("dog") and their interaction (the variant the calibration picks by AIC; the
branching, depth and material columns stay at zero unless a later fit says
they help).

The planner uses the RESTRICTED mean of what is still missing:

    pn(leaf) = E[ min(C, H) - I | C > I ]

with I the nodes already invested in the leaf itself and H a horizon (1e13,
about a day of the fleet).  Conditioning on C > I makes a leaf that was
searched and did not close look MORE expensive, the opposite of the old band
initialisation.  The horizon brackets the mean: without it a lognormal with
sigma 3.3 is all tail.  Below +3 for the attacker the model is not used: the
price grows tenfold per three pawns above H, finite so that the descent can
still come back when nothing better is left.

``dn`` cannot be calibrated (two positions proven against the engine's sign
in 102,247), so it is ordinal: it only has to send the descent, at a Black
node, to the reply that looks most dangerous.

Runtime needs only ``math``: the normal tail comes from ``erfc``.  SciPy is
used by the calibration alone.
"""

import json
import math
import os
from datetime import datetime, timezone

SCHEMA = 'atomicdb.planner.costmodel.v1'
NAMES = ('const', 'log_a', 'dog', 'dog*log_a', 'log_branch', 'dog*log_branch',
         'depth/10', 'pieces/10', 'mat_fav/10')
MIN_A = 300            # below +3 for the attacker: priced above the horizon
MAX_PN = 1e22          # cap of the sub-+3 price (finite on purpose)
DEFAULT_HORIZON = 1e13
MODEL_FILENAME = 'planner_costmodel.json'

# The fit of 11-Nov-2026 (docs/atomicdb-planner-design.md, section 2.3):
# 3,698 walked positions, 1,125 proven, AIC picks band and role.  It is the
# model the planner runs with until ``calibrate_planner`` writes its own.
PROTOTYPE_MODEL = {
    'schema': SCHEMA,
    'version': 'proto-20261111',
    'created': '2026-11-11T00:00:00+00:00',
    'source': 'tools/planner-proto/fit_costs.py',
    'names': list(NAMES),
    'beta': [46.503948347304565, -6.074569828285225, 1.0591858648252124,
             0.06028191889784377, 0.0, 0.0, 0.0, 0.0, 0.0],
    'sigma': 3.293962426377268,
    'median_fill': {'n_children': 28.0, 'depth': 19.0},
    'horizon': DEFAULT_HORIZON,
    'variant': 'band_role',
    'walked': 3698,
    'events': 1125,
    # The survivor-only fit (events alone, what audit table 2.2 measured):
    # the optimistic end of the bracket the metrics print.
    'survivor_beta': [24.610586367266116, -0.9419336940521689,
                      0.19307160260776451, 0.32408722584532107,
                      0.0, 0.0, 0.0, 0.0, 0.0],
    'survivor_sigma': 2.276744546210702,
}

_SQRT2 = math.sqrt(2.0)


def norm_sf(z):
    """P(N(0,1) > z), accurate in both tails."""
    return 0.5 * math.erfc(z / _SQRT2)


def norm_cdf(z):
    return 0.5 * math.erfc(-z / _SQRT2)


def restricted_mean(mu, s, lo=0.0, hi=DEFAULT_HORIZON):
    """E[min(C, hi) - lo | C > lo] for C ~ LogNormal(mu, s)."""
    if lo >= hi:
        return 0.0
    llo = math.log(lo) if lo > 0 else -1e9
    lhi = math.log(hi)
    surv_lo = norm_sf((llo - mu) / s)
    if surv_lo < 1e-300:
        return hi - lo
    # exp(mu + s^2/2) can overflow for absurd mu; the bracket caps it anyway
    try:
        scale = math.exp(mu + s * s / 2.0)
    except OverflowError:
        return hi - lo
    # cdf(b) - cdf(a) written as sf(a) - sf(b): exact in the upper tail,
    # where both cdfs are 1 to the last digit
    part = scale * (norm_sf((llo - mu - s * s) / s)
                    - norm_sf((lhi - mu - s * s) / s))
    tail = hi * norm_sf((lhi - mu) / s)
    return min(hi - lo, max(0.0, (part + tail) / surv_lo - lo))


def features(a_cp, dog, n_branch, depth, pieces, mat_fav, med):
    a = max(a_cp, MIN_A) / 100.0
    nb = n_branch or med.get('n_children', 28.0)
    d = depth if depth is not None and depth >= 0 else med.get('depth', 19.0)
    la = math.log(a)
    lb = math.log(max(1, nb))
    return (1.0, la, dog, dog * la, lb, dog * lb, d / 10.0, pieces / 10.0,
            mat_fav / 10.0)


def dn_leaf(a_cp):
    """Disproof number of a leaf: ORDINAL, grows with the attacker's eval."""
    if a_cp is None:
        return 1e9
    return 1e9 * math.exp(min(60.0, max(-30.0, (a_cp - MIN_A) / 250.0)))


class CostModel:
    """A fitted cost model; ``pn`` is the proof number of one leaf."""

    def __init__(self, data=None, survivor=False):
        data = dict(PROTOTYPE_MODEL if data is None else data)
        if data.get('schema', SCHEMA) != SCHEMA:
            raise ValueError('unknown cost model schema %r' % data.get('schema'))
        self.data = data
        self.version = str(data.get('version', 'unknown'))
        if survivor:
            self.beta = tuple(float(b) for b in data['survivor_beta'])
            self.sigma = float(data['survivor_sigma'])
            self.version += '+survivor'
        else:
            self.beta = tuple(float(b) for b in data['beta'])
            self.sigma = float(data['sigma'])
        if len(self.beta) != len(NAMES):
            raise ValueError('cost model needs %d coefficients' % len(NAMES))
        self.med = dict(data.get('median_fill') or {})
        self.H = float(data.get('horizon', DEFAULT_HORIZON))
        # Only the band/role columns are non-zero in the fitted variant; the
        # fast path skips the feature vector when that is the case.
        self._simple = not any(self.beta[4:])
        self._cache = {}
        self._fresh = {}

    def survivor(self):
        """The optimistic end of the bracket (survivor-only fit)."""
        if 'survivor_beta' not in self.data:
            return None
        return CostModel(self.data, survivor=True)

    def mu(self, a_cp, dog, n_branch=None, depth=None, pieces=24, mat_fav=0):
        if self._simple:
            la = math.log(max(a_cp, MIN_A) / 100.0)
            b = self.beta
            return b[0] + b[1] * la + b[2] * dog + b[3] * dog * la
        x = features(a_cp, dog, n_branch, depth, pieces, mat_fav, self.med)
        return sum(b * v for b, v in zip(self.beta, x))

    def below_threshold(self, a_cp):
        return min(MAX_PN, self.H * 10 ** (1 + (MIN_A - a_cp) / 300.0))

    def pn(self, a_cp, dog, n_branch=None, depth=None, pieces=24, mat_fav=0,
           invested=0.0):
        """Expected remaining engine nodes to close (attacker eval ``a_cp``).

        ``a_cp`` is the eval from the ATTACKER's point of view (White for the
        goal WHITE_WIN); ``dog`` is 1.0 when the defender is to move.
        """
        if a_cp is None:
            return self.H
        if a_cp < MIN_A:
            return self.below_threshold(a_cp)
        # The invested nodes are quantised to quarter powers of two and the
        # value is computed AT the quantised point, so the cache never makes
        # the answer depend on which leaf asked first (an incremental pass
        # and a full one must agree to the bit).
        q = int(math.log2(invested + 1.0) * 4) if invested > 0 else 0
        a_cp = int(a_cp)
        if self._simple:
            key = (a_cp, dog, q)
        else:
            key = (a_cp, dog, n_branch, depth, pieces, mat_fav, q)
        v = self._cache.get(key)
        if v is None:
            inv_q = 2.0 ** (q / 4.0) - 1.0 if q else 0.0
            mu = self.mu(a_cp, dog, n_branch, depth, pieces, mat_fav)
            v = restricted_mean(mu, self.sigma, inv_q, self.H)
            if len(self._cache) > 500_000:
                self._cache.clear()
            self._cache[key] = v
        return v

    def fresh_pn(self, a_cp, dog):
        """pn of a leaf nobody has searched yet (I = 0): a table lookup."""
        key = (int(a_cp), dog)
        v = self._fresh.get(key)
        if v is None:
            v = self.pn(a_cp, dog)
            if len(self._fresh) > 200_000:
                self._fresh.clear()
            self._fresh[key] = v
        return v

    def to_json(self):
        return dict(self.data)


def state_dir():
    from django.conf import settings
    return str(getattr(settings, 'ATOMICDB_STATE_DIR', '') or '')


def model_path(root=None):
    root = state_dir() if root is None else root
    if not root:
        return None
    return os.path.join(root, MODEL_FILENAME)


def load_model(path=None):
    """The calibrated model in the state dir, or the prototype fit.

    Anything odd (missing file, broken JSON, wrong schema) falls back to the
    prototype fit: the planner is advisory, a missing calibration must never
    stop it.  The returned model says which one it is in ``version``.
    """
    path = model_path() if path is None else path
    if path:
        try:
            with open(path, encoding='utf-8') as fh:
                return CostModel(json.load(fh))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return CostModel()


def save_model(data, path):
    data = dict(data)
    data.setdefault('schema', SCHEMA)
    data.setdefault('created', datetime.now(timezone.utc).isoformat())
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return path
