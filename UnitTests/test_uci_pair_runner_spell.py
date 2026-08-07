#!/usr/bin/env python3
"""Coverage for the spell-chess path of Client/uci_pair_runner.py.

uci_pair_runner.py arbitrates every Spell-Stockfish game on OpenBench -- there
is no cutechess in that path -- and until now nothing in UnitTests/ executed a
single line of it. test_runner_startup.py covers worker.py's *routing* to the
runner; test_alice_pair_worker.py covers Client/uci_pair_worker.py, a different
file. This module covers the runner itself, on the spell command line the
worker actually builds.

The engine is a duck-typed stand-in for uci_pair_runner.Engine, so these tests
need no binary, no network and no wall clock: every arbitration branch is
reached on demand and the assertions are exact. The one place a real subprocess
would add signal -- Engine.search()'s UCI line parsing -- is covered with a
scripted pipe instead.

What is pinned here:
  * the OpenBench stdout contract: 'Started game', 'Finished game N (W vs B):
    R {reason}', 'Score of'; every line ASCII and non-blank; dev plays White in
    odd games and pairs are (1,2),(3,4),...; the exact token positions
    worker.Cutechess.update_results() indexes into, and the reason substrings
    it counts ('disconnect', 'stalls', 'on time', 'illegal');
  * every game-ending branch reachable on the spell path: variant terminal
    (mate and stalemate), resign adjudication, draw adjudication, ply cap,
    illegal move, engine death, engine stall, time forfeit;
  * spell-specific input handling: gated/dropped bestmoves such as
    'f@e4,d2d4' must never be mistaken for illegal moves, and spell book FENs
    carry a '{...}' state token between board and side-to-move;
  * PGN shape for spell: [Variant "spell-chess"] follows -variant, and none of
    the Alice evidence headers (OutcomeClass/FailureCode/ShadowAdjudication)
    appear;
  * the Alice gate stays shut for spell: the worker's spell command line arms
    neither shadow adjudication nor acceptance mode, an 'info string
    alice_result' line cannot change a spell verdict, and shadow=true on
    -resign/-draw is refused at startup for any variant outside
    SHADOW_VARIANTS instead of silently playing an unadjudicated batch;
  * opening selection is a pure function of -srand/start/order, and -repeat
    gives both games of a pair the same FEN.

What is NOT covered here (deliberately):
  * anything alice-only -- acceptance mode, shadow adjudication, the machine
    evidence JSONL, the strict terminal records. Those belong in an alice test;
  * concurrency > 1. The runner interleaves stdout across threads by design,
    so line order is not a contract and is not asserted;
  * real engine determinism or Elo. That is an SPRT question, not a unit test;
  * the optional real-binary smoke at the bottom, which only runs when
    SPELL_RUNNER_ENGINE points at a Spell-Stockfish executable.
"""

import io
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Client"))

import uci_pair_runner as runner


# A real spell book line: pockets in '[...]', then a '{...}' gate-state token
# between the board and the side to move.
SPELL_FEN_W = (
    "rnbqkbnr/ppppppp1/8/7p/8/5P2/PPPPP1PP/RNBQKBNR[JJFFFFFjjffff] "
    "{F@-:0,J@-:0,f@d4:3,j@-:0} w KQkq - 0 2"
)
SPELL_FEN_B = (
    "1nbqkbnr/pppppppp/7R/8/r7/8/PPPPPPPP/RNBQKBN1[JFFFFFjfffff] "
    "{F@-:0,J@-:2,f@-:0,j@a7:3} b Qk - 2 2"
)

# Bestmoves the spell engine really produces: plain, gated, gated+promotion.
SPELL_MOVES = ["e2e4", "f@e4,d2d4", "j@h4,e1f1", "f@e4,e7e8q", "e8g8", "0000"]


class _Recorder(io.StringIO):
    """main() closes stdout on its way out; keep the buffer readable."""

    def close(self):
        pass


