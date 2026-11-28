"""Build roots_extra.json (main line, main line by day, best-record chart
data) from a saved copy of the production roots page.

    python build_roots_extra_fixture.py <saved roots.html>

The chart's records come back from the hover columns' titles; the node
series is read back from the drawn path with the axis labels (so it is as
precise as the drawing).  The v2 page recomputes the geometry from these.
"""

import calendar
import html
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UNITS = {'T': 1e12, 'B': 1e9, 'M': 1e6}
WIDTH, HEIGHT, INSET = 600, 160, 8


def number(text):
    text = text.strip().replace(',', '')
    return float(text[:-1]) * UNITS[text[-1]] if text and text[-1] in UNITS else float(text)


def cp(text):
    text = html.unescape(text).strip().replace('\u2212', '-')
    return int(round(float(text) * 100))


def epoch(stamp, fmt):
    return calendar.timegm(time.strptime(stamp, fmt))


def main():
    page = open(sys.argv[1], encoding='utf-8').read()
    ml = re.search(r'<h2>Main line</h2>.*?<p><strong>([^<]+)</strong> · (\d+) pl\w+(?: · ([^<]*?))? <span class="dim">· recorded '
                   r'(\d{4}-\d\d-\d\d \d\d:\d\d) UTC</span></p>(?:<p style="margin-top:.5rem">([^<]*)</p>)?'
                   r'(?:<p class="dim lb-foot"><a href="/atomicdb/explore/([0-9a-f]{64})/">)?', page, re.S)
    main_line = {'value': cp(ml.group(1)), 'plies': int(ml.group(2)), 'end_text': html.unescape(ml.group(3) or '') or None,
                 'at': ml.group(4), 'san': html.unescape(ml.group(5) or ''), 'end_key': ml.group(6)} if ml else None
    hist = []
    block = re.search(r'id="main-line-by-day">(.*?)</details>', page, re.S)
    if block:
        for m in re.finditer(r'<tr><td style="white-space:nowrap">([\d-]+)</td><td class="n">([^<]+)</td><td class="n">(\d+)</td>'
                             r'<td title="([^"]*)">', block.group(1)):
            hist.append({'day': m.group(1), 'value': cp(m.group(2)), 'plies': int(m.group(3)), 'san': html.unescape(m.group(4))})
    chart = None
    sec = re.search(r'<section aria-labelledby="root-chart-title" class="root-chart">(.*?)</section>', page, re.S)
    if sec:
        s = sec.group(1)
        records = []
        for m in re.finditer(r'<title>(\d{4}-\d\d-\d\d \d\d:\d\d) UTC · record ([^<]+)</title>', s):
            records.append([epoch(m.group(1), '%Y-%m-%d %H:%M'), cp(m.group(2)), 'UNKNOWN'])
        year = time.strftime('%Y', time.gmtime(records[-1][0]))
        end = epoch(re.findall(r'<span>(\d+ \w+ \d\d:\d\d) UTC</span>', s)[-1] + ' ' + year, '%d %b %H:%M %Y')
        first, span = records[0][0], end - records[0][0]
        ticks = [number(t) for t in re.findall(r'<span style="top:[\d.]+%">([\d.]+[TBM])</span>', s)]
        nodes = []
        area = re.search(r'class="rc-nodes" d="M([^"]+)"', s)
        if area and len(ticks) == 3:
            n_hi, n_lo = ticks[0], ticks[2]
            usable = HEIGHT - 2 * INSET
            pts = [p.split() for p in area.group(1).split(' L')][:-2]
            for x, y in pts:
                at = first + float(x) / WIDTH * span
                n = n_lo + (HEIGHT - INSET - float(y)) / usable * (n_hi - n_lo)
                nodes.append([int(at), int(n)])
        chart = {'records': records, 'nodes': nodes, 'end': end}
    json.dump({'main_line': main_line, 'history': hist, 'chart': chart},
              open(os.path.join(HERE, 'roots_extra.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('main line' if main_line else 'no main line', len(hist), 'days,',
          len(chart['records']) if chart else 0, 'records,', len(chart['nodes']) if chart else 0, 'node points')


if __name__ == '__main__':
    main()
