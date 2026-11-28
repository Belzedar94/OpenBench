//! Step 2 of the port: the exact layer, pure functions of one node and its
//! children.
//!
//! - `backup_status` (proofs file CAS-1.1, `logic.backup_status`), `backup_eval` (CAS-1.2)
//! - clock slack (DTM-5.1 to DTM-5.9, `logic.py`)
//! - fresh context (CAS-1.2, `ingest._supporting_status`)
//! - proof grade of a MINIMAX closure (CAS-3.1, `ingest._minimax_proof`)
//! - slack of a MINIMAX closure (DTM-5.10, `ingest._minimax_slack`)
//! - witness of a new MINIMAX closure (CAS-1.3, `backup_cascade` 1805-1814)

use crate::conv::{CLOCK_SLACK_MAX, is_decisive, is_verified, mover_loss, mover_win};
use av2_model::{Core, Move, PosId, Proof, Status};

// ---------------------------------------------------------------- children

/// One out-edge of a node with the child's record, as the cascade reads it
/// (v1 `Edge` with `select_related('child')`).
#[derive(Clone, Copy, Debug)]
pub struct Kid {
    pub mv: Move,
    pub id: PosId,
    pub core: Core,
}

// ---------------------------------------------------------------- zeroing

/// Does a move reset the fifty-move counter (`logic.is_zeroing`, DTM-5.1)?
///
/// v1 asks the pinned Fairy-Stockfish build for the halfmove counter after
/// the move.  The rules only need the answer, so it is a trait: the default
/// [`BoardZeroing`] reads the parent's FEN (a pawn move or a capture of an
/// occupied square, which is what resets the counter in atomic: en passant
/// and promotions are pawn moves, castling never captures); a caller with
/// the engine library may plug that in instead.
pub trait Zeroing {
    fn is_zeroing(&mut self, parent_fen: &str, mv: Move) -> bool;
}

/// The board of a FEN: one byte per square (`a1` = 0), `0` for empty.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Board(pub [u8; 64]);

impl Board {
    pub fn parse(fen: &str) -> Option<Board> {
        let placement = fen.split(' ').next()?;
        let mut b = [0u8; 64];
        for (ri, rank) in placement.split('/').enumerate() {
            if ri >= 8 {
                return None;
            }
            let r = 7 - ri;
            let mut file = 0usize;
            for c in rank.bytes() {
                if c.is_ascii_digit() {
                    file += (c - b'0') as usize;
                } else {
                    if file >= 8 {
                        return None;
                    }
                    b[r * 8 + file] = c;
                    file += 1;
                }
            }
        }
        Some(Board(b))
    }

    /// Pawn move or capture.
    pub fn is_zeroing(&self, mv: Move) -> bool {
        let piece = self.0[mv.from_sq() as usize];
        piece == b'P' || piece == b'p' || self.0[mv.to_sq() as usize] != 0
    }
}

/// [`Zeroing`] from the parent's board.
#[derive(Default, Debug, Clone, Copy)]
pub struct BoardZeroing;

impl Zeroing for BoardZeroing {
    fn is_zeroing(&mut self, parent_fen: &str, mv: Move) -> bool {
        Board::parse(parent_fen).map(|b| b.is_zeroing(mv)).unwrap_or(false)
    }
}

/// The parent's FEN, parsed at most once per visit and only when a rule
/// needs it (a decisive child through a move, a MINIMAX slack).
pub struct ParentFen<'a> {
    fen: Option<&'a str>,
    board: Option<Option<Board>>,
}

impl<'a> ParentFen<'a> {
    pub fn new(fen: Option<&'a str>) -> Self {
        ParentFen { fen, board: None }
    }
    pub fn is_zeroing(&mut self, z: &mut dyn Zeroing, mv: Move) -> bool {
        match self.fen {
            None => false,
            Some(f) => z.is_zeroing(f, mv),
        }
    }
    /// Fast path for the default oracle.
    pub fn board(&mut self) -> Option<Board> {
        if self.board.is_none() {
            self.board = Some(self.fen.and_then(Board::parse));
        }
        self.board.unwrap()
    }
}

// ---------------------------------------------------------------- slack

/// DTM-5.9 `_clamp_slack`.
#[inline]
pub fn clamp_slack(v: i64) -> u8 {
    v.clamp(0, CLOCK_SLACK_MAX as i64) as u8
}

/// DTM-5.2 `edge_slack`: what a child gives the parent through this edge.
#[inline]
pub fn edge_slack(zeroing: bool, child_slack: Option<u8>) -> u8 {
    if zeroing {
        return CLOCK_SLACK_MAX;
    }
    match child_slack {
        None => 0,
        Some(s) => clamp_slack((s as i64 - 1).min(CLOCK_SLACK_MAX as i64)),
    }
}

