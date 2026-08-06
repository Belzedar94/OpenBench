#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from artifact_receipt import ARTIFACTS, COMMIT_PATTERN, load_manifest, verify_payload


HERE = Path(__file__).resolve().parent
DEFAULT_DESTINATION = HERE.parents[1]


def load_and_verify_receipt(
    artifact_dir: Path,
    platform: str,
    expected_source_commit: str,
    expected_run_id: int,
) -> dict:
    receipt_path = artifact_dir / "artifact-receipt.json"
    if not receipt_path.is_file():
        raise ValueError(f"missing artifact receipt: {receipt_path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    manifest = load_manifest()
    payload = verify_payload(artifact_dir, platform)
    artifact_name, binary_name, _ = ARTIFACTS[platform]

    exact = {
        "schema": 1,
        "contract": manifest["contract"],
        "workflow": "Horde referee",
        "workflow_run_id": expected_run_id,
        "source_commit": expected_source_commit,
        "platform": platform,
        "artifact_name": artifact_name,
        "referee_commit": manifest["referee"]["commit"],
        "patch_sha256": manifest["referee"]["patch_sha256"].lower(),
        "material_corpus_sha256": manifest["material_corpus"]["sha256"].lower(),
    }
    for key, value in exact.items():
        if receipt.get(key) != value:
            raise ValueError(f"artifact receipt mismatch for {platform}: {key}")
    if not isinstance(receipt.get("workflow_run_attempt"), int) or receipt[
        "workflow_run_attempt"
    ] <= 0:
        raise ValueError(f"invalid workflow run attempt for {platform}")
    expected_binary = {
        "name": binary_name,
        "sha256": payload["sha256"],
        "bytes": payload["bytes"],
    }
    if receipt.get("binary") != expected_binary:
        raise ValueError(f"artifact binary receipt mismatch for {platform}")
    return receipt


def verify_pair(
    windows_dir: Path,
    linux_dir: Path,
    expected_source_commit: str,
    expected_run_id: int,
) -> dict:
    expected_source_commit = expected_source_commit.lower()
    if not COMMIT_PATTERN.fullmatch(expected_source_commit):
        raise ValueError("expected source commit must contain 40 lowercase hex digits")
    if expected_run_id <= 0:
        raise ValueError("expected workflow run ID must be positive")

    receipts = {
        "windows": load_and_verify_receipt(
            windows_dir, "windows", expected_source_commit, expected_run_id
        ),
        "linux": load_and_verify_receipt(
            linux_dir, "linux", expected_source_commit, expected_run_id
        ),
    }
    if (
        receipts["windows"]["workflow_run_attempt"]
        != receipts["linux"]["workflow_run_attempt"]
    ):
        raise ValueError("Windows and Linux receipts came from different run attempts")
    return receipts


def atomic_install(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        shutil.copyfile(source, temporary_path)
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", type=Path, required=True)
    parser.add_argument("--linux", type=Path, required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--expected-run-id", type=int, required=True)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()

    windows_dir = args.windows.resolve(strict=True)
    linux_dir = args.linux.resolve(strict=True)
    destination = args.destination.resolve()
    receipts = verify_pair(
        windows_dir,
        linux_dir,
        args.expected_source_commit,
        args.expected_run_id,
    )

    if args.install:
        atomic_install(
            windows_dir / "cutechess-ob.exe",
            destination / "cutechess-ob.exe",
            0o755,
        )
        atomic_install(
            linux_dir / "cutechess-ob",
            destination / "cutechess-ob",
            0o755,
        )

    summary = {
        "installed": args.install,
        "source_commit": receipts["windows"]["source_commit"],
        "workflow_run_id": receipts["windows"]["workflow_run_id"],
        "workflow_run_attempt": receipts["windows"]["workflow_run_attempt"],
        "windows_sha256": receipts["windows"]["binary"]["sha256"],
        "linux_sha256": receipts["linux"]["binary"]["sha256"],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
