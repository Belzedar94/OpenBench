/*
  survive50-verify — a native checker for atomic-survival-threshold-v1.

  WHY THIS EXISTS AND WHY IT IS BUILT AGAINST UPSTREAM
  ----------------------------------------------------
  The Python verifier in ``atomicdb/survive.py`` is correct and stays the
  reference, but it walks pyffish, and pyffish charges a flat ~15 ms per
  position it constructs — Python and PyObject overhead, not move generation.
  That put a 10k-state certificate at roughly twenty minutes and made the
  fleet ladder of doc 18 §5 unaffordable. This tool does the same checks
  against the same move generator lineage, with the Python layer removed.

  The independence doctrine survives intact, and that is the whole point of
  the choice of source tree:

      our fork  (Atomic-Stockfish)  SOLVES and emits certificates
      upstream  (Fairy-Stockfish)   VERIFIES them

  pyffish IS upstream Fairy-Stockfish with a Python skin, so verifying with
  upstream C++ keeps exactly the property the server had: a move generator
  bug would have to exist identically in our fork and in upstream to get
  past. What is dropped is the skin, not the second implementation. Linking
  this against Atomic-Stockfish instead would destroy that and make the
  verifier worthless — do not do it.

  THE PIN
  -------
      repository  https://github.com/fairy-stockfish/Fairy-Stockfish
      commit      fb78cb561aa01708338e35b3dc3b65a42149a3c4   (2026-07-01)
      pyffish     setup.py declares 0.0.89, the same version as the wheel
                  the Python reference imports

  The installed wheel was compiled earlier from its own 0.0.89 commit (it
  reports "Fairy-Stockfish 010526"), so the two are the same declared version
  but not the same build. That is a feature here rather than a nuisance: the
  differential test compares this binary against that wheel, so any drift in
  atomic between them shows up as a disagreement instead of hiding.

  Pin lives in the Makefile too, which is what actually reads it.

  WHAT IT REPRODUCES, EXACTLY
  ---------------------------
  ``pyffish.cpp`` is the specification, not an inspiration. Every primitive
  below is the same FSF call sequence its Python counterpart makes:

      legal_moves        MoveList<LEGAL> mapped through UCI::move
      get_fen            do_move, then pos.fen(false, false, 0)
      terminal_status    is_immediate_game_end, else checkmate/stalemate
                         value by checkers(), mapped by side to move
      canonical_fen      AtomicDB canonicaliser v2: the en passant square
                         survives only when executing it is actually legal,
                         decided by comparing the legal sets with and without

  Rejections carry the same machine-readable codes as the Python reference,
  because a differential that only checks "both said no" would pass while the
  two disagreed about why — which is a disagreement wearing a matching hat.

  The proof obligations themselves are documented in atomicdb/survive.py; this
  file deliberately does not restate them, so there is one place to change if
  they ever move.
*/

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <vector>

#include "bitboard.h"
#include "misc.h"
#include "movegen.h"
#include "piece.h"
#include "position.h"
#include "psqt.h"
#include "search.h"
#include "thread.h"
#include "types.h"
#include "uci.h"
#include "variant.h"

using namespace Stockfish;

namespace {

const char* const CERTIFICATE_FORMAT = "atomic-survival-threshold-v1";
const char* const RULESET_ID = "atomic-fide-claim-v1";
const char* const REPETITION_MODE = "NO_REPETITION_SHORTCUTS";
const char* const TERMINAL_PRECEDENCE_ID = "terminal-before-clock/1";
const char* const VARIANT = "atomic";
const int         CANONICAL_VERSION = 2;

const int TAU_MAX = 100;

// The one variant this tool speaks, resolved once. See Movegen::build.
const Variant* g_rules = nullptr;

const Variant* rules() { return g_rules; }

void bind_variant() {
    g_rules = variants.find(std::string(VARIANT))->second;
    UCI::init_variant(g_rules);
}

// Structural ceilings, mirroring the Python reference.
const size_t MAX_STATES = 20000;
const size_t MAX_EDGES = 200000;
const size_t MAX_FANOUT = 256;
const size_t MAX_HEADER_LINES = 32;

// ---------------------------------------------------------------------------
// Rejection, typed
// ---------------------------------------------------------------------------

struct Verdict {
    bool        ok = true;
    std::string code;
    std::string message;
};

Verdict fail(const std::string& code, const std::string& message) {
    Verdict verdict;
    verdict.ok      = false;
    verdict.code    = code;
    verdict.message = message;
    return verdict;
}

std::string to_text(int value) {
    char buffer[32];
    std::snprintf(buffer, sizeof(buffer), "%d", value);
    return std::string(buffer);
}

// ---------------------------------------------------------------------------
// The move generator, wrapped exactly the way pyffish wraps it
// ---------------------------------------------------------------------------

class Movegen {
   public:
    explicit Movegen(uint64_t positionBudget) :
        budget(positionBudget) {}

