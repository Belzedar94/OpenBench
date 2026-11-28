#!/usr/bin/env python3
"""The SQL fixture behind a golden recording: v1's rows for a few positions.

The tree grows while it is recorded, so a golden response can only be
reproduced by a v2 loaded with the rows v1 answered from.  This reads those
rows on production, by primary key and index only (never a scan; the plans
were checked with EXPLAIN: ``atomicdb_position_pkey``, ``uniq_parent_move``,
``uniq_task_per_generation``), for the keys in ``keys.txt``:

* ``position``: the position row (every column, ``row_to_json``);
* ``edge``: its edges (``parent_id = key``);
* ``child``: the rows of its children;
* ``searched``: the deepest budget an engine ran on each child (COMPLETED
  tasks with nodes, the ``searched_nodes`` column of ``api/query``).

Then it fetches ``api/query`` again for every key (at most 2 requests a
second) and marks a key ``consistent`` when that answer equals the recorded
one: nothing moved between the recording and the read, so the rows are
exactly what produced the recorded body.

Output: ``fixture-rows.jsonl.gz`` next to ``keys.txt``.  The first line is
``{"kind": "meta", ...}`` with the read time; then one line per row.

Usage (PowerShell or any shell with ssh/scp)::

    python dump_fixture.py --dir ../../tests/golden/v1-20261128 \
        --ssh "root@94.130.239.23" --key C:/Users/me/.ssh/key
"""

import argparse
import datetime as dt
import gzip
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

QUIET_START = dt.time(4, 30)
QUIET_END = dt.time(7, 15)
USER_AGENT = 'atomicdb-v2-golden-recorder/1 (read-only, <=2 req/s)'

SQL = r"""
SET statement_timeout = '60s';
SET default_transaction_read_only = on;
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT json_build_object('kind', 'meta', 'read_at', now(),
                         'keys', cardinality(%(keys)s::text[]));
SELECT json_build_object('kind', 'position', 'key', p.key, 'row', row_to_json(p))
  FROM atomicdb_position p WHERE p.key = ANY(%(keys)s::text[]);
SELECT json_build_object('kind', 'edge', 'key', e.parent_id, 'row', row_to_json(e))
  FROM atomicdb_edge e WHERE e.parent_id = ANY(%(keys)s::text[]);
SELECT json_build_object('kind', 'child', 'key', p.key, 'row', row_to_json(p))
  FROM atomicdb_position p
 WHERE p.key IN (SELECT child_id FROM atomicdb_edge
                  WHERE parent_id = ANY(%(keys)s::text[]));
SELECT json_build_object('kind', 'searched', 'key', t.position_id,
                         'budget_nodes', max(t.budget_nodes))
  FROM atomicdb_analysistask t
 WHERE t.position_id IN (SELECT child_id FROM atomicdb_edge
                          WHERE parent_id = ANY(%(keys)s::text[]))
   AND t.state = 'COMPLETED' AND t.nodes_searched > 0
 GROUP BY t.position_id;
COMMIT;
"""


def in_quiet_window():
    t = dt.datetime.now(dt.timezone.utc).time()
    return QUIET_START <= t < QUIET_END


def load_keys(directory):
    keys = []
    for line in (directory / 'keys.txt').read_text(encoding='utf-8').splitlines():
        if line.strip():
            key, fen = line.split('\t', 1)
            keys.append((key, fen))
    return keys


def recorded_queries(directory):
    """``{key: body}`` of the plain ``api/query`` answers in the recording."""
    found = {}
    with gzip.open(directory / 'responses.jsonl.gz', 'rt',
                   encoding='utf-8') as handle:
        for line in handle:
            record = json.loads(line)
            if (record['endpoint'] != 'query' or record['status'] != 200
                    or record['method'] != 'GET'
                    or any(name == 'opening' for name, _ in record['query'])):
                continue
            try:
                body = json.loads(record['body'])
            except ValueError:
                continue
            found.setdefault(body['key'], record['body'])
    return found


def run_sql(keys, ssh, key_file):
    literal = "ARRAY[" + ",".join("'%s'" % k for k, _ in keys) + "]"
    for k, _ in keys:
        if len(k) != 64 or any(c not in '0123456789abcdef' for c in k):
            sys.exit(f'refusing a key that is not 64 hex: {k!r}')
    script = SQL % {'keys': literal}
    with tempfile.NamedTemporaryFile('w', suffix='.sql', delete=False,
                                     newline='\n', encoding='ascii') as tmp:
        tmp.write(script)
        local = tmp.name
    remote = '/tmp/av2_golden_fixture.sql'
    base = ['-i', key_file] if key_file else []
    subprocess.run(['scp', '-q', *base, local, f'{ssh}:{remote}'], check=True)
    command = (f'nice -n 10 sudo -u postgres psql -X -q -At -d atomicdb '
               f'-v ON_ERROR_STOP=1 -f {remote}; status=$?; rm -f {remote}; '
               f'exit $status')
    result = subprocess.run(['ssh', *base, ssh, command], check=True,
                            capture_output=True)
    Path(local).unlink()
    rows = []
    for line in result.stdout.decode('utf-8').splitlines():
        line = line.strip()
        if line.startswith('{'):
            rows.append(json.loads(line))
    return rows


def refetch(base, keys, recorded):
    """``{key: True/False}``: does ``api/query`` still answer as recorded?"""
    session = requests.Session()
    session.headers['User-Agent'] = USER_AGENT
    consistent = {}
    last = 0.0
    for key, fen in keys:
        while in_quiet_window():
            time.sleep(60)
        wait = last + 0.5 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        response = session.get(base + '/atomicdb/api/query',
                               params={'fen': fen}, timeout=(10, 60))
        consistent[key] = (response.status_code == 200
                           and response.text == recorded.get(key))
    return consistent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dir', required=True)
    parser.add_argument('--ssh', required=True, help='user@host of production')
    parser.add_argument('--key', default='', help='ssh identity file')
    parser.add_argument('--base', default='https://belzedar.duckdns.org')
    args = parser.parse_args()
    if in_quiet_window():
        sys.exit('refusing to run inside 04:30-07:15 UTC')
    directory = Path(args.dir)
    keys = load_keys(directory)
    recorded = recorded_queries(directory)
    keys = [(k, f) for k, f in keys if k in recorded]
    rows = run_sql(keys, args.ssh, args.key)
    consistent = refetch(args.base, keys, recorded)
    meta = next(row for row in rows if row['kind'] == 'meta')
    meta.update({'consistent_keys': sum(consistent.values()),
                 'refetched_at': dt.datetime.now(dt.timezone.utc).isoformat()})
    with gzip.open(directory / 'fixture-rows.jsonl.gz', 'wt',
                   encoding='utf-8', compresslevel=9) as out:
        out.write(json.dumps(meta) + '\n')
        for key, _ in keys:
            out.write(json.dumps({'kind': 'consistency', 'key': key,
                                  'consistent': consistent[key]}) + '\n')
        for row in rows:
            if row['kind'] != 'meta':
                out.write(json.dumps(row, ensure_ascii=False) + '\n')
    counts = {}
    for row in rows:
        counts[row['kind']] = counts.get(row['kind'], 0) + 1
    print(json.dumps({'rows': counts, 'consistent': meta['consistent_keys'],
                      'keys': len(keys)}, indent=1))


if __name__ == '__main__':
    main()
