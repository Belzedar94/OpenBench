#!/usr/bin/env python3

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SHA256 = "3d4b7fd0ab387f4f60da2078f612c9e8890e6026f551aebe8631efc157788f23"
NETWORK = "atomic_run3b_e202_l05.nnue"
NETWORK_SHA256 = "99DC67EABF26A64FAEECA3A88B4C38597A840B8D4A874B9F2CF658C6F92A04A6"
BOOK_SHA256 = "28ED51C2F42E723D5E127D2D3F21C0BFA4A9B318615AFDB299B93EA62DEA2B1E"
BOOKS_V2_RELEASE = (
    "https://github.com/Belzedar94/Atomic-Stockfish/releases/download/"
    "openbench-books-v2-1b-20260721"
)
BOOKS_V2 = {
    "ATOMIC_V3_1B_opening_train.epd": "41edcb8a19b3ba93383f3554db7610431e2068162289d6b6adda99d389fcf3ea",
    "ATOMIC_V3_1B_opening_val.epd": "e5f542f9deb52a42ac134dfb6fd5462e2525b72c6a491775f0faf2068a6f60f8",
    "ATOMIC_V3_1B_opening_test.epd": "1db7affb90ac368635e1cdedef2e8adfbf27b32c8fe5e8855810c2f0863a072b",
    "ATOMIC_V3_1B_midgame_train.epd": "e1625b508003c9030c6a02cf3e70bf357b195cdfdb96b22038850a2dc9b6f476",
    "ATOMIC_V3_1B_midgame_val.epd": "d0352e8694d8142c3e429b2151899aa7edf6c9dd7fba085cacf967dd6a86e215",
    "ATOMIC_V3_1B_midgame_test.epd": "1808560bb5a9ddbc847ed8589cc4240c50af775451baaa3f4ac7a60d7423b9c6",
    "ATOMIC_V3_1B_endgame_train.epd": "fe699812f008205ba1f0b74908fd69604f9e5f63b78318a449c11ba20cf9672a",
    "ATOMIC_V3_1B_endgame_val.epd": "de13ac5d08ee19b3963c58ab14513c1903762cb3a31790eb490cd49c03ac41b6",
    "ATOMIC_V3_1B_endgame_test.epd": "37f2f0b3188e65f38df5fcabffa5f8d772769df80f9d25d3676ed9395a23f03f",
}


