#!/usr/bin/env python3
"""UCI pair-game runner for variants cutechess-cli cannot arbitrate (spell-chess).

Drop-in stand-in for cutechess-ob inside an OpenBench worker, and a local match
runner. The runner NEVER arbitrates rules itself: engines are trusted, and game
ends come from UCI terminals (`bestmove (none)`), score adjudication, clocks,
or the ply cap. Generalized from tools/fixed_nodes_match.py.

OpenBench worker stdout contract honored here (see docs in repo):
  * every stdout line is ASCII, non-blank, flushed immediately;
  * results travel ONLY in lines of the exact shape
      Finished game N (WhiteName vs BlackName): RESULT {reason}
    with RESULT in {1-0, 0-1, 1/2-1/2}, 1-based game numbers, dev (the first
    -engine) playing White in odd games, pairs = (1,2),(3,4),...;
  * reasons carry the substrings the worker counts: 'disconnect', 'stalls',
    'on time', 'illegal';
  * 'Started game' / 'Score of' lines are emitted (worker suppresses them);
  * a valid PGN (headers, blank line, movetext with cutechess-style comments,
    result token) is appended to the -pgnout path per game; the file exists
    even when zero games have errors; anomalous games carry
    [Termination "abandoned"|"stalled connection"|"illegal move"|"time forfeit"].

CLI: accepts the cutechess flag set the worker builds (-repeat -recover
-variant -concurrency -games -resign -draw -engine ... -openings ... -srand
-pgnout ...) plus -each, and ignores with a stderr warning whatever does not
apply. No argument may contain spaces (the worker Popens command.split()).

Example (local smoke):
  python tools/uci_pair_runner.py -repeat -recover -variant standard \
      -concurrency 2 -games 8 \
      -resign movecount=3 score=400 -draw movenumber=40 movecount=8 score=10 \
      -engine cmd=./spell-dev.exe name=spell-dev proto=uci tc=5+0.05 timemargin=250 option.Hash=8 \
      -engine cmd=./spell-base.exe name=spell-base proto=uci tc=5+0.05 timemargin=250 option.Hash=8 \
      -each option.UCI_Variant=spell-chess option.EvalFile=spell.nnue \
      -openings file=books/spell.epd format=epd order=random start=1 \
      -srand 42 -pgnout out.pgn
"""

import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

MATE_ISH = 25000                       # forced win in internal cp units
NONE_MOVES = ("(none)", "0000", "none")
# permissive: spell-chess gated moves look like 'f@e4,d2d4' (with promotion
# 'f@e4,e7e8q' reaches 10 chars); only reject clearly malformed bestmoves so
# we never false-positive an 'illegal move'
MOVE_RE = re.compile(r"^[A-Za-z0-9@+=,\-]{2,12}$")

_out_lock = threading.Lock()


def emit(line):
    """stdout writer: ASCII only, never blank, one line, immediate flush."""
    line = " ".join(str(line).split())
    if not line:
        return
    line = line.encode("ascii", "replace").decode("ascii")
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def warn(msg):
    # stderr is inherited (never parsed) in the worker: safe channel
    sys.stderr.write("uci_pair_runner: %s\n" % msg)
    sys.stderr.flush()


class EngineDied(RuntimeError):
    pass


class EngineStalled(RuntimeError):
    pass


# --------------------------------------------------------------------------
# time control


