//! The "double gate" (`atomicdb/solve_estimate.py`, PN-2.1 to PN-2.10): a
//! hand-made estimate of how tedious a position is to SOLVE, which multiplies
//! a leaf's pn by 1..8 when the switch is on.
//!
//! Production (28-Nov-2026, read from the systemd units): `ATOMICDB_SOLVE_GATE=1`
//! only for the `atomicdb-selector` and `openbench` (web) services; the
//! ingest and propagation services, which run almost every refresh, have it
//! off.  So the stored pn/dn are mostly gate-off numbers, while the descent
//! ranks children with the gate on.  The port keeps the switch per call
//! ([`GateConfig`]) so the writer and the selector can differ the same way.

use std::collections::HashMap;

pub const REVERSIBLE_WEIGHT: f64 = 0.35;
pub const BRANCHING_WEIGHT: f64 = 0.25;
pub const MATERIAL_WEIGHT: f64 = 0.20;
pub const EVAL_BAND_WEIGHT: f64 = 0.20;
pub const NEUTRAL: f64 = 0.5;
pub const BRANCHING_SATURATION: f64 = 40.0;
pub const MULTIPV_SATURATION: f64 = 5.0;
pub const TB_MEN: u32 = 6;
pub const BARE_MEN: u32 = 12;
pub const FULL_MEN: u32 = 32;
pub const MATE_BAND: i32 = 9_000;
pub const WON_BAND: i32 = 800;
pub const EDGE_BAND: i32 = 300;
pub const PV_SCAN_MAX_PLIES: usize = 32;
pub const MAX_FACTOR: f64 = 8.0;
/// PN-2.10: one event per key per window.
pub const SOLVE_GATE_LOG_WINDOW: f64 = 900.0;
pub const SOLVE_GATE_LOG_MAX: usize = 4_096;

/// What the estimator reads of a position (v1 `solve_estimate.Leaf` or a
/// full `Position` row).  `last_analysis` is the JSON list v1 stores.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct GateRow {
    pub fen: Option<String>,
    pub eval_cp: Option<i32>,
    pub last_analysis: Option<serde_json::Value>,
    pub mate_in: Option<u32>,
}

impl GateRow {
    /// PN-2.9 `shallow`: what an edge knows of its child (fen and eval only).
    pub fn shallow(fen: Option<String>, eval_cp: Option<i32>) -> GateRow {
        GateRow { fen, eval_cp, last_analysis: None, mate_in: None }
    }
}

#[inline]
pub fn clamp01(v: f64) -> f64 {
    // Same as v1's `0.0 if v < 0 else (1.0 if v > 1 else v)`, NaN included.
    v.clamp(0.0, 1.0)
}

fn lines(row: &GateRow) -> impl Iterator<Item = &serde_json::Map<String, serde_json::Value>> {
    row.last_analysis
        .as_ref()
        .and_then(|v| v.as_array())
        .into_iter()
        .flatten()
        .filter_map(|l| l.as_object())
        .filter(|l| !truthy(l.get("prior_pass")))
}

/// Python truthiness of a JSON value.
fn truthy(v: Option<&serde_json::Value>) -> bool {
    match v {
        None | Some(serde_json::Value::Null) => false,
        Some(serde_json::Value::Bool(b)) => *b,
        Some(serde_json::Value::Number(n)) => n.as_f64().is_some_and(|x| x != 0.0),
        Some(serde_json::Value::String(s)) => !s.is_empty(),
        Some(serde_json::Value::Array(a)) => !a.is_empty(),
        Some(serde_json::Value::Object(o)) => !o.is_empty(),
    }
}

/// PN-2.2 `current_line`: the first dict line without `prior_pass`.
pub fn current_line(row: &GateRow) -> Option<&serde_json::Map<String, serde_json::Value>> {
    lines(row).next()
}

/// PN-2.3 `current_lines`.
pub fn current_lines(row: &GateRow) -> usize {
    lines(row).count()
}

