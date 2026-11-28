"""Build home.json (the home page's own sections) from a saved copy of the
production home page.

    python build_home_fixture.py <saved home.html>

Values are parsed back from the printed text (rounded as printed); good
enough for a fixture.  The first-moves table and the leaderboard come from
the other fixtures.
"""

import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UNITS = {'T': 1e12, 'B': 1e9, 'M': 1e6}


def number(text):
    text = html.unescape(text).strip().replace(',', '')
    if text and text[-1] in UNITS:
        return int(round(float(text[:-1]) * UNITS[text[-1]]))
    return int(float(text or 0))


def text(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s)).strip()


def main():
    page = open(sys.argv[1], encoding='utf-8').read()
    main_html = re.search(r'<main[^>]*>(.*)</main>', page, re.S).group(1)
    metrics = {}
    for m in re.finditer(r'<div class="v">(?:<a[^>]*>)?([^<]+)(?:</a>)?</div><div class="l">([^<]+)</div>', main_html):
        metrics[m.group(2)] = number(m.group(1))
    at = re.search(r'taken at (\d{4}-\d\d-\d\d \d\d:\d\d) UTC', main_html)
    cards = []
    for m in re.finditer(r'<section class="campaign-card">(.*?)</section>', main_html, re.S):
        c = m.group(1)
        stats = re.findall(r'<span>([^<]+)</span>', re.search(r'campaign-stats dim">(.*?)</div>', c, re.S).group(1))
        explored = re.match(r'([\d,]+) of ([\d,]+) positions explored', stats[1])
        cards.append({
            'root_key': re.search(r'/atomicdb/explore/([0-9a-f]{64})/', c).group(1),
            'title': text(re.search(r'Campaign: (.*?)</a>', c).group(1)),
            'line_shown': text(re.search(r'campaign-line dim">(.*?)</div>', c).group(1)),
            'votes': number(stats[0].split()[0]),
            'explored': number(explored.group(1)), 'total': number(explored.group(2)),
            'resolved': number(stats[2].split()[0]),
            'solved_pct': float(stats[3].split('%')[0]),
            'proposed_by': text(re.search(r'Proposed by (.*?)</div>', c).group(1)),
        })
    prog = re.search(r'Root conquest: (\d+)/(\d+) first moves solved · ([\d.]+)% of tree positions solved · '
                     r'last 24h: ([^ ]+) closures, ([^ ]+) nodes searched', main_html)

    def feed(block):
        rows = []
        for li in re.finditer(r'<li>(.*?)</li>', block, re.S):
            s = li.group(1)
            a = re.search(r'<a class="qline" href="/atomicdb/explore/([0-9a-f]{64})/(?:\?play=([^"]*))?" title="([^"]*)">(.*?)</a>', s)
            rest = re.search(r'</a>\s*<span class="dim"[^>]*>(.*)</span>\s*$', s, re.S)
            source = opening = budget = machine = None
            if rest:
                r = text(rest.group(1))
                sm = re.match(r'\((\w+)\)', r)
                source = sm.group(1).upper() if sm else None
                parts = [p.strip() for p in r.split('·')]
                for p in parts[1:]:
                    if p.endswith(' nodes'):
                        budget = number(p[:-6])
                    elif machine is None and budget is not None:
                        machine = p
                    else:
                        opening = p
            rows.append({'key': a.group(1), 'play': html.unescape(a.group(2) or ''), 'full': html.unescape(a.group(3)),
                         'san': text(a.group(4)), 'source': source, 'opening': opening, 'budget': budget, 'machine': machine})
        return rows

    queue = re.search(r'<div class="queue">(.*?)</div></div>', main_html, re.S).group(1)
    analyzing, upnext = queue.split('Up next', 1)
    proposed = []
    for m in re.finditer(r'<li class="campaign-row">(.*?)</li>', main_html, re.S):
        c = m.group(1)
        votes = re.search(r'proposed by (.*?) · (\d+) votes?', c)
        proposed.append({
            'id': int(re.search(r'/atomicdb/campaign/(\d+)/vote/', c).group(1)),
            'root_key': re.search(r'/atomicdb/explore/([0-9a-f]{64})/', c).group(1),
            'title': text(re.search(r'class="qline"[^>]*>(.*?)</a>', c).group(1)),
            'line_shown': text((re.search(r'campaign-row-san dim">(.*?)</div>', c) or re.match('()', '')).group(1)),
            'proposed_by': text(votes.group(1)), 'votes': int(votes.group(2)),
        })
    health = re.search(r'median disproof number on the AND frontier: <b>(\d+)</b> over (\d+) open obligations, of which '
                       r'(\d+) \(([\d.]+)%\) sit at or below dn (\d+).*?<b>(\d+)</b> open node.*?close in <b>([^<]+)</b> '
                       r'\(median of (\d+)\) · measured at (\d{4}-\d\d-\d\d \d\d:\d\d) UTC', main_html, re.S)
    attribution = re.search(r'Closures by source, last 24h \((\d+) of (\d+) labelled\): (.*?) · last 7d \((\d+) of (\d+)\): (.*?)\. Labelling', main_html, re.S)

    def shares(s):
        return [{'source': m.group(1), 'pct': float(m.group(2)), 'count': int(m.group(3))}
                for m in re.finditer(r'(\w+) ([\d.]+)% \((\d+)\)', s)]

    out = {
        'counters_at': at.group(1) if at else None,
        'total': metrics.get('positions'), 'closed': metrics.get('practically solved'),
        'nodes': metrics.get('engine nodes'), 'analyses': metrics.get('analyses'),
        'requested': metrics.get('in queue'), 'workers': metrics.get('active workers'),
        'cores': metrics.get('active cores'), 'nps': metrics.get('nodes / sec'),
        'positions_pm': metrics.get('positions / min'),
        'root_pn': metrics.get('proof number'), 'root_dn': metrics.get('disproof number'),
        'campaign_cards': cards,
        'progress': {'solved_first': int(prog.group(1)), 'n_first': int(prog.group(2)), 'solved_pct': float(prog.group(3)),
                     'closed_24h': number(prog.group(4)), 'nodes_24h': number(prog.group(5))} if prog else None,
        'health': {'dn_median': int(health.group(1)), 'and_nodes': int(health.group(2)), 'thin': int(health.group(3)),
                   'thin_pct': float(health.group(4)), 'dn_floor': int(health.group(5)), 'saturated': int(health.group(6)),
                   'human_close': health.group(7), 'human_close_samples': int(health.group(8)), 'at': health.group(9)} if health else None,
        'attribution': {'stamped_24h': int(attribution.group(1)), 'total_24h': int(attribution.group(2)), 'rows_24h': shares(attribution.group(3)),
                        'stamped_7d': int(attribution.group(4)), 'total_7d': int(attribution.group(5)), 'rows_7d': shares(attribution.group(6))} if attribution else None,
        'analyzing': feed(analyzing), 'upnext': feed(upnext),
        'campaigns_proposed': proposed, 'campaigns_proposed_total': len(proposed) + 1,
    }
    json.dump(out, open(os.path.join(HERE, 'home.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(len(cards), 'cards,', len(out['analyzing']), 'analyzing,', len(out['upnext']), 'up next,', len(proposed), 'proposed')


if __name__ == '__main__':
    main()