/// DTM-5.3 `edge_supports` (fresh-context rule).
#[inline]
pub fn edge_supports(zeroing: bool, child_slack: Option<u8>) -> bool {
    zeroing || matches!(child_slack, Some(s) if s >= 1)
}

/// DTM-5.4 `minimax_slack`: edges as `(zeroing, child_slack)`.
pub fn minimax_slack(winning: &[(bool, Option<u8>)], all: &[(bool, Option<u8>)], mover_wins: bool) -> Option<u8> {
    if mover_wins { winning.iter().map(|&(z, s)| edge_slack(z, s)).max() } else { all.iter().map(|&(z, s)| edge_slack(z, s)).min() }
}

/// DTM-5.5 `line_clock_slack`: walking back from the end of a line, edges as
/// `zeroing[i]` for ply `i`; the smallest slack of the nodes before the end,
/// or None for an empty line or when a quiet move needs a negative margin.
pub fn line_clock_slack(zeroing: &[bool], end_slack: Option<u8>) -> Option<u8> {
    let mut slack = end_slack;
    let mut smallest: Option<u8> = None;
    for &z in zeroing.iter().rev() {
        if !edge_supports(z, slack) {
            return None;
        }
        let s = edge_slack(z, slack);
        slack = Some(s);
        smallest = Some(smallest.map_or(s, |m| m.min(s)));
    }
    smallest
}

/// DTM-5.6 `slack_from_run`.
pub fn slack_from_run(worst_reversible_run: Option<i64>) -> Option<u8> {
    worst_reversible_run.map(|r| clamp_slack(CLOCK_SLACK_MAX as i64 - r))
}

/// DTM-5.7 `slack_from_witness_length`.
pub fn slack_from_witness_length(plies: i64) -> u8 {
    clamp_slack(CLOCK_SLACK_MAX as i64 - plies.max(0))
}

/// DTM-5.8 `slack_from_dtz`.
pub fn slack_from_dtz(dtz: Option<i64>) -> u8 {
    match dtz {
        None => 0,
        Some(d) => clamp_slack(CLOCK_SLACK_MAX as i64 - (d.abs() + 1)),
    }
}

// ---------------------------------------------------------------- minimax

/// Proofs file CAS-1.1 `backup_status`: the three-valued minimax of one node.
pub fn backup_status(stm_white: bool, expanded: bool, child_statuses: &[Status]) -> Status {
    let win = mover_win(stm_white);
    if child_statuses.contains(&win) {
        return win;
    }
    if !expanded {
        return Status::Unknown;
    }
    if !child_statuses.is_empty() && child_statuses.iter().all(|&s| s == mover_loss(stm_white)) {
        return mover_loss(stm_white);
    }
    if !child_statuses.is_empty() && child_statuses.iter().all(|&s| s != Status::Unknown) {
        return Status::Draw;
    }
    Status::Unknown
}

/// Proofs file CAS-1.2 `backup_eval`: max for White, min for Black, None if
/// nothing is known.
pub fn backup_eval(stm_white: bool, child_evals: &[Option<i32>]) -> Option<i32> {
    let vals = child_evals.iter().flatten().copied();
    if stm_white { vals.max() } else { vals.min() }
}

/// CAS-1.2 `_supporting_status`: what a child can support when the cascade
/// asks.  A decisive child through a quiet move without clock margin counts
/// as `UNKNOWN` (with `fresh_context` on).
pub fn supporting_status(kid: &Kid, fresh_context: bool, zeroing: impl FnOnce(Move) -> bool) -> Status {
    let s = kid.core.status();
    if matches!(s, Status::Unknown | Status::Draw) || !fresh_context {
        return s;
    }
    if edge_supports(zeroing(kid.mv), kid.core.slack()) { s } else { Status::Unknown }
}

/// CAS-3.1 `_minimax_proof`: the weakest assurance the backup shape needs.
pub fn minimax_proof(stm_white: bool, kids: &[Kid], status: Status) -> Proof {
    if kids.is_empty() || status == Status::Unknown {
        return Proof::None;
    }
    if status == mover_win(stm_white) {
        if kids.iter().any(|k| k.core.status() == status && is_verified(&k.core)) { Proof::AndOr } else { Proof::Engine }
    } else if kids.iter().all(|k| is_verified(&k.core)) {
        Proof::AndOr
    } else {
        Proof::Engine
    }
}

