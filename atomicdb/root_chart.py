"""The start position's best record over time, drawn on the roots page.

Community request (Torom, 9-Nov-2026): a chart of how the start position
moves as the tree grows.  rhaxier asked for the best value, which is what
matters for a solution, and both then agreed on that one series alone.  It is
a step line from ``RootRecord``, the history of the start position's best:
each step is a new record, and the line holds to the right edge, which is the
moment the chart was measured.  The value axis is fitted to the records and
snapped to a readable step, so a climb of a few centipawns is not flat.

Behind it, faint and on its own scale, the cumulative engine nodes from
``ProgressSnapshot``, downsampled to at most ``MAX_POINTS`` points (the last
of each slot).  Nothing is reconstructed and nothing new is recorded.

The chart is drawn once into SVG coordinates and kept in the shared cache
for ``FRESH_SECONDS``, so a visitor never pays a query for it while the entry
is warm.  The SVG stretches to the width of its box; the labels are HTML
around it, so they keep their size on a phone.
"""

import math

from django.utils import timezone

from . import logic, metrics, roots
from .models import ProgressSnapshot, RootRecord

CACHE_KEY = 'atomicdb.root-chart.v5'
# Records arrive at most once an hour; ten minutes keeps a new one visible
# soon without measuring per visitor.
FRESH_SECONDS = 600
TTL_SECONDS = 6 * 3600
MAX_POINTS = 180
# SVG user units.  The drawing is stretched (``preserveAspectRatio="none"``)
# and its strokes do not scale, so only the proportions matter.
WIDTH = 600
HEIGHT = 160
# Room above and below the line inside the drawing, in user units.
INSET = 8
# The value axis snaps to the smallest of these steps (centipawns) that
# covers the records in at most ``MAX_TICK_STEPS`` steps.
TICK_STEPS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)
MAX_TICK_STEPS = 4


def _axis(values):
    """``(lo, hi, step)``: the records' range snapped outward to a step."""
    v_lo, v_hi = min(values), max(values)
    for step in TICK_STEPS:
        lo = math.floor(v_lo / step) * step
        hi = math.ceil(v_hi / step) * step
        if (hi - lo) / step <= MAX_TICK_STEPS:
            break
    if hi == lo:
        lo, hi = lo - step, hi + step
    return lo, hi, step


def _fmt(n):
    return f'{n:.1f}'.rstrip('0').rstrip('.')


def _path(coords):
    return 'M' + ' L'.join(f'{_fmt(x)} {_fmt(y)}' for x, y in coords)


def _downsample(rows, first, step_seconds):
    """The last row of each ``step_seconds`` slot."""
    out = []
    last_slot = None
    for at, value in rows:
        slot = int((at - first).total_seconds() // step_seconds)
        if out and slot == last_slot:
            out[-1] = (at, value)
        else:
            out.append((at, value))
        last_slot = slot
    return out


def measure(now=None):
    """The chart, ready to render; ``{'chart': None}`` with no record."""
    now = now or timezone.now()
    records = list(RootRecord.objects
                   .filter(key=logic.key_of(logic.start_fen()))
                   .order_by('at')
                   .values_list('at', 'value', 'status'))
    if not records:
        return {'chart': None}
    first = records[0][0]
    end = max(now, records[-1][0])
    span = (end - first).total_seconds()

    # A proven status (+10000) would flatten everything else: it is named in
    # the header and drawn at the edge, but does not stretch the axis.
    scaled = [v for _at, v, st in records if st == 'UNKNOWN']
    lo, hi, step = _axis(scaled or [v for _at, v, _st in records])
    usable = HEIGHT - 2 * INSET

    def x_of(at):
        return (at - first).total_seconds() / span * WIDTH if span else 0.0

    def y_of(value):
        y = INSET + (hi - value) / (hi - lo) * usable
        return min(max(y, INSET), HEIGHT - INSET)

    xs = [x_of(at) for at, _v, _st in records]
    line = ''
    for x, (_at, value, _st) in zip(xs, records):
        line += (f' H{_fmt(x)} V{_fmt(y_of(value))}' if line
                 else f'M{_fmt(x)} {_fmt(y_of(value))}')
    line += f' H{WIDTH}'

    # One hover column per record, from its step to the next one.
    columns = []
    for i, (at, value, status) in enumerate(records):
        right = xs[i + 1] if i + 1 < len(records) else WIDTH
        columns.append({
            'x': _fmt(xs[i]), 'w': _fmt(max(right - xs[i], 0.1)),
            'title': (f'{at:%Y-%m-%d %H:%M} UTC · record '
                      f'{roots.value_text(value, status)}'),
        })

    # Engine nodes, cumulative, on their own scale from the lowest to the
    # highest total in the window: the shape is what reads, not the zero.
    node_rows = list(ProgressSnapshot.objects
                     .filter(bucket_start__gte=first, bucket_start__lte=end)
                     .order_by('bucket_start')
                     .values_list('bucket_start', 'engine_nodes_total'))
    node_rows = _downsample(node_rows, first,
                            max(3600.0, (span + 1) / MAX_POINTS))
    node_area = ''
    nodes_last = None
    node_ticks = []
    if len(node_rows) >= 2:
        n_lo = node_rows[0][1]
        n_hi = max(n for _at, n in node_rows)
        n_span = (n_hi - n_lo) or 1
        coords = [(x_of(at), HEIGHT - INSET - (n - n_lo) / n_span * usable)
                  for at, n in node_rows]
        node_area = (_path(coords)
                     + f' L{_fmt(coords[-1][0])} {HEIGHT}'
                     + f' L{_fmt(coords[0][0])} {HEIGHT} Z')
        nodes_last = node_rows[-1][1]
        # The nodes' own scale, on the right edge (rhaxier, 9-Nov-2026).
        for n in (n_hi, (n_hi + n_lo) / 2, n_lo):
            y = HEIGHT - INSET - (n - n_lo) / n_span * usable
            node_ticks.append({'label': _human(int(n)),
                               'top': _fmt(y / HEIGHT * 100)})

    tick_values = range(hi, lo - 1, -step)
    latest_at, latest, latest_status = records[-1]
    return {'chart': {
        'width': WIDTH, 'height': HEIGHT,
        'line': line,
        'node_area': node_area,
        'nodes_last_h': _human(nodes_last) if nodes_last is not None else '',
        'node_ticks': node_ticks,
        'columns': columns,
        'ticks': [{'label': roots.value_text(v),
                   'top': _fmt(y_of(v) / HEIGHT * 100)} for v in tick_values],
        'gridlines': [_fmt(y_of(v)) for v in tick_values],
        'first_at': first, 'last_at': end,
        'record_h': roots.value_text(latest, latest_status),
        'record_at': latest_at,
        'records': len(records),
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
    """The cached chart for the roots page, or ``None`` when there is none."""
    entry = metrics.shared_snapshot(
        CACHE_KEY, build=measure, now=now or timezone.now(), force=force,
        fresh_seconds=FRESH_SECONDS, ttl=TTL_SECONDS)
    return entry.get('chart') if entry else None