fn board_map(fen: &str) -> HashMap<(u8, u8), char> {
    let mut board = HashMap::new();
    let field = fen.split_whitespace().next().unwrap_or("");
    for (index, rank_str) in field.split('/').take(8).enumerate() {
        let rank = 8 - index as u8;
        let mut column: u32 = 0;
        for ch in rank_str.chars() {
            if let Some(d) = ch.to_digit(10) {
                column += d;
            } else if column < 8 {
                board.insert((column as u8, rank), ch);
                column += 1;
            } else {
                break;
            }
        }
    }
    board
}

fn square(s: &str) -> Option<(u8, u8)> {
    let b = s.as_bytes();
    if b.len() != 2 {
        return None;
    }
    // Python keys the map by the two-character string; any string that is
    // not a real square simply never matches a board key.
    let f = b[0].wrapping_sub(b'a');
    let r = b[1].wrapping_sub(b'0');
    (f < 8 && (1..=8).contains(&r)).then_some((f, r))
}

/// PN-2.2 `zeroing_plies`: `(plies read, plies that reset the 50-move counter)`.
pub fn zeroing_plies(fen: &str, pv: &[serde_json::Value], max_plies: usize) -> (usize, usize) {
    let mut board = board_map(fen);
    let (mut read, mut zeroing) = (0, 0);
    for v in pv.iter().take(max_plies) {
        let Some(uci) = v.as_str() else { break };
        if uci.chars().count() < 4 {
            break;
        }
        let origin: String = uci.chars().take(2).collect();
        let dest: String = uci.chars().skip(2).take(2).collect();
        let o = square(&origin);
        let d = square(&dest);
        let piece = o.and_then(|s| board.get(&s).copied());
        let captured = d.and_then(|s| board.get(&s).copied());
        read += 1;
        if uci.chars().count() > 4 || captured.is_some() || matches!(piece, Some('P') | Some('p')) {
            zeroing += 1;
        }
        if let Some(s) = o {
            board.remove(&s);
        }
        if captured.is_some() {
            if let Some(s) = d {
                board.remove(&s);
            }
        } else if let (Some(p), Some(s)) = (piece, d) {
            board.insert(s, p);
        }
    }
    (read, zeroing)
}

/// PN-2.2 `reversible_feature`.
pub fn reversible_feature(row: &GateRow) -> f64 {
    let pv = current_line(row).and_then(|l| l.get("pv"));
    let Some(fen) = row.fen.as_deref().filter(|f| !f.is_empty()) else { return NEUTRAL };
    let pv = match pv {
        Some(serde_json::Value::Array(a)) if !a.is_empty() => a,
        _ => return NEUTRAL,
    };
    let (read, zeroing) = zeroing_plies(fen, pv, PV_SCAN_MAX_PLIES);
    if read == 0 {
        return NEUTRAL;
    }
    clamp01(1.0 - zeroing as f64 / read as f64)
}

/// PN-2.3 `branching_feature`.
pub fn branching_feature(row: &GateRow, branching: Option<u64>) -> f64 {
    if let Some(b) = branching {
        return clamp01(b as f64 / BRANCHING_SATURATION);
    }
    let n = current_lines(row);
    if n == 0 {
        return NEUTRAL;
    }
    NEUTRAL.max(clamp01(n as f64 / MULTIPV_SATURATION))
}

/// `logic.piece_count`: letters in the board field.
pub fn piece_count(fen: &str) -> u32 {
    fen.split_whitespace().next().unwrap_or("").chars().filter(|c| c.is_alphabetic()).count() as u32
}

/// PN-2.4 `material_feature`: a tent peaking at 12 men, 0 at or below 6.
pub fn material_feature(row: &GateRow) -> f64 {
    let Some(fen) = row.fen.as_deref().filter(|f| !f.is_empty()) else { return NEUTRAL };
    let men = piece_count(fen);
    if men <= TB_MEN {
        return 0.0;
    }
    if men <= BARE_MEN {
        return clamp01((men - TB_MEN) as f64 / (BARE_MEN - TB_MEN) as f64);
    }
    clamp01((FULL_MEN as f64 - men as f64) / (FULL_MEN - BARE_MEN) as f64)
}

