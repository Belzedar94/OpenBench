#!/usr/bin/env python3

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Client"))

import uci_pair_worker


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class AlicePairWorkerTests(unittest.TestCase):
    def definition(self, directory):
        root = Path(directory).resolve()
        binary = root / "engine.bin"
        binary.write_bytes(b"pinned executable\n")
        engine = {
            "path": str(binary),
            "binary_sha256": digest(binary),
            "cwd": str(root),
            "name": "Alice-test",
            "evaluator": "Zero",
            "network_sha256": "",
            "time_control": "2+0.02",
            "options": {
                "Threads": "1",
                "Hash": "512",
                "Use NNUE": "false",
                "Alice Evaluation": "Zero",
            },
        }
        return {
            "schema": "alice-pair-worker-definition-v1",
            "engines": [engine, dict(engine)],
            "max_plies": 900,
            "fixed_budget_seconds": 600,
            "stall_grace_seconds": 10,
        }

    def test_valid_definition_freezes_resources_and_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = uci_pair_worker.validate_definition(self.definition(directory))
        self.assertEqual(len(config.specs), 2)
        self.assertEqual(config.specs[0].options["Threads"], "1")
        self.assertEqual(config.specs[0].options["Hash"], "512")
        self.assertTrue(config.acceptance_mode)
        self.assertEqual(config.variant, "alice")

    def test_definition_rejects_unknown_fields_and_implicit_evaluator(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = self.definition(directory)
            definition["ignored"] = True
            with self.assertRaisesRegex(ValueError, "unknown fields"):
                uci_pair_worker.validate_definition(definition)

            definition = self.definition(directory)
            del definition["engines"][0]["options"]["Alice Evaluation"]
            with self.assertRaisesRegex(ValueError, "selected explicitly"):
                uci_pair_worker.validate_definition(definition)

    def test_definition_rejects_noncanonical_hash_and_nonfinite_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = self.definition(directory)
            definition["engines"][0]["binary_sha256"] = definition["engines"][0][
                "binary_sha256"
            ].upper()
            with self.assertRaisesRegex(ValueError, "lowercase"):
                uci_pair_worker.validate_definition(definition)

            definition = self.definition(directory)
            definition["stall_grace_seconds"] = float("inf")
            with self.assertRaisesRegex(ValueError, "finite"):
                uci_pair_worker.validate_definition(definition)

    def test_request_is_exact_normalized_and_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fen = "test-fen"
            request = {
                "schema": "alice-pair-request-v1",
                "pair_ordinal": 3,
                "opening": {
                    "book_line": 1,
                    "raw_line_sha256": "1" * 64,
                    "fen": fen,
                    "fen_sha256": hashlib.sha256(fen.encode("utf-8")).hexdigest(),
                },
                "evidence_directory": str(root / "pair"),
            }
            ordinal, parsed_fen, evidence = uci_pair_worker.validate_request(
                request, set()
            )
            self.assertEqual((ordinal, parsed_fen, evidence), (3, fen, str(root / "pair")))

            with self.assertRaisesRegex(ValueError, "new and non-negative"):
                uci_pair_worker.validate_request(request, {3})
            altered = dict(request)
            altered["ignored"] = True
            with self.assertRaisesRegex(ValueError, "fields"):
                uci_pair_worker.validate_request(altered, set())
            altered = dict(request)
            altered["evidence_directory"] = str(root / "child" / ".." / "pair")
            with self.assertRaisesRegex(ValueError, "normalized"):
                uci_pair_worker.validate_request(altered, set())

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "definition.json"
            path.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                uci_pair_worker.load_json(path)

            nonfinite = Path(directory) / "nonfinite.json"
            nonfinite.write_text('{"value":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-finite JSON number"):
                uci_pair_worker.load_json(nonfinite)


if __name__ == "__main__":
    unittest.main(verbosity=2)
