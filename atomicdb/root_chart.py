"""The start position's value over time, drawn on the home page.

Community request (Torom, 9-Nov-2026): a chart of how the start position's
value moves as the tree grows.  The data is already recorded: the hourly
capture writes the start position's headline value into
``MainLineSnapshot.value`` (White's side, centipawns, the scale of the roots
page) and the cumulative engine nodes into ``ProgressSnapshot``.  Nothing is
reconstructed and nothing new is recorded; the chart starts where that record
starts.

The series is downsampled to at most ``MAX_POINTS`` points (the last value
of each slot), drawn once into SVG coordinates and kept in the shared cache
for ``FRESH_SECONDS``, so a visitor never pays a query for it while the entry
is warm.  The SVG stretches to the width of its box; the labels are HTML
around it, so they keep their size on a phone.
"""

import math

from django.utils import timezone

from . import metrics, roots
from .models import MainLineSnapshot, ProgressSnapshot

CACHE_KEY = 'atomicdb.root-chart.v1'
# The source changes once an hour; ten minutes keeps a new hour visible soon
# without measuring per visitor.
FRESH_SECONDS = 600
TTL_SECONDS = 6 * 3600
MAX_POINTS = 180
# SVG user units.  The drawing is stretched (``preserveAspectRatio="none"``)
# and its strokes do not scale, so only the proportions matter.
WIDTH = 600
HEIGHT = 160
# Room above and below the line inside the drawing, in user units.
INSET = 8
# The value axis snaps to this many centipawns.
VALUE_STEP = 25


def _slots(rows, first, step_hours):
    """The last row of each ``step_hours`` slot, in order."""
    points = []
    for at, value in rows:
        slot = int((at - first).total_seconds() // 3600) // step_hours
        if points and points[-1][0] == slot:
            points[-1] = (slot, at, value)
        else:
            points.append((slot, at, value))
    return [(at, value) for _slot, at, value in points]


def _axis(values):
    lo = math.floor(min(values) / VALUE_STEP) * VALUE_STEP
    hi = math.ceil(max(values) / VALUE_STEP) * VALUE_STEP
    if hi == lo:
        lo, hi = lo - VALUE_STEP, hi + VALUE_STEP
    return lo, hi


def _fmt(n):
    return f'{n:.1f}'.rstrip('0').rstrip('.')


def _path(coords):
    return 'M' + ' L'.join(f'{_fmt(x)} {_fmt(y)}' for x, y in coords)


def measure(now=None):
    """The chart, ready to render; ``{'chart': None}`` with under two points."""
    rows = list(MainLineSnapshot.objects
                .filter(value__isnull=False)
                .order_by('bucket_start')
                .values_list('bucket_start', 'value'))
    if len(rows) < 2:
        return {'chart': None}
    first, last = rows[0][0], rows[-1][0]
    span = (last - first).total_seconds()
    span_hours = span / 3600
    step_hours = max(1, math.ceil((span_hours + 1) / MAX_POINTS))
    points = _slots(rows, first, step_hours)
    nodes = dict(ProgressSnapshot.objects
                 .filter(bucket_start__gte=first, bucket_start__lte=last)
                 .values_list('bucket_start', 'engine_nodes_total'))

    lo, hi = _axis([value for _at, value in points])
    usable = HEIGHT - 2 * INSET

    def x_of(at):
        return (at - first).total_seconds() / span * WIDTH if span else 0.0

    def y_of(value):
        return INSET + (hi - value) / (hi - lo) * usable

    line = [(x_of(at), y_of(value)) for at, value in points]

    # Engine nodes, cumulative, on their own scale from the lowest to the
    # highest total in the window: the shape is what reads, not the zero.
    node_points = [(x_of(at), nodes[at]) for at, _v in points if at in nodes]
    node_area = ''
    nodes_last = None
    if len(node_points) >= 2:
        n_lo = node_points[0][1]
        n_hi = max(n for _x, n in node_points)
        n_span = (n_hi - n_lo) or 1
        coords = [(x, HEIGHT - (n - n_lo) / n_span * usable - INSET)
                  for x, n in node_points]
        node_area = (_path(coords)
                     + f' L{_fmt(coords[-1][0])} {HEIGHT}'
                     + f' L{_fmt(coords[0][0])} {HEIGHT} Z')
        nodes_last = node_points[-1][1]

    # One hover column per point, edge to edge between its neighbours.
    columns = []
    xs = [x for x, _y in line]
    for i, (at, value) in enumerate(points):
        left = 0.0 if i == 0 else (xs[i - 1] + xs[i]) / 2
        right = WIDTH if i == len(points) - 1 else (xs[i] + xs[i + 1]) / 2
        title = (f'{at:%Y-%m-%d %H:%M} UTC · '
                 f'{roots.value_text(value)}')
        if at in nodes:
            title += f' · {_human(nodes[at])} engine nodes'
        columns.append({'x': _fmt(left), 'w': _fmt(max(right - left, 0.1)),
                        'title': title})

    mid = (lo + hi) / 2
    ticks = [{'label': roots.value_text(round(v)),
              'top': _fmt(y_of(v) / HEIGHT * 100)} for v in (hi, mid, lo)]
    latest_at, latest = points[-1]
    return {'chart': {
        'width': WIDTH, 'height': HEIGHT,
        'line': _path(line),
        'node_area': node_area,
        'nodes_last_h': _human(nodes_last) if nodes_last is not None else '',
        'columns': columns,
        'ticks': ticks,
        'gridlines': [_fmt(y_of(v)) for v in (hi, mid, lo)],
        'first_at': first, 'last_at': latest_at,
        'latest_h': roots.value_text(latest),
        'first_h': roots.value_text(points[0][1]),
        'step_hours': step_hours,
    }}


def _human(n):
    # The home page's own formatting of node counts (``views._human``),
    # repeated here so this module does not import the views.
    if n >= 1_000_000_000_000:
        return f'{n / 1e12:.2f}T'
    if n >= 1_000_000_000:
        return f'{n / 1e9:.2f}B'
    if n >= 1_000_000:
        return f'{n / 1e6:.1f}M'
    return f'{n:,}'


def chart(*, now=None, force=False):
    """The cached chart for the home page, or ``None`` when there is none."""
    entry = metrics.shared_snapshot(
        CACHE_KEY, build=measure, now=now or timezone.now(), force=force,
        fresh_seconds=FRESH_SECONDS, ttl=TTL_SECONDS)
    return entry.get('chart') if entry else None
