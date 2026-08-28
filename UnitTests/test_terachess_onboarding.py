import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BOOK = "TERACHESS_openings_v1.epd"
BOOK_REF = "c716735d8fc4a731fb80f8c138520c198adde053"
BOOK_SHA256 = "1f117b0ed03049afad62481494fff9e3232774d188433a99ffff1454d84babe7"
ARCHIVE_SHA256 = "87ed4fba357de4020e42e711c16e9f9a08ec0d6eac12851f224699aafa2cb256"


def load_json(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class TerachessOnboardingTests(unittest.TestCase):

    def setUp(self):
        self.general = load_json("Config/config.json")
        self.book = load_json("Books/%s.json" % BOOK)

    def test_client_and_book_are_immutably_registered(self):
        self.assertEqual(self.general["client_version"], 48)
        self.assertRegex(self.general["client_repo_ref"], r"^[0-9a-f]{40}$")
        self.assertIn("Terachess-Stockfish", self.general["engines"])
        self.assertIn(BOOK, self.general["books"])
        self.assertTrue(self.book["onboarding_ready"])
        self.assertFalse(self.book["datagen_enabled"])
        self.assertEqual(self.book["sha"], BOOK_SHA256)
        self.assertEqual(self.book["raw_sha"], BOOK_SHA256)
        self.assertEqual(
            self.book["source"],
            "https://raw.githubusercontent.com/Belzedar94/OpenBench/"
            + BOOK_REF
            + "/Books/TERACHESS_openings_v1.epd.zip",
        )

    def test_archive_and_payload_are_exact(self):
        archive = ROOT / "Books" / "TERACHESS_openings_v1.epd.zip"
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), ARCHIVE_SHA256)
        with zipfile.ZipFile(archive) as container:
            self.assertEqual(container.namelist(), [BOOK])
            payload = container.read(BOOK)
        self.assertEqual(len(payload), 911542)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), BOOK_SHA256)
        lines = payload.decode("ascii").splitlines()
        self.assertEqual(len(lines), 5000)
        self.assertEqual(len(set(lines)), 5000)

    def test_book_and_engine_fallback_use_terachess_runner(self):
        import sys

        sys.path.insert(0, str(ROOT / "Client"))
        import worker

        def routing(book):
            return SimpleNamespace(
                workload={
                    "test": {
                        "book": {"name": book},
                        "dev": {"engine": "Terachess-Stockfish"},
                        "base": {"engine": "Terachess-Stockfish"},
                    }
                }
            )

        self.assertEqual(
            worker.variant_routing(routing(BOOK)),
            ("uci-pair-runner", "terachess"),
        )
        self.assertEqual(
            worker.variant_routing(routing("None")),
            ("uci-pair-runner", "terachess"),
        )


if __name__ == "__main__":
    unittest.main()
