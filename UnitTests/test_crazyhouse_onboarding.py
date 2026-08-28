#!/usr/bin/env python3

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "LICHESS_CRAZYHOUSE_2026_08_12"
ENGINE = "Crazyhouse-Stockfish"
BOOK = "CRAZYHOUSE_openings.epd"
ENGINE_COMMIT = "5883acbeffd53138d31b278894d1fee451adffe8"
BOOK_SHA256 = "1371e87ce3bdb875d922ad0061c96c4a123bc571daf4ae2bff24e5176287f0fa"
BOOK_ARCHIVE_SHA256 = "d24bb6d72015af9930f76f9191ba36c016652a6f2708a2cc79e9e2c8ec600d9c"
NETWORK_SHA256 = "8ebf84784ad20fa33df403e60211818a7486db7cb8c3decfc86a80238d254f43"
REFEREE_SHA256 = "f465025b2ad21526e2cbab2b7da1a231ff3d64f6e8a01a0be5963f525a0bddae"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Client"))

from OpenBench import variant_contract
import worker


INSTALLER_PATH = (
    ROOT
    / "Client"
    / "referees"
    / CONTRACT
    / "install_artifact.py"
)
INSTALLER_SPEC = importlib.util.spec_from_file_location(
    "crazyhouse_referee_installer", INSTALLER_PATH
)
installer = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(installer)