/// PN-2.5 `eval_band_feature` (colour-blind).
pub fn eval_band_feature(row: &GateRow) -> f64 {
    if row.mate_in.is_some() {
        return 0.0;
    }
    if current_line(row).is_some_and(|l| truthy(l.get("mate"))) {
        return 0.0;
    }
    let Some(e) = row.eval_cp else { return NEUTRAL };
    let s = e.unsigned_abs() as i64;
    if s >= MATE_BAND as i64 {
        0.0
    } else if s >= WON_BAND as i64 {
        0.2
    } else if s >= EDGE_BAND as i64 {
        0.5
    } else {
        1.0
    }
}

/// PN-2.6 `annoyance`: the weighted sum, in v1's summation order.
pub fn annoyance(row: &GateRow, branching: Option<u64>) -> f64 {
    // Python: sum(w * v for ...) starts at int 0 and adds in dict order.
    let mut total = 0.0f64;
    total += REVERSIBLE_WEIGHT * reversible_feature(row);
    total += BRANCHING_WEIGHT * branching_feature(row, branching);
    total += MATERIAL_WEIGHT * material_feature(row);
    total += EVAL_BAND_WEIGHT * eval_band_feature(row);
    clamp01(total)
}

/// PN-2.7 `gate_factor`.
pub fn gate_factor(a: f64) -> f64 {
    1.0 + (MAX_FACTOR - 1.0) * clamp01(a)
}

/// PN-2.1: the switch, per caller.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct GateConfig {
    pub enabled: bool,
}

impl GateConfig {
    /// PN-2.9 `gate_annoyance` (full row, frontier leaves and `root_numbers`).
    pub fn full(&self, row: impl FnOnce() -> GateRow) -> Option<f64> {
        self.enabled.then(|| annoyance(&row(), None))
    }
    /// PN-2.9 `shallow_annoyance` (a child priced from its edge).
    pub fn shallow(&self, fen: impl FnOnce() -> Option<String>, eval_cp: Option<i32>) -> Option<f64> {
        self.enabled.then(|| annoyance(&GateRow::shallow(fen(), eval_cp), None))
    }
}

/// PN-2.10: the rate limit of `SOLVE_GATE_DISAGREE`.
///
/// v1 keys a process-level dict by position key with `time.monotonic()`.  The
/// port takes `now` (seconds) from the caller so that a batch stays a pure
/// function of its inputs: the writer passes the batch's logical time.  Like
/// v1, the log never changes a number.
#[derive(Clone, Debug, Default)]
pub struct GateLog {
    seen: HashMap<u64, f64>,
    pub max: usize,
}

impl GateLog {
    pub fn new() -> GateLog {
        GateLog { seen: HashMap::new(), max: SOLVE_GATE_LOG_MAX }
    }
    /// True if an event must be written for `key` at `now`.
    pub fn admit(&mut self, key: u64, now: f64) -> bool {
        if let Some(&t) = self.seen.get(&key)
            && now - t < SOLVE_GATE_LOG_WINDOW
        {
            return false;
        }
        if self.seen.len() >= self.max {
            let cutoff = now - SOLVE_GATE_LOG_WINDOW;
            self.seen.retain(|_, ts| *ts >= cutoff);
            if self.seen.len() >= self.max {
                self.seen.clear();
            }
        }
        self.seen.insert(key, now);
        true
    }
    /// `reset_solve_gate_log`.
    pub fn reset(&mut self) {
        self.seen.clear();
    }
    pub fn len(&self) -> usize {
        self.seen.len()
    }
    pub fn is_empty(&self) -> bool {
        self.seen.is_empty()
    }
}

/// Python `round(x, n)` for the event payload (correctly rounded, like Rust's
/// float formatting).
pub fn py_round(x: f64, digits: usize) -> f64 {
    format!("{x:.digits$}").parse().unwrap_or(x)
}

#[cfg(test)]
mod tests {
    //! Ports of `test_solve_gate.py`: WeightsTests, ReversibleFeatureTests,
    //! BranchingFeatureTests, MaterialFeatureTests, EvalBandFeatureTests,
    //! AnnoyanceTests, GateFactorTests, and the rate-limit half of
    //! DisagreementQuadrantTests.
    use super::*;
    use serde_json::json;

    const START: &str = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
    const BARE: &str = "4k3/pp4pp/8/8/8/8/PP4PP/3RK2R w K - 0 1";
    const TABLEBASE: &str = "8/8/8/4k3/8/8/4K3/4R3 w - - 0 1";

