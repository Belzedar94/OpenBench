//! The arithmetic of proof numbers: constants, saturating sums, eval bands,
//! terminal / leaf / internal numbers and the sticky primary.
//!
//! Every function here is a pure port of `atomicdb/proof.py` (rule ids of
//! `docs/rules-spec-proofs.md` in each doc comment).  Numbers are `u64`:
//! v1 uses unbounded Python ints clamped at `PROOF_INFINITY = 2^62`, and every
//! sum here saturates before it can leave 63 bits.

use crate::gate;
use av2_model::Status;

/// PN-1.2: `2^62`, "proved" (dn) or "refuted" (pn).
pub const PROOF_INFINITY: u64 = 1 << 62;
/// PN-1.2: ceiling of a heuristic leaf number.
pub const PROOF_MAX_LEAF: u64 = 1 << 20;
/// PN-5.1: a challenger must be strictly more than 3 times cheaper.
pub const SELECTED_CHILD_HYSTERESIS: u64 = 3;
/// PN-1.5: `(threshold, pn weight, dn weight)` in the attacker's POV, first match wins.
pub const EVAL_BANDS: [(i32, u64, u64); 6] = [(9_000, 1, 64), (800, 2, 16), (300, 4, 8), (-300, 8, 8), (-800, 16, 4), (-9_000, 32, 2)];
/// PN-1.5: below the last threshold.
pub const DEFAULT_EVAL_BAND: (u64, u64) = (64, 1);
/// PN-2.8: the band exempt from the gate (`EVAL_BANDS[0][0]`).
pub const SOLVE_GATE_MATE_BAND: i32 = EVAL_BANDS[0].0;
/// PN-2.8: attacker score from which a taxed leaf is a "disagreement".
pub const SOLVE_GATE_OPTIMISTIC: i32 = 300;

/// v1 `ProofCampaign.Goal`.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Hash)]
pub enum Goal {
    WhiteWin,
    BlackWin,
}

impl Goal {
    /// PN-1.3: the attacker is White iff the goal is `WHITE_WIN`.
    #[inline]
    pub fn attacker_is_white(self) -> bool {
        self == Goal::WhiteWin
    }
    /// The status that proves this goal.
    #[inline]
    pub fn status(self) -> Status {
        match self {
            Goal::WhiteWin => Status::WhiteWin,
            Goal::BlackWin => Status::BlackWin,
        }
    }
    pub fn v1(self) -> &'static str {
        match self {
            Goal::WhiteWin => "WHITE_WIN",
            Goal::BlackWin => "BLACK_WIN",
        }
    }
    pub fn from_v1(s: &str) -> Option<Goal> {
        match s {
            "WHITE_WIN" => Some(Goal::WhiteWin),
            "BLACK_WIN" => Some(Goal::BlackWin),
            _ => None,
        }
    }
    /// PN-1.3: OR node iff the side to move is the attacker.
    #[inline]
    pub fn is_or_node(self, black_to_move: bool) -> bool {
        !black_to_move == self.attacker_is_white()
    }
    /// PN-1.5: the eval (White POV) seen from the attacker.
    #[inline]
    pub fn attacker_score(self, eval_cp: Option<i32>) -> Option<i32> {
        eval_cp.map(|e| if self.attacker_is_white() { e } else { -e })
    }
}

/// PN-1.1: sum of `max(0, v)`, `INF` as soon as the running total reaches it.
pub fn saturating_sum(values: impl IntoIterator<Item = u64>) -> u64 {
    let mut total: u64 = 0;
    for v in values {
        // v <= 2^62 and total < 2^62 before the add: no u64 overflow.
        total = total.saturating_add(v);
        if total >= PROOF_INFINITY {
            return PROOF_INFINITY;
        }
    }
    total
}

/// PN-1.1: minimum, `INF` when empty.  Values above `INF` are kept as they
/// are (v1 does not clamp a min), which only matters for hand-made rows.
pub fn saturating_min(values: impl IntoIterator<Item = u64>) -> u64 {
    values.into_iter().fold(PROOF_INFINITY, u64::min)
}

