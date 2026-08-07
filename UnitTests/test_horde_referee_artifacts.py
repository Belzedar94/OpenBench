#!/usr/bin/env python3

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
REFEREE = ROOT / "Client" / "referees" / "LICHESS_HORDE_V1"
sys.path.insert(0, str(REFEREE))
sys.path.insert(0, str(ROOT / "Client"))

import artifact_receipt
import install_artifacts
import worker


SOURCE_COMMIT = "a" * 40
RUN_ID = 123456
RUN_ATTEMPT = 2


class HordeRefereeArtifactTests(unittest.TestCase):

    def test_windows_toolchain_lock_is_complete_and_self_consistent(self):
        lock_path = REFEREE / "windows-toolchain-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertEqual(lock["schema"], 1)
        self.assertEqual(
            lock["package_repository"],
            "https://repo.msys2.org/mingw/mingw64",
        )
        self.assertRegex(lock["msys2_base"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(lock["msys2_base"]["bytes"], 0)

        packages = lock["packages"]
        names = [package["name"] for package in packages]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 50)
        for package in packages:
            self.assertTrue(
                package["file"].startswith(
                    f"{package['name']}-{package['version']}-"
                )
            )
            self.assertRegex(package["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(package["bytes"], 0)

        required = {
            "mingw-w64-x86_64-binutils",
            "mingw-w64-x86_64-cmake",
            "mingw-w64-x86_64-crt",
            "mingw-w64-x86_64-gcc",
            "mingw-w64-x86_64-gcc-libs",
            "mingw-w64-x86_64-headers",
            "mingw-w64-x86_64-ninja",
            "mingw-w64-x86_64-openssl",
            "mingw-w64-x86_64-qt5-static",
            "mingw-w64-x86_64-winpthreads",
            "mingw-w64-x86_64-zlib",
            "mingw-w64-x86_64-zstd",
        }
        self.assertTrue(required.issubset(names))

        digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        windows_manifest = artifact_receipt.load_manifest()["static_build"][
            "windows"
        ]
        self.assertEqual(windows_manifest["lock_sha256"].lower(), digest)
        self.assertEqual(windows_manifest["package_count"], len(packages))
        self.assertEqual(
            windows_manifest["msys2_base_release"],
            lock["msys2_base"]["release"],
        )
        self.assertEqual(windows_manifest["expected_referee_bytes"], 7511040)
        self.assertRegex(
            windows_manifest["expected_referee_sha256"], r"^[0-9A-F]{64}$"
        )
        build_script = (REFEREE / "build_static_windows.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'expected_lock_sha256="{digest}"', build_script)
        self.assertIn(
            'expected_binary_sha256="'
            f'{windows_manifest["expected_referee_sha256"].lower()}"',
            build_script,
        )

    def fixture_payload(self, platform: str) -> bytes:
        _, _, magic = artifact_receipt.ARTIFACTS[platform]
        return magic + b"\0fixture\0" + artifact_receipt.HORDE_MARKER

    def locked_manifest(self):
        """Manifest whose reproducible locks describe the test fixtures.

        The install gate now refuses anything that does not reproduce the
        committed hash, so the fixtures have to declare their own lock instead
        of bypassing the check.
        """
        manifest = copy.deepcopy(artifact_receipt.load_manifest())
        for platform in artifact_receipt.ARTIFACTS:
            payload = self.fixture_payload(platform)
            manifest["static_build"][platform].update({
                "expected_referee_sha256": hashlib.sha256(payload).hexdigest(),
                "expected_referee_bytes": len(payload),
            })
        return manifest

    def patched_locks(self):
        return mock.patch.object(
            install_artifacts, "load_manifest", self.locked_manifest
        )

    def make_artifact(self, root: Path, platform: str) -> Path:
        artifact_name, binary_name, magic = artifact_receipt.ARTIFACTS[platform]
        artifact_dir = root / artifact_name
        artifact_dir.mkdir()
        payload = self.fixture_payload(platform)
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
            with self.patched_locks():
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
            with self.patched_locks(), self.assertRaisesRegex(ValueError, "different run attempts"):
                install_artifacts.verify_pair(
                    windows, linux, SOURCE_COMMIT, RUN_ID
                )

    def test_pair_rejects_tampering_and_wrong_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            windows = self.make_artifact(root, "windows")
            linux = self.make_artifact(root, "linux")
            with self.patched_locks(), self.assertRaisesRegex(ValueError, "source_commit"):
                install_artifacts.verify_pair(
                    windows, linux, "b" * 40, RUN_ID
                )
            with (linux / "cutechess-ob").open("ab") as binary:
                binary.write(b"tampered")
            with self.patched_locks(), self.assertRaisesRegex(ValueError, "SHA256SUMS mismatch"):
                install_artifacts.verify_pair(
                    windows, linux, SOURCE_COMMIT, RUN_ID
                )

    def test_pair_rejects_toolchain_receipt_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            windows = self.make_artifact(root, "windows")
            linux = self.make_artifact(root, "linux")
            with (windows / "toolchain.txt").open("ab") as toolchain:
                toolchain.write(b"tampered")
            with self.patched_locks(), self.assertRaisesRegex(ValueError, "toolchain receipt mismatch"):
                install_artifacts.verify_pair(
                    windows, linux, SOURCE_COMMIT, RUN_ID
                )


    def test_install_refuses_an_unrecorded_or_drifting_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            windows = self.make_artifact(root, "windows")
            linux = self.make_artifact(root, "linux")

            # A build that nobody has reproduced yet must not be installable.
            def without_lock():
                manifest = self.locked_manifest()
                manifest["static_build"]["linux"].update({
                    "expected_referee_sha256": None,
                    "expected_referee_bytes": None,
                })
                return manifest

            with mock.patch.object(
                install_artifacts, "load_manifest", without_lock
            ), self.assertRaisesRegex(ValueError, "no reproducible binary lock"):
                install_artifacts.verify_pair(
                    windows, linux, SOURCE_COMMIT, RUN_ID
                )

            # A self-consistent artifact that does not reproduce the committed
            # hash is exactly the case the receipt chain cannot detect alone.
            def drifted():
                manifest = self.locked_manifest()
                manifest["static_build"]["linux"]["expected_referee_sha256"] = (
                    "0" * 64
                )
                return manifest

            with mock.patch.object(
                install_artifacts, "load_manifest", drifted
            ), self.assertRaisesRegex(ValueError, "does not match the linux hash lock"):
                install_artifacts.verify_pair(
                    windows, linux, SOURCE_COMMIT, RUN_ID
                )


class HordeRefereeGateTests(unittest.TestCase):

    CONTRACT = "LICHESS_HORDE_V1"

    def config(self, contract=CONTRACT):
        test = {
            "book": {"name": "HORDE_openings.epd"},
            "dev": {"engine": "Horde-Stockfish"},
            "base": {"engine": "Horde-Stockfish"},
        }
        if contract is not None:
            test["variant_contract"] = contract
        return SimpleNamespace(workload={"test": test})

    def setUp(self):
        worker._REFEREE_DIGESTS.clear()

    def test_pins_are_the_manifest_values_and_never_diverge(self):
        manifest = artifact_receipt.load_manifest()
        self.assertEqual(manifest["contract"], self.CONTRACT)
        pinned = worker.REFEREE_PINS[self.CONTRACT]
        self.assertEqual(set(pinned), {"Windows", "Linux"})
        for platform, key in (("Windows", "windows"), ("Linux", "linux")):
            expected = manifest["static_build"][key].get(
                "expected_referee_sha256"
            )
            if pinned[platform] is None:
                self.assertIsNone(
                    expected,
                    "%s referee is pinned in the manifest but not in worker.py"
                    % platform,
                )
            else:
                self.assertRegex(pinned[platform], r"^[0-9A-F]{64}$")
                self.assertEqual(pinned[platform].upper(), expected.upper())

    def test_contract_without_a_recorded_build_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "cutechess-ob"
            binary.write_bytes(b"stock cutechess that answers to -variant horde")
            with mock.patch.dict(
                worker.REFEREE_PINS,
                {self.CONTRACT: {"Windows": None, "Linux": None}},
            ), mock.patch.object(worker.platform, "system", lambda: "Linux"):
                with self.assertRaisesRegex(
                    worker.VariantRoutingError, "no recorded referee build"
                ):
                    worker.verify_referee_binary(self.config(), str(binary))

    def test_unverified_referee_is_refused_and_the_pinned_one_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "cutechess-ob"
            payload = b"stock cutechess that answers to -variant horde"
            binary.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest().upper()

            with mock.patch.dict(
                worker.REFEREE_PINS,
                {self.CONTRACT: {"Windows": "A" * 64, "Linux": "B" * 64}},
            ), mock.patch.object(worker.platform, "system", lambda: "Linux"):
                with self.assertRaisesRegex(
                    worker.VariantRoutingError, "install_artifacts.py"
                ):
                    worker.verify_referee_binary(self.config(), str(binary))

            worker._REFEREE_DIGESTS.clear()
            with mock.patch.dict(
                worker.REFEREE_PINS,
                {self.CONTRACT: {"Windows": digest, "Linux": digest}},
            ), mock.patch.object(worker.platform, "system", lambda: "Linux"):
                worker.verify_referee_binary(self.config(), str(binary))

    def test_missing_referee_is_refused_instead_of_crashing(self):
        with mock.patch.dict(
            worker.REFEREE_PINS,
            {self.CONTRACT: {"Windows": "A" * 64, "Linux": "B" * 64}},
        ), mock.patch.object(worker.platform, "system", lambda: "Linux"):
            with self.assertRaisesRegex(
                worker.VariantRoutingError, "cannot be read"
            ):
                worker.verify_referee_binary(
                    self.config(), "/nonexistent/cutechess-ob"
                )

    def test_workloads_without_a_contract_are_untouched(self):
        # Every live Spell/Atomic workload lands here: no contract, therefore
        # no hashing and no new failure mode on the existing path.
        with mock.patch.object(worker, "referee_sha256") as digest:
            worker.verify_referee_binary(
                self.config(contract=None), "/nonexistent/cutechess-ob"
            )
        digest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
