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

Community request (rhaxier, 9-Nov-2026): the best value too, which is what
matters for a solution.  It is drawn as a step line from ``RootRecord``, the
history of the start position's best, over the same time axis: each step is
a new record, and the line holds to the right edge.
"""

import math

from django.utils import timezone

from . import logic, metrics, roots
from .models import MainLineSnapshot, ProgressSnapshot, RootRecord

CACHE_KEY = 'atomicdb.root-chart.v3'
# The source changes once an hour; ten minutes keeps a new hour visible soon
# without measuring per visitor.
FRESH_SECONDS = 600
TTL_SECONDS = 6 * 3600
MAX_POINTS = 180
# One point per UTC-aligned day of the record (see ``_slots``).
DAY_HOURS = 24
# SVG user units.  The drawing is stretched (``preserveAspectRatio="none"``)
# and its strokes do not scale, so only the proportions matter.
WIDTH = 600
HEIGHT = 160
# Room above and below the line inside the drawing, in user units.
INSET = 8
# The value axis snaps to this many centipawns.
VALUE_STEP = 25


def _slots(rows, first, step_hours):
    """One point per ``step_hours`` slot: the MEDIAN value of the slot, placed
    at its last row.  Hour to hour the headline value swings with whichever
    line was just searched (Torom: "very noisy with every change"); the
    median of a day is the level it sits at."""
    slots = []
    for at, value in rows:
        slot = int((at - first).total_seconds() // 3600) // step_hours
        if slots and slots[-1][0] == slot:
            slots[-1][1] = at
            slots[-1][2].append(value)
        else:
            slots.append([slot, at, [value]])
    points = []
    for _slot, at, values in slots:
        values = sorted(values)
        mid = len(values) // 2
        median = (values[mid] if len(values) % 2
                  else (values[mid - 1] + values[mid]) / 2)
        points.append((at, median))
    return points


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


def _record_at(records, at):
    """The record in force at ``at``: the newest one not after it."""
    held = None
    for record_at, value, status in records:
        if record_at > at:
            break
        held = (value, status)
    return held


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
    step_hours = max(DAY_HOURS, math.ceil((span_hours + 1) / MAX_POINTS))
    points = _slots(rows, first, step_hours)
    nodes = dict(ProgressSnapshot.objects
                 .filter(bucket_start__gte=first, bucket_start__lte=last)
                 .values_list('bucket_start', 'engine_nodes_total'))
    records = list(RootRecord.objects
                   .filter(key=logic.key_of(logic.start_fen()))
                   .order_by('at')
                   .values_list('at', 'value', 'status'))
    # A record from before the chart starts only sets where the step line
    # begins; one after its last point lands on the right edge.
    shown = [r for r in records if r[0] > first]
    carried = _record_at(records, first)
    # A proven status (+10000) would flatten everything else: it is named in
    # the legend and drawn at the edge, but does not stretch the axis.
    scaled = [v for _at, v, st in shown if st == 'UNKNOWN']
    if carried is not None and carried[1] == 'UNKNOWN':
        scaled.append(carried[0])

    lo, hi = _axis([value for _at, value in points] + scaled)
    usable = HEIGHT - 2 * INSET

    def x_of(at):
        return (at - first).total_seconds() / span * WIDTH if span else 0.0

    def y_of(value):
        return INSET + (hi - value) / (hi - lo) * usable

    line = [(x_of(at), y_of(value)) for at, value in points]

    best_line = ''
    if carried is not None or shown:
        def y_clamped(value):
            return min(max(y_of(value), INSET), HEIGHT - INSET)

        if carried is not None:
            best_line = f'M0 {_fmt(y_clamped(carried[0]))}'
        for at, value, _status in shown:
            x = _fmt(min(x_of(at), WIDTH))
            best_line += (f' H{x} V{_fmt(y_clamped(value))}' if best_line
                          else f'M{x} {_fmt(y_clamped(value))}')
        best_line += f' H{WIDTH}'

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
                 f'{roots.value_text(round(value))}'
                 + (' (median of the day)' if step_hours >= DAY_HOURS else ''))
        if at in nodes:
            title += f' · {_human(nodes[at])} engine nodes'
        held = _record_at(records, at)
        if held is not None:
            title += f' · best record {roots.value_text(*held)}'
        columns.append({'x': _fmt(left), 'w': _fmt(max(right - left, 0.1)),
                        'title': title})

    mid = (lo + hi) / 2
    ticks = [{'label': roots.value_text(round(v)),
              'top': _fmt(y_of(v) / HEIGHT * 100)} for v in (hi, mid, lo)]
    latest_at = points[-1][0]
    # The headline numbers are the real first and latest readings, not medians.
    latest = rows[-1][1]
    return {'chart': {
        'width': WIDTH, 'height': HEIGHT,
        'line': _path(line),
        'node_area': node_area,
        'best_line': best_line,
        'best_h': (roots.value_text(records[-1][1], records[-1][2])
                   if best_line else ''),
        'nodes_last_h': _human(nodes_last) if nodes_last is not None else '',
        'columns': columns,
        'ticks': ticks,
        'gridlines': [_fmt(y_of(v)) for v in (hi, mid, lo)],
        'first_at': first, 'last_at': latest_at,
        'latest_h': roots.value_text(latest),
        'first_h': roots.value_text(rows[0][1]),
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