/// PN-1.5: `(pn weight, dn weight)` of an attacker score, `None` without one.
pub fn eval_band(attacker_score: Option<i32>) -> Option<(u64, u64)> {
    let s = attacker_score?;
    for (threshold, pw, dw) in EVAL_BANDS {
        if s >= threshold {
            return Some((pw, dw));
        }
    }
    Some(DEFAULT_EVAL_BAND)
}

/// PN-1.4: `(0, INF)` for the goal, `(INF, 0)` for any other closure, `None` open.
pub fn terminal_numbers(status: Status, goal: Goal) -> Option<(u64, u64)> {
    match status {
        Status::Unknown => None,
        s if s == goal.status() => Some((0, PROOF_INFINITY)),
        _ => Some((PROOF_INFINITY, 0)),
    }
}

/// What a leaf computation needs to know of a position.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct LeafInput {
    pub black_to_move: bool,
    pub status: Status,
    /// White POV (v1 `eval_cp`, NULL = `None`).
    pub eval_cp: Option<i32>,
}

/// A request to log a `SOLVE_GATE_DISAGREE` event (PN-2.10); the rate limit
/// is applied by [`gate::GateLog`].
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Disagreement {
    pub eval_cp: Option<i32>,
    pub annoyance: f64,
    pub factor: f64,
}

/// PN-1.6 `leaf_numbers` (with the gate of PN-2.8).
///
/// `legal_moves` exists for parity with v1's signature; **no production
/// caller passes it** (proofs Q5), so the refresh always calls with `None`.
/// `annoyance = None` is the gate switched off.  When `log_key` is true and
/// the gate taxed an optimistic leaf, the disagreement is returned for the
/// caller to rate-limit and log (v1 logs it inside `gated_pn`).
pub fn leaf_numbers(
    leaf: LeafInput,
    goal: Goal,
    legal_moves: Option<u64>,
    annoyance: Option<f64>,
    log_key: bool,
) -> ((u64, u64), Option<Disagreement>) {
    if let Some(exact) = terminal_numbers(leaf.status, goal) {
        return (exact, None);
    }
    let attacker_score = goal.attacker_score(leaf.eval_cp);
    let band = eval_band(attacker_score);
    let legal = legal_moves.unwrap_or(0);
    if band.is_none() && legal == 0 {
        return ((1, 1), None);
    }
    let (pw, dw) = band.unwrap_or((1, 1));
    let branching = legal.max(1);
    let (base_pn, base_dn) = if goal.is_or_node(leaf.black_to_move) { (1, branching) } else { (branching, 1) };
    let pn = base_pn.saturating_mul(pw).clamp(1, PROOF_MAX_LEAF);
    let dn = base_dn.saturating_mul(dw).clamp(1, PROOF_MAX_LEAF);
    let (pn, why) = gated_pn(pn, annoyance, attacker_score, log_key, leaf.eval_cp);
    ((pn, dn), why)
}

/// PN-2.8 `gated_pn`: the leaf pn multiplied by the gate factor.
pub fn gated_pn(
    pn: u64,
    annoyance: Option<f64>,
    attacker_score: Option<i32>,
    log_key: bool,
    eval_cp: Option<i32>,
) -> (u64, Option<Disagreement>) {
    let Some(a) = annoyance else { return (pn, None) };
    if matches!(attacker_score, Some(s) if s >= SOLVE_GATE_MATE_BAND) {
        return (pn, None);
    }
    let factor = gate::gate_factor(a);
    // Python: min(MAX_LEAF, max(1, int(pn * factor))), float64 product
    // truncated toward zero; pn <= 2^20 so the product is exact enough.
    let gated = ((pn as f64 * factor) as u64).clamp(1, PROOF_MAX_LEAF);
    let why = (log_key && gated > pn && matches!(attacker_score, Some(s) if s >= SOLVE_GATE_OPTIMISTIC)).then_some(Disagreement {
        eval_cp,
        annoyance: a,
        factor,
    });
    (gated, why)
}

/// PN-1.7 `internal_numbers`: OR = (min pn, sum dn), AND = (sum pn, min dn).
pub fn internal_numbers(or_node: bool, children: &[(u64, u64)]) -> Option<(u64, u64)> {
    if children.is_empty() {
        return None;
    }
    let pns = children.iter().map(|c| c.0);
    let dns = children.iter().map(|c| c.1);
    Some(if or_node { (saturating_min(pns), saturating_sum(dns)) } else { (saturating_sum(pns), saturating_min(dns)) })
}