    uint64_t spent() const { return built; }
    bool     exhausted() const { return built > budget; }

    std::vector<std::string> legal_moves(const std::string& fen) {
        Position     pos;
        StateListPtr states;
        build(pos, states, fen);
        std::vector<std::string> moves;
        for (const auto& move : MoveList<LEGAL>(pos))
            moves.push_back(UCI::move(pos, move));
        return moves;
    }

    // pyffish get_fen(variant, fen, [uci]): apply, then serialise.
    std::string advance(const std::string& fen, const std::string& uci) {
        Position     pos;
        StateListPtr states;
        build(pos, states, fen);
        std::string parsed = uci;
        Move        move   = UCI::to_move(pos, parsed);
        if (move == MOVE_NONE)
            return std::string();
        states->emplace_back();
        pos.do_move(move, states->back());
        return pos.fen(false, false, 0);
    }

    // logic.terminal_status: empty string when the position is interior.
    //
    // One construction instead of pyffish's three. The Python reference calls
    // is_immediate_game_end, legal_moves and game_result as three separate
    // module calls and therefore builds the position three times; the calls
    // and their order are identical here, only the rebuilding is dropped.
    std::string terminal_status(const std::string& fen) {
        Position     pos;
        StateListPtr states;
        build(pos, states, fen);

        Value      result    = VALUE_DRAW;
        const bool immediate = pos.is_immediate_game_end(result);
        const bool anyMove   = MoveList<LEGAL>(pos).size() != 0;
        if (!immediate && anyMove)
            return std::string();
        if (!immediate)
            result = pos.checkers() ? pos.checkmate_value() : pos.stalemate_value();

        const bool whiteToMove = pos.side_to_move() == WHITE;
        if (result > VALUE_DRAW)
            return whiteToMove ? "WHITE_WIN" : "BLACK_WIN";
        if (result < VALUE_DRAW)
            return whiteToMove ? "BLACK_WIN" : "WHITE_WIN";
        return "DRAW";
    }

    // AtomicDB canonicaliser v2. The en passant square is part of the identity
    // of a position, so a PHANTOM right -- declared by the move generator but
    // impossible to execute, because taking would explode our own king or the
    // capturing pawn is pinned -- would split one position into two keys. It
    // survives only when the legal sets differ with and without it.
    std::string canonical(const std::string& fen) {
        std::vector<std::string> parts = split(fen);
        while (parts.size() < 6)
            parts.push_back("-");
        if (parts[3] != "-" && !en_passant_matters(parts))
            parts[3] = "-";
        return parts[0] + " " + parts[1] + " " + parts[2] + " " + parts[3]
             + " 0 1";
    }

    static std::vector<std::string> split(const std::string& text) {
        std::vector<std::string> parts;
        std::istringstream       stream(text);
        std::string              token;
        while (stream >> token)
            parts.push_back(token);
        return parts;
    }

   private:
    // WHERE THE COST ACTUALLY WAS.
    //
    // pyffish's buildPosition calls UCI::init_variant(v) for every single
    // position it constructs, and that is pieceMap.init(v) followed by
    // Bitboards::init_pieces() -- a full rebuild of the piece attack tables.
    // It has to: the variant is an argument of every pyffish call, so the
    // library cannot know the caller is not about to switch to shogi. That
    // rebuild, not the Python layer, is where the ~15 ms per position went,
    // and measuring it is what corrected the cost model in the phase 3 report.
    //
    // This tool speaks one variant and only one, so the tables are built once
    // in main() and the result is identical by construction -- they depend on
    // the variant and on nothing else. The variant pointer is checked rather
    // than assumed, because "identical by construction" stops being true the
    // moment somebody adds a second variant here.
    void build(Position& pos, StateListPtr& states, const std::string& fen) {
        ++built;
        states = StateListPtr(new std::deque<StateInfo>(1));
        pos.set(rules(), fen, false, &states->back(), Threads.main());
    }

