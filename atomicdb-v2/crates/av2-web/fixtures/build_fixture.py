"""Build the explorer fixture of av2-web from the public AtomicDB API.

The fixture lets the v2 pages render before the store exists.  It is a small
tree around the start position: the start position, its children, and the
children of the most-played reply (1. Nf3), read from the public
``/atomicdb/api/query`` endpoint, one request every 1.2 s, no login.

pyffish (Fairy-Stockfish's Python binding, variant ``atomic``) supplies what
the API does not return: SAN, the FEN after each move and the legal moves.
Keys are ``sha256(canonical_fen)`` exactly as v1 computes them
(``atomicdb/logic.py``): counters reset to ``0 1``; Fairy-Stockfish already
writes the en-passant square only when a capture is legal.  Every key is
checked against the one the API returns.

Usage (from this directory):  python build_fixture.py [--base URL]
Writes explorer.json next to this file.  roots.json is a copy of
``/atomicdb/api/roots`` taken the same day.
"""

import argparse
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

import pyffish as sf

START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
HERE = os.path.dirname(os.path.abspath(__file__))
UA = 'av2-web-fixture/1.0'


def canonical(fen):
    p = fen.split()
    return f'{p[0]} {p[1]} {p[2]} {p[3]} 0 1'


def key_of(fen):
    return hashlib.sha256(canonical(fen).encode()).hexdigest()


class Api:
    def __init__(self, base):
        self.base = base.rstrip('/')
        self.last = 0.0

    def query(self, fen):
        wait = 1.2 - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        url = self.base + '/atomicdb/api/query?' + urllib.parse.urlencode({'fen': fen})
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        finally:
            self.last = time.time()


def proof_numbers_from_saved_page(path):
    """pn/dn of the start position's children, from a saved copy of the
    production explorer page (the API does not expose them)."""
    out = {}
    if not path or not os.path.exists(path):
        return out
    html = open(path, encoding='utf-8').read()
    for m in re.finditer(r'data-uci="([a-h][1-8][a-h][1-8][qrbn]?)" title="[^"]*">.*?'
                         r'class="dim proofnums">([^<]*)</td>', html):
        nums = m.group(2).replace(',', '').split(' / ')
        if len(nums) == 2:
            out[m.group(1)] = [nums[0], nums[1]]
    return out


def position_record(api, fen, openings, proof):
    q = api.query(fen)
    if q is None:
        return None
    assert q['key'] == key_of(fen), (fen, q['key'])
    legal = sf.legal_moves('atomic', fen, [])
    sans = {u: sf.get_san('atomic', fen, u) for u in legal}
    rows = []
    for m in q['moves']:
        child_fen = sf.get_fen('atomic', fen, [m['uci']])
        k = key_of(child_fen)
        row = dict(m)
        row['san'] = sans.get(m['uci'], m['uci'])
        row['key'] = k
        row['fen'] = canonical(child_fen)
        if k in openings:
            row['enters_opening'] = openings[k]
        if fen == START and m['uci'] in proof:
            row['pn_dn'] = proof[m['uci']]
        rows.append(row)
    a = q.get('analysis') or {}
    return {
        'fen': canonical(fen),
        'key': q['key'],
        'status': q['status'],
        'closure': q['closure'],
        'score': q['score'],
        'point': q['point'],
        'backed_plies': q['backed_plies'],
        'best_move': q['best_move'],
        'visits': q['visits'],
        'nodes': q['nodes'],
        'seconds': a.get('seconds'),
        'analysis': [{'raw': ln['raw'], 'prior': False} for ln in a.get('lines', [])]
                    + [{'raw': ln['raw'], 'prior': True} for ln in a.get('previous', [])],
        'legal': [[u, sans[u]] for u in legal],
        'moves': rows,
        'opening': openings.get(q['key']),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='https://belzedar.duckdns.org')
    ap.add_argument('--saved-explore', help='saved HTML of the production explorer at the start position (for pn/dn)')
    args = ap.parse_args()
    api = Api(args.base)
    roots = json.load(open(os.path.join(HERE, 'roots.json'), encoding='utf-8'))
    openings = {o['key']: o['label'] for o in roots['openings']}
    proof = proof_numbers_from_saved_page(args.saved_explore)
    positions = []
    root = position_record(api, START, openings, proof)
    positions.append(root)
    second = None
    for row in root['moves']:
        rec = position_record(api, row['fen'], openings, proof)
        if rec:
            positions.append(rec)
            if row['uci'] == 'g1f3':
                second = rec
    if second:
        for row in second['moves']:
            rec = position_record(api, row['fen'], openings, proof)
            if rec:
                positions.append(rec)
    out = {'taken': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'source': args.base,
           'positions': positions}
    with open(os.path.join(HERE, 'explorer.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    print(len(positions), 'positions')


if __name__ == '__main__':
    main()