class TimeControl:
    TIMED, ST, FIXED, NONE = "timed", "st", "fixed", "none"

    def __init__(self):
        self.kind = TimeControl.NONE
        self.base_ms = 0
        self.inc_ms = 0
        self.moves = 0          # moves per session (moves/base+inc), 0 = sudden death
        self.margin_ms = 0      # cutechess timemargin
        self.st_ms = 0          # fixed time per move
        self.nodes = 0
        self.depth = 0
        self.label = "inf"

    @classmethod
    def from_settings(cls, s):
        tc = cls()
        tc.margin_ms = int(float(s.get("timemargin", "0")))
        nodes = int(s["nodes"]) if "nodes" in s else 0
        depth = int(s["depth"]) if "depth" in s else 0
        if nodes or depth:
            tc.kind, tc.nodes, tc.depth = cls.FIXED, nodes, depth
            tc.label = "inf"
            return tc
        if "st" in s:
            tc.kind = cls.ST
            tc.st_ms = int(round(float(s["st"]) * 1000))
            tc.label = "st=" + s["st"]
            return tc
        raw = s.get("tc", "")
        if not raw or raw == "inf":
            return tc  # NONE: caller must reject or supply nodes/depth
        tc.kind = cls.TIMED
        tc.label = raw
        if "/" in raw:
            m, raw = raw.split("/", 1)
            tc.moves = int(m)
        if "+" in raw:
            base, inc = raw.split("+", 1)
        else:
            base, inc = raw, "0"
        tc.base_ms = int(round(_parse_clock(base) * 1000))
        tc.inc_ms = int(round(float(inc) * 1000))
        return tc

    def new_clock(self):
        if self.kind != TimeControl.TIMED:
            return None
        return {"time": float(self.base_ms),
                "moves_left": self.moves if self.moves else 0}


def _parse_clock(text):
    """'5.02' seconds or 'mm:ss' -> seconds (float)."""
    if ":" in text:
        mins, secs = text.split(":", 1)
        return int(mins) * 60 + float(secs)
    return float(text)


# --------------------------------------------------------------------------
# engine


class EngineSpec:
    def __init__(self):
        self.name = "engine"
        self.path = None
        self.cwd = None
        self.options = {}       # ordered dict: UCI option -> value
        self.tc = None          # TimeControl

    @classmethod
    def from_settings(cls, s, index):
        spec = cls()
        d = s.get("dir", ".")
        cmd = s.get("cmd")
        if not cmd:
            raise SystemExit("uci_pair_runner: -engine %d is missing cmd=" % index)
        if cmd.startswith("./") or cmd.startswith(".\\"):
            cmd = cmd[2:]
        path = cmd if os.path.isabs(cmd) else os.path.abspath(os.path.join(d, cmd))
        if not os.path.exists(path) and os.path.exists(path + ".exe"):
            path += ".exe"
        spec.path = path
        spec.cwd = os.path.abspath(d)
        proto = s.get("proto", "uci")
        if proto != "uci":
            warn("engine %d: proto=%s unsupported, only uci; proceeding as uci"
                 % (index, proto))
        raw = s.get("name") or os.path.basename(cmd)
        spec.name = re.sub(r"[\s:(){}]+", "-", raw)  # names must stay one token
        spec.options = dict(s.get("options", {}))
        if "Threads" not in spec.options:
            spec.options["Threads"] = "1"
        spec.tc = TimeControl.from_settings(s)
        if spec.tc.kind == TimeControl.NONE:
            raise SystemExit(
                "uci_pair_runner: engine %d (%s) has no usable time control "
                "(need tc=base+inc, st=, nodes= or depth=)" % (index, spec.name))
        return spec


