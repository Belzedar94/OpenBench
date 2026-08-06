#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re


HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "manifest.json"
WORKFLOW_NAME = "Horde referee"
ARTIFACTS = {
    "windows": ("horde-referee-windows-x86-64", "cutechess-ob.exe", b"MZ"),
    "linux": ("horde-referee-linux-x86-64", "cutechess-ob", b"\x7fELF"),
}
HORDE_MARKER = b"'horde': Horde Chess (v2)"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def verify_payload(artifact_dir: Path, platform: str) -> dict:
    artifact_name, binary_name, magic = ARTIFACTS[platform]
    binary_path = artifact_dir / binary_name
    checksum_path = artifact_dir / "SHA256SUMS"
    toolchain_path = artifact_dir / "toolchain.txt"

    if not binary_path.is_file():
        raise ValueError(f"missing referee binary: {binary_path}")
    if not checksum_path.is_file():
        raise ValueError(f"missing referee checksums: {checksum_path}")
    if not toolchain_path.is_file() or not toolchain_path.read_text(
        encoding="utf-8", errors="replace"
    ).strip():
        raise ValueError(f"missing referee toolchain receipt: {toolchain_path}")

    payload = binary_path.read_bytes()
    digest = sha256(payload)
    expected_checksum = f"{digest}  {binary_name}"
    if checksum_path.read_text(encoding="ascii").strip() != expected_checksum:
        raise ValueError(f"referee SHA256SUMS mismatch for {platform}")
    if not payload.startswith(magic):
        raise ValueError(f"referee executable format mismatch for {platform}")
    if HORDE_MARKER not in payload:
        raise ValueError(f"native Horde marker missing from {platform} referee")

    return {
        "artifact_name": artifact_name,
        "binary_name": binary_name,
        "bytes": len(payload),
        "sha256": digest,
    }


def build_receipt(artifact_dir: Path, platform: str, environment: dict) -> dict:
    source_commit = environment.get("GITHUB_SHA", "").lower()
    run_id = environment.get("GITHUB_RUN_ID", "")
    run_attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise ValueError("GITHUB_SHA must be a full lowercase commit")
    if not run_id.isdecimal() or int(run_id) <= 0:
        raise ValueError("GITHUB_RUN_ID must be a positive integer")
    if not run_attempt.isdecimal() or int(run_attempt) <= 0:
        raise ValueError("GITHUB_RUN_ATTEMPT must be a positive integer")

    manifest = load_manifest()
    payload = verify_payload(artifact_dir, platform)
    return {
        "schema": 1,
        "contract": manifest["contract"],
        "workflow": WORKFLOW_NAME,
        "workflow_run_id": int(run_id),
        "workflow_run_attempt": int(run_attempt),
        "source_commit": source_commit,
        "platform": platform,
        "artifact_name": payload["artifact_name"],
        "binary": {
            "name": payload["binary_name"],
            "sha256": payload["sha256"],
            "bytes": payload["bytes"],
        },
        "referee_commit": manifest["referee"]["commit"],
        "patch_sha256": manifest["referee"]["patch_sha256"].lower(),
        "material_corpus_sha256": manifest["material_corpus"]["sha256"].lower(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=sorted(ARTIFACTS), required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    args = parser.parse_args()

    artifact_dir = args.artifact_dir.resolve(strict=True)
    receipt = build_receipt(artifact_dir, args.platform, os.environ)
    receipt_path = artifact_dir / "artifact-receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
