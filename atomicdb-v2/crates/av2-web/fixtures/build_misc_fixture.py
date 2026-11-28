"""Build misc.json (milestones feed, the full campaigns list) from saved
copies of the production milestones and campaigns pages.

    python build_misc_fixture.py <saved milestones.html> <saved campaigns.html> <year>
"""

import calendar
import html
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def text(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s)).strip()


def num(s):
    return int(s.replace(',', '').replace('+', ''))


def milestones(page, year):
    out = []
    for li in re.finditer(r'<li><span class="t">([^<]+)</span>(.*?)</li>', page, re.S):
        body = li.group(2)
        ts = calendar.timegm(time.strptime(f'{li.group(1)} {year}', '%b %d %H:%M %Y'))
        san = re.search(r'<summary class="qline" title="([^"]*)">', body)
        key = re.search(r'/atomicdb/explore/([0-9a-f]{64})/', body)
        out.append({'ts': ts, 'text': text(re.split(r'<details|<a ', body)[0]),
                    'san': html.unescape(san.group(1)) if san else None,
                    'full': html.unescape(san.group(1)) if san else None,
                    'key': key.group(1) if key else None})
    return out


def campaigns(page):
    out = []
    for m in re.finditer(r'<li class="campaign-row">(.*?)</li>', page, re.S):
        c = m.group(1)
        votes = re.search(r'proposed by (.*?) · (\d+) votes?', c)
        reach = None
        stats = re.search(r'<div class="campaign-stats dim">(.*?)</div>', c, re.S)
        if stats:
            spans = [text(s) for s in re.findall(r'<span>(.*?)</span>', stats.group(1))]
            capped = spans[0].endswith('+ positions')
            if capped:
                total, explored, resolved = num(spans[0].split()[0]), num(spans[1].split()[0]), num(spans[2].split()[0])
                pct = float(spans[3].split('%')[0])
            else:
                mm = re.match(r'([\d,]+) of ([\d,]+) positions explored', spans[0])
                explored, total = num(mm.group(1)), num(mm.group(2))
                resolved, pct = num(spans[1].split()[0]), float(spans[2].split('%')[0])
            ago = next((s[len('measured '):-len(' ago')] for s in spans if s.startswith('measured ')), None)
            reach = {'capped': capped, 'total': total, 'explored': explored, 'resolved': resolved,
                     'solved_pct': pct, 'measured_ago': ago}
        proof = None
        pr = re.search(r'campaign-proof">(.*?)</div>', c, re.S)
        if pr:
            p = text(pr.group(1))
            nums = re.findall(r'([\d,]+) (?:positions to prove|to show)', p)
            proof = {'proven': 'Proven win' in p, 'refuted': 'cannot win' in p and 'Shown' in p,
                     'pn': num(nums[0]) if len(nums) > 0 else None, 'dn': num(nums[1]) if len(nums) > 1 else None}
        out.append({'id': int(re.search(r'/atomicdb/campaign/(\d+)/vote/', c).group(1)),
                    'root_key': re.search(r'/atomicdb/explore/([0-9a-f]{64})/', c).group(1),
                    'title': text(re.search(r'class="qline"[^>]*>(.*?)</a>', c).group(1)),
                    'line_shown': text((re.search(r'campaign-row-san dim">(.*?)</div>', c) or re.match('()', '')).group(1)),
                    'proposed_by': text(votes.group(1)), 'votes': int(votes.group(2)), 'reach': reach, 'proof': proof})
    return out


def main():
    ms = milestones(open(sys.argv[1], encoding='utf-8').read(), sys.argv[3])
    cs = campaigns(open(sys.argv[2], encoding='utf-8').read())
    json.dump({'milestones': ms, 'campaigns': cs}, open(os.path.join(HERE, 'misc.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(len(ms), 'milestones,', len(cs), 'campaigns')


if __name__ == '__main__':
    main()