class Engine:
    def __init__(self, spec, debug=False):
        self.spec = spec
        self.name = spec.name
        self.debug = debug
        try:
            self.proc = subprocess.Popen(
                [spec.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=spec.cwd)
        except OSError as exc:
            raise EngineDied("%s failed to launch (%s): %s"
                             % (spec.name, spec.path, exc))
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.send("uci")
        self.read_until("uciok", timeout=60)
        for k, v in spec.options.items():
            self.send("setoption name %s value %s" % (k, v))
        self.sync()

    def send(self, cmd):
        if self.debug:
            warn("-> %s: %s" % (self.name, cmd))
        try:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
        except OSError as exc:
            raise EngineDied("%s died (write: %s, exit=%s)"
                             % (self.name, exc, self.proc.poll())) from exc

    def _pump(self):
        # Reader thread: timeouts must interrupt reads even when the engine
        # stays alive but silent
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)  # EOF marker

    def read_until(self, token, timeout=120):
        deadline = time.monotonic() + timeout
        lines = []
        while True:
            try:
                line = self._lines.get(
                    timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                raise EngineStalled("%s: timeout waiting for %s"
                                    % (self.name, token))
            if line is None:
                tail = " | ".join(lines[-4:])
                raise EngineDied("%s died (EOF, exit=%s) last: %s"
                                 % (self.name, self.proc.poll(), tail))
            line = line.rstrip("\n")
            lines.append(line)
            if line.startswith(token):
                return lines
            if time.monotonic() > deadline:
                raise EngineStalled("%s: timeout waiting for %s"
                                    % (self.name, token))

    def sync(self):
        self.send("isready")
        self.read_until("readyok", timeout=60)

    def new_game(self):
        self.send("ucinewgame")
        self.sync()

    def search(self, pos_cmd, go_cmd, budget_s, stall_grace):
        """Run one search. Returns (bestmove, info, elapsed_ms).

        info: cp (mover POV, mates folded to +/-MATE_ISH), raw_mate (or None),
        depth, seldepth, nodes. If no bestmove arrives within budget_s we send
        'stop'; if it still does not arrive within stall_grace, EngineStalled.
        """
        self.send(pos_cmd)
        t0 = time.monotonic()
        self.send(go_cmd)
        stopped_at = None
        info = {"cp": None, "raw_mate": None, "depth": 0, "seldepth": 0,
                "nodes": 0}
        while True:
            now = time.monotonic()
            if stopped_at is None:
                wait = budget_s - (now - t0)
                if wait <= 0:
                    self.send("stop")
                    stopped_at = now
                    continue
            else:
                wait = stall_grace - (now - stopped_at)
                if wait <= 0:
                    raise EngineStalled(
                        "%s: no bestmove %.0fs after stop"
                        % (self.name, stall_grace))
            try:
                line = self._lines.get(timeout=max(0.01, min(wait, 1.0)))
            except queue.Empty:
                continue
            if line is None:
                raise EngineDied("%s died mid-search (EOF, exit=%s)"
                                 % (self.name, self.proc.poll()))
            line = line.rstrip("\n")
            if self.debug:
                warn("<- %s: %s" % (self.name, line))
            parts = line.split()
            if line.startswith("info") and "score" in parts:
                try:
                    i = parts.index("score")
                    kind, val = parts[i + 1], int(parts[i + 2])
                    if kind == "mate":
                        info["raw_mate"] = val
                        info["cp"] = MATE_ISH if val > 0 else -MATE_ISH
                    elif kind == "cp":
                        info["raw_mate"] = None
                        info["cp"] = val
                    for key in ("depth", "seldepth", "nodes"):
                        if key in parts:
                            info[key] = int(parts[parts.index(key) + 1])
                except (ValueError, IndexError):
                    pass
            elif line.startswith("bestmove"):
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                best = parts[1] if len(parts) > 1 else "(none)"
                return best, info, elapsed_ms

    def quit(self):
        try:
            self.send("quit")
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------
# game outcome


class Outcome:
    def __init__(self, result, reason, termination="normal", restart=False):
        self.result = result            # '1-0' | '0-1' | '1/2-1/2'
        self.reason = reason            # goes inside {...}
        self.termination = termination  # PGN [Termination] value
        self.restart = restart          # engines must be rebooted after game
        self.moves = []                 # [(uci, comment_str)]
        self.plies = 0


def _fen_fields(fen):
    # spell FENs carry a '{...}' state token between board and stm, so scan
    # for the side-to-move field instead of trusting a fixed position
    t = fen.split()
    stm = next((tok for tok in t[1:] if tok in ("w", "b")), "w")
    fullmove = 1
    for tok in reversed(t):
        if tok.isdigit():
            fullmove = int(tok)
            break
    return stm, fullmove


def _fmt_comment(info, elapsed_ms):
    """cutechess verbose move comment: 'score depth/seldepth time nodes'."""
    if info["raw_mate"] is not None:
        score = "%sM%d" % ("+" if info["raw_mate"] > 0 else "-",
                           abs(info["raw_mate"]))
    elif info["cp"] is not None:
        score = "%+.2f" % (info["cp"] / 100.0)
    else:
        score = "+0.00"
    depth = max(1, info["depth"])
    seldepth = max(depth, info["seldepth"])
    return "%s %d/%d %d %d" % (score, depth, seldepth,
                               int(elapsed_ms), info["nodes"])


def play_game(white, black, fen, cfg):
    """Play a single game; never raises for engine failures (folds them into
    the Outcome per -recover semantics: dead/stalled side loses, restart=True).
    """
    wtc, btc = white.spec.tc, black.spec.tc
    clocks = {white: wtc.new_clock(), black: btc.new_clock()}
    resign_cnt = {white: 0, black: 0}
    draw_cnt = 0
    adj_streak, adj_leader = 0, 0
    moves = []
    record = []
    stm0, fullmove0 = _fen_fields(fen)

    def finish(result, reason, termination="normal", restart=False):
        out = Outcome(result, reason, termination, restart)
        out.moves = record
        out.plies = len(record)
        return out

    def side_of(eng):
        return "White" if eng is white else "Black"

    def loses(eng, reason, termination, restart=False):
        return finish("0-1" if eng is white else "1-0",
                      reason, termination, restart)

    for ply in range(cfg.max_plies):
        stm_is_white = (ply % 2 == 0) == (stm0 == "w")
        eng = white if stm_is_white else black
        tc = eng.spec.tc
        clk = clocks[eng]

        pos_cmd = "position fen %s" % fen
        if moves:
            pos_cmd += " moves " + " ".join(moves)

        if tc.kind == TimeControl.TIMED:
            mine = max(1, int(clk["time"]))
            wclk = clocks[white]
            bclk = clocks[black]
            wt = max(1, int(wclk["time"])) if wclk else mine
            bt = max(1, int(bclk["time"])) if bclk else mine
            go_cmd = ("go wtime %d btime %d winc %d binc %d"
                      % (wt, bt,
                         wtc.inc_ms if wtc.kind == TimeControl.TIMED else 0,
                         btc.inc_ms if btc.kind == TimeControl.TIMED else 0))
            if tc.moves:
                go_cmd += " movestogo %d" % max(1, clk["moves_left"])
            budget_s = (mine + tc.margin_ms) / 1000.0
        elif tc.kind == TimeControl.ST:
            go_cmd = "go movetime %d" % tc.st_ms
            budget_s = (tc.st_ms + tc.margin_ms) / 1000.0 + 0.05
        else:  # FIXED nodes/depth
            go_cmd = "go"
            if tc.nodes:
                go_cmd += " nodes %d" % tc.nodes
            if tc.depth:
                go_cmd += " depth %d" % tc.depth
            budget_s = cfg.fixed_budget_s

        try:
            best, info, elapsed_ms = eng.search(
                pos_cmd, go_cmd, budget_s, cfg.stall_grace_s)
        except EngineStalled as exc:
            warn(str(exc))
            return loses(eng, "%s's connection stalls" % side_of(eng),
                         "stalled connection", restart=True)
        except EngineDied as exc:
            warn(str(exc))
            return loses(eng, "%s disconnects" % side_of(eng),
                         "abandoned", restart=True)

        # ---- clock accounting / time forfeit (cutechess semantics:
        #      elapsed beyond remaining+timemargin is a loss on time)
        if tc.kind == TimeControl.TIMED:
            clk["time"] -= elapsed_ms
            if clk["time"] < -tc.margin_ms:
                return loses(eng, "%s loses on time" % side_of(eng),
                             "time forfeit")
            clk["time"] += tc.inc_ms
            if tc.moves:
                clk["moves_left"] -= 1
                if clk["moves_left"] <= 0:
                    clk["time"] += tc.base_ms
                    clk["moves_left"] = tc.moves
        elif tc.kind == TimeControl.ST:
            if elapsed_ms > tc.st_ms + tc.margin_ms:
                return loses(eng, "%s loses on time" % side_of(eng),
                             "time forfeit")

        score = info["cp"]  # mover POV, mates folded to +/-MATE_ISH

        # ---- spell-chess terminal: no legal move. Losing terminals (king
        # captured / stalled while attacked) carry a huge negative root score;
        # a quiet stall (score ~ 0) is a draw.
        if best in NONE_MOVES:
            if score is not None and abs(score) < cfg.stall_draw_cp:
                return finish("1/2-1/2", "Draw by stalemate")
            winner = black if stm_is_white else white
            return finish("0-1" if stm_is_white else "1-0",
                          "%s mates" % side_of(winner))

        if not MOVE_RE.match(best):
            return loses(eng, "%s makes an illegal move: %s"
                         % (side_of(eng), best), "illegal move")

        moves.append(best)
        record.append((best, _fmt_comment(info, elapsed_ms)))

        # ---- resign adjudication (cutechess -resign movecount=M score=S:
        # a side whose own score stays <= -S for M consecutive moves loses)
        if cfg.resign and score is not None:
            if score <= -cfg.resign["score"]:
                resign_cnt[eng] += 1
                if resign_cnt[eng] >= cfg.resign["movecount"]:
                    winner = black if stm_is_white else white
                    return finish("0-1" if stm_is_white else "1-0",
                                  "%s wins by adjudication" % side_of(winner),
                                  "adjudication")
            else:
                resign_cnt[eng] = 0

        # ---- draw adjudication (cutechess -draw movenumber=N movecount=M
        # score=S: after move N, both sides within +/-S for M moves each)
        if cfg.draw:
            if (score is not None and info["raw_mate"] is None
                    and abs(score) <= cfg.draw["score"]):
                draw_cnt += 1
            else:
                draw_cnt = 0
            fullmove_now = fullmove0 + (ply + (1 if stm0 == "b" else 0)) // 2
            if (draw_cnt >= 2 * cfg.draw["movecount"]
                    and fullmove_now >= cfg.draw["movenumber"]):
                return finish("1/2-1/2", "Draw by adjudication",
                              "adjudication")

        # ---- optional symmetric adjudication (local probes): both engines
        # agree on a |score| >= adj_cp leader for adj_plies consecutive plies
        if cfg.adj_cp and score is not None and abs(score) >= cfg.adj_cp:
            side = 1 if (score > 0) == stm_is_white else -1
            adj_streak = adj_streak + 1 if side == adj_leader else 1
            adj_leader = side
            if adj_streak >= cfg.adj_plies or (
                    abs(score) >= MATE_ISH and adj_streak >= 2):
                return finish("1-0" if adj_leader > 0 else "0-1",
                              "%s wins by adjudication"
                              % ("White" if adj_leader > 0 else "Black"),
                              "adjudication")
        elif cfg.adj_cp:
            adj_streak, adj_leader = 0, 0

    return finish("1/2-1/2", "Draw by adjudication: max game length",
                  "adjudication")


# --------------------------------------------------------------------------
# PGN


_pgn_lock = threading.Lock()


def write_pgn(path, game_no, white_name, black_name, fen, outcome, tc_label):
    stm0, _ = _fen_fields(fen)
    headers = [
        ("Event", "uci_pair_runner"),
        ("Site", "?"),
        ("Date", datetime.now().strftime("%Y.%m.%d")),
        ("Round", str((game_no + 1) // 2)),
        ("White", white_name),
        ("Black", black_name),
        ("Result", outcome.result),
        ("SetUp", "1"),
        ("FEN", fen),
        ("Variant", "spell-chess"),
        ("TimeControl", tc_label),
        ("PlyCount", str(outcome.plies)),
        ("GameEndTime", datetime.now().strftime("%Y-%m-%dT%H:%M:%S")),
    ]
    if outcome.termination != "normal":
        headers.append(("Termination", outcome.termination))

    tokens = []
    ply = 0 if stm0 == "w" else 1
    moveno = 1
    if stm0 == "b" and outcome.moves:
        tokens.append("%d..." % moveno)
    for uci, comment in outcome.moves:
        if ply % 2 == 0:
            tokens.append("%d." % moveno)
        tokens.append(uci)
        tokens.append("{%s}" % comment)
        if ply % 2 == 1:
            moveno += 1
        ply += 1
    tokens.append(outcome.result)

    lines, cur = [], ""
    for tok in tokens:
        if cur and len(cur) + 1 + len(tok) > 79:
            lines.append(cur)
            cur = tok
        else:
            cur = tok if not cur else cur + " " + tok
    if cur:
        lines.append(cur)

    block = "".join('[%s "%s"]\n' % (k, v.replace('"', "'"))
                    for k, v in headers)
    block += "\n" + "\n".join(lines) + "\n\n"
    with _pgn_lock:
        with open(path, "a", encoding="ascii", errors="replace",
                  newline="\n") as f:
            f.write(block)
            f.flush()


# --------------------------------------------------------------------------
# CLI parsing (cutechess-style: single-dash flags, k=v token lists)


KV_FLAGS = {"-engine", "-each", "-openings", "-resign", "-draw", "-sprt"}
ONE_ARG_FLAGS = {"-variant", "-concurrency", "-games", "-rounds", "-srand",
                 "-pgnout", "-event", "-site", "-tb", "-tbpieces",
                 "-ratinginterval", "-tournament", "-maxmoves",
                 "--seed", "--max-plies", "--stall-draw-cp", "--adj-cp",
                 "--adj-plies", "--stall-grace", "--fixed-budget"}
ZERO_ARG_FLAGS = {"-repeat", "-recover", "-wait", "--debug", "-debug"}
IGNORED = {"-tb", "-tbpieces", "-event", "-site", "-ratinginterval",
           "-tournament", "-rounds", "-wait", "-sprt", "-maxmoves"}


def _collect_kv(argv, i):
    """Collect the k=v tokens that follow argv[i]; returns (tokens, next_i)."""
    toks = []
    i += 1
    while i < len(argv) and "=" in argv[i] and not argv[i].startswith("-"):
        toks.append(argv[i])
        i += 1
    return toks, i


def _kv_dict(tokens):
    d = {"options": {}}
    for tok in tokens:
        k, v = tok.split("=", 1)
        if k.startswith("option."):
            d["options"][k[len("option."):]] = v
        else:
            d[k] = v
    return d


class Config:
    pass


def parse_cli(argv):
    cfg = Config()
    cfg.engines_raw = []
    cfg.each_raw = {"options": {}}
    cfg.openings = None
    cfg.repeat = False
    cfg.concurrency = 1
    cfg.games = 2
    cfg.srand = None
    cfg.pgnout = None
    cfg.resign = None
    cfg.draw = None
    cfg.max_plies = 900
    cfg.stall_draw_cp = 800
    cfg.adj_cp = 0
    cfg.adj_plies = 4
    cfg.stall_grace_s = 10.0
    cfg.fixed_budget_s = 600.0
    cfg.debug = False

    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag in KV_FLAGS:
            toks, i = _collect_kv(argv, i)
            d = _kv_dict(toks)
            if flag == "-engine":
                cfg.engines_raw.append(d)
            elif flag == "-each":
                cfg.each_raw["options"].update(d.pop("options"))
                cfg.each_raw.update(d)
            elif flag == "-openings":
                cfg.openings = d
            elif flag == "-resign":
                cfg.resign = {"movecount": int(d.get("movecount", 3)),
                              "score": int(d.get("score", 400))}
            elif flag == "-draw":
                cfg.draw = {"movenumber": int(d.get("movenumber", 40)),
                            "movecount": int(d.get("movecount", 8)),
                            "score": int(d.get("score", 10))}
            elif flag == "-sprt":
                warn("-sprt ignored (the server computes SPRT from batches)")
            continue
        if flag in ZERO_ARG_FLAGS:
            if flag == "-repeat":
                cfg.repeat = True
            elif flag in ("--debug", "-debug"):
                cfg.debug = True
            # -recover: always-on behavior; -wait: ignored
            i += 1
            continue
        if flag in ONE_ARG_FLAGS:
            if i + 1 >= len(argv):
                raise SystemExit("uci_pair_runner: %s needs a value" % flag)
            val = argv[i + 1]
            i += 2
            if flag == "-variant":
                if val not in ("standard",):
                    warn("-variant %s noted; variant play is engine-side "
                         "(pass option.UCI_Variant=...)" % val)
            elif flag == "-concurrency":
                cfg.concurrency = max(1, int(val))
            elif flag == "-games":
                cfg.games = int(val)
            elif flag in ("-srand", "--seed"):
                cfg.srand = int(val)
            elif flag == "-pgnout":
                cfg.pgnout = val
            elif flag == "--max-plies":
                cfg.max_plies = int(val)
            elif flag == "--stall-draw-cp":
                cfg.stall_draw_cp = int(val)
            elif flag == "--adj-cp":
                cfg.adj_cp = int(val)
            elif flag == "--adj-plies":
                cfg.adj_plies = int(val)
            elif flag == "--stall-grace":
                cfg.stall_grace_s = float(val)
            elif flag == "--fixed-budget":
                cfg.fixed_budget_s = float(val)
            elif flag in IGNORED:
                warn("%s %s ignored (not applicable to spell-chess)"
                     % (flag, val))
            continue
        # unknown flag: warn, skip it and its non-flag tail
        warn("unknown flag %s ignored" % flag)
        i += 1
        while i < len(argv) and not argv[i].startswith("-"):
            i += 1

    if len(cfg.engines_raw) != 2:
        raise SystemExit("uci_pair_runner: exactly two -engine blocks "
                         "required (got %d)" % len(cfg.engines_raw))
    if not cfg.openings or "file" not in cfg.openings:
        raise SystemExit("uci_pair_runner: -openings file=... is required")
    fmt = cfg.openings.get("format", "epd")
    if fmt != "epd":
        raise SystemExit("uci_pair_runner: only format=epd books are "
                         "supported (got %s)" % fmt)

    # merge -each defaults under each -engine block (engine wins)
    specs = []
    for idx, raw in enumerate(cfg.engines_raw, 1):
        merged = dict(cfg.each_raw)
        merged["options"] = dict(cfg.each_raw["options"])
        merged["options"].update(raw.get("options", {}))
        for k, v in raw.items():
            if k != "options":
                merged[k] = v
        specs.append(EngineSpec.from_settings(merged, idx))
    cfg.specs = specs
    if specs[0].name == specs[1].name:
        specs[1].name += "-2"
    return cfg


# --------------------------------------------------------------------------
# book


def load_book(path):
    fens = []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.split(";")[0].strip()  # drop EPD opcodes if any
            if line and not line.startswith("#"):
                fens.append(line)
    if not fens:
        raise SystemExit("uci_pair_runner: empty opening book: %s" % path)
    return fens


def build_opening_sequence(book_len, order, start, seed, count):
    """Deterministic opening indices; honors order=, start= (1-based), seed."""
    need = max(0, start - 1) + count
    seq = []
    if order == "random":
        rng = random.Random(seed)
        while len(seq) < need:
            cycle = list(range(book_len))
            rng.shuffle(cycle)
            seq.extend(cycle)
    else:
        seq = [i % book_len for i in range(need)]
    return seq[start - 1:start - 1 + count]


# --------------------------------------------------------------------------
# match driver


def elo_stats(w, losses, d):
    n = w + losses + d
    if n == 0:
        return 0.0, 50.0
    score = (w + d / 2) / n
    score = min(max(score, 1e-9), 1 - 1e-9)
    elo = -400 * math.log10(1 / score - 1)
    if w + losses > 0:
        los = 0.5 * (1 + math.erf((w - losses) / math.sqrt(2 * (w + losses))))
    else:
        los = 0.5
    return elo, 100 * los


def main():
    cfg = parse_cli(sys.argv[1:])
    dev, base = cfg.specs  # first -engine = dev, plays White in odd games

    book = load_book(cfg.openings["file"])
    order = cfg.openings.get("order", "sequential")
    start = max(1, int(cfg.openings.get("start", "1") or 1))
    seed = cfg.srand if cfg.srand is not None else random.randrange(1 << 30)

    total = cfg.games
    pairs = (total + 1) // 2
    # one opening per pair with -repeat, one per game without
    n_openings = pairs if cfg.repeat else total
    opening_idx = build_opening_sequence(len(book), order, start, seed,
                                         n_openings)

    if cfg.pgnout:
        os.makedirs(os.path.dirname(cfg.pgnout) or ".", exist_ok=True)
        open(cfg.pgnout, "a").close()  # must exist even with zero errors

    jobs = queue.Queue()
    for p in range(pairs):
        jobs.put(p)

    lock = threading.Lock()
    tally = {"w": 0, "l": 0, "d": 0, "n": 0}
    pair_scores = {}   # pair -> {game_in_pair: dev_score 0/1/2}
    penta = [0, 0, 0, 0, 0]
    res_lookup = {"0-1": 0, "1/2-1/2": 1, "1-0": 2}
    tc_label = "%s / %s" % (dev.tc.label, base.tc.label) \
        if dev.tc.label != base.tc.label else dev.tc.label

    def account(game_no, result):
        """Update dev-POV tallies and pentanomial under the lock."""
        odd = game_no % 2 == 1
        dev_score = res_lookup[result] if odd else 2 - res_lookup[result]
        tally["n"] += 1
        tally["w" if dev_score == 2 else "d" if dev_score == 1 else "l"] += 1
        p = (game_no - 1) // 2
        slot = pair_scores.setdefault(p, {})
        slot[game_no] = dev_score
        if len(slot) == 2:
            penta[sum(slot.values())] += 1

    def worker():
        holder = {"engines": None}  # (dev Engine, base Engine) or None

        while True:
            try:
                p = jobs.get_nowait()
            except queue.Empty:
                break
            game_numbers = [2 * p + 1]
            if 2 * p + 2 <= total:
                game_numbers.append(2 * p + 2)
            for g in game_numbers:
                if cfg.repeat:
                    fen = book[opening_idx[p]]
                else:
                    fen = book[opening_idx[g - 1]]
                dev_is_white = g % 2 == 1
                wspec, bspec = (dev, base) if dev_is_white else (base, dev)
                emit("Started game %d of %d (%s vs %s)"
                     % (g, total, wspec.name, bspec.name))
                try:
                    if holder["engines"] is None:
                        holder["engines"] = (Engine(dev, cfg.debug),
                                             Engine(base, cfg.debug))
                    e_dev, e_base = holder["engines"]
                    e_dev.new_game()
                    e_base.new_game()
                    e_white, e_black = (e_dev, e_base) if dev_is_white \
                        else (e_base, e_dev)
                    outcome = play_game(e_white, e_black, fen, cfg)
                except (EngineDied, EngineStalled) as exc:
                    # boot/newgame failure: charge it to the culprit engine.
                    # Every EngineDied/EngineStalled message starts with the
                    # engine name; substring tests misfire when one name is a
                    # prefix of the other (self-play dedupes to 'X'/'X-2')
                    warn(str(exc))
                    first = str(exc).split()[0].rstrip(":")
                    culprit_is_dev = first == dev.name
                    culprit_white = culprit_is_dev == dev_is_white
                    outcome = Outcome(
                        "0-1" if culprit_white else "1-0",
                        "%s disconnects"
                        % ("White" if culprit_white else "Black"),
                        "abandoned", restart=True)
                emit("Finished game %d (%s vs %s): %s {%s}"
                     % (g, wspec.name, bspec.name, outcome.result,
                        outcome.reason))
                with lock:
                    account(g, outcome.result)
                    w, l, d, n = (tally["w"], tally["l"], tally["d"],
                                  tally["n"])
                emit("Score of %s vs %s: %d - %d - %d  [%.3f] %d"
                     % (dev.name, base.name, w, l, d,
                        (w + d / 2) / max(n, 1), n))
                if cfg.pgnout:
                    try:
                        write_pgn(cfg.pgnout, g, wspec.name, bspec.name,
                                  fen, outcome, tc_label)
                    except OSError as exc:
                        warn("pgn write failed: %s" % exc)
                if outcome.restart and holder["engines"]:
                    for e in holder["engines"]:
                        e.quit()
                    holder["engines"] = None
        if holder["engines"]:
            for e in holder["engines"]:
                e.quit()

    nthreads = min(cfg.concurrency, pairs)
    threads = [threading.Thread(target=worker) for _ in range(nthreads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    elo, los = elo_stats(tally["w"], tally["l"], tally["d"])
    emit("Finished match: %s vs %s: %d - %d - %d penta [%d,%d,%d,%d,%d] "
         "elo %+.1f los %.1f%%"
         % (dev.name, base.name, tally["w"], tally["l"], tally["d"],
            penta[0], penta[1], penta[2], penta[3], penta[4], elo, los))
    sys.stdout.close()
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
