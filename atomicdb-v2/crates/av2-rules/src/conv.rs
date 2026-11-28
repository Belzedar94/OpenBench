//! Step 1 of the port (`rules-spec.md` section 16): the conventions every rule
//! shares (spec section 1.1, 1.2) and the headline value (BKD-10.1).
//!
//! Every stored number is from White's point of view; the side to move comes
//! from the record's `F_BLACK_TO_MOVE` flag (v1: `fen.split()[1] == 'w'`).

use av2_model::{Closure, Core, Move, Proof, Status};

/// `ingest.MATE_BAND` (= `score_bands.BIG_LOW`).
pub const MATE_BAND: i32 = crate::bands::BIG_LOW;
/// `ingest.TB_CLAMP_CEILING` (= `score_bands.TB_TOP`).
pub const TB_CLAMP_CEILING: i32 = crate::bands::TB_TOP;
/// `ingest.PROVEN_QUALITY`: the quality of an exact value.
pub const PROVEN_QUALITY: u64 = av2_model::record::PROVEN_QUALITY;
/// `logic.CLOCK_SLACK_MAX` (= `FIFTY_MOVE_PLIES`).
pub const CLOCK_SLACK_MAX: u8 = 100;

/// White to move?  (v1 `fen.split()[1] == 'w'`.)
#[inline]
pub fn stm_white(core: &Core) -> bool {
    !core.black_to_move()
}

/// The status that is a win for the side to move.
#[inline]
pub fn mover_win(stm_white: bool) -> Status {
    if stm_white { Status::WhiteWin } else { Status::BlackWin }
}

/// The status that is a loss for the side to move.
#[inline]
pub fn mover_loss(stm_white: bool) -> Status {
    if stm_white { Status::BlackWin } else { Status::WhiteWin }
}

#[inline]
pub fn is_decisive(s: Status) -> bool {
    matches!(s, Status::WhiteWin | Status::BlackWin)
}

/// `ingest._status_eval`: the truth value of a decided status.
#[inline]
pub fn status_eval(s: Status) -> Option<i32> {
    match s {
        Status::WhiteWin => Some(10_000),
        Status::BlackWin => Some(-10_000),
        Status::Draw => Some(0),
        Status::Unknown => None,
    }
}

/// `ingest._better_for_mover`: strict, `>` for White, `<` for Black.
#[inline]
pub fn better_for_mover(value: i32, reference: i32, stm_white: bool) -> bool {
    if stm_white { value > reference } else { value < reference }
}

/// `ingest.known_eval_of` (BKD-10.1): status truth value, else backed, else
/// own eval.
#[inline]
pub fn known_eval_of(status: Status, backed_eval: Option<i32>, eval_cp: Option<i32>) -> Option<i32> {
    status_eval(status).or(backed_eval).or(eval_cp)
}

/// `ingest.best_known_eval` on a record.
#[inline]
pub fn best_known_eval(core: &Core) -> Option<i32> {
    known_eval_of(core.status(), core.backed().map(i32::from), core.eval().map(i32::from))
}

/// `ingest._mate_moves_to_plies` (EXP-4.1): UCI `score mate N` (moves) to
/// plies.  The side to move mates on ply `2N-1`; the other side on ply `2N`.
#[inline]
pub fn mate_moves_to_plies(moves: i64, winner_white: bool, stm_white: bool) -> i64 {
    if moves <= 0 {
        return 0;
    }
    if winner_white == stm_white { 2 * moves - 1 } else { 2 * moves }
}

/// `ingest._child_is_verified` (spec 1.2): `TERMINAL` or `TB` closure, or an
/// `ANDOR` proof.
#[inline]
pub fn is_verified(core: &Core) -> bool {
    matches!(core.closure(), Closure::Terminal | Closure::Tb) || core.proof() == Proof::AndOr
}

/// `ingest._closure_is_independent` (spec 1.2): never revoked by a child.
#[inline]
pub fn closure_is_independent(core: &Core) -> bool {
    matches!(core.closure(), Closure::Terminal | Closure::Tb) || (core.closure() != Closure::Minimax && core.proof() == Proof::AndOr)
}

