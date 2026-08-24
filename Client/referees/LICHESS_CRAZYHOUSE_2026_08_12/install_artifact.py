#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "manifest.json"


def load_manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest().upper()


def verify_artifact(path, platform_name, manifest=None):
    manifest = load_manifest() if manifest is None else manifest
    platform_name = platform_name.lower()
    if platform_name not in manifest["artifacts"]:
        raise ValueError("unknown platform: %s" % platform_name)

    contract = manifest["contract"]
    record = manifest["artifacts"][platform_name]
    expected_path = record.get("relative_path")
    expected_bytes = record.get("expected_bytes")
    expected_sha256 = record.get("expected_sha256")
    if expected_path is None or expected_bytes is None or expected_sha256 is None:
        raise ValueError(
            "%s has no qualified %s referee artifact"
            % (contract, platform_name)
        )

    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("referee artifact is not a regular file: %s" % path)
    observed_bytes = path.stat().st_size
    if observed_bytes != expected_bytes:
        raise ValueError(
            "referee byte count mismatch: expected %d, observed %d"
            % (expected_bytes, observed_bytes)
        )

    expected_magic = bytes.fromhex(record["magic_hex"])
    with path.open("rb") as source:
        observed_magic = source.read(len(expected_magic))
    if observed_magic != expected_magic:
        raise ValueError("referee executable magic mismatch")

    observed_sha256 = file_sha256(path)
    if observed_sha256 != expected_sha256.upper():
        raise ValueError(
            "referee sha256 mismatch: expected %s, observed %s"
            % (expected_sha256.upper(), observed_sha256)
        )

    return {
        "contract": contract,
        "platform": platform_name,
        "artifact": str(path),
        "bytes": observed_bytes,
        "sha256": observed_sha256,
        "destination": str((ROOT / expected_path).resolve()),
    }


def install_artifact(verification):
    source = Path(verification["artifact"])
    destination = Path(verification["destination"])
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        if (
            destination.stat().st_size == verification["bytes"]
            and file_sha256(destination) == verification["sha256"]
        ):
            return "already-installed"
        raise ValueError(
            "refusing to overwrite a different installed referee: %s"
            % destination
        )

    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=destination.name + ".",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        shutil.copyfile(source, temporary_path)
        if (
            temporary_path.stat().st_size != verification["bytes"]
            or file_sha256(temporary_path) != verification["sha256"]
        ):
            raise ValueError("copied referee changed before installation")
        if os.name != "nt":
            temporary_path.chmod(0o755)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return "installed"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--platform", choices=("windows", "linux"), required=True)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()

    verification = verify_artifact(args.artifact, args.platform)
    verification["status"] = (
        install_artifact(verification) if args.install else "verified"
    )
    print(json.dumps(verification, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