    fn leaf(fen: &str, eval: Option<i32>, lines: Option<serde_json::Value>, mate: Option<u32>) -> GateRow {
        GateRow { fen: Some(fen.into()), eval_cp: eval, last_analysis: lines, mate_in: mate }
    }
    fn pvline(pv: &[&str]) -> serde_json::Value {
        json!([{ "eval_cp": 0, "pv": pv }])
    }
    fn pv(v: &[&str]) -> Vec<serde_json::Value> {
        v.iter().map(|s| json!(s)).collect()
    }

    #[test]
    fn test_the_weights_sum_to_one() {
        assert!((REVERSIBLE_WEIGHT + BRANCHING_WEIGHT + MATERIAL_WEIGHT + EVAL_BAND_WEIGHT - 1.0).abs() < 1e-12);
    }
    #[test]
    fn test_every_feature_has_a_weight() {
        // Four features, four weights: the combination in `annoyance` names each.
        let row = leaf(START, None, None, None);
        let manual = REVERSIBLE_WEIGHT * reversible_feature(&row)
            + BRANCHING_WEIGHT * branching_feature(&row, None)
            + MATERIAL_WEIGHT * material_feature(&row)
            + EVAL_BAND_WEIGHT * eval_band_feature(&row);
        assert_eq!(annoyance(&row, None), clamp01(manual));
    }
    #[test]
    fn test_the_declared_constants_are_the_approved_ones() {
        assert_eq!(MAX_FACTOR, 8.0);
        assert_eq!(REVERSIBLE_WEIGHT, 0.35);
        assert_eq!(NEUTRAL, 0.5);
    }

    #[test]
    fn test_a_pv_of_manoeuvres_is_the_tedious_end() {
        let r = leaf(START, None, Some(pvline(&["g1f3", "g8f6", "f3g1", "f6g8", "b1c3", "b8c6"])), None);
        assert_eq!(reversible_feature(&r), 1.0);
    }
    #[test]
    fn test_a_pv_of_pawn_moves_and_captures_is_the_cheap_end() {
        let r = leaf(START, None, Some(pvline(&["e2e4", "d7d5", "a2a4", "h7h5"])), None);
        assert_eq!(reversible_feature(&r), 0.0);
    }
    #[test]
    fn test_a_capture_counts_even_when_no_pawn_moves() {
        let r = leaf(START, None, Some(pvline(&["e2e4", "d7d5", "e4d5", "d8d5"])), None);
        assert!((reversible_feature(&r) - 0.25).abs() < 1e-12);
    }
    #[test]
    fn test_the_density_is_a_fraction_not_a_count() {
        let r = leaf(START, None, Some(pvline(&["e2e4", "g8f6", "d2d4", "f6g8"])), None);
        assert!((reversible_feature(&r) - 0.5).abs() < 1e-12);
    }
    #[test]
    fn test_no_pv_is_neutral_not_a_guess() {
        assert_eq!(reversible_feature(&leaf(START, None, None, None)), NEUTRAL);
        assert_eq!(reversible_feature(&leaf(START, None, Some(json!([{"eval_cp": 3}])), None)), NEUTRAL);
    }
    #[test]
    fn test_a_prior_pass_line_is_not_the_current_verdict() {
        let r = leaf(START, None, Some(json!([{"pv": ["g1f3", "g8f6"], "prior_pass": true}])), None);
        assert_eq!(reversible_feature(&r), NEUTRAL);
    }
    #[test]
    fn test_the_first_ply_is_exact_deeper_ones_are_a_proxy() {
        assert_eq!(zeroing_plies(START, &pv(&["e2e4"]), PV_SCAN_MAX_PLIES), (1, 1));
        assert_eq!(zeroing_plies(START, &pv(&["g1f3"]), PV_SCAN_MAX_PLIES), (1, 0));
    }
    #[test]
    fn test_a_promotion_counts_as_zeroing() {
        assert_eq!(zeroing_plies("8/4P3/8/8/8/8/8/4K1k1 w - - 0 1", &pv(&["e7e8q"]), PV_SCAN_MAX_PLIES), (1, 1));
    }
    #[test]
    fn test_the_scan_is_bounded() {
        let long: Vec<&str> = ["g1f3", "g8f6", "f3g1", "f6g8"].iter().cycle().take(160).copied().collect();
        assert_eq!(zeroing_plies(START, &pv(&long), PV_SCAN_MAX_PLIES).0, PV_SCAN_MAX_PLIES);
    }
    #[test]
    fn test_a_malformed_pv_stops_the_walk_instead_of_crashing() {
        let v = vec![json!("e2e4"), serde_json::Value::Null, json!("d2d4")];
        assert_eq!(zeroing_plies(START, &v, PV_SCAN_MAX_PLIES).0, 1);
    }

