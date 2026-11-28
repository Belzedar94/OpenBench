#!/usr/bin/env python3
"""Summarise a shim run and keep the ratchet.

    python atomicdb-v2/scripts/shim_report.py REPORT_V2.json \\
        [--v1 REPORT_V1.json] [--baseline atomicdb-v2/docs/shim-baseline.txt] \\
        [--update-baseline] [--markdown SUMMARY.md]

REPORT_V2.json is what ``ShimRunner --shim-report`` wrote with
``ATOMICDB_ENGINE=v2``.  Classes of a test:

* ``v2-pass``: at least one of its calls went to v2, and it passed;
* ``v2-fail``: at least one call went to v2, and it failed;
* ``v1-only``: it passed with every rule call still on v1 (the functions it
  waits for are listed as ``blocked by``);
* ``no-rule-call``: it never called a function the shim wraps;
* ``fail-without-v2``: it failed although nothing went to v2.  The shim's
  wrappers broke it (or v1 itself is red: compare with ``--v1``);
* ``skip``.

Exit status 1 when a test of the baseline (tests that passed on v2 before)
is not ``v2-pass`` any more, or when a test fails without any v2 call that
passes on v1.  New passes are listed; ``--update-baseline`` writes them in.
"""

import argparse
import collections
import json
import os
import sys


def load(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def read_baseline(path):
    if not path or not os.path.exists(path):
        return set()
    with open(path, encoding='utf-8') as handle:
        return {line.strip() for line in handle if line.strip() and not line.startswith('#')}


def write_baseline(path, tests):
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write('# Tests that pass on AtomicDB v2 through the shim (atomicdb/v2shim).\n')
        handle.write('# CI fails if one of them stops passing.  Regenerate with\n')
        handle.write('# atomicdb-v2/scripts/shim_report.py REPORT.json --update-baseline\n')
        for test in sorted(tests):
            handle.write(test + '\n')


def first_line(text, limit=160):
    lines = [l for l in (text or '').strip().splitlines() if l.strip()]
    return (lines[-1] if lines else '')[:limit]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('report')
    parser.add_argument('--v1', help='the same tests run with ATOMICDB_ENGINE=v1')
    parser.add_argument('--baseline')
    parser.add_argument('--update-baseline', action='store_true')
    parser.add_argument('--markdown')
    args = parser.parse_args()

    report = load(args.report)
    tests = report['tests']
    v1 = load(args.v1)['tests'] if args.v1 else {}
    classes = collections.Counter(t['class'] for t in tests.values())
    passing = {name for name, t in tests.items() if t['class'] == 'v2-pass'}
    baseline = read_baseline(args.baseline)

    # Setup helpers (chess rules, fixtures, pages) are not what a test waits for.
    setup = ('logic.', 'testing.', 'models.', 'views.')
    blocked = collections.Counter()
    for t in tests.values():
        if t['class'] == 'v1-only':
            for name in t.get('v1_calls', {}):
                if not name.startswith(setup):
                    blocked[name] += 1
    routed_use = collections.Counter()
    for t in tests.values():
        for name in t.get('v2_calls', {}):
            routed_use[name] += 1

    regressions = sorted(baseline - passing)
    new = sorted(passing - baseline)
    broken = sorted(name for name, t in tests.items()
                    if t['class'] == 'fail-without-v2'
                    and v1.get(name, {}).get('outcome', 'pass') in ('pass', 'expected_failure'))
    v2_fail = sorted(name for name, t in tests.items() if t['class'] == 'v2-fail')

    out = []
    out.append(f"## AtomicDB v2 shim: {len(passing)} of {len(tests)} tests pass on v2")
    out.append('')
    out.append(f"engine={report.get('engine')}  portability={report.get('portability')}  "
               f"seconds={report.get('seconds')}")
    out.append(f"routed to v2 ({len(report.get('routed', []))}): "
               f"{', '.join(report.get('routed', [])) or 'none yet'}")
    out.append('')
    out.append('| class | tests |')
    out.append('|---|---:|')
    for name in ('v2-pass', 'v2-fail', 'v1-only', 'no-rule-call', 'fail-without-v2', 'skip'):
        out.append(f'| {name} | {classes.get(name, 0)} |')
    out.append('')
    if routed_use:
        out.append('Calls that went to v2, by function (tests): ' +
                   ', '.join(f'{n} {c}' for n, c in routed_use.most_common()))
        out.append('')
    if blocked:
        out.append('Most waited-for functions (tests that would move to v2 once ported):')
        out.append('')
        for name, count in blocked.most_common(25):
            out.append(f'- `{name}`: {count}')
        out.append('')
    if v2_fail:
        out.append(f'v2-fail ({len(v2_fail)}), first 30:')
        out.append('')
        for name in v2_fail[:30]:
            t = tests[name]
            why = (t.get('v2_errors') or [{}])[0].get('message') or first_line(t.get('detail'))
            out.append(f'- `{name}`: {why}')
        out.append('')
    if new:
        out.append(f'New passes not in the baseline ({len(new)}): run with --update-baseline.')
        out.append('')
    if regressions:
        out.append(f'**Regressions** ({len(regressions)}): tests of the baseline that no longer pass on v2:')
        out.append('')
        for name in regressions:
            out.append(f"- `{name}`: {tests.get(name, {}).get('class', 'not run')}")
        out.append('')
    if broken:
        out.append(f'**Broken by the shim** ({len(broken)}): failing with no v2 call:')
        out.append('')
        for name in broken[:30]:
            out.append(f"- `{name}`: {first_line(tests[name].get('detail'))}")
        out.append('')

    text = '\n'.join(out)
    print(text)
    for path in filter(None, [args.markdown, os.environ.get('GITHUB_STEP_SUMMARY')]):
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(text + '\n')
    if args.update_baseline and args.baseline:
        write_baseline(args.baseline, (baseline - set(regressions)) | passing)
        print(f'baseline: {args.baseline} ({len((baseline - set(regressions)) | passing)} tests)')
        return 0
    return 1 if regressions or broken else 0


if __name__ == '__main__':
    sys.exit(main())
