import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENGINE_NAME = "TaikyokuShogi-Stockfish"
ENGINE_REF = "b2014c422f4aae7420636ab17884dfb862cf4c15"
CLIENT_REF = "c1ee4b7fa834cc2c6cec47715123e5e4bb18f735"
CAMPAIGN = "taikyoku-material-regime-20260812"
COHORT = "material-n8000-startpos-v1"
TEACHER = "taikyoku-material-advance-v1"
PRIORITY = 400


def load_json(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class TaikyokuOnboardingTests(unittest.TestCase):

    def setUp(self):
        self.general = load_json("Config/config.json")
        self.engine = load_json("Engines/%s.json" % ENGINE_NAME)
        self.presets = self.engine["datagen_presets"]

    def test_public_engine_identity_and_build_are_frozen(self):
        self.assertEqual(self.general["client_version"], 51)
        self.assertEqual(self.general["client_repo_ref"], CLIENT_REF)
        self.assertIn(ENGINE_NAME, self.general["engines"])
        self.assertTrue(self.engine["onboarding_ready"])
        self.assertFalse(self.engine["private"])
        self.assertEqual(self.engine["nps"], 36000)
        self.assertEqual(
            self.engine["source"],
            "https://github.com/Belzedar94/TaikyokuShogi-Stockfish",
        )
        self.assertEqual(self.engine["build"], {
            "path": ".",
            "compilers": ["g++"],
            "cpuflags": [],
            "systems": ["Windows", "Linux"],
        })
        self.assertEqual(self.engine["test_presets"], {"default": {}})
        self.assertEqual(self.engine["tune_presets"], {"default": {}})

    def test_v42_command_freezes_all_publication_inputs(self):
        expected_command = (
            "datagen teacher_id {TEACHER_ID} source_commit {ENGINE_COMMIT} "
            "contract_sha256 {PUBLICATION_CONTRACT_SHA256} producer_sha256 "
            "{PRODUCER_SHA256} book {BOOK} book_sha256 {BOOK_SHA256} "
            "network {NETWORK} network_sha256 {NETWORK_SHA256} format "
            "TK01-v1 nodes 8000 count {COUNT} threads {THREADS} seed "
            "{SEED} out {OUT} random_plies 8 write_min_ply 6 eval_limit "
            "10000 max_game_plies 20000"
        )
        required_placeholders = {
            "TEACHER_ID",
            "ENGINE_COMMIT",
            "PUBLICATION_CONTRACT_SHA256",
            "PRODUCER_SHA256",
            "BOOK",
            "BOOK_SHA256",
            "NETWORK",
            "NETWORK_SHA256",
            "COUNT",
            "THREADS",
            "SEED",
            "OUT",
        }

        for name, preset in self.presets.items():
            with self.subTest(name=name):
                command = preset["datagen_command"]
                self.assertEqual(command, expected_command)
                self.assertEqual(
                    set(re.findall(r"\{([A-Z0-9_]+)\}", command)),
                    required_placeholders,
                )
                self.assertNotIn("TEACHER_MODE", command)
                self.assertNotIn("Syzygy", command)
                self.assertEqual(preset["datagen_publication_protocol"], "42")
                self.assertEqual(preset["dev_branch"], ENGINE_REF)
                self.assertEqual(preset["both_bench"], 9586353)
                self.assertEqual(preset["both_network"], "")
                self.assertEqual(preset["book_name"], "NONE")
                self.assertEqual(preset["datagen_teacher_id"], TEACHER)
                self.assertEqual(preset["datagen_campaign_id"], CAMPAIGN)
                self.assertEqual(preset["datagen_cohort"], COHORT)
                self.assertEqual(preset["priority"], PRIORITY)

    def test_canary_and_regime_have_distinct_frozen_slots(self):
        canary = self.presets["default"]
        regime = self.presets["Material Regime 5.2M"]

        self.assertEqual(canary["datagen_total_count"], 260000)
        self.assertEqual(canary["datagen_positions_per_chunk"], 260000)
        self.assertEqual(canary["datagen_base_seed"], 202608120200000)
        self.assertEqual(canary["datagen_external_workload_id"], "canary-260k")
        self.assertEqual(canary["datagen_role"], "canary")

        self.assertEqual(regime["datagen_total_count"], 5200000)
        self.assertEqual(regime["datagen_positions_per_chunk"], 260000)
        self.assertEqual(
            regime["datagen_total_count"] //
            regime["datagen_positions_per_chunk"],
            20,
        )
        self.assertEqual(regime["datagen_base_seed"], 202608120300000)
        self.assertEqual(regime["datagen_external_workload_id"], "train-5m2")
        self.assertEqual(regime["datagen_role"], "train")

        slots = {
            (
                preset["datagen_campaign_id"],
                preset["datagen_external_workload_id"],
                preset["datagen_role"],
            )
            for preset in self.presets.values()
        }
        self.assertEqual(len(slots), len(self.presets))


if __name__ == "__main__":
    unittest.main()