def load_json(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class AtomicOnboardingTests(unittest.TestCase):

    def setUp(self):
        self.general = load_json("Config/config.json")
        self.engine = load_json("Engines/Atomic-Stockfish.json")
        self.baseline = load_json("Engines/Fairy-Stockfish-Atomic-Baseline.json")
        self.book = load_json("Books/ATOMIC_openings.epd.json")
        self.syzygy_book = load_json("Books/ATOMIC_syzygy_6man.epd.json")

    def test_engines_and_books_are_registered(self):
        self.assertEqual(
            self.general["client_repo_ref"],
            "3b711d64652f518c4a29ca6506c068173465e903",
        )
        self.assertIn("Atomic-Stockfish", self.general["engines"])
        self.assertIn("Fairy-Stockfish-Atomic-Baseline", self.general["engines"])
        self.assertIn("ATOMIC_openings.epd", self.general["books"])
        self.assertIn("ATOMIC_syzygy_6man.epd", self.general["books"])
        self.assertEqual(
            self.book["sha"],
            "ec3752727cd732a966fd6cb7b3340fb68a726f0b3426d198a3da7b891faa2e91",
        )
        self.assertEqual(self.book["raw_sha"].upper(), BOOK_SHA256)
        self.assertEqual(
            self.syzygy_book["sha"],
            "ad83b0f3b8ee08d0f61f2f9afa11c1c72978ad0462d63a306c32697c92c5b449",
        )

    def test_atomic_v3_1b_books_are_registered_and_pinned(self):
        configured = [name for name in self.general["books"] if name in BOOKS_V2]
        self.assertEqual(configured, list(BOOKS_V2))
        for name, expected_sha in BOOKS_V2.items():
            self.assertLessEqual(len(name), 32, name)
            descriptor = load_json("Books/%s.json" % name)
            self.assertEqual(descriptor["sha"], expected_sha, name)
            self.assertEqual(descriptor["raw_sha"], expected_sha, name)
            self.assertEqual(
                descriptor["source"],
                "%s/%s.zip" % (BOOKS_V2_RELEASE, name),
                name,
            )

    def test_atomic_build_defaults_and_tablebase_pin(self):
        defaults = self.engine["test_presets"]["default"]
        self.assertEqual(self.engine["source"], "https://github.com/Belzedar94/Atomic-Stockfish")
        self.assertEqual(self.engine["nps"], 1338320)
        self.assertEqual(self.engine["cutechess_max_concurrency"], 8)
        self.assertEqual(self.engine["cutechess_launch_stagger_ms"], 1500)
        self.assertEqual(self.engine["build"]["path"], "src")
        self.assertEqual(self.engine["build"]["compilers"], ["g++"])
        self.assertIn("BMI2", self.engine["build"]["cpuflags"])
        self.assertEqual(set(self.engine["build"]["systems"]), {"Windows", "Linux"})
        self.assertEqual(self.engine["tablebase_family"], "atomic")
        self.assertEqual(self.engine["tablebase_manifest_sha256"], MANIFEST_SHA256)
        self.assertEqual(defaults["base_branch"], "main")
        self.assertEqual(defaults["both_bench"], 338376)
        self.assertEqual(defaults["both_network"], NETWORK)
        self.assertEqual(defaults["test_bounds"], "[1.00, 4.00]")
        self.assertEqual(defaults["win_adj"], "movecount=4 score=800")
        self.assertEqual(defaults["draw_adj"], "movenumber=40 movecount=8 score=10")

    def test_frozen_fairy_baseline_contract(self):
        defaults = self.baseline["test_presets"]["default"]
        self.assertEqual(
            self.baseline["source"], "https://github.com/Belzedar94/Fairy-Stockfish"
        )
        self.assertEqual(self.baseline["nps"], 1165682)
        self.assertEqual(self.baseline["cutechess_max_concurrency"], 8)
        self.assertEqual(self.baseline["cutechess_launch_stagger_ms"], 1500)
        self.assertEqual(defaults["base_branch"], "atomic-openbench-baseline")
        self.assertEqual(defaults["both_bench"], 97362)
        self.assertEqual(defaults["both_network"], NETWORK)
        self.assertEqual(defaults["test_bounds"], "[1.00, 6.00]")
        self.assertEqual(defaults["win_adj"], "movecount=4 score=800")
        self.assertEqual(self.baseline["tablebase_family"], "atomic")
        self.assertEqual(self.baseline["tablebase_manifest_sha256"], MANIFEST_SHA256)
        for name, preset in self.baseline["test_presets"].items():
            if name == "default":
                continue
            self.assertIn("UCI_Variant=atomic", preset["both_options"], name)
            self.assertIn('"Use NNUE=', preset["both_options"], name)
            self.assertNotIn("Use NNUE=pure", preset["both_options"], name)

    def test_four_fixed_game_syzygy_presets(self):
        presets = {
            name: data
            for name, data in self.engine["test_presets"].items()
            if name.startswith("Syzygy ")
        }
        expected = {
            "Syzygy STC NNUE": ("8.0+0.08", "Hash=32", "Use NNUE=true"),
            "Syzygy LTC NNUE": ("40.0+0.4", "Hash=128", "Use NNUE=true"),
            "Syzygy STC classical": ("8.0+0.08", "Hash=32", "Use NNUE=false"),
            "Syzygy LTC classical": ("40.0+0.4", "Hash=128", "Use NNUE=false"),
        }
        self.assertEqual(set(presets), set(expected))

        defaults = self.engine["test_presets"]["default"]
        for name, preset in presets.items():
            time_control, hash_option, eval_option = expected[name]
            effective = {**defaults, **preset}
            self.assertEqual(effective["both_bench"], 338376, name)
            self.assertEqual(effective["both_network"], NETWORK, name)
            self.assertNotIn("test_mode", preset, name)
            self.assertEqual(preset["test_max_games"], 2000, name)
            self.assertEqual(preset["both_time_control"], time_control, name)
            self.assertEqual(preset["workload_size"], 1, name)
            self.assertEqual(preset["book_name"], "ATOMIC_syzygy_6man.epd", name)
            self.assertEqual(preset["syzygy_wdl"], "6-MAN", name)
            self.assertEqual(preset["syzygy_adj"], "DISABLED", name)

            for key, limit in (("dev_options", 6), ("base_options", 0)):
                options = preset[key]
                self.assertIn("Threads=1", options, name)
                self.assertIn(hash_option, options, name)
                self.assertIn(eval_option, options, name)
                self.assertIn(f'"{eval_option}"', options, name)
                self.assertIn(f"SyzygyProbeLimit={limit}", options, name)
                self.assertIn("SyzygyProbeDepth=1", options, name)
                self.assertIn("Syzygy50MoveRule=true", options, name)
                self.assertNotIn("Use NNUE=pure", options, name)

            self.assertEqual(
                preset["dev_options"].replace("SyzygyProbeLimit=6", ""),
                preset["base_options"].replace("SyzygyProbeLimit=0", ""),
                name,
            )

    def test_no_playing_preset_uses_pure(self):
        for name, preset in self.engine["test_presets"].items():
            for key, value in preset.items():
                if key.endswith("_options"):
                    self.assertNotIn("Use NNUE=pure", value, name)

    def test_atomic_bootstrap_datagen_preset_is_frozen(self):
        preset = self.engine["datagen_presets"]["default"]
        command = preset["datagen_command"]
        self.assertEqual(preset["both_bench"], 338376)
        self.assertEqual(preset["both_network"], NETWORK)
        self.assertEqual(preset["book_name"], "ATOMIC_openings.epd")
        self.assertEqual(preset["datagen_total_count"], 500_000_000)
        self.assertEqual(preset["datagen_positions_per_chunk"], 12_500_000)
        self.assertEqual(preset["datagen_base_seed"], 202_607_150_500_000)
        self.assertEqual(preset["priority"], 100)
        for placeholder in (
            "{SEED}",
            "{COUNT}",
            "{OUT}",
            "{THREADS}",
            "{BOOK}",
            "{BOOK_SHA256}",
            "{NETWORK}",
        ):
            self.assertIn(placeholder, command)
        for option in (
            "depth 6",
            "eval_limit 10000",
            "eval_diff_limit 32000",
            "random_multi_pv 4",
            "random_multi_pv_diff 200",
            "filter_captures true",
            "filter_checks false",
            "filter_promotions true",
        ):
            self.assertIn(option, command)
        self.assertIn("network_sha256 " + NETWORK_SHA256, command)
        self.assertIn("book_sha256 {BOOK_SHA256}", command)

    def test_atomic_syzygy_datagen_canaries_are_explicit_and_separate(self):
        presets = self.engine["datagen_presets"]
        expected = {
            "Atomic Syzygy depth-7 pure canary": "pure",
            "Atomic Syzygy depth-7 true canary": "true",
        }
        self.assertEqual(
            {name for name in presets if name.endswith("canary")}, set(expected)
        )

        for name, teacher_mode in expected.items():
            preset = presets[name]
            command = preset["datagen_command"]
            self.assertEqual(preset["datagen_teacher_mode"], teacher_mode)
            self.assertEqual(preset["syzygy_wdl"], "6-MAN")
            self.assertEqual(preset["syzygy_adj"], "DISABLED")
            self.assertIn("out {OUT} depth 7 nodes 0", command)
            self.assertNotIn("out {OUT} depth 6 nodes 0", command)
            for placeholder in (
                "{PRODUCER_SHA256}",
                "{SYZYGY}",
                "{SYZYGY_MANIFEST_SHA256}",
                "{SYZYGY_MAX}",
                "{TEACHER_MODE}",
            ):
                self.assertIn(placeholder, command, name)
            self.assertIn(
                "producer_sha256 {PRODUCER_SHA256}", command
            )
            self.assertIn('syzygy "{SYZYGY}"', command)
            self.assertIn(
                "syzygy_manifest_sha256 {SYZYGY_MANIFEST_SHA256}", command
            )
            self.assertIn("syzygy_max {SYZYGY_MAX}", command)
            self.assertIn("teacher_mode {TEACHER_MODE}", command)

    def test_atomic_v3_1b_preset_contains_only_campaign_invariants(self):
        preset = self.engine["datagen_presets"][
            "Atomic V3 1B pure depth-7 Syzygy6 invariant-only"
        ]
        self.assertEqual(
            preset["dev_branch"], "01a74371d6c947bb03ae12a8cff6a35044d3aa0b"
        )
        self.assertEqual(preset["dev_bench"], 338376)
        self.assertEqual(preset["dev_network"], NETWORK)
        self.assertEqual(preset["book_name"], "NONE")
        self.assertEqual(preset["datagen_total_count"], "")
        self.assertEqual(preset["datagen_positions_per_chunk"], "")
        self.assertEqual(preset["datagen_base_seed"], "")
        self.assertEqual(preset["datagen_teacher_mode"], "pure")
        self.assertEqual(preset["priority"], 6)
        self.assertEqual(preset["datagen_publication_protocol"], "41")
        self.assertEqual(
            preset["datagen_campaign_id"],
            "atomic-v3-run3b-d7-syzygy6-1b-20260719-final",
        )
        for field in (
            "datagen_external_workload_id",
            "datagen_role",
            "datagen_cohort",
        ):
            self.assertEqual(preset[field], "", field)
        self.assertEqual(preset["syzygy_wdl"], "6-MAN")
        self.assertEqual(preset["syzygy_adj"], "DISABLED")
        command = preset["datagen_command"]
        self.assertIn("depth 7 nodes 0", command)
        self.assertIn("teacher_mode {TEACHER_MODE}", command)
        self.assertNotIn("depth 6 nodes 0", command)

    def test_dedicated_datagen_form_renders_v41_publication_fields(self):
        template = (ROOT / "Templates/OpenBench/create_workload.html").read_text(
            encoding="utf-8"
        )
        dedicated = template.split('{% elif workload == "DATAGEN" %}', 1)[1]
        for field in (
            "datagen_teacher_mode",
            "datagen_publication_protocol",
            "datagen_campaign_id",
            "datagen_external_workload_id",
            "datagen_role",
            "datagen_cohort",
        ):
            self.assertIn('id="%s"' % field, dedicated, field)


if __name__ == "__main__":
    unittest.main()