def load_json(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def routing_config(book=BOOK, contract=CONTRACT):
    test = {
        "type": "TEST",
        "book": {"name": book},
        "dev": {"engine": ENGINE},
        "base": {"engine": ENGINE},
    }
    if contract is not None:
        test["variant_contract"] = contract
        test["book"]["variant_contract"] = contract
        test["dev"]["variant_contract"] = contract
        test["base"]["variant_contract"] = contract
    return SimpleNamespace(workload={"test": test})


class CrazyhouseActivationTests(unittest.TestCase):

    def test_engine_and_book_are_active_with_frozen_defaults(self):
        general = load_json("Config/config.json")
        engine = load_json("Engines/%s.json" % ENGINE)
        book = load_json("Books/%s.json" % BOOK)
        self.assertIn(ENGINE, general["engines"])
        self.assertIn(BOOK, general["books"])
        self.assertTrue(engine["onboarding_ready"])
        self.assertTrue(book["onboarding_ready"])
        self.assertFalse(book["datagen_enabled"])
        self.assertEqual(engine["nps"], 234000)
        self.assertEqual(
            engine["nps_status"],
            "THREE_DEPTH10_RUNS_235088_233349_232775",
        )
        self.assertEqual(engine["source"], "https://github.com/Belzedar94/Crazyhouse-Stockfish")
        self.assertEqual(engine["variant_contract"], CONTRACT)
        self.assertEqual(book["variant_contract"], CONTRACT)
        self.assertEqual(engine["build"]["systems"], ["Windows"])
        self.assertEqual(engine["build"]["artifact_roles"], ["play"])
        defaults = engine["test_presets"]["default"]
        self.assertEqual(defaults["base_branch"], ENGINE_COMMIT)
        self.assertEqual(defaults["both_bench"], 38919)
        self.assertEqual(
            defaults["both_network"],
            "crazyhouse_run15rl_e190_l03.nnue",
        )
        self.assertEqual(defaults["test_bounds"], "[0.00, 10.00]")
        self.assertNotIn("test_max_games", defaults)
        self.assertEqual(
            engine["test_presets"]["STC"]["both_time_control"],
            "10.0+0.1",
        )
        self.assertEqual(
            engine["test_presets"]["LTC"]["both_time_control"],
            "30.0+0.3",
        )
        self.assertNotIn("test_max_games", engine["test_presets"]["STC"])
        self.assertNotIn("test_max_games", engine["test_presets"]["LTC"])
        self.assertEqual(
            engine["qualified_source"]["official_stockfish_ancestor"],
            "229f6339e537a097a79831cd06dbfdb3e623d4ac",
        )
        self.assertEqual(
            engine["legacy_evaluator"]["sha256"], NETWORK_SHA256
        )
        self.assertFalse(
            engine["legacy_evaluator"][
                "alias_or_champion_change_authorized"
            ]
        )
        self.assertEqual(book["sha"], BOOK_SHA256)
        self.assertEqual(book["raw_sha"], BOOK_SHA256)
        self.assertEqual(book["archive_sha256"], BOOK_ARCHIVE_SHA256)
        self.assertEqual(book["source_status"], "PUBLISHED_AND_REAUTHENTICATED")
        self.assertEqual(
            book["source"],
            "https://raw.githubusercontent.com/Belzedar94/"
            "Crazyhouse-Stockfish/" + ENGINE_COMMIT
            + "/openbench/books/" + BOOK + ".zip",
        )

    def test_opening_alias_is_byte_exact_and_deterministic(self):
        archive = ROOT / "Books" / (BOOK + ".zip")
        self.assertEqual(
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            BOOK_ARCHIVE_SHA256,
        )
        with zipfile.ZipFile(archive) as container:
            self.assertEqual(container.namelist(), [BOOK])
            payload = container.read(BOOK)
        self.assertEqual(len(payload), 39922)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), BOOK_SHA256)
        self.assertEqual(len(payload.splitlines()), 599)
        self.assertEqual(len(set(payload.splitlines())), 489)

    def test_manifest_pins_only_the_qualified_windows_referee(self):
        manifest = load_json(
            "Client/referees/%s/manifest.json" % CONTRACT
        )
        self.assertTrue(manifest["onboarding_ready"])
        self.assertEqual(manifest["contract"], CONTRACT)
        self.assertEqual(
            manifest["profile"]["sha256"].lower(),
            "d0602bc32877639f2d9a70741614882512083431b48b9f4e98a88e1067eb4d68",
        )
        windows = manifest["artifacts"]["windows"]
        linux = manifest["artifacts"]["linux"]
        self.assertEqual(windows["expected_bytes"], 2293660)
        self.assertEqual(windows["expected_sha256"].lower(), REFEREE_SHA256)
        self.assertTrue(windows["published"])
        self.assertTrue(
            manifest["referee_source"][
                "public_corresponding_source_available"
            ]
        )
        binary = (
            ROOT / "Client" / "referees" / CONTRACT
            / windows["relative_path"]
        )
        self.assertEqual(binary.stat().st_size, windows["expected_bytes"])
        self.assertEqual(
            hashlib.sha256(binary.read_bytes()).hexdigest(),
            REFEREE_SHA256,
        )
        self.assertEqual(
            worker.REFEREE_PINS[CONTRACT]["Windows"].lower(),
            REFEREE_SHA256,
        )
        self.assertIsNone(linux["expected_sha256"])
        self.assertIsNone(linux["relative_path"])
        self.assertIsNone(worker.REFEREE_PINS[CONTRACT]["Linux"])
        worker_path = worker.CONTRACT_REFEREE_PATHS[CONTRACT]["Windows"]
        self.assertEqual(
            Path(worker_path),
            Path("referees") / CONTRACT / windows["relative_path"],
        )
        self.assertIsNone(
            worker.CONTRACT_REFEREE_PATHS[CONTRACT]["Linux"]
        )


class CrazyhouseServerContractTests(unittest.TestCase):

    @staticmethod
    def config(contract=CONTRACT, book_name=BOOK):
        return {
            "engines": {
                ENGINE: {"variant_contract": contract},
                "Baseline": {"variant_contract": contract},
            },
            "books": {book_name: {"variant_contract": contract}},
        }

    def test_exact_contract_is_accepted(self):
        self.assertEqual(
            variant_contract.configured_variant_contract(
                self.config(), ENGINE, "Baseline", BOOK
            ),
            CONTRACT,
        )

    def test_missing_or_wrong_contract_is_rejected(self):
        with self.assertRaisesRegex(
            variant_contract.VariantContractError,
            "Crazyhouse workloads require variant_contract",
        ):
            variant_contract.configured_variant_contract(
                self.config(None), ENGINE, "Baseline", BOOK
            )
        with self.assertRaisesRegex(
            variant_contract.VariantContractError,
            "Crazyhouse workloads require variant_contract",
        ):
            variant_contract.configured_variant_contract(
                self.config("LICHESS_HORDE_V1"),
                ENGINE,
                "Baseline",
                BOOK,
            )

    def test_ambiguous_protected_family_is_rejected(self):
        mixed_book = "CRAZYHOUSE_HORDE.epd"
        with self.assertRaisesRegex(
            variant_contract.VariantContractError,
            "multiple protected variant families",
        ):
            variant_contract.configured_variant_contract(
                self.config(CONTRACT, mixed_book),
                ENGINE,
                "Baseline",
                mixed_book,
            )