    #[test]
    fn test_the_edge_count_is_trusted_in_both_directions() {
        let r = leaf(START, None, None, None);
        assert!(branching_feature(&r, Some(3)) < branching_feature(&r, Some(30)));
        assert_eq!(branching_feature(&r, Some(200)), 1.0);
    }
    #[test]
    fn test_the_multipv_proxy_can_only_push_upwards() {
        let narrow = leaf(START, None, Some(json!([{"pv": ["e2e4"]}])), None);
        let wide = leaf(
            START,
            None,
            Some(json!([{"pv": ["e2e4"]}, {"pv": ["e2e4"]}, {"pv": ["e2e4"]}, {"pv": ["e2e4"]}, {"pv": ["e2e4"]}])),
            None,
        );
        assert_eq!(branching_feature(&narrow, None), NEUTRAL);
        assert_eq!(branching_feature(&wide, None), 1.0);
    }
    #[test]
    fn test_no_information_at_all_is_neutral() {
        assert_eq!(branching_feature(&leaf(START, None, None, None), None), NEUTRAL);
    }

    #[test]
    fn test_below_the_tablebase_horizon_the_answer_is_a_lookup() {
        assert_eq!(material_feature(&leaf(TABLEBASE, None, None, None)), 0.0);
    }
    #[test]
    fn test_the_peak_is_the_bare_endgame_above_that_horizon() {
        assert_eq!(material_feature(&leaf(BARE, None, None, None)), 1.0);
    }
    #[test]
    fn test_a_full_board_is_tactical_not_tedious() {
        assert_eq!(material_feature(&leaf(START, None, None, None)), 0.0);
    }
    #[test]
    fn test_the_curve_is_a_tent_not_a_ramp() {
        let bare = material_feature(&leaf(BARE, None, None, None));
        assert!(bare > material_feature(&leaf(START, None, None, None)));
        assert!(bare > material_feature(&leaf(TABLEBASE, None, None, None)));
    }

    #[test]
    fn test_seen_mate_is_the_fast_lane() {
        assert_eq!(eval_band_feature(&leaf(START, Some(9_900), None, None)), 0.0);
        assert_eq!(eval_band_feature(&leaf(START, None, None, Some(7))), 0.0);
        assert_eq!(eval_band_feature(&leaf(START, Some(40), Some(json!([{"mate": 5, "pv": ["e2e4"]}])), None)), 0.0);
    }
    #[test]
    fn test_the_band_is_ordinal_and_monotone() {
        let v: Vec<f64> = [9_500, 1_000, 500, 20].iter().map(|&s| eval_band_feature(&leaf(START, Some(s), None, None))).collect();
        let mut sorted = v.clone();
        sorted.sort_by(f64::total_cmp);
        assert_eq!(v, sorted);
    }
    #[test]
    fn test_the_sign_does_not_matter() {
        assert_eq!(eval_band_feature(&leaf(START, Some(1_000), None, None)), eval_band_feature(&leaf(START, Some(-1_000), None, None)));
    }
    #[test]
    fn test_no_eval_is_neutral() {
        assert_eq!(eval_band_feature(&leaf(START, None, None, None)), NEUTRAL);
    }