/// DTM-5.10 `_minimax_slack`: the slack of a MINIMAX closure from its
/// children.  `zeroing(move)` answers DTM-5.1 for the node's FEN.
pub fn node_minimax_slack(stm_white: bool, status: Status, kids: &[Kid], mut zeroing: impl FnMut(Move) -> bool) -> Option<u8> {
    if !is_decisive(status) {
        return None;
    }
    if status == mover_win(stm_white) {
        kids.iter().filter(|k| k.core.status() == status).map(|k| edge_slack(zeroing(k.mv), k.core.slack())).max()
    } else {
        kids.iter().map(|k| edge_slack(zeroing(k.mv), k.core.slack())).min()
    }
}

/// CAS-1.3: the witness of a new MINIMAX closure, first in edge order (`kids`
/// sorted by move): a verified child with the new status, else any child with
/// it, else any decided child.  Raw child statuses.
pub fn minimax_witness(kids: &[Kid], status: Status) -> Option<Move> {
    kids.iter()
        .find(|k| k.core.status() == status && is_verified(&k.core))
        .or_else(|| kids.iter().find(|k| k.core.status() == status))
        .or_else(|| kids.iter().find(|k| k.core.status() != Status::Unknown))
        .map(|k| k.mv)
}

#[cfg(test)]
mod tests {
    //! `test_clock_slack.py::SlackRecurrenceTests`, `::ZeroingTests`; the
    //! pure half of `test_proofs.py::MinimaxProofInheritanceTests`.
    use super::*;
    use crate::testkit::START_FEN;
    use av2_model::Closure;

    fn mv(s: &str) -> Move {
        Move::parse(s).unwrap()
    }

    const QUIET_FEN: &str = "8/8/8/8/8/8/1k6/K6Q w - - 0 1";
    const CAPTURE_FEN: &str = "4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1";

    fn z(fen: &str, m: &str) -> bool {
        BoardZeroing.is_zeroing(fen, mv(m))
    }

    #[test]
    fn test_pawn_moves_and_captures_reset_the_counter() {
        assert!(z(START_FEN, "e2e4"));
        assert!(z(CAPTURE_FEN, "e4d5"));
    }

    #[test]
    fn test_quiet_moves_do_not() {
        assert!(!z(START_FEN, "g1f3"));
        assert!(!z(QUIET_FEN, "h1h2"));
    }

    #[test]
    fn test_a_zeroing_edge_restores_the_full_range() {
        assert_eq!(edge_slack(z(START_FEN, "e2e4"), Some(3)), 100);
        assert_eq!(edge_slack(z(START_FEN, "g1f3"), Some(3)), 2);
    }

    #[test]
    fn test_an_unmeasured_child_is_treated_as_zero() {
        assert_eq!(edge_slack(z(QUIET_FEN, "h1h2"), None), 0);
        assert!(!edge_supports(false, None));
        assert!(!edge_supports(false, Some(0)));
        assert!(edge_supports(false, Some(1)));
        assert!(edge_supports(true, None));
    }

    #[test]
    fn test_minimax_win_takes_the_best_usable_winning_edge() {
        let w = [(z(QUIET_FEN, "h1h2"), Some(10)), (z(QUIET_FEN, "h1h8"), Some(40))];
        assert_eq!(minimax_slack(&w, &[], true), Some(39));
        assert_eq!(minimax_slack(&[], &[], true), None);
    }

    #[test]
    fn test_minimax_loss_takes_the_worst_of_every_edge() {
        let a = [(z(QUIET_FEN, "h1h2"), Some(40)), (z(QUIET_FEN, "h1h8"), Some(10))];
        assert_eq!(minimax_slack(&[], &a, false), Some(9));
        assert_eq!(minimax_slack(&[], &[], false), None);
    }

    #[test]
    fn test_exhaustive_proof_uses_its_worst_reversible_run() {
        assert_eq!(slack_from_run(Some(0)), Some(100));
        assert_eq!(slack_from_run(Some(7)), Some(93));
        assert_eq!(slack_from_run(Some(250)), Some(0));
        assert_eq!(slack_from_run(None), None);
    }

    #[test]
    fn test_uncertified_witness_uses_the_crude_length_bound() {
        assert_eq!(slack_from_witness_length(0), 100);
        assert_eq!(slack_from_witness_length(9), 91);
        assert_eq!(slack_from_witness_length(500), 0);
    }

    #[test]
    fn test_tablebase_needs_a_dtz_or_it_is_zero() {
        assert_eq!(slack_from_dtz(None), 0);
        assert_eq!(slack_from_dtz(Some(0)), 99);
        assert_eq!(slack_from_dtz(Some(10)), 89);
        assert_eq!(slack_from_dtz(Some(-10)), 89);
        assert_eq!(slack_from_dtz(Some(500)), 0);
    }