    bool en_passant_matters(const std::vector<std::string>& parts) {
        const std::string with = parts[0] + " " + parts[1] + " " + parts[2]
                               + " " + parts[3] + " 0 1";
        const std::string without = parts[0] + " " + parts[1] + " " + parts[2]
                                  + " - 0 1";
        std::vector<std::string> a = legal_moves(with);
        std::vector<std::string> b = legal_moves(without);
        std::set<std::string>    left(a.begin(), a.end());
        std::set<std::string>    right(b.begin(), b.end());
        return left != right;
    }

    uint64_t budget = 0;
    uint64_t built  = 0;
};

// ---------------------------------------------------------------------------
// The certificate
// ---------------------------------------------------------------------------

struct Edge {
    std::string move;
    std::string target;
};

struct Certificate {
    std::map<std::string, std::string> header;
    std::vector<int>                   tau;
    std::vector<std::string>           fen;
    std::map<std::string, size_t>      byFen;
    std::map<size_t, std::vector<Edge>> white;
    std::map<size_t, Edge>              black;
    size_t                              edges = 0;
};

std::string trim(const std::string& text) {
    size_t first = text.find_first_not_of(" \t\r\n");
    if (first == std::string::npos)
        return std::string();
    size_t last = text.find_last_not_of(" \t\r\n");
    return text.substr(first, last - first + 1);
}

bool parse_int(const std::string& text, int& out) {
    if (text.empty())
        return false;
    char*     end   = nullptr;
    const long value = std::strtol(text.c_str(), &end, 10);
    if (end == text.c_str() || *end != '\0')
        return false;
    out = int(value);
    return true;
}

int fifty_move_counter(const std::string& fen) {
    std::vector<std::string> parts = Movegen::split(fen);
    int                      value = 0;
    if (parts.size() >= 5 && parse_int(parts[4], value))
        return value;
    return 0;
}

Verdict parse(const std::string& text, Certificate& cert) {
    std::vector<std::string> lines;
    {
        std::istringstream stream(text);
        std::string        line;
        while (std::getline(stream, line))
            lines.push_back(line);
    }
    if (lines.empty() || trim(lines[0]) != std::string("# ") + CERTIFICATE_FORMAT)
        return fail("format-unknown", "unknown certificate format");

    size_t start   = 0;
    bool   terminated = false;
    for (size_t i = 1; i < lines.size() && i < MAX_HEADER_LINES; ++i)
    {
        const std::string line = trim(lines[i]);
        if (line == "---")
        {
            start      = i + 1;
            terminated = true;
            break;
        }
        if (line.empty())
            continue;
        const size_t space = line.find(' ');
        if (space == std::string::npos || space == 0
            || space + 1 >= line.size())
            return fail("header-line-malformed",
                        "malformed header line: " + line);
        cert.header[line.substr(0, space)] = trim(line.substr(space + 1));
    }
    if (!terminated)
        return fail("header-unterminated", "certificate header is not terminated");

    bool seenEdge = false;
    for (size_t i = start; i < lines.size(); ++i)
    {
        const std::string line = trim(lines[i]);
        if (line.empty())
            continue;
        const size_t      space = line.find(' ');
        const std::string kind  = line.substr(0, space == std::string::npos
                                                   ? line.size() : space);
        const std::string rest  = space == std::string::npos
                                  ? std::string() : line.substr(space + 1);

        if (kind == "S")
        {
            if (seenEdge)
                return fail("state-after-edge", "a state is declared after an edge");
            std::vector<std::string> head = Movegen::split(rest);
            if (head.size() < 3)
                return fail("state-record-malformed", "malformed state record: " + line);
            int stateId = 0, tau = 0;
            if (!parse_int(head[0], stateId) || !parse_int(head[1], tau))
                return fail("state-record-malformed", "malformed state record: " + line);
            if (size_t(stateId) != cert.fen.size())
                return fail("state-id-gap",
                            "state identifiers must run from 0 without gaps");
            if (cert.fen.size() >= MAX_STATES)
                return fail("state-limit", "certificate exceeds the state limit");
            if (tau < 0 || tau > TAU_MAX)
                return fail("tau-range", "state " + to_text(stateId) + " has tau "
                                           + to_text(tau) + " outside [0, 100]");
            // The FEN is the remainder of the line, spaces and all.
            size_t offset = rest.find(head[0]);
            offset        = rest.find(head[1], offset + head[0].size());
            offset        = rest.find_first_not_of(" \t", offset + head[1].size());
            const std::string fen = trim(rest.substr(offset));
            if (cert.byFen.count(fen))
                return fail("state-duplicate",
                            "state " + to_text(stateId)
                              + " repeats the position of state "
                              + to_text(int(cert.byFen[fen])));
            cert.byFen[fen] = cert.fen.size();
            cert.fen.push_back(fen);
            cert.tau.push_back(tau);
            continue;
        }

        if (kind == "W" || kind == "B")
        {
            seenEdge                        = true;
            std::vector<std::string> fields = Movegen::split(rest);
            if (fields.size() != 3)
                return fail("edge-record-malformed", "malformed edge record: " + line);
            int stateId = 0;
            if (!parse_int(fields[0], stateId))
                return fail("edge-record-malformed", "malformed edge record: " + line);
            if (stateId < 0 || size_t(stateId) >= cert.fen.size())
                return fail("edge-unknown-state",
                            "edge cites unknown state " + fields[0]);
            ++cert.edges;
            if (cert.edges > MAX_EDGES)
                return fail("edge-limit", "certificate exceeds the edge limit");
            Edge edge;
            edge.move   = fields[1];
            edge.target = fields[2];
            if (kind == "W")
            {
                std::vector<Edge>& bucket = cert.white[size_t(stateId)];
                if (bucket.size() >= MAX_FANOUT)
                    return fail("fanout-limit",
                                "a White state exceeds the fan-out limit");
                bucket.push_back(edge);
            }
            else
            {
                if (cert.black.count(size_t(stateId)))
                    return fail("black-reply-duplicate",
                                "state " + fields[0]
                                  + " has more than one Black reply");
                cert.black[size_t(stateId)] = edge;
            }
            continue;
        }

        return fail("record-kind-unknown", "unknown record kind '" + kind + "'");
    }
    return Verdict();
}

// ``#id`` -> index, ``T`` -> -1.
Verdict resolve(const std::string& target, size_t stateCount, int& out) {
    if (target == "T")
    {
        out = -1;
        return Verdict();
    }
    if (target.empty() || target[0] != '#')
        return fail("edge-target-malformed", "malformed edge target '" + target + "'");
    int index = 0;
    if (!parse_int(target.substr(1), index))
        return fail("edge-target-malformed", "malformed edge target '" + target + "'");
    if (index < 0 || size_t(index) >= stateCount)
        return fail("edge-target-unknown",
                    "edge target #" + to_text(index) + " is not a state");
    out = index;
    return Verdict();
}

struct Report {
    int      rootTau     = 0;
    int      entryClock  = 0;
    size_t   states      = 0;
    size_t   edges       = 0;
    size_t   reachable   = 0;
    size_t   zeroing     = 0;
    size_t   exits       = 0;
    int      maxTau      = 0;
    uint64_t positions   = 0;
};

Verdict verify(const std::string& text, const std::string& rootOverride,
               uint64_t budget, Report& report) {
    Certificate cert;
    Verdict     verdict = parse(text, cert);
    if (!verdict.ok)
        return verdict;

    if (cert.header["ruleset"] != RULESET_ID)
        return fail("ruleset-mismatch",
                    "certificate ruleset '" + cert.header["ruleset"]
                      + "' is not '" + RULESET_ID + "'");
    if (cert.header["repetition"] != REPETITION_MODE)
        return fail("repetition-mismatch",
                    "certificate repetition mode '" + cert.header["repetition"]
                      + "' is not '" + REPETITION_MODE + "'");
    if (cert.header["terminal_precedence"] != TERMINAL_PRECEDENCE_ID)
        return fail("precedence-mismatch",
                    "certificate terminal precedence '"
                      + cert.header["terminal_precedence"] + "' is not '"
                      + TERMINAL_PRECEDENCE_ID + "'");
    if (cert.header["canonical"] != to_text(CANONICAL_VERSION))
        return fail("canonical-version-mismatch",
                    "certificate canonical version '" + cert.header["canonical"]
                      + "' is not " + to_text(CANONICAL_VERSION));

    const std::string certRoot = cert.header.count("root") ? cert.header["root"]
                                                           : std::string();
    if (certRoot.empty())
        return fail("root-missing", "certificate has no root position");

    Movegen movegen(budget);

    if (!rootOverride.empty()
        && movegen.canonical(certRoot) != movegen.canonical(rootOverride))
        return fail("root-mismatch", "certificate refutes a different position");

    if (!cert.header.count("entry_clock"))
        return fail("entry-clock-missing", "certificate has no entry clock");
    int entryClock = 0;
    if (!parse_int(cert.header["entry_clock"], entryClock))
        return fail("entry-clock-malformed", "certificate entry clock is malformed");
    if (entryClock < 0 || entryClock > TAU_MAX)
        return fail("entry-clock-range", "certificate entry clock is out of range");
    const int actual =
      fifty_move_counter(rootOverride.empty() ? certRoot : rootOverride);
    if (entryClock != std::min(actual, TAU_MAX))
        return fail("entry-clock-mismatch",
                    "certificate entry clock " + to_text(entryClock)
                      + " does not match the root position's halfmove counter "
                      + to_text(actual));

    if (cert.fen.empty())
        return fail("states-empty", "certificate has no states");

    const char* counted[2] = {"states", "edges"};
    const size_t values[2] = {cert.fen.size(), cert.edges};
    for (int which = 0; which < 2; ++which)
    {
        if (!cert.header.count(counted[which]))
            continue;
        int declared = 0;
        if (!parse_int(cert.header[counted[which]], declared))
            return fail("count-malformed",
                        std::string("certificate ") + counted[which]
                          + " count is malformed");
        if (size_t(declared) != values[which])
            return fail("count-mismatch",
                        "certificate declares " + to_text(declared) + " "
                          + counted[which] + " but carries "
                          + to_text(int(values[which])));
    }

    // PASS ONE: the states in isolation.
    std::vector<std::vector<Edge>> plan(cert.fen.size());
    for (size_t id = 0; id < cert.fen.size(); ++id)
    {
        const std::string& fen = cert.fen[id];
        if (movegen.canonical(fen) != fen)
            return fail("state-not-canonical",
                        "state " + to_text(int(id))
                          + " is not in canonical form: " + fen);
        if (movegen.exhausted())
            return fail("budget-exceeded", "certificate exceeds the move generator budget");
        const std::string status = movegen.terminal_status(fen);
        if (!status.empty())
            return fail("state-terminal",
                        "state " + to_text(int(id)) + " is already terminal ("
                          + status + "): " + fen);

        std::vector<std::string> parts = Movegen::split(fen);
        const bool whiteToMove = parts.size() > 1 && parts[1] == "w";
        std::vector<std::string> legal = movegen.legal_moves(fen);
        if (movegen.exhausted())
            return fail("budget-exceeded", "certificate exceeds the move generator budget");

        if (whiteToMove)
        {
            if (cert.black.count(id))
                return fail("white-state-has-black-reply",
                            "state " + to_text(int(id))
                              + " has a Black reply but White is to move");
            const std::vector<Edge>& listed = cert.white[id];
            std::set<std::string>    moves;
            for (const Edge& edge : listed)
                if (!moves.insert(edge.move).second)
                    return fail("white-move-duplicate",
                                "state " + to_text(int(id)) + " repeats a White move");
            std::set<std::string> legalSet(legal.begin(), legal.end());
            if (moves != legalSet)
            {
                std::string missing, extra;
                for (const std::string& move : legalSet)
                    if (!moves.count(move))
                        missing += (missing.empty() ? "" : ", ") + move;
                for (const std::string& move : moves)
                    if (!legalSet.count(move))
                        extra += (extra.empty() ? "" : ", ") + move;
                return fail("white-coverage-mismatch",
                            "state " + to_text(int(id))
                              + " does not cover exactly the legal White moves "
                                "(missing=[" + missing + "], extra=[" + extra + "])");
            }
            plan[id] = listed;
        }
        else
        {
            if (cert.white.count(id))
                return fail("black-state-has-white-moves",
                            "state " + to_text(int(id))
                              + " has White moves but Black is to move");
            if (!cert.black.count(id))
                return fail("black-reply-missing",
                            "state " + to_text(int(id)) + " has no Black reply");
            const Edge& edge = cert.black[id];
            if (std::find(legal.begin(), legal.end(), edge.move) == legal.end())
                return fail("black-reply-illegal",
                            "state " + to_text(int(id)) + " selects illegal reply '"
                              + edge.move + "'");
            plan[id].push_back(edge);
        }
    }

    // PASS TWO: the edges and the two local inequalities.
    for (size_t id = 0; id < plan.size(); ++id)
    {
        const int tau = cert.tau[id];
        for (const Edge& edge : plan[id])
        {
            const std::string child = movegen.advance(cert.fen[id], edge.move);
            if (movegen.exhausted())
                return fail("budget-exceeded",
                            "certificate exceeds the move generator budget");
            const bool zeroing = fifty_move_counter(child) == 0;
            int        index   = 0;
            verdict            = resolve(edge.target, cert.fen.size(), index);
            if (!verdict.ok)
                return verdict;

            if (index < 0)
            {
                const std::string status = movegen.terminal_status(child);
                if (status.empty())
                    return fail("terminal-claim-but-game-continues",
                                "state " + to_text(int(id)) + ": move " + edge.move
                                  + " is declared terminal but the game continues");
                if (status == "WHITE_WIN")
                    return fail("terminal-reaches-white-win",
                                "state " + to_text(int(id)) + ": move " + edge.move
                                  + " reaches a White win");
                ++report.exits;
                report.zeroing += zeroing;
                continue;
            }

            const std::string childCanonical = movegen.canonical(child);
            if (cert.fen[size_t(index)] != childCanonical)
                return fail("edge-lands-elsewhere",
                            "state " + to_text(int(id)) + ": move " + edge.move
                              + " lands on " + childCanonical + " but claims state "
                              + to_text(index));
            const int childTau = cert.tau[size_t(index)];
            if (zeroing)
            {
                if (childTau != 0)
                    return fail("reset-into-nonzero-tau",
                                "state " + to_text(int(id)) + ": zeroing move "
                                  + edge.move + " enters state " + to_text(index)
                                  + " with tau " + to_text(childTau) + ", not 0");
                ++report.zeroing;
            }
            else if (childTau > tau + 1)
                return fail("quiet-tau-inequality",
                            "state " + to_text(int(id)) + " (tau " + to_text(tau)
                              + "): quiet move " + edge.move + " enters state "
                              + to_text(index) + " with tau " + to_text(childTau)
                              + " > " + to_text(tau + 1));
        }
    }

    const std::string rootCanonical = movegen.canonical(certRoot);
    if (!cert.byFen.count(rootCanonical))
        return fail("root-not-a-state", "the root position is not among the states");
    const size_t rootId = cert.byFen[rootCanonical];
    if (cert.tau[rootId] > entryClock)
        return fail("root-tau-above-clock",
                    "certificate proves survival only from clock "
                      + to_text(cert.tau[rootId]) + ", but the root enters at "
                      + to_text(entryClock));

    std::vector<char>   reached(cert.fen.size(), 0);
    std::vector<size_t> stack;
    reached[rootId] = 1;
    stack.push_back(rootId);
    size_t reachedCount = 1;
    while (!stack.empty())
    {
        const size_t current = stack.back();
        stack.pop_back();
        for (const Edge& edge : plan[current])
        {
            int index = 0;
            if (!resolve(edge.target, cert.fen.size(), index).ok || index < 0)
                continue;
            if (!reached[size_t(index)])
            {
                reached[size_t(index)] = 1;
                ++reachedCount;
                stack.push_back(size_t(index));
            }
        }
    }

    report.rootTau    = cert.tau[rootId];
    report.entryClock = entryClock;
    report.states     = cert.fen.size();
    report.edges      = cert.edges;
    report.reachable  = reachedCount;
    report.maxTau     = *std::max_element(cert.tau.begin(), cert.tau.end());
    report.positions  = movegen.spent();
    return Verdict();
}

std::string escape(const std::string& text) {
    std::string out;
    for (char character : text)
    {
        if (character == '"' || character == '\\')
            out += '\\';
        if (character == '\n' || character == '\r')
        {
            out += ' ';
            continue;
        }
        out += character;
    }
    return out;
}

void usage() {
    std::cout << "usage: survive50-verify <certificate> [--root FEN] "
                 "[--budget N] [--repeat N]\n";
}

}  // namespace

