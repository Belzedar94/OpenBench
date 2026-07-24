#!/usr/bin/env python3
"""Compile the audited Atomic opening sources into AtomicDB's static catalog."""

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from atomicdb.openings import (  # noqa: E402
    CATALOG_PATH,
    OpeningCatalogError,
    build_catalog,
    write_catalog,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--eao', required=True, type=Path)
    parser.add_argument('--modern', required=True, type=Path)
    parser.add_argument('--atomix', type=Path)
    parser.add_argument('--output', type=Path, default=CATALOG_PATH)
    parser.add_argument(
        '--check',
        action='store_true',
        help='Build in memory and fail if --output is not identical.',
    )
    args = parser.parse_args()

    sources = [('eao', args.eao), ('modern', args.modern)]
    if args.atomix is not None:
        sources.append(('atomix', args.atomix))
    try:
        if args.check:
            payload = build_catalog(sources)
            committed = json.loads(args.output.read_text(encoding='utf-8'))
            if committed != payload:
                parser.error(
                    f'{args.output} differs from the deterministic build')
        else:
            payload = write_catalog(sources, args.output)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        OpeningCatalogError,
    ) as exc:
        parser.error(str(exc))
    counts = payload['counts']
    print(
        f"{'verified' if args.check else 'wrote'} {args.output}: "
        f"positions={counts['positions']} "
        f"records={counts['source_records']} "
        f"sha256={payload['catalog_sha256']}"
    )


if __name__ == '__main__':
    main()
