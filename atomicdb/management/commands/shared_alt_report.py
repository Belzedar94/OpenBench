"""Summary of the SHARED_ALT shadow events over a window (§ atomicdb.shared_alt).

Prints, for the last ``--hours``:

* how many events, distinct W and distinct (W, C) pairs;
* the distribution of ``diff_cp`` (S minus C, for the side to move);
* for each (W, S), the tasks bought AFTER its first event of the window:
  - ``at_s``: AnalysisTask rows at S itself;
  - ``on_s_spine``: rows on S's CURRENT spine (S, then its ``backed_move``
    chain down to ``--spine-plies``), read at report time.  That chain is
    where the value walker buys under S; detours off it are not counted, and
    the spine may have moved since the event, so this is an estimate;
  - the share of the window's ``descend`` arm tasks those rows represent.

Reads only: the event rows by the (kind, ts) index, positions and tasks by
primary key and by ``position_id``.  ``--json`` prints the same as one object.
"""

import json
from collections import Counter, defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from atomicdb import ingest
from atomicdb.models import AnalysisTask, DBEvent, Edge, Position
from atomicdb.shared_alt import SHARED_ALT_EVENT

DIFF_BUCKETS = ((None, 0), (0, 25), (25, 50), (50, 100), (100, 150),
                (150, None))


def _label(low, high):
    return f'{"" if low is None else low}..{"" if high is None else high}'


def _bucket(diff):
    for low, high in DIFF_BUCKETS:
        if (low is None or diff >= low) and (high is None or diff < high):
            return _label(low, high)
    return '?'


def _spine_keys(key, plies):
    keys, seen = [key], {key}
    for _ in range(plies):
        move = Position.objects.filter(key=key).values_list(
            'backed_move', flat=True).first()
        if not move:
            break
        child = Edge.objects.filter(parent_id=key, move_uci=move).values_list(
            'child_id', flat=True).first()
        if child is None or child in seen:
            break
        keys.append(child)
        seen.add(child)
        key = child
    return keys


class Command(BaseCommand):
    help = 'Summarise SHARED_ALT shadow events over a window.'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=float, default=168.0)
        parser.add_argument('--spine-plies', type=int, default=40)
        parser.add_argument('--top', type=int, default=30)
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **opts):
        since = timezone.now() - timedelta(hours=opts['hours'])
        events = list(DBEvent.objects.filter(
            kind=SHARED_ALT_EVENT, ts__gte=since).order_by('ts')
            .values_list('ts', 'payload'))
        first = {}
        pairs, ws = set(), set()
        diffs = Counter()
        diff_values = []
        for ts, payload in events:
            w, s, c = payload.get('w'), payload.get('s'), payload.get('c')
            ws.add(w)
            pairs.add((w, c))
            diff = payload.get('diff_cp')
            if diff is not None:
                diffs[_bucket(diff)] += 1
                diff_values.append(diff)
            entry = first.setdefault((w, s), {
                'first_ts': ts, 'events': 0, 'alternatives': set(),
                'pn_s': payload.get('pn_s')})
            entry['events'] += 1
            entry['alternatives'].add(c)

        descend_total = AnalysisTask.objects.filter(
            arm=ingest.VALUE_DESCEND_ARM, created__gte=since).count()
        rows = []
        bought_at_s, bought_on_spine = 0, 0
        for (w, s), entry in first.items():
            tasks = AnalysisTask.objects.filter(created__gte=entry['first_ts'])
            at_s = tasks.filter(position_id=s)
            spine = _spine_keys(s, opts['spine_plies'])
            on_spine = tasks.filter(position_id__in=spine)
            row = {
                'w': w, 's': s, 'events': entry['events'],
                'alternatives': len(entry['alternatives']),
                'pn_s': entry['pn_s'],
                'first_ts': entry['first_ts'].isoformat(),
                'at_s': at_s.count(),
                'at_s_descend': at_s.filter(
                    arm=ingest.VALUE_DESCEND_ARM).count(),
                'on_s_spine': on_spine.count(),
                'on_s_spine_descend': on_spine.filter(
                    arm=ingest.VALUE_DESCEND_ARM).count(),
                's_spine_len': len(spine),
            }
            bought_at_s += row['at_s_descend']
            bought_on_spine += row['on_s_spine_descend']
            rows.append(row)
        rows.sort(key=lambda row: -row['on_s_spine'])

        diff_values.sort()
        summary = {
            'window_hours': opts['hours'],
            'events': len(events),
            'distinct_w': len(ws),
            'distinct_w_c': len(pairs),
            'distinct_w_s': len(first),
            'diff_cp_buckets': {_label(low, high): diffs.get(_label(low, high),
                                                             0)
                                for low, high in DIFF_BUCKETS},
            'diff_cp_median': (diff_values[len(diff_values) // 2]
                               if diff_values else None),
            'descend_tasks_in_window': descend_total,
            'descend_tasks_at_s': bought_at_s,
            'descend_tasks_on_s_spine': bought_on_spine,
            'share_at_s': (bought_at_s / descend_total
                           if descend_total else None),
            'share_on_s_spine': (bought_on_spine / descend_total
                                 if descend_total else None),
            'pairs': rows[:opts['top']],
        }
        if opts['json']:
            self.stdout.write(json.dumps(summary, indent=2, default=str))
            return
        out = self.stdout.write
        out(f'SHARED_ALT over the last {opts["hours"]:g} h: '
            f'{summary["events"]} events, {summary["distinct_w"]} distinct W, '
            f'{summary["distinct_w_c"]} distinct (W, C), '
            f'{summary["distinct_w_s"]} distinct (W, S)')
        out('diff_cp (S - C, side to move), events per bucket:')
        for label, count in summary['diff_cp_buckets'].items():
            out(f'  {label:>10}  {count}')
        out(f'  median {summary["diff_cp_median"]}')
        out(f'descend-arm tasks in the window: {descend_total}')
        out(f'  bought at S itself after the first event: {bought_at_s}'
            f' ({_pct(summary["share_at_s"])})')
        out(f'  bought on S\'s current spine (<= {opts["spine_plies"]} plies,'
            f' read now; an estimate) after the first event:'
            f' {bought_on_spine} ({_pct(summary["share_on_s_spine"])})')
        out(f'top {opts["top"]} (W, S) by tasks on S\'s spine (all arms; '
            f'descend arm in brackets):')
        for row in summary['pairs']:
            out(f'  W {row["w"][:12]} S {row["s"][:12]}  events {row["events"]}'
                f'  alts {row["alternatives"]}  pn(S) {row["pn_s"]}'
                f'  at S {row["at_s"]} [{row["at_s_descend"]}]'
                f'  on spine {row["on_s_spine"]} [{row["on_s_spine_descend"]}]'
                f'  spine {row["s_spine_len"]}')


def _pct(share):
    return 'n/a' if share is None else f'{100 * share:.2f} %'