class Step:
    """One scripted engine reply."""

    def __init__(self, best="e2e4", cp=0, mate=None, depth=8, seldepth=9,
                 nodes=1234, elapsed_ms=10.0, raises=None):
        self.best = best
        self.cp = cp
        self.mate = mate
        self.depth = depth
        self.seldepth = seldepth
        self.nodes = nodes
        self.elapsed_ms = elapsed_ms
        self.raises = raises


def scripted_engine(plan):
    """Build a stand-in for runner.Engine driven by plan(name, ply, game)."""

    class ScriptedEngine:
        booted = []

        def __init__(self, spec, debug=False):
            self.spec = spec
            self.name = spec.name
            self.ply = 0
            self.game = 0
            ScriptedEngine.booted.append(spec.name)

        def new_game(self):
            self.game += 1
            self.ply = 0

        def quit(self):
            pass

        def search(self, pos_cmd, go_cmd, budget_s, stall_grace, strict=False):
            step = plan(self.name, self.ply, self.game)
            self.ply += 1
            if step.raises is not None:
                raise step.raises("%s: scripted failure" % self.name)
            info = {
                "cp": step.cp,
                "raw_mate": step.mate,
                "depth": step.depth,
                "seldepth": step.seldepth,
                "nodes": step.nodes,
                "terminal": None,
            }
            return step.best, info, step.elapsed_ms

    return ScriptedEngine


def always(**kwargs):
    step = Step(**kwargs)
    return lambda name, ply, game: step


