/*
  AtomicDB v2: a small C ABI over the Fairy-Stockfish sources that pyffish
  0.0.89 is built from (vendored in ../fsf, see ../fsf/VENDOR.md).

  Every function mirrors one pyffish call that atomicdb/logic.py makes, with
  the same initialisation (PyInit_pyffish) and the same position set-up
  (buildPosition): variant "atomic", chess960 false, Threads.main() as the
  position's thread.  Nothing here reimplements a rule.

  Thread safety.  The global tables (piece map, attack tables, Zobrist keys,
  variant definitions) are written once, in av2fsf_init, under std::call_once,
  and only read afterwards.  Each call builds its own Position and StateInfo
  on the calling thread's stack.  The only shared object a call touches is
  Threads.main(): Position::set stores the pointer, do_move bumps its atomic
  node counter (relaxed) and prefetches its material table; none of that
  changes an answer.  Results are returned in a thread_local buffer that stays
  valid until the next call on the same thread.
*/

#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>

#include "misc.h"
#include "types.h"
#include "bitboard.h"
#include "evaluate.h"
#include "position.h"
#include "search.h"
#include "syzygy/tbprobe.h"
#include "thread.h"
#include "tt.h"
#include "uci.h"
#include "piece.h"
#include "variant.h"
#include "apiutil.h"
#include "psqt.h"

using namespace Stockfish;

namespace {

std::once_flag initFlag;
const Variant* atomicVariant = nullptr;
thread_local std::string outBuf;

void init_once() {
    // PyInit_pyffish, in the same order.
    pieceMap.init();
    variants.init();
    UCI::init(Options);
    PSQT::init(variants.find(Options["UCI_Variant"])->second);
    Bitboards::init();
    Position::init();
    Bitbases::init();
    Search::init();
    Threads.set(Options["Threads"]);
    Search::clear();
    // The first buildPosition("atomic", ...) of a pyffish process.
    atomicVariant = variants.find(std::string("atomic"))->second;
    UCI::init_variant(atomicVariant);
}

// buildPosition with an empty move list.
inline void set_position(Position& pos, StateInfo& st, const char* fen) {
    pos.set(atomicVariant, std::string(fen), false, &st, Threads.main());
}

inline const char* hand_out(size_t* len) {
    *len = outBuf.size();
    return outBuf.data();
}

} // namespace

extern "C" {

int av2fsf_init(void) {
    try {
        std::call_once(initFlag, init_once);
        return atomicVariant ? 0 : -1;
    } catch (...) {
        return -2;
    }
}

// engine_info(), as pyffish.info().  The banner embeds __DATE__; builds made
// with -Brepro (cc-rs on MSVC) have no date and engine_info() throws on it,
// so the banner then names the vendored commit instead.
const char* av2fsf_info(size_t* len) {
    try {
        outBuf = engine_info();
    } catch (...) {
        outBuf = "Fairy-Stockfish b523fd1d LB by Fabian Fichter";
    }
    return hand_out(len);
}

// pf.start_fen('atomic').
const char* av2fsf_start_fen(size_t* len) {
    outBuf = atomicVariant->startFen;
    return hand_out(len);
}

// pf.validate_fen(fen, 'atomic'): FEN::FEN_OK (1) or a negative code.
int av2fsf_validate_fen(const char* fen) {
    try {
        return int(FEN::validate_fen(std::string(fen), atomicVariant, false));
    } catch (...) {
        return -100;
    }
}

// pf.legal_moves('atomic', fen, []): UCI moves in movegen order, separated by
// one space.  Returns the number of moves, or -1 on an exception.
int av2fsf_legal_moves(const char* fen, const char** out, size_t* len) {
    try {
        StateInfo st;
        Position pos;
        set_position(pos, st, fen);
        outBuf.clear();
        int n = 0;
        for (const auto& m : MoveList<LEGAL>(pos)) {
            if (n++)
                outBuf.push_back(' ');
            outBuf += UCI::move(pos, m);
        }
        *out = hand_out(len);
        return n;
    } catch (...) {
        return -1;
    }
}

// pf.get_fen('atomic', fen, moves): moves separated by single spaces (may be
// empty).  Returns 0, or k > 0 when the k-th move (1-based) is not legal
// (pyffish raises ValueError there), or -1 on an exception.
int av2fsf_get_fen(const char* fen, const char* moves, const char** out, size_t* len) {
    try {
        StateListPtr states(new std::deque<StateInfo>(1));
        Position pos;
        set_position(pos, states->back(), fen);
        std::istringstream ss{std::string(moves)};
        std::string token;
        int k = 0;
        while (ss >> token) {
            ++k;
            Move m = UCI::to_move(pos, token);
            if (m == MOVE_NONE)
                return k;
            states->emplace_back();
            pos.do_move(m, states->back());
        }
        outBuf = pos.fen(false, false, 0);
        *out = hand_out(len);
        return 0;
    } catch (...) {
        return -1;
    }
}

// Every legal move with the FEN after it, as pf.get_fen(fen, [uci]) gives it:
// "uci fen\n" per move, movegen order.  Each child is computed on a freshly
// set position (no undo_move), exactly as a separate pyffish call would.
// Returns the number of moves, or -1 on an exception.
int av2fsf_children(const char* fen, const char** out, size_t* len) {
    try {
        StateInfo st;
        Position pos;
        set_position(pos, st, fen);
        outBuf.clear();
        int n = 0;
        for (const auto& m : MoveList<LEGAL>(pos)) {
            StateInfo st0, st1;
            Position child;
            set_position(child, st0, fen);
            outBuf += UCI::move(pos, m);
            outBuf.push_back(' ');
            child.do_move(m, st1);
            outBuf += child.fen(false, false, 0);
            outBuf.push_back('\n');
            ++n;
        }
        *out = hand_out(len);
        return n;
    } catch (...) {
        return -1;
    }
}

// pf.is_immediate_game_end('atomic', fen, []): returns 1/0, result in *result;
// -1 on an exception.  When the game does not end, pyffish hands back an
// uninitialised Value (0 on production, measured); here it is always 0.
int av2fsf_is_immediate_game_end(const char* fen, int* result) {
    try {
        StateInfo st;
        Position pos;
        set_position(pos, st, fen);
        Value r = VALUE_ZERO;
        bool end = pos.is_immediate_game_end(r);
        *result = int(r);
        return end ? 1 : 0;
    } catch (...) {
        return -1;
    }
}

// pf.game_result('atomic', fen, []) (side-to-move POV).  Returns 0, or -1 on
// an exception.  Like pyffish, it does not check that the position has no
// legal move (pyffish only asserts it, and is built with NDEBUG).
int av2fsf_game_result(const char* fen, int* result) {
    try {
        StateInfo st;
        Position pos;
        set_position(pos, st, fen);
        Value r = VALUE_NONE;
        bool end = pos.is_immediate_game_end(r);
        if (!end)
            r = pos.checkers() ? pos.checkmate_value() : pos.stalemate_value();
        *result = int(r);
        return 0;
    } catch (...) {
        return -1;
    }
}

} // extern "C"