    #[test]
    fn test_terminal_takes_the_maximum() {
        assert_eq!(CLOCK_SLACK_MAX, 100);
    }

    #[test]
    fn line_clock_slack_walks_back() {
        assert_eq!(line_clock_slack(&[], Some(5)), None);
        // quiet, quiet into an end with slack 3: 2 then 1.
        assert_eq!(line_clock_slack(&[false, false], Some(3)), Some(1));
        // quiet into an end with no margin: refused.
        assert_eq!(line_clock_slack(&[false], Some(0)), None);
        // a capture resets.
        assert_eq!(line_clock_slack(&[false, true], None), Some(99));
    }

    #[test]
    fn backup_status_table() {
        use Status::*;
        assert_eq!(backup_status(true, false, &[WhiteWin]), WhiteWin);
        assert_eq!(backup_status(true, false, &[BlackWin]), Unknown);
        assert_eq!(backup_status(true, true, &[BlackWin, BlackWin]), BlackWin);
        assert_eq!(backup_status(true, true, &[BlackWin, Draw]), Draw);
        assert_eq!(backup_status(true, true, &[BlackWin, Unknown]), Unknown);
        assert_eq!(backup_status(true, true, &[]), Unknown);
        assert_eq!(backup_status(false, true, &[WhiteWin, WhiteWin]), WhiteWin);
        assert_eq!(backup_status(false, true, &[WhiteWin, BlackWin]), BlackWin);
        assert_eq!(backup_eval(true, &[Some(1), None, Some(5)]), Some(5));
        assert_eq!(backup_eval(false, &[Some(1), None, Some(5)]), Some(1));
        assert_eq!(backup_eval(false, &[None]), None);
    }

    fn kid(m: &str, s: Status, c: Closure, p: Proof) -> Kid {
        let mut core = Core::default();
        core.set_status(s, c, p);
        Kid { mv: mv(m), id: PosId(0), core }
    }

    #[test]
    fn proof_grade_and_witness() {
        use Status::*;
        // Mover-win: ANDOR when any winning child is verified.
        let ks = [kid("a2a3", WhiteWin, Closure::MatePv, Proof::Engine), kid("b2b3", WhiteWin, Closure::Terminal, Proof::None)];
        assert_eq!(minimax_proof(true, &ks, WhiteWin), Proof::AndOr);
        assert_eq!(minimax_witness(&ks, WhiteWin), Some(mv("b2b3")));
        let ks = [kid("a2a3", WhiteWin, Closure::MatePv, Proof::Engine)];
        assert_eq!(minimax_proof(true, &ks, WhiteWin), Proof::Engine);
        // A loss or draw needs every child verified.
        let ks = [kid("a2a3", BlackWin, Closure::Tb, Proof::None), kid("b2b3", Draw, Closure::Minimax, Proof::AndOr)];
        assert_eq!(minimax_proof(true, &ks, Draw), Proof::AndOr);
        let ks = [kid("a2a3", BlackWin, Closure::Tb, Proof::None), kid("b2b3", Draw, Closure::Minimax, Proof::Engine)];
        assert_eq!(minimax_proof(true, &ks, Draw), Proof::Engine);
        // Third fallback of the witness: any decided child.
        assert_eq!(minimax_witness(&ks, Draw), Some(mv("b2b3")));
        let ks = [kid("a2a3", Unknown, Closure::None, Proof::None), kid("b2b3", BlackWin, Closure::Tb, Proof::None)];
        assert_eq!(minimax_witness(&ks, Draw), Some(mv("b2b3")));
        assert_eq!(minimax_proof(true, &[], WhiteWin), Proof::None);
        assert_eq!(minimax_proof(true, &ks, Unknown), Proof::None);
    }

    #[test]
    fn supporting_status_fresh_context() {
        use Status::*;
        let mut k = kid("g1f3", WhiteWin, Closure::MatePv, Proof::Engine);
        assert_eq!(supporting_status(&k, true, |_| false), Unknown);
        assert_eq!(supporting_status(&k, true, |_| true), WhiteWin);
        assert_eq!(supporting_status(&k, false, |_| false), WhiteWin);
        k.core.clock_slack = 1;
        assert_eq!(supporting_status(&k, true, |_| false), WhiteWin);
        let d = kid("g1f3", Draw, Closure::Minimax, Proof::Engine);
        assert_eq!(supporting_status(&d, true, |_| false), Draw);
    }
}
