#!/usr/bin/env python3
"""Record v1's public read API into a golden file (AtomicDB v2, plan section 6).

What it does, politely:

* samples positions only through the public API: the start position, every
  line of ``api/roots``, and random descents through the tree (each move row
  of ``api/query`` is an edge of the tree, so every step lands on a known
  position).  No table is scanned, nothing is written.
* records, for each sampled position, ``api/query`` (and ``?opening=1`` for a
  share), ``api/principal-variations`` (default and a few bounds),
  ``api/backed-source``, ``api/frontier/<key>/`` and
  ``api/live-request/<key>/``; plus ``api/roots``, ``api/map/v1`` views, and
  the error bodies of every read endpoint.
* at most ``--rate`` requests a second (default 2), never between 04:30 and
  07:15 UTC (the production dump window): it refuses to start there and
  sleeps through it if a run reaches it.

Output (``--out``):

* ``responses.jsonl.gz``: one JSON object per response, see ``_record``.
  ``body`` is the raw response text, so key order and number formatting are
  kept for byte comparisons.
* ``manifest.json``: when, against what, how many of each, and the seed.
* ``keys.txt``: the positions sampled for the SQL fixture (``--fixture-keys``
  of them, spread over the run), one ``key<TAB>fen`` per line.

The child FEN of a move is computed locally with pyffish (the same library the
server uses); the server canonicalises whatever FEN it is sent, so the
counters do not matter.

Usage::

    python record_v1.py --out ../../tests/golden/v1-20261128 --target 3000
"""

import argparse
import datetime as dt
import gzip
import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import pyffish as pf
import requests

RECORDER_VERSION = 1
VARIANT = 'atomic'
USER_AGENT = 'atomicdb-v2-golden-recorder/1 (read-only, <=2 req/s)'
# Production's nightly dump: no heavy work in this window (common brief).
QUIET_START = dt.time(4, 30)
QUIET_END = dt.time(7, 15)
# Response headers worth keeping.  Everything else is either transport
# (Date, Server, Connection, Content-Length of a gzip body) or noise.
KEPT_HEADERS = ('content-type', 'cache-control', 'vary', 'etag',
                'retry-after', 'content-encoding', 'expires', 'allow',
                'x-frame-options', 'cross-origin-opener-policy',
                'referrer-policy', 'x-content-type-options')


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def in_quiet_window(now=None):
    t = (now or utc_now()).time()
    return QUIET_START <= t < QUIET_END


