#!/usr/bin/env python3
"""Create a deterministic OpenBench Horde opening-book archive."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_payload(data: bytes) -> int:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("EPD payload must be ASCII") from exc

    lines = text.splitlines()
    if not lines:
        raise ValueError("EPD payload is empty")
    if any(not line.strip() or not line.rstrip().endswith(";") for line in lines):
        raise ValueError("every EPD record must be non-empty and end with ';'")
    return len(lines)


def package(source: Path, output: Path, member: str) -> dict[str, object]:
    if not member or Path(member).name != member or not member.endswith(".epd"):
        raise ValueError("archive member must be a plain .epd filename")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")

    payload = source.read_bytes()
    records = validate_payload(payload)

    info = ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    with ZipFile(output, mode="x") as archive:
        archive.writestr(info, payload, compress_type=ZIP_DEFLATED, compresslevel=9)

    archive_bytes = output.read_bytes()
    return {
        "schema": "HORDE_BOOK_PACKAGE_V1",
        "source": str(source.resolve()),
        "member": member,
        "records": records,
        "payload_bytes": len(payload),
        "payload_sha256": sha256_bytes(payload),
        "archive_bytes": len(archive_bytes),
        "archive_sha256": sha256_bytes(archive_bytes),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--member", default="HORDE_openings.epd")
    args = parser.parse_args()
    print(json.dumps(package(args.source, args.output, args.member), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
