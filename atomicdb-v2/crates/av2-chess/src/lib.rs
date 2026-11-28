//! The rules of atomic chess for AtomicDB v2, through the very Fairy-Stockfish
//! build v1 runs.
//!
//! v1 asks `pyffish` 0.0.89 for every rule (`atomicdb/logic.py`); production
//! runs that version with one patch that only caches the variant tables
//! (`pyffish-0.0.89+variantcache`, KOTH-Stockfish PR #102, commit
//! `b523fd1d`).  This crate compiles the same sources (`fsf/`, see
//! `fsf/VENDOR.md`) with the same flags and the same initialisation, behind a
//! small C ABI (`shim/av2fsf.cpp`).  Nothing here reimplements a rule.
//!
//! Two layers:
//!
//! - [`pf`]: one function per pyffish call v1 makes (`legal_moves`,
//!   `get_fen`, `game_result`, `is_immediate_game_end`, `start_fen`,
//!   `validate_fen`), same arguments, same answers.
//! - this module: `atomicdb/logic.py`'s identity and terminal functions on top
//!   of them, line by line: [`canonical_fen`], [`key_of`], [`apply_move`],
//!   [`is_zeroing`], [`terminal_status`], [`start_fen`], plus the tablebase
//!   applicability helpers (TB-1.1, TB-1.3), and [`expansion`], the pure half
//!   of `ingest.expand` (EXP-1.1) that the preparers run outside the writer.
//!
//! **Threads.**  Every function may be called from any number of threads at
//! once (plan open question 3; measured in `tests/threads.rs` and in the PR).
//! The library is initialised once, on first use.
//!
//! **Untrusted FENs.**  Like pyffish, the movegen trusts its input.  v1 only
//! hands it FENs the movegen produced or the public parser validated; v2 must
//! do the same: run [`pf::validate_fen`] on anything that comes from outside.

mod ffi;
pub mod pf;

use av2_model::{Key, Status};

/// `logic.VARIANT`.
pub const VARIANT: &str = "atomic";
/// `logic.RULESET_ID`: the statement being proved.
pub const RULESET_ID: &str = "atomic-fide-claim-v1";
/// `logic.CANONICAL_VERSION`: how a position becomes a key (v2 keeps the
/// en-passant square only when the capture is executable).
pub const CANONICAL_VERSION: u32 = 2;
/// `logic.FIFTY_MOVE_PLIES`.
pub const FIFTY_MOVE_PLIES: u8 = 100;
/// `logic.CLOCK_SLACK_MAX`.
pub const CLOCK_SLACK_MAX: u8 = 100;

/// Why a call failed.  pyffish raises in the same places.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChessError {
    /// A string with a NUL byte cannot cross the C boundary.
    Nul,
    /// The `index`-th move (0-based) of the list is not legal there
    /// (pyffish: `ValueError: Invalid move`).
    IllegalMove { index: usize, uci: String },
    /// The C++ side threw.
    Engine(&'static str),
}

impl std::fmt::Display for ChessError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ChessError::Nul => write!(f, "NUL byte in a FEN or move"),
            ChessError::IllegalMove { uci, .. } => write!(f, "Invalid move '{uci}'"),
            ChessError::Engine(what) => write!(f, "Fairy-Stockfish failed in {what}"),
        }
    }
}
impl std::error::Error for ChessError {}

pub type Result<T> = std::result::Result<T, ChessError>;

// ------------------------------------------------------------- identity

/// Python's `str.split()` (any run of whitespace, no empty fields).
#[inline]
fn py_split(s: &str) -> impl Iterator<Item = &str> {
    s.split_whitespace()
}

/// `logic._en_passant_right_exists(prefix)`: does the declared en-passant
/// right change the set of legal moves?  `prefix` is the first four FEN
/// fields.  Asked of the movegen, never of a reimplementation: legal moves
/// with the square and without it (counters `0 1`); an error counts as "the
/// right exists" (v1's `except Exception: return True`).
pub fn en_passant_right_exists(prefix: &str) -> bool {
    let with_right = format!("{prefix} 0 1");
    let fields: Vec<&str> = py_split(prefix).take(3).collect();
    let without_right = format!("{} - 0 1", fields.join(" "));
    match (pf::legal_moves(&with_right), pf::legal_moves(&without_right)) {
        (Ok(mut a), Ok(mut b)) => {
            a.sort_unstable();
            a.dedup();
            b.sort_unstable();
            b.dedup();
            a != b
        }
        _ => true,
    }
}

/// `logic.canonical_fen`: board, side to move, castling, the en-passant
/// square only when the capture is executable, counters `0 1` (POS-1.1).
pub fn canonical_fen(fen: &str) -> String {
    let mut parts: Vec<&str> = py_split(fen).collect();
    if parts.len() >= 4 && parts[3] != "-" && !en_passant_right_exists(&parts[..4].join(" ")) {
        parts.truncate(3);
        parts.push("-");
    }
    parts.truncate(4);
    parts.push("0");
    parts.push("1");
    parts.join(" ")
}

/// `logic.key_of`: sha256 of the canonical FEN.
pub fn key_of(fen: &str) -> Key {
    Key::of_canonical_fen(&canonical_fen(fen))
}

/// `logic.start_fen`: the canonical start position.
pub fn start_fen() -> String {
    canonical_fen(&pf::start_fen())
}