class SpellRunnerCase(unittest.TestCase):
    """Drives runner.main() end to end with a scripted engine pair."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.pgn = self.dir / "out.pgn"
        self.book = self.dir / "book.epd"
        self.write_book([SPELL_FEN_W])

    def write_book(self, fens):
        self.book.write_text("\n".join(fens) + "\n", encoding="ascii")

    def spell_argv(self, games=2, extra=(), engine_extra=(), adjudication=()):
        """The flag surface Client/worker.py builds for a SPELL workload."""
        argv = ["-repeat", "-recover", "-variant", "spell-chess",
                "-concurrency", "1", "-games", str(games)]
        argv += list(adjudication)
        for branch in ("dev", "base"):
            argv += ["-engine", "dir=%s" % self.dir, "cmd=./engine",
                     "proto=uci", "tc=inf", "nodes=20000",
                     "option.Threads=1", "option.Hash=8"]
            argv += list(engine_extra)
            argv += ["name=Spell-Stockfish-%s" % branch]
        argv += ["-openings", "file=%s" % self.book, "format=epd",
                 "order=random", "start=1", "-srand", "42",
                 "-pgnout", str(self.pgn)]
        argv += list(extra)
        return argv

    def run_match(self, argv, plan):
        out, err = _Recorder(), io.StringIO()
        cls = scripted_engine(plan)
        with mock.patch.object(runner, "Engine", cls), \
                mock.patch.object(sys, "argv", ["uci_pair_runner.py"] + argv), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            try:
                runner.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    # -- helpers ----------------------------------------------------------

    def finished(self, stdout):
        """[(game_no, result, reason)] from the lines the worker parses."""
        rows = []
        for line in stdout.splitlines():
            m = re.match(
                r"^Finished game (\d+) \((\S+) vs (\S+)\): (\S+) \{(.*)\}$",
                line)
            if m:
                rows.append((int(m.group(1)), m.group(4), m.group(5)))
        return rows

    def pgn_games(self):
        text = self.pgn.read_text(encoding="ascii")
        games, cur = [], {}
        for line in text.splitlines():
            m = re.match(r'^\[(\w+) "(.*)"\]$', line)
            if m:
                cur[m.group(1)] = m.group(2)
            elif not line.strip() and cur:
                games.append(cur)
                cur = {}
        if cur:
            games.append(cur)
        return games


class WorkerStdoutContractTests(SpellRunnerCase):

    def test_pairing_and_result_lines_match_what_the_worker_parses(self):
        code, out, _ = self.run_match(
            self.spell_argv(games=4),
            always(best="0000", cp=-runner.MATE_ISH))
        self.assertEqual(code, 0)

        rows = self.finished(out)
        self.assertEqual([r[0] for r in rows], [1, 2, 3, 4])

        # worker.Cutechess.update_results indexes fixed token positions
        for line in out.splitlines():
            if not line.startswith("Finished game"):
                continue
            tokens = line.split()
            self.assertEqual(int(tokens[2]), int(line.split()[2]))
            self.assertIn(tokens[6], ("1-0", "0-1", "1/2-1/2"))
            # and it slices the reason off the first colon
            self.assertGreaterEqual(len(line.split(":")), 2)

        # dev is White in odd games, Black in even ones
        starts = [l for l in out.splitlines() if l.startswith("Started game")]
        self.assertEqual(len(starts), 4)
        self.assertIn("(Spell-Stockfish-dev vs Spell-Stockfish-base)", starts[0])
        self.assertIn("(Spell-Stockfish-base vs Spell-Stockfish-dev)", starts[1])
        self.assertIn("(Spell-Stockfish-dev vs Spell-Stockfish-base)", starts[2])

        self.assertTrue(any(l.startswith("Score of") for l in out.splitlines()))
        self.assertTrue(
            any(l.startswith("Finished match") for l in out.splitlines()))

    def test_every_stdout_line_is_ascii_and_non_blank(self):
        _, out, _ = self.run_match(self.spell_argv(games=2),
                                   always(best="0000", cp=-runner.MATE_ISH))
        self.assertTrue(out.endswith("\n"))
        for line in out.splitlines():
            self.assertTrue(line.strip(), "blank line on stdout")
            line.encode("ascii")  # raises if the runner ever leaks non-ASCII

    def test_engine_names_stay_single_tokens(self):
        """worker.parse_finished_game() reads tokens[2] and tokens[6]."""
        spec = runner.EngineSpec.from_settings(
            {"cmd": "./engine", "dir": str(self.dir), "nodes": "1",
             "name": "Spell (dev):{x} y"}, 1)
        self.assertNotIn(" ", spec.name)
        for bad in "(){}: ":
            self.assertNotIn(bad, spec.name)


class TerminalAndAdjudicationTests(SpellRunnerCase):

    def test_variant_terminal_losing_score_is_a_mate(self):
        _, out, _ = self.run_match(
            self.spell_argv(games=1),
            always(best="(none)", cp=-runner.MATE_ISH))
        self.assertEqual(self.finished(out), [(1, "0-1", "Black mates")])
        self.assertEqual(self.pgn_games()[0].get("Termination"), None)

    def test_variant_terminal_quiet_score_is_a_stalemate_draw(self):
        _, out, _ = self.run_match(
            self.spell_argv(games=1), always(best="(none)", cp=0))
        self.assertEqual(
            self.finished(out), [(1, "1/2-1/2", "Draw by stalemate")])

    def test_zero_move_and_none_spellings_are_both_terminals(self):
        for spelling in runner.NONE_MOVES:
            with self.subTest(spelling=spelling):
                self.pgn.unlink(missing_ok=True)
                _, out, _ = self.run_match(
                    self.spell_argv(games=1),
                    always(best=spelling, cp=-runner.MATE_ISH))
                self.assertEqual(self.finished(out)[0][1], "0-1")

    def test_resign_adjudication_fires_on_the_side_that_is_losing(self):
        _, out, _ = self.run_match(
            self.spell_argv(
                games=1,
                adjudication=["-resign", "movecount=3", "score=400"]),
            always(best="e2e4", cp=-500))
        self.assertEqual(
            self.finished(out),
            [(1, "0-1", "Black wins by adjudication")])
        game = self.pgn_games()[0]
        self.assertEqual(game["Termination"], "adjudication")
        # three of White's own moves below -400, so White has moved 3 times
        self.assertEqual(game["PlyCount"], "5")

    def test_draw_adjudication_fires_after_the_ply_threshold(self):
        _, out, _ = self.run_match(
            self.spell_argv(
                games=1,
                adjudication=["-draw", "movenumber=2", "movecount=2",
                              "score=10"]),
            always(best="e2e4", cp=0))
        self.assertEqual(
            self.finished(out), [(1, "1/2-1/2", "Draw by adjudication")])
        self.assertEqual(self.pgn_games()[0]["PlyCount"], "4")

    def test_ply_cap_ends_the_game_as_a_draw(self):
        _, out, _ = self.run_match(
            self.spell_argv(games=1, extra=["--max-plies", "6"]),
            always(best="e2e4", cp=0))
        self.assertEqual(
            self.finished(out),
            [(1, "1/2-1/2", "Draw by adjudication: max game length")])
        self.assertEqual(self.pgn_games()[0]["PlyCount"], "6")

    def test_mate_score_does_not_trip_the_draw_counter(self):
        """raw_mate set means the position is not quiet, whatever cp says."""
        _, out, _ = self.run_match(
            self.spell_argv(
                games=1,
                extra=["--max-plies", "8"],
                adjudication=["-draw", "movenumber=1", "movecount=1",
                              "score=100000"]),
            always(best="e2e4", cp=0, mate=5))
        self.assertEqual(
            self.finished(out)[0][2], "Draw by adjudication: max game length")


class FailureReasonTests(SpellRunnerCase):
    """The four substrings worker.Cutechess.update_results() counts."""

    def reason_of(self, out):
        return self.finished(out)[0][2]

    def test_illegal_move(self):
        _, out, _ = self.run_match(self.spell_argv(games=1),
                                   always(best="!!not-a-move!!"))
        self.assertIn("illegal", self.reason_of(out))
        self.assertEqual(self.pgn_games()[0]["Termination"], "illegal move")

    def test_engine_death_reports_disconnect(self):
        _, out, _ = self.run_match(
            self.spell_argv(games=1),
            always(raises=runner.EngineDied))
        self.assertIn("disconnect", self.reason_of(out))
        self.assertEqual(self.pgn_games()[0]["Termination"], "abandoned")

    def test_engine_stall_reports_stalls(self):
        _, out, _ = self.run_match(
            self.spell_argv(games=1),
            always(raises=runner.EngineStalled))
        self.assertIn("stalls", self.reason_of(out))
        self.assertEqual(
            self.pgn_games()[0]["Termination"], "stalled connection")

    def test_overrunning_a_movetime_budget_reports_on_time(self):
        argv = self.spell_argv(games=1)
        argv = [a.replace("nodes=20000", "st=0.05") for a in argv]
        argv = [a for a in argv if a != "tc=inf"]
        _, out, _ = self.run_match(argv, always(best="e2e4", elapsed_ms=5000.0))
        self.assertIn("on time", self.reason_of(out))
        self.assertEqual(self.pgn_games()[0]["Termination"], "time forfeit")

    def test_a_dead_engine_loses_and_its_opponent_wins(self):
        """Only dev dies; it must lose as White and lose again as Black."""

        def plan(name, ply, game):
            if name.endswith("-dev"):
                return Step(raises=runner.EngineDied)
            return Step(best="e2e4")

        _, out, _ = self.run_match(self.spell_argv(games=2), plan)
        rows = self.finished(out)
        self.assertEqual(rows[0][1], "0-1")   # dev is White in game 1
        self.assertEqual(rows[1][1], "1-0")   # dev is Black in game 2
        self.assertIn("disconnect", rows[0][2])
        self.assertIn("disconnect", rows[1][2])


class SpellInputHandlingTests(SpellRunnerCase):

    def test_gated_and_dropped_bestmoves_are_not_illegal_moves(self):
        for move in SPELL_MOVES:
            if move in runner.NONE_MOVES:
                continue
            with self.subTest(move=move):
                self.assertTrue(runner.MOVE_RE.match(move), move)

    def test_a_game_made_of_gated_moves_is_arbitrated_normally(self):
        def plan(name, ply, game):
            if ply >= len(SPELL_MOVES) - 1:
                return Step(best="(none)", cp=-runner.MATE_ISH)
            return Step(best=SPELL_MOVES[ply])

        _, out, _ = self.run_match(self.spell_argv(games=1), plan)
        self.assertNotIn("illegal", out)
        self.assertEqual(self.finished(out)[0][1], "0-1")
        movetext = self.pgn.read_text(encoding="ascii")
        self.assertIn("f@e4,d2d4", movetext)
        self.assertIn("f@e4,e7e8q", movetext)

    def test_spell_fens_with_a_state_token_are_parsed_for_side_to_move(self):
        self.assertEqual(runner._fen_fields(SPELL_FEN_W), ("w", 2))
        self.assertEqual(runner._fen_fields(SPELL_FEN_B), ("b", 2))

    def test_a_black_to_move_opening_starts_the_movetext_at_black(self):
        self.write_book([SPELL_FEN_B])
        _, out, _ = self.run_match(
            self.spell_argv(games=1, extra=["--max-plies", "3"]),
            always(best="e2e4", cp=0))
        game = self.pgn_games()[0]
        self.assertEqual(game["FEN"], SPELL_FEN_B)
        self.assertEqual(game["PlyCount"], "3")
        body = self.pgn.read_text(encoding="ascii").split("\n\n", 1)[1]
        self.assertTrue(body.lstrip().startswith("1..."), body[:40])


class PgnAndGateTests(SpellRunnerCase):

    def test_pgn_variant_header_follows_the_variant_flag(self):
        self.run_match(self.spell_argv(games=1),
                       always(best="(none)", cp=-runner.MATE_ISH))
        game = self.pgn_games()[0]
        self.assertEqual(game["Variant"], "spell-chess")
        self.assertEqual(game["FEN"], SPELL_FEN_W)
        self.assertEqual(game["Event"], "uci_pair_runner")

    def test_shadow_headers_never_appear_on_the_spell_path(self):
        """Shadow adjudication is armed only by shadow=true on -resign/-draw,
        which no spell preset sets. It must stay off even on crash games."""
        self.run_match(self.spell_argv(games=2),
                       always(raises=runner.EngineDied))
        for game in self.pgn_games():
            self.assertNotIn("ShadowAdjudication", game)
            self.assertNotIn("ShadowInversion", game)

    def test_a_clean_spell_game_carries_no_evidence_headers(self):
        self.run_match(self.spell_argv(games=2),
                       always(best="(none)", cp=-runner.MATE_ISH))
        for game in self.pgn_games():
            for header in ("OutcomeClass", "FailureCode", "FailureStage",
                           "OffendingMove", "ShadowAdjudication",
                           "ShadowInversion"):
                self.assertNotIn(header, game)

    def test_a_crashed_spell_game_still_classifies_as_a_disconnect(self):
        """Pins the one ungated post-Alice delta on the spell path.

        write_pgn() emits OutcomeClass/FailureCode/FailureStage/OffendingMove
        for any non-scorable outcome, and play_game() sets those fields whether
        or not the Alice gate is open -- so a spell crash PGN grew four headers
        that the pre-Alice runner did not write. That is cosmetic:
        pgn_util.pgn_strip_headers() drops them on upload, and the worker
        classifies errors off [Termination] alone. This test pins the part that
        must not move, and tolerates the extra headers only with the values the
        runner is supposed to produce.
        """
        self.run_match(self.spell_argv(games=2),
                       always(raises=runner.EngineDied))
        for game in self.pgn_games():
            self.assertEqual(game["Termination"], "abandoned")
            self.assertEqual(game["Variant"], "spell-chess")
            if "OutcomeClass" in game:
                self.assertEqual(game["OutcomeClass"], "OPERATIONAL_ABORT")
                self.assertEqual(game["FailureCode"], "engine-died")
                self.assertEqual(game["FailureStage"], "search")
                self.assertEqual(game["OffendingMove"], "")

    def test_evidence_headers_do_not_disturb_the_workers_error_slicing(self):
        """worker.PGNHelper reads headers by prefix until the blank line."""
        self.run_match(self.spell_argv(games=2),
                       always(raises=runner.EngineStalled))
        text = self.pgn.read_text(encoding="ascii")
        blocks = [b for b in text.split("\n\n") if b.strip()]
        self.assertEqual(len(blocks) % 2, 0)  # header block + movetext block
        for headers in blocks[0::2]:
            lines = headers.splitlines()
            self.assertTrue(all(l.startswith("[") and l.endswith("]")
                                for l in lines), lines)
            found = [l.split('"')[1] for l in lines
                     if l.startswith("[Termination ")]
            self.assertEqual(found, ["stalled connection"])

    def test_the_spell_command_line_arms_neither_shadow_nor_acceptance(self):
        cfg = runner.parse_cli(self.spell_argv(
            games=2,
            adjudication=["-resign", "movecount=3", "score=400",
                          "-draw", "movenumber=40", "movecount=8",
                          "score=10"]))
        self.assertEqual(cfg.variant, "spell-chess")
        self.assertFalse(getattr(cfg, "shadow_adjudication", False))
        self.assertFalse(getattr(cfg, "acceptance_mode", False))
        self.assertIsNone(getattr(cfg, "result_jsonl", None))
        self.assertEqual(cfg.resign, {"movecount": 3, "score": 400})
        self.assertEqual(cfg.draw,
                         {"movenumber": 40, "movecount": 8, "score": 10})
        for spec in cfg.specs:
            self.assertEqual(spec.options["UCI_Variant"], "spell-chess")
            self.assertEqual(spec.options["Threads"], "1")
            self.assertEqual(spec.tc.kind, runner.TimeControl.FIXED)
            self.assertEqual(spec.tc.nodes, 20000)

    def test_an_explicit_uci_variant_option_wins_over_the_flag(self):
        cfg = runner.parse_cli(self.spell_argv(
            games=2, engine_extra=["option.UCI_Variant=spell-chess"]))
        for spec in cfg.specs:
            self.assertEqual(spec.options["UCI_Variant"], "spell-chess")

    def test_finished_match_summary_is_not_part_of_the_result_contract(self):
        """The worker prints this line; it never parses it. Shape may grow."""
        _, out, _ = self.run_match(self.spell_argv(games=2),
                                   always(best="(none)", cp=-runner.MATE_ISH))
        summary = [l for l in out.splitlines()
                   if l.startswith("Finished match")]
        self.assertEqual(len(summary), 1)
        self.assertRegex(
            summary[0],
            r"^Finished match: \S+ vs \S+: \d+ - \d+ - \d+ "
            r"penta \[\d+,\d+,\d+,\d+,\d+\] elo [-+]?[\d.]+ los [\d.]+%")


class OpeningSelectionTests(SpellRunnerCase):

    def test_the_sequence_is_a_pure_function_of_the_seed(self):
        a = runner.build_opening_sequence(100, "random", 1, 42, 20)
        b = runner.build_opening_sequence(100, "random", 1, 42, 20)
        c = runner.build_opening_sequence(100, "random", 1, 43, 20)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 20)

    def test_start_offsets_into_the_same_shuffle(self):
        full = runner.build_opening_sequence(100, "random", 1, 42, 20)
        tail = runner.build_opening_sequence(100, "random", 11, 42, 10)
        self.assertEqual(full[10:], tail)

    def test_sequential_order_walks_the_book(self):
        self.assertEqual(
            runner.build_opening_sequence(3, "sequential", 1, 42, 5),
            [0, 1, 2, 0, 1])

    def test_repeat_gives_both_games_of_a_pair_the_same_opening(self):
        self.write_book([SPELL_FEN_W, SPELL_FEN_B])
        self.run_match(self.spell_argv(games=4),
                       always(best="(none)", cp=-runner.MATE_ISH))
        fens = [g["FEN"] for g in self.pgn_games()]
        self.assertEqual(len(fens), 4)
        self.assertEqual(fens[0], fens[1])
        self.assertEqual(fens[2], fens[3])

    def test_book_comments_and_epd_opcodes_are_dropped(self):
        self.write_book(["# a comment", SPELL_FEN_W + " ; id \"x\""])
        book = runner.load_book(str(self.book))
        self.assertEqual(book, [SPELL_FEN_W])


class EngineLineParsingTests(unittest.TestCase):
    """Engine.search() over a scripted pipe: no binary, real parsing code."""

    def boot(self, lines):
        class FakeStdin:
            def __init__(self):
                self.sent = []

            def write(self, data):
                self.sent.append(data)

            def flush(self):
                pass

            def close(self):
                pass

        class FakeProc:
            def __init__(self):
                self.stdin = FakeStdin()
                self.stdout = iter(lines)

            def poll(self):
                return None

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

        spec = runner.EngineSpec()
        spec.name = "Spell-Stockfish-dev"
        spec.path = "engine"
        spec.cwd = "."
        spec.options = {"Threads": "1"}
        spec.tc = runner.TimeControl()
        proc = FakeProc()
        with mock.patch.object(runner.subprocess, "Popen", return_value=proc):
            engine = runner.Engine(spec)
        return engine

    def search(self, extra_lines):
        lines = ["uciok\n", "readyok\n"] + extra_lines
        engine = self.boot(lines)
        return engine.search("position startpos", "go nodes 20000", 60.0, 10.0)

    def test_a_normal_info_score_reaches_the_arbiter(self):
        best, info, _ = self.search([
            "info depth 7 seldepth 9 score cp 42 nodes 20001 pv e2e4\n",
            "bestmove e2e4\n",
        ])
        self.assertEqual(best, "e2e4")
        self.assertEqual(info["cp"], 42)
        self.assertEqual(info["depth"], 7)
        self.assertEqual(info["nodes"], 20001)

    def test_a_mate_score_is_folded_and_kept_raw(self):
        _, info, _ = self.search([
            "info depth 3 score mate -2 nodes 500\n",
            "bestmove e2e4\n",
        ])
        self.assertEqual(info["raw_mate"], -2)
        self.assertLessEqual(info["cp"], -runner.MATE_ISH + 10)

    def test_an_alice_result_line_cannot_change_a_spell_search(self):
        """Spell never emits this line; if it ever did it must stay inert.

        The post-Alice runner does record the record into info['terminal'],
        ungated -- but play_game() only reads that key under
        strict_alice_evidence, which spell never sets. So the contract is that
        the *search result* is untouched, not that the key is absent.
        """
        best, info, _ = self.search([
            "info string alice_result result=1-0 reason=checkmate\n",
            "info depth 5 seldepth 6 score cp 25 nodes 800 pv e2e4\n",
            "bestmove e2e4\n",
        ])
        self.assertEqual(best, "e2e4")
        self.assertEqual(info["cp"], 25)
        self.assertIsNone(info["raw_mate"])
        self.assertEqual(info["depth"], 5)
        self.assertEqual(info["nodes"], 800)

    def test_a_malformed_alice_result_line_is_ignored_not_fatal(self):
        best, info, _ = self.search([
            "info string alice_result nonsense\n",
            "info depth 4 score cp 7 nodes 90\n",
            "bestmove f@e4,d2d4\n",
        ])
        self.assertEqual(best, "f@e4,d2d4")
        self.assertEqual(info["cp"], 7)

    def test_a_bestmove_with_no_move_token_is_read_as_a_terminal(self):
        best, _, _ = self.search(["bestmove\n"])
        self.assertIn(best, runner.NONE_MOVES)


HAS_SHADOW_GATE = hasattr(runner, "SHADOW_VARIANTS")


@unittest.skipUnless(HAS_SHADOW_GATE, "runner predates the shadow gate")
class ShadowVariantGateTests(SpellRunnerCase):
    """shadow=true must be refused for spell, loudly and before any game.

    Shadow adjudication stops adjudicating, plays every game to its natural
    end, and then discards any game whose real result contradicts the virtual
    one -- dropping it with no 'Finished game' line at all, so the worker never
    learns the game existed. That audit only means something for a variant that
    emits the evidence records, so anything outside SHADOW_VARIANTS must fail
    to start rather than silently play a different match.
    """

    def shadow_argv(self, games=2, extra=()):
        return self.spell_argv(
            games=games, extra=extra,
            adjudication=["-resign", "movecount=3", "score=400", "shadow=true"])

    def test_spell_is_not_a_shadow_variant(self):
        self.assertEqual(runner.SHADOW_VARIANTS, frozenset({"alice"}))
        self.assertNotIn("spell-chess", runner.SHADOW_VARIANTS)

    def test_shadow_true_on_a_spell_command_line_is_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            runner.parse_cli(self.shadow_argv())
        message = str(caught.exception)
        self.assertIn("shadow=true", message)
        self.assertIn("alice", message)
        self.assertIn("spell-chess", message)
        self.assertIn("win_adj", message)   # tells the reader what to change

    def test_the_rejection_does_not_depend_on_flag_order(self):
        """-variant may legitimately arrive after -resign."""
        argv = self.shadow_argv()
        variant_at = argv.index("-variant")
        reordered = (argv[:variant_at] + argv[variant_at + 2:]
                     + ["-variant", "spell-chess"])
        with self.assertRaises(SystemExit):
            runner.parse_cli(reordered)

    def test_nothing_is_launched_and_no_game_is_played(self):
        code, out, err = self.run_match(self.shadow_argv(games=2),
                                        always(best="e2e4", cp=-500))
        self.assertNotEqual(code, 0)
        self.assertNotIn("Started game", out)
        self.assertNotIn("Finished game", out)
        self.assertEqual(self.finished(out), [])

    def test_shadow_adjudication_cannot_be_armed_by_assignment(self):
        """The flag is derived, so a future consumer cannot be handed a config
        with shadow on and the wrong variant."""
        cfg = runner.parse_cli(self.spell_argv(games=2))
        self.assertFalse(cfg.shadow_adjudication)
        with self.assertRaises(AttributeError):
            cfg.shadow_adjudication = True
        cfg.shadow_requested = True
        self.assertFalse(cfg.shadow_adjudication)  # variant still decides

    def test_the_gate_does_not_disturb_a_plain_spell_command_line(self):
        cfg = runner.parse_cli(self.spell_argv(
            games=2,
            adjudication=["-resign", "movecount=3", "score=400",
                          "-draw", "movenumber=40", "movecount=8",
                          "score=10"]))
        self.assertFalse(cfg.shadow_requested)
        self.assertFalse(cfg.shadow_adjudication)
        self.assertEqual(cfg.resign, {"movecount": 3, "score": 400})

    def test_alice_still_arms_shadow(self):
        argv = [a.replace("spell-chess", "alice") for a in self.shadow_argv()]
        cfg = runner.parse_cli(argv)
        self.assertTrue(cfg.shadow_adjudication)


@unittest.skipUnless(
    os.environ.get("SPELL_RUNNER_ENGINE"),
    "set SPELL_RUNNER_ENGINE=<path to a spell binary> for the real smoke")
class RealEngineSmokeTests(unittest.TestCase):
    """Opt-in: two fixed-node games with a real Spell-Stockfish binary.

    Not part of the default run -- it needs a 90 MB binary and a network.
    SPELL_RUNNER_NET may point at an EvalFile; SPELL_RUNNER_BOOK at an EPD.
    """

    def test_two_fixed_node_games_finish_cleanly(self):
        import subprocess

        engine = os.environ["SPELL_RUNNER_ENGINE"]
        book = os.environ.get("SPELL_RUNNER_BOOK")
        net = os.environ.get("SPELL_RUNNER_NET")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            if not book:
                book = str(tmp / "book.epd")
                Path(book).write_text(SPELL_FEN_W + "\n", encoding="ascii")
            opts = ["option.Threads=1", "option.Hash=8"]
            if net:
                opts.append("option.EvalFile=%s" % net)
            argv = [sys.executable, str(ROOT / "Client" / "uci_pair_runner.py"),
                    "-repeat", "-recover", "-variant", "spell-chess",
                    "-concurrency", "1", "-games", "2"]
            for branch in ("dev", "base"):
                argv += ["-engine", "cmd=%s" % engine, "proto=uci",
                         "tc=inf", "nodes=5000"] + opts + [
                             "name=Spell-Stockfish-%s" % branch]
            argv += ["-openings", "file=%s" % book, "format=epd",
                     "order=sequential", "start=1", "-srand", "42",
                     "-pgnout", str(tmp / "out.pgn")]
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        finished = [l for l in proc.stdout.splitlines()
                    if l.startswith("Finished game")]
        self.assertEqual(len(finished), 2)
        for line in finished:
            self.assertIn(line.split()[6], ("1-0", "0-1", "1/2-1/2"))


if __name__ == "__main__":
    unittest.main()