class CrazyhouseClientRoutingTests(unittest.TestCase):

    def setUp(self):
        worker._REFEREE_DIGESTS.clear()

    def test_exact_contract_routes_to_crazyhouse(self):
        config = routing_config()
        self.assertEqual(
            worker.variant_routing(config), ("cutechess", "crazyhouse")
        )
        self.assertEqual(
            worker.Cutechess.basic_settings(config),
            "-repeat -recover -variant crazyhouse",
        )

    def test_name_inference_never_replaces_the_contract(self):
        for config in (
            routing_config(contract=None),
            routing_config(book="ordinary.epd", contract=None),
        ):
            with self.subTest(book=config.workload["test"]["book"]["name"]):
                with self.assertRaisesRegex(
                    worker.VariantRoutingError,
                    "require variant_contract=%s" % CONTRACT,
                ):
                    worker.variant_routing(config)

    def test_contract_conflict_is_rejected(self):
        config = routing_config(book="ATOMIC_openings.epd")
        with self.assertRaisesRegex(
            worker.VariantRoutingError, "conflicts with inferred route"
        ):
            worker.variant_routing(config)

    def test_windows_path_is_contract_specific(self):
        with mock.patch.object(worker.platform, "system", lambda: "Windows"):
            path = Path(worker.referee_binary_path(routing_config()))
        expected = (
            ROOT
            / "Client"
            / "referees"
            / CONTRACT
            / "windows"
            / "cutechess-cli.exe"
        )
        self.assertEqual(path.resolve(), expected.resolve())
        self.assertNotEqual(path.resolve(), (ROOT / "Client" / "cutechess-ob.exe").resolve())

    def test_linux_is_refused_without_a_pin_or_path(self):
        with mock.patch.object(worker.platform, "system", lambda: "Linux"):
            with self.assertRaisesRegex(
                worker.VariantRoutingError,
                "no recorded referee path.*refusing to fall back",
            ):
                worker.runner_base_command(routing_config())

    def test_wrong_windows_referee_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "cutechess-cli.exe"
            binary.write_bytes(b"MZwrong-crazyhouse-referee")
            with mock.patch.object(
                worker, "referee_binary_path", return_value=str(binary)
            ), mock.patch.object(worker.platform, "system", lambda: "Windows"):
                with self.assertRaisesRegex(
                    worker.VariantRoutingError, "requires the Windows referee"
                ):
                    worker.runner_base_command(routing_config())


class CrazyhouseInstallerTests(unittest.TestCase):

    @staticmethod
    def fixture_manifest(payload, destination):
        return {
            "contract": CONTRACT,
            "artifacts": {
                "windows": {
                    "relative_path": str(destination),
                    "expected_bytes": len(payload),
                    "expected_sha256": hashlib.sha256(payload).hexdigest(),
                    "magic_hex": "4D5A",
                },
                "linux": {
                    "relative_path": None,
                    "expected_bytes": None,
                    "expected_sha256": None,
                    "magic_hex": "7F454C46",
                },
            },
        }

    def test_check_and_install_are_hash_locked_and_non_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"MZqualified-fixture"
            source = root / "source.exe"
            destination = root / "installed" / "cutechess-cli.exe"
            source.write_bytes(payload)
            manifest = self.fixture_manifest(payload, destination)
            verification = installer.verify_artifact(
                source, "windows", manifest
            )
            verification["destination"] = str(destination)
            self.assertEqual(
                installer.install_artifact(verification), "installed"
            )
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(
                installer.install_artifact(verification), "already-installed"
            )
            destination.write_bytes(b"MZdifferent")
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                installer.install_artifact(verification)

    def test_linux_without_a_qualified_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "referee"
            source.write_bytes(b"\x7fELFfixture")
            manifest = self.fixture_manifest(b"MZfixture", "unused")
            with self.assertRaisesRegex(ValueError, "no qualified linux"):
                installer.verify_artifact(source, "linux", manifest)


if __name__ == "__main__":
    unittest.main()