int main(int argc, char** argv) {
    std::string certificatePath, rootFen;
    uint64_t    budget = 200000;
    int         repeat = 1;

    for (int i = 1; i < argc; ++i)
    {
        const std::string argument = argv[i];
        if (argument == "--root" && i + 1 < argc)
            rootFen = argv[++i];
        else if (argument == "--budget" && i + 1 < argc)
            budget = std::strtoull(argv[++i], nullptr, 10);
        else if (argument == "--repeat" && i + 1 < argc)
            repeat = std::atoi(argv[++i]);
        else if (argument == "--help" || argument == "-h")
        {
            usage();
            return 2;
        }
        else if (certificatePath.empty())
            certificatePath = argument;
        else
        {
            usage();
            return 2;
        }
    }
    if (certificatePath.empty())
    {
        usage();
        return 2;
    }

    std::ifstream input(certificatePath.c_str(), std::ios::binary);
    if (!input)
    {
        std::cout << "{\"ok\":false,\"code\":\"io-error\",\"message\":\"cannot read "
                  << escape(certificatePath) << "\"}\n";
        return 2;
    }
    std::ostringstream buffer;
    buffer << input.rdbuf();
    const std::string text = buffer.str();

    // The pyffish initialisation sequence, in its order.
    pieceMap.init();
    variants.init();
    UCI::init(Options);
    PSQT::init(variants.find(Options["UCI_Variant"])->second);
    Bitboards::init();
    Position::init();
    Bitbases::init();
    Search::init();
    Threads.set(size_t(Options["Threads"]));
    Search::clear();
    bind_variant();

    if (repeat < 1)
        repeat = 1;

    Report        report;
    Verdict       verdict;
    const TimePoint started = now();
    for (int pass = 0; pass < repeat; ++pass)
    {
        report  = Report();
        verdict = verify(text, rootFen, budget, report);
    }
    const TimePoint elapsed = now() - started;

    const double seconds = double(elapsed) / 1000.0;
    const double rate    = seconds > 0.0
                           ? double(report.positions) * double(repeat) / seconds
                           : 0.0;

    if (!verdict.ok)
        std::cout << "{\"ok\":false,\"code\":\"" << escape(verdict.code)
                  << "\",\"message\":\"" << escape(verdict.message) << "\"}\n";
    else
        std::cout << "{\"ok\":true,\"result\":\"DISPROVED_WHITE_WIN\""
                  << ",\"root_tau\":" << report.rootTau
                  << ",\"entry_clock\":" << report.entryClock
                  << ",\"states\":" << report.states
                  << ",\"edges\":" << report.edges
                  << ",\"reachable\":" << report.reachable
                  << ",\"zeroing_edges\":" << report.zeroing
                  << ",\"terminal_exits\":" << report.exits
                  << ",\"max_tau\":" << report.maxTau
                  << ",\"positions\":" << report.positions
                  << ",\"repeat\":" << repeat
                  << ",\"elapsed_ms\":" << int64_t(elapsed)
                  << ",\"positions_per_second\":" << int64_t(rate)
                  << "}\n";

    Threads.set(0);
    return verdict.ok ? 0 : 1;
}
