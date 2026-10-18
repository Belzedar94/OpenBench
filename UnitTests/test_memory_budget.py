import contextlib
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Client'))

import worker


MIB = 1024 ** 2

# One Spell-Stockfish engine as the worker measures it (commit charge with
# Hash=32), so one game -- both engines resident -- costs 810 MB.
SPELL_ENGINE_MB = 405


def capped_concurrency(ram_total_mb, available_mb, commit_mb, concurrency_per,
                       cutechess_cnt=1, engine_mb=SPELL_ENGINE_MB):

    # Every reading the cap takes from the machine is mocked: installed RAM
    # (from the Configuration), free memory (psutil), the Windows commit
    # headroom (None off Windows) and the per-engine footprint.
    config = SimpleNamespace(ram_total_mb=ram_total_mb)
    memory = SimpleNamespace(
        total=ram_total_mb * MIB, available=available_mb * MIB
    )
    output = io.StringIO()
    with (
        mock.patch.object(worker, 'engine_memory_mb', return_value=engine_mb),
        mock.patch.object(worker.psutil, 'virtual_memory', return_value=memory),
        mock.patch.object(worker, 'available_commit_mb', return_value=commit_mb),
        contextlib.redirect_stdout(output),
    ):
        capped = worker.memory_capped_concurrency(
            config, 'dev-engine', 'base-engine', cutechess_cnt, concurrency_per
        )
    return capped, output.getvalue()


class MemoryMarginTests(unittest.TestCase):

    def test_margin_is_two_gb_or_an_eighth_of_installed_ram(self):
        self.assertEqual(worker.memory_margin_mb(8192), 2048)
        self.assertEqual(worker.memory_margin_mb(16384), 2048)
        self.assertEqual(worker.memory_margin_mb(24000), 3000)
        self.assertEqual(worker.memory_margin_mb(32768), 4096)
        self.assertEqual(worker.memory_margin_mb(65536), 8192)
        self.assertEqual(worker.memory_margin_mb(131072), 16384)


class MemoryCappedConcurrencyTests(unittest.TestCase):

    def test_big_desktop_uses_its_free_memory_past_the_old_ceiling(self):
        # 26000 - 4096 MB fits 27 games. The old 40% ceiling (13107 MB) held
        # this machine to 16 whatever -T asked for.
        capped, output = capped_concurrency(
            ram_total_mb=32768, available_mb=26000, commit_mb=60000,
            concurrency_per=24,
        )
        self.assertEqual(capped, 24)
        self.assertEqual(output, '')

    def test_big_server_without_commit_limit_uses_its_free_memory(self):
        # Linux reports no commit headroom. 120000 - 16384 MB fits 127 games;
        # the old ceiling (52428 MB) allowed 64.
        capped, output = capped_concurrency(
            ram_total_mb=131072, available_mb=120000, commit_mb=None,
            concurrency_per=120,
        )
        self.assertEqual(capped, 120)
        self.assertEqual(output, '')

    def test_the_server_number_is_never_raised(self):
        capped, _ = capped_concurrency(
            ram_total_mb=131072, available_mb=120000, commit_mb=None,
            concurrency_per=8,
        )
        self.assertEqual(capped, 8)

    def test_margin_dominates_on_a_small_machine(self):
        # 3500 MB free on an 8 GB laptop leaves 1452 MB once the 2 GB margin
        # is kept free: one game, where the server asked for four.
        capped, output = capped_concurrency(
            ram_total_mb=8192, available_mb=3500, commit_mb=None,
            concurrency_per=4,
        )
        self.assertEqual(capped, 1)
        self.assertIn('Memory cap: 4 -> 1', output)
        self.assertIn('budget 1452 MB', output)
        self.assertIn('3500 MB free, 2048 MB margin', output)

    def test_less_free_memory_than_the_margin_still_plays_one_game(self):
        capped, output = capped_concurrency(
            ram_total_mb=8192, available_mb=1500, commit_mb=None,
            concurrency_per=4,
        )
        self.assertEqual(capped, 1)
        self.assertIn('budget 0 MB', output)

    def test_busy_desktop_is_no_longer_floored_at_a_sixth_of_ram(self):
        # 4000 - 2048 MB fits two games. The old floor (a sixth of 16 GB,
        # 2730 MB) would have run three and eaten into the margin.
        capped, _ = capped_concurrency(
            ram_total_mb=16384, available_mb=4000, commit_mb=None,
            concurrency_per=8,
        )
        self.assertEqual(capped, 2)

    def test_windows_commit_headroom_still_clamps_the_budget(self):
        # Physical memory alone would fit 27 games, but only 6000 MB can be
        # committed: 6000 - 2048 MB fits four.
        capped, output = capped_concurrency(
            ram_total_mb=32768, available_mb=26000, commit_mb=6000,
            concurrency_per=24,
        )
        self.assertEqual(capped, 4)
        self.assertIn('Memory cap: 24 -> 4', output)
        self.assertIn('budget 3952 MB', output)
        self.assertIn('6000 MB commit', output)

    def test_exhausted_commit_headroom_still_plays_one_game(self):
        capped, output = capped_concurrency(
            ram_total_mb=32768, available_mb=26000, commit_mb=1000,
            concurrency_per=24,
        )
        self.assertEqual(capped, 1)
        self.assertIn('budget 810 MB', output)

    def test_free_memory_is_read_again_for_every_workload(self):
        readings = []
        for available_mb in (26000, 8000, 26000):
            capped, _ = capped_concurrency(
                ram_total_mb=32768, available_mb=available_mb, commit_mb=None,
                concurrency_per=24,
            )
            readings.append(capped)
        # 8000 - 4096 MB fits four games while the desktop is busy; the next
        # workload gets the whole number back once the memory is free again.
        self.assertEqual(readings, [24, 4, 24])

    def test_copies_share_one_budget(self):
        # 14000 - 4096 MB fits 12 games in all, so six per copy.
        capped, output = capped_concurrency(
            ram_total_mb=32768, available_mb=14000, commit_mb=None,
            concurrency_per=12, cutechess_cnt=2,
        )
        self.assertEqual(capped, 6)
        self.assertIn('Memory cap: 12 -> 6', output)

    def test_an_unmeasurable_engine_leaves_the_server_number(self):
        capped, output = capped_concurrency(
            ram_total_mb=8192, available_mb=1500, commit_mb=None,
            concurrency_per=4, engine_mb=None,
        )
        self.assertEqual(capped, 4)
        self.assertEqual(output, '')

    def test_a_failed_reading_leaves_the_server_number(self):
        config = SimpleNamespace(ram_total_mb=32768)
        output = io.StringIO()
        with (
            mock.patch.object(worker, 'engine_memory_mb', return_value=405),
            mock.patch.object(
                worker.psutil, 'virtual_memory',
                side_effect=RuntimeError('no reading'),
            ),
            contextlib.redirect_stdout(output),
        ):
            capped = worker.memory_capped_concurrency(
                config, 'dev-engine', 'base-engine', 1, 24
            )
        self.assertEqual(capped, 24)
        self.assertIn('Memory cap skipped: no reading', output.getvalue())


if __name__ == '__main__':
    unittest.main()