    #[test]
    fn test_the_value_is_always_a_fraction() {
        for r in [
            leaf(START, None, None, None),
            leaf(BARE, Some(-30_000), None, None),
            leaf(TABLEBASE, Some(12), None, None),
            leaf(START, Some(0), Some(json!([{ "pv": vec!["g1f3"; 30] }])), None),
        ] {
            let a = annoyance(&r, None);
            assert!((0.0..=1.0).contains(&a));
        }
    }
    #[test]
    fn test_the_tedious_endgame_beats_the_promising_mate() {
        let tedious = leaf(
            BARE,
            Some(1_200),
            Some(
                json!([{ "eval_cp": 1_200, "pv": ["d1d2", "e8e7", "d1d2", "e8e7", "d1d2", "e8e7", "d1d2", "e8e7", "d1d2", "e8e7", "d1d2", "e8e7"] }]),
            ),
            None,
        );
        let promising = leaf(START, Some(1_200), Some(json!([{"mate": 4, "pv": ["e2e4"]}])), None);
        assert!(annoyance(&tedious, None) > annoyance(&promising, None));
    }
    #[test]
    fn test_it_is_pure_same_row_same_number() {
        let r = leaf(BARE, Some(700), Some(json!([{"eval_cp": 700, "pv": ["d1d2", "e8e7"]}])), None);
        assert_eq!(annoyance(&r, None).to_bits(), annoyance(&r, None).to_bits());
    }
    #[test]
    fn test_an_empty_row_lands_on_the_neutral_middle() {
        let v = annoyance(&leaf(START, None, None, None), None);
        assert!((v - NEUTRAL * (1.0 - MATERIAL_WEIGHT)).abs() < 1e-12);
    }

    #[test]
    fn test_no_annoyance_costs_exactly_what_it_used_to() {
        assert_eq!(gate_factor(0.0), 1.0);
    }
    #[test]
    fn test_full_annoyance_costs_the_declared_maximum() {
        assert_eq!(gate_factor(1.0), MAX_FACTOR);
    }
    #[test]
    fn test_it_is_monotone_and_never_below_one() {
        let mut prev = 0.0;
        for step in 0..=10 {
            let f = gate_factor(step as f64 / 10.0);
            assert!(f >= 1.0 && f > prev);
            prev = f;
        }
    }
    #[test]
    fn test_out_of_range_input_is_clamped_not_trusted() {
        assert_eq!(gate_factor(-5.0), 1.0);
        assert_eq!(gate_factor(99.0), MAX_FACTOR);
    }

    // GateOffIsTheHistoricBehaviourTests::test_no_annoyance_is_computed_at_all_when_it_is_off
    #[test]
    fn test_no_annoyance_is_computed_at_all_when_it_is_off() {
        let off = GateConfig { enabled: false };
        assert_eq!(off.full(|| panic!("the estimator must not run with the gate off")), None);
        assert_eq!(off.shallow(|| panic!("no fen read with the gate off"), Some(1_200)), None);
        let on = GateConfig { enabled: true };
        assert!(on.shallow(|| Some(BARE.into()), Some(1_200)).is_some());
    }

    // DisagreementQuadrantTests: rate limit
    #[test]
    fn test_the_same_leaf_is_not_logged_once_per_pass() {
        let mut log = GateLog::new();
        let n = (0..20).filter(|_| log.admit(1, 10.0)).count();
        assert_eq!(n, 1);
        assert!(log.admit(1, 10.0 + SOLVE_GATE_LOG_WINDOW));
    }
    #[test]
    fn test_different_leaves_are_all_logged() {
        let mut log = GateLog::new();
        assert!(log.admit(1, 0.0) && log.admit(2, 0.0));
    }
    #[test]
    fn test_the_rate_limit_cache_can_be_reset() {
        let mut log = GateLog::new();
        assert!(log.admit(1, 0.0));
        log.reset();
        assert!(log.admit(1, 0.0));
    }
    #[test]
    fn test_the_cache_does_not_grow_without_bound() {
        let mut log = GateLog { max: 8, ..GateLog::new() };
        for k in 0..40 {
            log.admit(k, 0.0);
        }
        assert!(log.len() <= 8);
    }
    #[test]
    fn payload_rounding_matches_python() {
        assert_eq!(py_round(0.123456, 4), 0.1235);
        assert_eq!(py_round(8.0, 3), 8.0);
        assert_eq!(py_round(2.675, 2), 2.67); // binary 2.67499999...
    }
}