/// A sort key with the order of the UCI strings (`ORDER BY move_uci`,
/// `sorted(..., key=lambda c: c.move)`): `from` file, `from` rank, `to` file,
/// `to` rank, then no promotion before any promotion letter in alphabetical
/// order (`b` < `k` < `n` < `q` < `r`).  `Move::NONE` (v1 NULL / `''`) sorts
/// first, as `''` does.
#[inline]
pub fn uci_order(m: Move) -> u32 {
    if m.is_none() {
        return 0;
    }
    let (f, t) = (m.from_sq() as u32, m.to_sq() as u32);
    // encoding order n=1 b=2 r=3 q=4 k=5 -> alphabetical rank b=1 k=2 n=3 q=4 r=5
    const PROMO_RANK: [u32; 6] = [0, 3, 1, 5, 4, 2];
    let p = PROMO_RANK[(m.promo() as usize).min(5)];
    1 + ((f % 8) << 15 | (f / 8) << 12 | (t % 8) << 9 | (t / 8) << 6 | p)
}

/// Compare two moves as v1 compares their UCI strings.
#[inline]
pub fn uci_cmp(a: Move, b: Move) -> std::cmp::Ordering {
    uci_order(a).cmp(&uci_order(b))
}

/// `mate_in` as an `i64` (None for NULL).
#[inline]
pub fn mate_of(core: &Core) -> Option<i64> {
    core.mate().map(i64::from)
}

/// Store a distance in the 16-bit column.  v1 has no upper bound; the
/// sentinel `NO_MATE` (65535) is never produced: a distance past 65534 (only
/// reachable by an inflating loop that DTM-3 and the rewrite limit stop long
/// before) saturates.  Listed as a deviation in `docs/rules-port-status.md`.
#[inline]
pub fn store_mate(v: Option<i64>) -> u16 {
    match v {
        None => av2_model::record::NO_MATE,
        Some(x) => x.clamp(0, (av2_model::record::NO_MATE - 1) as i64) as u16,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uci_order_is_string_order() {
        let mut all = Vec::new();
        for from in 0..64u8 {
            for to in 0..64u8 {
                for promo in 0..6u8 {
                    let m = Move::new(from, to, promo);
                    if !m.is_none() {
                        all.push(m);
                    }
                }
            }
        }
        let mut by_key = all.clone();
        by_key.sort_by_key(|m| uci_order(*m));
        let mut by_str = all;
        by_str.sort_by_key(|m| m.to_uci());
        assert_eq!(by_key, by_str);
        assert!(uci_order(Move::NONE) < uci_order(Move::parse("a1a2").unwrap()));
    }

    #[test]
    fn status_eval_and_known_eval() {
        assert_eq!(status_eval(Status::WhiteWin), Some(10_000));
        assert_eq!(status_eval(Status::BlackWin), Some(-10_000));
        assert_eq!(status_eval(Status::Draw), Some(0));
        assert_eq!(status_eval(Status::Unknown), None);
        // test_selector_leak.py::RegretUsesBestKnownEvalTests::test_the_value_map_prefers_status_then_backed_then_eval
        assert_eq!(known_eval_of(Status::BlackWin, Some(300), Some(200)), Some(-10_000));
        assert_eq!(known_eval_of(Status::Unknown, Some(300), Some(200)), Some(300));
        assert_eq!(known_eval_of(Status::Unknown, None, Some(200)), Some(200));
        assert_eq!(known_eval_of(Status::Unknown, None, None), None);
        assert_eq!(known_eval_of(Status::Draw, Some(300), Some(200)), Some(0));
    }

    #[test]
    fn mate_moves_to_plies_matches_v1() {
        // test_mate_clamp.py::MateDistanceUnitTests::test_the_worker_formula_decodes_back_to_plies
        assert_eq!(mate_moves_to_plies(1, true, true), 1);
        assert_eq!(mate_moves_to_plies(1, false, true), 2);
        assert_eq!(mate_moves_to_plies(3, false, false), 5);
        assert_eq!(mate_moves_to_plies(3, true, false), 6);
        assert_eq!(mate_moves_to_plies(0, true, true), 0);
        assert_eq!(mate_moves_to_plies(-2, true, true), 0);
    }

    #[test]
    fn better_for_mover_is_strict() {
        assert!(better_for_mover(5, 4, true));
        assert!(!better_for_mover(4, 4, true));
        assert!(better_for_mover(3, 4, false));
        assert!(!better_for_mover(4, 4, false));
    }
}
