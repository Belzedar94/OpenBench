"""Build site.json (queue and leaderboard sample) from saved copies of the
production queue and contributors pages.

    python build_site_fixture.py <saved queue.html> <saved contributors.html>

The numbers are parsed back from the formatted text, so they are rounded the
way the pages print them; good enough for a fixture.
"""

import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UNITS = {'T': 1e12, 'B': 1e9, 'M': 1e6}
AGE = {'minute': 60, 'hour': 3600, 'day': 86400, 'week': 604800, 'month': 2592000, 'year': 31536000}


def number(text):
    text = text.strip().replace(',', '')
    if text and text[-1] in UNITS:
        return int(round(float(text[:-1]) * UNITS[text[-1]]))
    return int(text or 0)


def age(text):
    m = re.match(r'(\d+)\s+(\w+?)s?$', html.unescape(text).replace('\xa0', ' ').strip())
    return int(m.group(1)) * AGE[m.group(2)] if m else None


def queue(page):
    rows = []
    for m in re.finditer(r'<tr><td><a class="qline" href="/atomicdb/queue/([^/]+)/">(?:<strong>)?([^<]+)(?:</strong>)?</a></td>'
                         r'<td>(\d+)</td><td class="dim">(\d+)</td><td class="dim">([^<]+)</td>'
                         r'<td>([^<]+) <span class="dim">\(([\d.]+)%\)</span></td><td class="dim">([^<]*)</td></tr>', page):
        slug, name, waiting, running, nodes, served, share, waited = m.groups()
        rows.append({'user': '' if slug == 'anonymous' else html.unescape(name), 'waiting': int(waiting),
                     'running': int(running), 'nodes': number(nodes), 'served': number(served),
                     'share': float(share), 'oldest_seconds': age(waited)})
    hour = re.search(r'<strong>([^<]+)</strong> nodes searched in the last hour', page)
    return {'rows': rows, 'fleet_nodes_last_hour': number(hour.group(1)) if hour else 0}


def backlog(page):
    """Backlog rows of the queue page's community backlog box (anonymous
    view: no adopt buttons, so the batch sizes are v1's ``sizes_of``)."""
    rows = []
    box = re.search(r'<section class="backlog-box" id="backlog">(.*?)</section>', page, re.S)
    if not box:
        return rows
    for part_i, part in enumerate(box.group(1).split('<details class="backlog-adopted">')):
        for m in re.finditer(r'<tr><td><strong>([^<]+)</strong><div class="dim backlog-about">([^<]*)</div></td>'
                             r'<td>(\d+)(?:<div[^>]*>(\d+) held</div>)?</td><td class="dim">(\d+)'
                             r'(?: <span[^>]*>\((\d+) accounts?\)</span>)?</td>', part):
            title = html.unescape(m.group(1))
            deep = title.lower().startswith('deep')
            rows.append({'name': re.sub(r'\W+', '-', title.lower()).strip('-'), 'title': title,
                         'about': html.unescape(m.group(2)), 'open': int(m.group(3)), 'held': int(m.group(4) or 0),
                         'adopted': int(m.group(5)), 'adopters': int(m.group(6) or 0),
                         'sizes': [5, 10, 20] if deep else [25, 50, 100], 'fully_adopted': part_i > 0})
    return rows


def boards(page):
    out = []
    for sec in re.finditer(r'<h3 class="contrib-sub">([^<]+)</h3>(.*?)</section>', page, re.S):
        rows = []
        for m in re.finditer(r'href="/atomicdb/user/[^"]+/">([^<]+)</a>.*?<td class="n">([^<]+)</td><td class="n">([^<]+)</td>',
                             sec.group(2), re.S):
            rows.append({'user': html.unescape(m.group(1)), 'nodes': number(m.group(2)), 'tasks': number(m.group(3)),
                         'machines': 1})
        out.append({'window': html.unescape(sec.group(1)), 'rows': rows})
    return out


def main():
    qpage = open(sys.argv[1], encoding='utf-8').read()
    q = queue(qpage)
    q['backlog'] = backlog(qpage)
    b = boards(open(sys.argv[2], encoding='utf-8').read())
    json.dump({'queue': q, 'boards': b}, open(os.path.join(HERE, 'site.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(len(q['rows']), 'queue rows;', [(x['window'], len(x['rows'])) for x in b])


if __name__ == '__main__':
    main()