/// `ingest.root_key`: the key of the start position.
pub fn root_key() -> Key {
    static ROOT: std::sync::OnceLock<Key> = std::sync::OnceLock::new();
    *ROOT.get_or_init(|| key_of(&start_fen()))
}

// ------------------------------------------------------------- moves

/// `logic.legal_moves`: UCI moves in movegen order.
pub fn legal_moves(fen: &str) -> Result<Vec<String>> {
    pf::legal_moves(fen)
}

/// `logic._fen_after`: the movegen's FEN after one move, counters included.
pub fn fen_after(fen: &str, uci: &str) -> Result<String> {
    pf::get_fen(fen, &[uci])
}

/// `logic.apply_move`: the canonical FEN after `uci`.
pub fn apply_move(fen: &str, uci: &str) -> Result<String> {
    Ok(canonical_fen(&fen_after(fen, uci)?))
}

/// `logic.is_zeroing`: does the move reset the fifty-move counter?  Asked of
/// the movegen: the counter after the move, from a canonical FEN, is 0.
pub fn is_zeroing(fen: &str, uci: &str) -> Result<bool> {
    let after = fen_after(&canonical_fen(fen), uci)?;
    let parts: Vec<&str> = py_split(&after).collect();
    Ok(parts.len() > 4 && parts[4] == "0")
}

// ------------------------------------------------------------- terminal

/// `logic._both_kings_on_board`.
fn both_kings_on_board(fen: &str) -> bool {
    let board = fen.split(' ').next().unwrap_or("");
    board.contains('K') && board.contains('k')
}

/// `logic._immediate_end`: with both kings on the board the flag is always
/// false for atomic (the only immediate end is extinction); otherwise ask.
pub fn immediate_end(fen: &str) -> Result<bool> {
    if both_kings_on_board(fen) {
        return Ok(false);
    }
    Ok(pf::is_immediate_game_end(fen)?.0)
}

/// `logic.terminal_status` (POS-4.1): `None` when the game goes on, else the
/// result (`WhiteWin`, `BlackWin`, `Draw`; the reason is always `terminal`).
/// The halfmove clock is ignored: mate and explosion precede the claim.
pub fn terminal_status(fen: &str) -> Result<Option<Status>> {
    if !pf::legal_moves(fen)?.is_empty() && !immediate_end(fen)? {
        return Ok(None);
    }
    let res = pf::game_result(fen)?;
    let stm_white = py_split(fen).nth(1) == Some("w");
    Ok(Some(if res > 0 {
        if stm_white { Status::WhiteWin } else { Status::BlackWin }
    } else if res < 0 {
        if stm_white { Status::BlackWin } else { Status::WhiteWin }
    } else {
        Status::Draw
    }))
}

// ------------------------------------------------------------- tablebases

/// `logic.piece_count`: letters of the board field, kings included.
pub fn piece_count(fen: &str) -> usize {
    py_split(fen).next().unwrap_or("").chars().filter(|c| c.is_alphabetic()).count()
}

/// `logic.tb_applicable` (TB-1.1): at most `max_men` pieces, no castling
/// right, no en-passant square.
pub fn tb_applicable(fen: &str, max_men: usize) -> bool {
    let parts: Vec<&str> = py_split(fen).collect();
    piece_count(fen) <= max_men && parts.get(2) == Some(&"-") && parts.get(3) == Some(&"-")
}

/// `logic.wdl_to_status` (TB-1.3): +2 wins for the side to move, -2 loses,
/// anything else (cursed, blessed, draw) is a draw.
pub fn wdl_to_status(wdl: i32, stm_white: bool) -> Status {
    if wdl >= 2 {
        if stm_white { Status::WhiteWin } else { Status::BlackWin }
    } else if wdl <= -2 {
        if stm_white { Status::BlackWin } else { Status::WhiteWin }
    } else {
        Status::Draw
    }
}

// ------------------------------------------------------------- expansion

/// One child of an expansion, as `ingest.expand` computes it before writing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Child {
    /// The move, UCI (`Edge.move_uci`).
    pub uci: String,
    /// Canonical FEN of the child (`Position.fen`).
    pub fen: String,
    /// `key_of(fen)` (`Position.key`).
    pub key: Key,
    /// `terminal_status(fen)`: a new child with a result is born closed.
    pub terminal: Option<Status>,
}

/// The pure half of `ingest.expand` (EXP-1.1): every legal move of `fen` in
/// movegen order, with the child's canonical FEN, key and terminal status.
/// It needs no graph, so the preparers run it before the writer sees the
/// input.  Two moves may lead to the same child (two captures on one square
/// explode the same pieces); both are listed, as v1 writes both edges.
pub fn expansion(fen: &str) -> Result<Vec<Child>> {
    let mut out = Vec::new();
    for (uci, raw) in pf::children(fen)? {
        let fen = canonical_fen(&raw);
        let key = Key::of_canonical_fen(&fen);
        let terminal = terminal_status(&fen)?;
        out.push(Child { uci, fen, key, terminal });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn start_position() {
        assert_eq!(start_fen(), "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
        assert_eq!(root_key().to_hex(), "b1791d7fc9ae3d38966568c257ffb3a02cbf8394cdb4805bc70f64fc3c0b6879");
        assert_eq!(legal_moves(&start_fen()).unwrap().len(), 20);
        assert_eq!(terminal_status(&start_fen()).unwrap(), None);
    }
}
