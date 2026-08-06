#!/usr/bin/env python3

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
REFEREE = ROOT / "Client" / "referees" / "LICHESS_HORDE_V1"
sys.path.insert(0, str(REFEREE))

import artifact_receipt
import install_artifacts


SOURCE_COMMIT = "a" * 40
RUN_ID = 123456
RUN_ATTEMPT = 2


class HordeRefereeArtifactTests(unittest.TestCase):

    def make_artifact(self, root: Path, platform: str) -> Path:
        artifact_name, binary_name, magic = artifact_receipt.ARTIFACTS[platform]
        artifact_dir = root / artifact_name
        artifact_dir.mkdir()
        payload = magic + b"\0fixture\0" + artifact_receipt.HORDE_MARKER
        (artifact_dir / binary_name).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        (artifact_dir / "SHA256SUMS").write_text(
            f"{digest}  {binary_name}\n", encoding="ascii"
        )
        (artifact_dir / "toolchain.txt").write_text(
            "pinned test toolchain\n", encoding="utf-8"
        )
        environment = {
            "GITHUB_SHA": SOURCE_COMMIT,
            "GITHUB_RUN_ID": str(RUN_ID),
            "GITHUB_RUN_ATTEMPT": str(RUN_ATTEMPT),
        }
        receipt = artifact_receipt.build_receipt(
            artifact_dir, platform, environment
        )
        (artifact_dir / "artifact-receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return artifact_dir

    def test_matching_pair_is_verified_and_installed_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            windows = self.make_artifact(root, "windows")
            linux = self.make_artifact(root, "linux")
            receipts = install_artifacts.verify_pair(
                windows, linux, SOURCE_COMMIT, RUN_ID
            )
            self.assertEqual(receipts["windows"]["workflow_run_attempt"], 2)
            destination = root / "client"
            install_artifacts.atomic_install(
                windows / "cutechess-ob.exe",
                destination / "cutechess-ob.exe",
                0o755,
            )
            install_artifacts.atomic_install(
                linux / "cutechess-ob", destination / "cutechess-ob", 0o755
            )
            self.assertEqual(
                (destination / "cutechess-ob.exe").read_bytes(),
                (windows / "cutechess-ob.exe").read_bytes(),
            )
            self.assertEqual(
                (destination / "cutechess-ob").read_bytes(),
                (linux / "cutechess-ob").read_bytes(),
            )

    def test_pair_rejects_different_run_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            windows = self.make_artifact(root, "windows")
            linux = self.make_artifact(root, "linux")
            receipt_path = linux / "artifact-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["workflow_run_attempt"] = RUN_ATTEMPT + 1
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different run attempts"):
                install_artifacts.verify_pair(
                    windows, linux, SOURCE_COMMIT, RUN_ID
                )

    def test_pair_rejects_tampering_and_wrong_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            windows = self.make_artifact(root, "windows")
            linux = self.make_artifact(root, "linux")
            with self.assertRaisesRegex(ValueError, "source_commit"):
                install_artifacts.verify_pair(
                    windows, linux, "b" * 40, RUN_ID
                )
            with (linux / "cutechess-ob").open("ab") as binary:
                binary.write(b"tampered")
            with self.assertRaisesRegex(ValueError, "SHA256SUMS mismatch"):
                install_artifacts.verify_pair(
                    windows, linux, SOURCE_COMMIT, RUN_ID
                )


if __name__ == "__main__":
    unittest.main()