/// PN-5.1 `sticky_index`: keep `held` unless `best` is more than K times cheaper.
pub fn sticky_index(best: usize, held: Option<usize>, numbers: &[(u64, u64)]) -> usize {
    match held {
        None => best,
        Some(h) if h == best => best,
        Some(h) => {
            // pn values are <= 2^62, times 3 fits in u64.
            if numbers[best].0.saturating_mul(SELECTED_CHILD_HYSTERESIS) < numbers[h].0 { best } else { h }
        }
    }
}

/// PN-4.6 `format_number`: `-` for absent, `∞` from `INF` up, thousands with `,`.
pub fn format_number(v: Option<u64>) -> String {
    let Some(v) = v else { return "-".into() };
    if v >= PROOF_INFINITY {
        return "\u{221e}".into();
    }
    let s = v.to_string();
    let mut out = String::with_capacity(s.len() + s.len() / 3);
    for (i, ch) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

#[cfg(test)]
mod tests {
    //! Ports of `test_proof_manager.py::RecurrenceTests`,
    //! `::LeafInitialisationTests`, `::SelectedChildHysteresisTests` (the pure
    //! ones), `test_proof_kpi.py::FormatNumberTests`, and the pure parts of
    //! `test_solve_gate.py::GateOffIsTheHistoricBehaviourTests` /
    //! `::GateArithmeticTests` / `::DisagreementQuadrantTests`.
    use super::*;
    const INF: u64 = PROOF_INFINITY;
    const W: bool = false; // white to move
    const B: bool = true; // black to move

    fn leaf(btm: bool, status: Status, eval: Option<i32>, goal: Goal) -> (u64, u64) {
        leaf_numbers(LeafInput { black_to_move: btm, status, eval_cp: eval }, goal, None, None, false).0
    }
    fn leaf_full(
        btm: bool,
        eval: Option<i32>,
        goal: Goal,
        legal: Option<u64>,
        a: Option<f64>,
        key: bool,
    ) -> ((u64, u64), Option<Disagreement>) {
        leaf_numbers(LeafInput { black_to_move: btm, status: Status::Unknown, eval_cp: eval }, goal, legal, a, key)
    }

    // ---- RecurrenceTests
    #[test]
    fn test_or_node_takes_min_pn_and_sum_dn() {
        assert_eq!(internal_numbers(Goal::WhiteWin.is_or_node(W), &[(3, 10), (7, 4), (5, 6)]), Some((3, 20)));
    }
    #[test]
    fn test_and_node_takes_sum_pn_and_min_dn() {
        assert_eq!(internal_numbers(Goal::WhiteWin.is_or_node(B), &[(3, 10), (7, 4), (5, 6)]), Some((15, 4)));
    }
    #[test]
    fn test_goal_reversal_swaps_the_roles() {
        assert_eq!(internal_numbers(Goal::BlackWin.is_or_node(B), &[(3, 10), (7, 4)]), Some((3, 14)));
        assert_eq!(internal_numbers(Goal::BlackWin.is_or_node(W), &[(3, 10), (7, 4)]), Some((10, 4)));
    }
    #[test]
    fn test_saturation_never_overflows() {
        assert_eq!(saturating_sum([INF, INF, INF]), INF);
        assert_eq!(saturating_sum([INF - 1, 5]), INF);
        assert_eq!(saturating_min([]), INF);
        assert_eq!(internal_numbers(true, &[(INF, 0), (INF, 0)]), Some((INF, 0)));
        assert_eq!(internal_numbers(false, &[(INF, 0), (2, 9)]), Some((INF, 0)));
    }
    #[test]
    fn test_proved_node_is_a_leaf_with_its_truth_value() {
        assert_eq!(leaf(W, Status::WhiteWin, None, Goal::WhiteWin), (0, INF));
        assert_eq!(leaf(W, Status::BlackWin, None, Goal::WhiteWin), (INF, 0));
        assert_eq!(leaf(W, Status::Draw, None, Goal::WhiteWin), (INF, 0));
    }

    // ---- LeafInitialisationTests
    #[test]
    fn test_no_information_falls_back_to_classic_one_one() {
        assert_eq!(leaf(W, Status::Unknown, None, Goal::WhiteWin), (1, 1));
    }
    #[test]
    fn test_attacker_advantage_makes_proving_look_cheaper() {
        let winning = leaf(W, Status::Unknown, Some(900), Goal::WhiteWin);
        let losing = leaf(W, Status::Unknown, Some(-900), Goal::WhiteWin);
        assert!(winning.0 < losing.0);
        assert!(winning.1 > losing.1);
    }
    #[test]
    fn test_eval_is_read_from_the_attacker_side() {
        assert_eq!(leaf(W, Status::Unknown, Some(900), Goal::WhiteWin), leaf(B, Status::Unknown, Some(-900), Goal::BlackWin));
    }
    #[test]
    fn test_branching_pushes_the_side_that_must_answer_everything() {
        let few = leaf_full(B, Some(0), Goal::WhiteWin, Some(3), None, false).0;
        let many = leaf_full(B, Some(0), Goal::WhiteWin, Some(40), None, false).0;
        assert!(few.0 < many.0);
        assert_eq!(few.1, many.1);
        let few_or = leaf_full(W, Some(0), Goal::WhiteWin, Some(3), None, false).0;
        let many_or = leaf_full(W, Some(0), Goal::WhiteWin, Some(40), None, false).0;
        assert_eq!(few_or.0, many_or.0);
        assert!(few_or.1 < many_or.1);
    }
    #[test]
    fn test_a_leaf_never_claims_infinity() {
        for score in [-30_000, -1_000, 0, 1_000, 30_000] {
            let (pn, dn) = leaf_full(W, Some(score), Goal::WhiteWin, Some(200), None, false).0;
            assert!(pn < INF && dn < INF);
            assert!(pn.min(dn) >= 1);
        }
    }
    // DescentTests::test_an_unexplored_sibling_keeps_the_classic_one_one
    #[test]
    fn test_an_unexplored_sibling_keeps_the_classic_one_one() {
        assert_eq!(leaf(B, Status::Unknown, None, Goal::WhiteWin), (1, 1));
        assert!(leaf(B, Status::Unknown, Some(3_000), Goal::WhiteWin).0 > 1);
    }

    // ---- SelectedChildHysteresisTests (pure sticky rule; the tree versions are in refresh.rs)
    #[test]
    fn test_the_declared_constant_is_the_approved_one() {
        assert_eq!(SELECTED_CHILD_HYSTERESIS, 3);
    }

    // ---- FormatNumberTests
    #[test]
    fn test_saturation_reads_as_infinity_not_as_a_huge_integer() {
        assert_eq!(format_number(Some(INF)), "\u{221e}");
        assert_eq!(format_number(Some(INF + 5)), "\u{221e}");
    }
    #[test]
    fn test_zero_is_a_real_answer_and_stays_zero() {
        assert_eq!(format_number(Some(0)), "0");
    }
    #[test]
    fn test_ordinary_numbers_get_thousands_separators() {
        assert_eq!(format_number(Some(1_234_567)), "1,234,567");
        assert_eq!(format_number(Some(999)), "999");
        assert_eq!(format_number(Some(1_000)), "1,000");
    }
    #[test]
    fn test_absent_is_a_dash() {
        assert_eq!(format_number(None), "-");
    }

    // ---- GateOffIsTheHistoricBehaviourTests (pure)
    #[test]
    fn test_the_numbers_are_the_documented_ones() {
        assert_eq!(leaf(W, Status::Unknown, None, Goal::WhiteWin), (1, 1));
        assert_eq!(leaf_full(B, Some(1_200), Goal::WhiteWin, Some(10), None, false).0, (20, 16));
    }
    #[test]
    fn test_passing_no_annoyance_is_the_same_as_the_old_signature() {
        for s in [None, Some(-900), Some(0), Some(400), Some(1_200), Some(30_000)] {
            assert_eq!(leaf_full(B, s, Goal::WhiteWin, Some(8), None, false), leaf_full(B, s, Goal::WhiteWin, Some(8), None, true));
        }
    }

    // ---- GateArithmeticTests
    fn gl(score: i32, a: Option<f64>) -> (u64, u64) {
        leaf_full(B, Some(score), Goal::WhiteWin, Some(10), a, false).0
    }
    #[test]
    fn test_annoyance_multiplies_the_pn() {
        assert_eq!(gl(1_200, Some(1.0)).0, gl(1_200, None).0 * 8);
    }
    #[test]
    fn test_zero_annoyance_changes_nothing() {
        assert_eq!(gl(1_200, Some(0.0)), gl(1_200, None));
    }
    #[test]
    fn test_dn_is_never_touched() {
        for a in [0.0, 0.3, 1.0] {
            assert_eq!(gl(1_200, Some(a)).1, gl(1_200, None).1);
        }
    }
    #[test]
    fn test_the_mate_band_is_exempt() {
        assert_eq!(gl(30_000, Some(1.0)), gl(30_000, None));
        assert_eq!(gl(9_000, Some(1.0)), gl(9_000, None));
        assert!(gl(8_999, Some(1.0)).0 > gl(8_999, None).0);
    }
    #[test]
    fn test_the_classic_one_one_is_exempt() {
        assert_eq!(leaf_full(W, None, Goal::WhiteWin, None, Some(1.0), false).0, (1, 1));
    }
    #[test]
    fn test_a_closed_leaf_keeps_its_truth_value() {
        for (st, n) in [(Status::WhiteWin, (0, INF)), (Status::BlackWin, (INF, 0)), (Status::Draw, (INF, 0))] {
            let r =
                leaf_numbers(LeafInput { black_to_move: W, status: st, eval_cp: Some(1_200) }, Goal::WhiteWin, None, Some(1.0), false).0;
            assert_eq!(r, n);
        }
    }
    #[test]
    fn test_a_gated_leaf_still_never_claims_infinity() {
        for score in [-30_000, -1_000, 0, 1_000] {
            let (pn, dn) = leaf_full(W, Some(score), Goal::WhiteWin, Some(200), Some(1.0), false).0;
            assert!(pn <= PROOF_MAX_LEAF && dn < INF && pn.min(dn) >= 1);
        }
    }
    #[test]
    fn test_the_gate_is_monotone_in_the_annoyance() {
        let mut prev = 0;
        for step in 0..=10 {
            let pn = gl(1_200, Some(step as f64 / 10.0)).0;
            assert!(pn >= prev);
            prev = pn;
        }
    }

    // ---- DisagreementQuadrantTests (the arithmetic half: which leaves ask to be logged)
    #[test]
    fn test_an_optimistic_eval_that_the_gate_taxes_is_logged() {
        let (_, why) = leaf_full(B, Some(1_200), Goal::WhiteWin, Some(10), Some(1.0), true);
        assert_eq!(why, Some(Disagreement { eval_cp: Some(1_200), annoyance: 1.0, factor: 8.0 }));
    }
    #[test]
    fn test_the_eval_in_the_payload_is_white_pov_like_the_column() {
        let (_, why) = leaf_full(W, Some(-1_200), Goal::BlackWin, Some(10), Some(1.0), true);
        assert_eq!(why.unwrap().eval_cp, Some(-1_200));
    }
    #[test]
    fn test_a_quiet_eval_is_not_a_disagreement() {
        assert_eq!(leaf_full(B, Some(120), Goal::WhiteWin, Some(10), Some(1.0), true).1, None);
        assert_eq!(leaf_full(B, Some(-2_000), Goal::WhiteWin, Some(10), Some(1.0), true).1, None);
    }
    #[test]
    fn test_the_mate_band_never_disagrees() {
        assert_eq!(leaf_full(B, Some(30_000), Goal::WhiteWin, Some(10), Some(1.0), true).1, None);
    }
    #[test]
    fn test_a_leaf_the_gate_did_not_tax_is_not_logged() {
        assert_eq!(leaf_full(B, Some(1_200), Goal::WhiteWin, Some(10), Some(0.0), true).1, None);
    }
    #[test]
    fn test_the_degraded_edge_view_does_not_write_the_dataset() {
        assert_eq!(leaf_full(B, Some(1_200), Goal::WhiteWin, Some(10), Some(1.0), false).1, None);
    }
}