class Recorder:
    def __init__(self, base, out, rate, seed, target, fixture_keys):
        self.base = base.rstrip('/')
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.interval = 1.0 / rate
        self.rng = random.Random(seed)
        self.seed = seed
        self.target = target
        self.fixture_keys = fixture_keys
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT,
                                     'Accept-Encoding': 'gzip'})
        self.last = 0.0
        self.count = 0
        self.by_endpoint = {}
        self.statuses = {}
        self.sink = gzip.open(self.out / 'responses.jsonl.gz', 'wt',
                              encoding='utf-8', compresslevel=9)
        self.started = utc_now()
        self.positions = {}          # key -> fen, every position recorded
        self.queried = set()
        self.errors = 0

    # ------------------------------------------------------------- HTTP --
    def _pace(self):
        while in_quiet_window():
            print('quiet window (04:30-07:15 UTC): sleeping', flush=True)
            time.sleep(60)
        wait = self.last + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()

    def request(self, endpoint, method, path, query=None, form=None,
                headers=None, note=''):
        """One request, recorded.  Returns (status, parsed JSON or None)."""
        url = self.base + path
        if query:
            url += '?' + urlencode(query)
        for attempt in range(4):
            self._pace()
            try:
                response = self.session.request(
                    method, url, data=form, headers=headers or {},
                    timeout=(10, 60), allow_redirects=False)
            except requests.RequestException as exc:
                print(f'transport error {exc}; retrying', flush=True)
                time.sleep(5 * (attempt + 1))
                continue
            if response.status_code == 429 or response.status_code >= 502:
                # nginx's rate limit or a restart: back off and try again,
                # and do not record the transient answer.
                time.sleep(5 * (attempt + 1))
                continue
            break
        else:
            self.errors += 1
            return None, None
        recorded_at = utc_now().isoformat()
        text = response.content.decode('utf-8', errors='replace')
        record = {
            'id': self.count,
            'endpoint': endpoint,
            'method': method,
            'path': path,
            'query': list(query.items()) if isinstance(query, dict)
            else (query or []),
            'form': form,
            'request_headers': headers or {},
            'note': note,
            'recorded_at': recorded_at,
            'elapsed_ms': round(response.elapsed.total_seconds() * 1000, 1),
            'status': response.status_code,
            'headers': {name: response.headers[name] for name in KEPT_HEADERS
                        if name in response.headers},
            'body': text,
        }
        self.sink.write(json.dumps(record, ensure_ascii=False) + '\n')
        self.count += 1
        self.by_endpoint[endpoint] = self.by_endpoint.get(endpoint, 0) + 1
        self.statuses[str(response.status_code)] = (
            self.statuses.get(str(response.status_code), 0) + 1)
        if self.count % 100 == 0:
            print(f'{self.count} responses ({utc_now():%H:%M:%S}Z) '
                  f'{json.dumps(self.by_endpoint)}', flush=True)
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        return response.status_code, parsed

    # -------------------------------------------------------- endpoints --
    def query(self, fen, opening=False, note=''):
        params = {'fen': fen}
        if opening:
            params['opening'] = '1'
        status, body = self.request('query', 'GET', '/atomicdb/api/query',
                                    params, note=note)
        if status == 200 and isinstance(body, dict):
            self.positions[body['key']] = body['fen']
            self.queried.add(body['key'])
        return body if status == 200 else None

    def principal_variations(self, fen, line_count=None, ply_count=None,
                             note=''):
        params = {'fen': fen}
        if line_count is not None:
            params['line_count'] = line_count
        if ply_count is not None:
            params['ply_count'] = ply_count
        return self.request('principal-variations', 'GET',
                            '/atomicdb/api/principal-variations', params,
                            note=note)

    def backed_source(self, fen, note=''):
        return self.request('backed-source', 'GET',
                            '/atomicdb/api/backed-source', {'fen': fen},
                            note=note)

    def frontier(self, key, note=''):
        return self.request('frontier', 'GET',
                            f'/atomicdb/api/frontier/{key}/', note=note)

    def live_request(self, key, note=''):
        return self.request('live-request', 'GET',
                            f'/atomicdb/api/live-request/{key}/', note=note)

    # ---------------------------------------------------------- walking --
    @staticmethod
    def child_fen(fen, uci):
        return pf.get_fen(VARIANT, fen, [uci])

    def fen_of_line(self, ucis):
        fen = pf.start_fen(VARIANT)
        for uci in ucis:
            fen = self.child_fen(fen, uci)
        return fen

    def per_position(self, body):
        """The extra endpoints for one queried position, by chance."""
        fen, key = body['fen'], body['key']
        r = self.rng.random()
        if r < 0.30:
            self.principal_variations(fen, note='default bounds')
        elif r < 0.36:
            self.principal_variations(
                fen, line_count=self.rng.randint(1, 5),
                ply_count=self.rng.randint(1, 5), note='random bounds')
        if self.rng.random() < 0.30:
            self.backed_source(fen)
        if self.rng.random() < 0.15:
            self.query(fen, opening=True, note='opening=1')
        if self.rng.random() < 0.10:
            self.frontier(key)
        if self.rng.random() < 0.05:
            self.live_request(key)

    def descend(self, fen, max_plies):
        """Walk down from ``fen``: best move half the time, else a random
        row of the move table that has been visited (any row otherwise)."""
        for ply in range(max_plies):
            if self.count >= self.target:
                return
            body = self.query(fen, note=f'descent ply {ply}')
            if body is None:
                return
            self.per_position(body)
            moves = body.get('moves') or []
            if not moves or body.get('closure') == 'TERMINAL':
                return
            if body.get('best_move') and self.rng.random() < 0.5:
                uci = body['best_move']
            else:
                visited = [m for m in moves if m.get('visits')]
                uci = self.rng.choice(visited or moves)['uci']
            try:
                fen = self.child_fen(fen, uci)
            except Exception as exc:          # noqa: BLE001
                print(f'pyffish refused {uci} in {fen}: {exc}', flush=True)
                return

    # ------------------------------------------------------------- run ---
    def errors_and_edges(self, start_fen, deep_fen):
        """Error bodies and boundary cases of every read endpoint."""
        unknown = '8/8/8/3k4/8/8/2QQQQ2/K7 w - - 0 1'   # legal, never in the tree
        illegal = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1 x'
        no_king = 'rnbq1bnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1'
        for endpoint, path in (('query', '/atomicdb/api/query'),
                               ('principal-variations',
                                '/atomicdb/api/principal-variations'),
                               ('backed-source',
                                '/atomicdb/api/backed-source')):
            self.request(endpoint, 'GET', path, None, note='error: no fen')
            self.request(endpoint, 'GET', path, {'fen': ''},
                         note='error: empty fen')
            self.request(endpoint, 'GET', path, {'fen': illegal},
                         note='error: malformed fen')
            self.request(endpoint, 'GET', path, {'fen': no_king},
                         note='error: missing king')
            self.request(endpoint, 'GET', path, {'fen': unknown},
                         note='error: unknown position')
            # underscores instead of spaces are accepted (the explorer's URLs)
            self.request(endpoint, 'GET', path,
                         {'fen': start_fen.replace(' ', '_')},
                         note='fen with underscores')
            # counters dropped: the canonical identity ignores them
            self.request(endpoint, 'GET', path,
                         {'fen': ' '.join(start_fen.split()[:4])},
                         note='fen without counters')
            self.request(endpoint, 'POST', path, None,
                         form={'fen': start_fen}, note='POST to a GET view')
        for line_count, ply_count in (('0', '0'), ('99', '99'), ('abc', ''),
                                      ('-3', '2'), ('3', 'x')):
            self.principal_variations(deep_fen, line_count, ply_count,
                                      note='bounds clamp')
        self.request('query', 'GET', '/atomicdb/api/query',
                     {'fen': start_fen, 'opening': '0'}, note='opening=0')
        self.request('query', 'HEAD', '/atomicdb/api/query',
                     {'fen': start_fen}, note='HEAD')
        bad_key = 'f' * 64
        self.frontier(bad_key, note='error: unknown key')
        self.live_request(bad_key, note='error: unknown key')
        self.request('frontier', 'GET', '/atomicdb/api/frontier/not-a-key/',
                     note='error: malformed key')
        # map/v1: the views the page asks for, and every error.
        for query, note in (({}, 'default'),
                            ({'weight': 'explored'}, 'explored'),
                            ({'weight': 'compute'}, 'compute'),
                            ({'limit': '50', 'depth': '4'}, 'small'),
                            ({'weight': 'bogus'}, 'error: weight'),
                            ({'limit': '0'}, 'error: limit low'),
                            ({'limit': '601'}, 'error: limit high'),
                            ({'depth': '33'}, 'error: depth high'),
                            ({'depth': 'x'}, 'error: depth not int'),
                            ({'root': 'xyz'}, 'error: root not hex'),
                            ({'root': bad_key}, 'error: root unknown'),
                            ({'foo': '1'}, 'error: unknown parameter')):
            self.request('map', 'GET', '/atomicdb/api/map/v1', query,
                         note=note)
        self.request('map', 'POST', '/atomicdb/api/map/v1', None, form={},
                     note='error: POST')
        self.request('map', 'GET', '/atomicdb/api/map/v1', None,
                     headers={'Accept-Encoding': 'identity'},
                     note='identity encoding')
        # roots, both spellings
        self.request('roots', 'GET', '/atomicdb/api/roots', note='no slash')
        self.request('roots', 'GET', '/atomicdb/api/roots/', note='slash')
        # The account endpoints, without an account (success shapes come
        # from the v1 test suite: tests/golden/v1-shapes).
        for path in ('/atomicdb/api/my-queue/', '/atomicdb/api/my-queue',
                     '/atomicdb/api/my-workers/', '/atomicdb/api/my-workers'):
            endpoint = 'my-queue' if 'queue' in path else 'my-workers'
            self.request(endpoint, 'GET', path, note='error: anonymous GET')
            self.request(endpoint, 'POST', path, form={},
                         note='error: POST without credentials')
            self.request(endpoint, 'PUT', path, note='error: method')

    def map_roots(self, start_body):
        """``api/map/v1`` rooted at a few first moves."""
        for move in (start_body.get('moves') or [])[:6]:
            fen = self.child_fen(start_body['fen'], move['uci'])
            body = self.query(fen, note='first move')
            if body is not None:
                self.request('map', 'GET', '/atomicdb/api/map/v1',
                             {'root': body['key'], 'limit': '100',
                              'depth': '6'}, note='rooted')

    def run(self):
        if in_quiet_window():
            sys.exit('refusing to start inside 04:30-07:15 UTC')
        start = pf.start_fen(VARIANT)
        start_body = self.query(start, note='start position')
        self.query(start, opening=True, note='start opening=1')
        self.principal_variations(start, note='start default')
        self.backed_source(start, note='start')
        self.frontier(start_body['key'], note='start')
        self.live_request(start_body['key'], note='start')
        status, roots = self.request('roots', 'GET', '/atomicdb/api/roots',
                                     note='roots')
        lines = []
        for table in ('positions', 'openings'):
            for row in (roots or {}).get(table, []):
                ucis = row.get('route') or row.get('line') or []
                if ucis:
                    lines.append(list(ucis))
        self.rng.shuffle(lines)
        self.map_roots(start_body)
        deep_fen = start
        # A third of the budget on the named lines and below them, the rest
        # on descents from the start.
        for ucis in lines:
            if self.count >= self.target // 3:
                break
            try:
                fen = self.fen_of_line(ucis)
            except Exception:                  # noqa: BLE001
                continue
            self.descend(fen, self.rng.randint(1, 6))
            if len(ucis) > 6:
                deep_fen = fen
        while self.count < self.target - 120:
            self.descend(start, self.rng.randint(8, 70))
        self.errors_and_edges(start, deep_fen)
        self.finish()

    def finish(self):
        self.sink.close()
        keys = sorted(self.queried)
        self.rng.shuffle(keys)
        chosen = keys[:self.fixture_keys]
        with open(self.out / 'keys.txt', 'w', encoding='utf-8') as handle:
            for key in chosen:
                handle.write(f'{key}\t{self.positions[key]}\n')
        manifest = {
            'recorder_version': RECORDER_VERSION,
            'base': self.base,
            'started_at': self.started.isoformat(),
            'finished_at': utc_now().isoformat(),
            'seed': self.seed,
            'responses': self.count,
            'by_endpoint': self.by_endpoint,
            'by_status': self.statuses,
            'positions_queried': len(self.queried),
            'fixture_keys': len(chosen),
            'transport_failures': self.errors,
            'rate_limit_per_second': round(1 / self.interval, 3),
            'user_agent': USER_AGENT,
        }
        with open(self.out / 'manifest.json', 'w', encoding='utf-8') as handle:
            json.dump(manifest, handle, indent=1)
            handle.write('\n')
        print(json.dumps(manifest, indent=1), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--base', default='https://belzedar.duckdns.org')
    parser.add_argument('--out', required=True)
    parser.add_argument('--rate', type=float, default=2.0,
                        help='requests per second, at most (default 2)')
    parser.add_argument('--target', type=int, default=3000,
                        help='responses to record (default 3000)')
    parser.add_argument('--seed', type=int, default=20261128)
    parser.add_argument('--fixture-keys', type=int, default=200)
    args = parser.parse_args()
    if args.rate > 2.0:
        sys.exit('--rate above 2 requests a second is not polite here')
    Recorder(args.base, args.out, args.rate, args.seed, args.target,
             args.fixture_keys).run()


if __name__ == '__main__':
    main()
