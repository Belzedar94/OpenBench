//! Steps 6 and 7 of the port: the inputs of the backed decision and the
//! decision itself.
//!
//! - BKD-1 child contributions (`_child_contribution`, `_ChildValue`), the
//!   echo rule (BKD-1.3)
//! - BKD-2 comparisons (`conv::better_for_mover`, the quality tolerance)
//! - BKD-3 floors (`is_floor`, `_showcase_lines`, `_beats_certificate`,
//!   `refuses_floor`)
//! - BKD-4 coverage (`_showcase_bound`, `coverage_is_partial`, bounded and
//!   complete)
//! - BKD-5 `_backed_for` (with the repetition branches ready for the flags
//!   the loop rules set: `cycles`, `returns`, `circular`)
//! - BKD-6 the tie order (`_best_child`, `_proof_rank`, `_defence_moves`,
//!   `_grounding`)
//!
//! `backed_eval` and every value here are White's point of view.

use crate::config::BACKED_ECHO_PLIES;
use crate::conv::{MATE_BAND, PROVEN_QUALITY, TB_CLAMP_CEILING, better_for_mover, status_eval, stm_white, uci_order};
use av2_model::{Core, Ext, GraphRead, Move, PosId, Status};
use serde_json::Value;

/// `ingest.BACKED_QUALITY_TOLERANCE` = 0.5, compared exactly as
/// `2 * quality < own_quality`.
pub const BACKED_QUALITY_TOLERANCE_DEN: u128 = 2;
/// `REQUEST_BUDGET_LADDER[0]`: the rung a floor's purchase is held at (BKD-3.3).
pub const REQUEST_FIRST_RUNG: u64 = 128_000_000;

/// `_ChildValue`: what a child gives the parent's negamax.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ChildValue {
    pub mv: Move,
    pub value: Option<i32>,
    pub quality: u64,
    pub plies: i64,
    pub id: PosId,
    /// The child's `backed_move` when its value is borrowed (else `NONE`).
    pub spine: Move,
    /// Decided child: its value is the truth (BKD-1.1).
    pub exact: bool,
    /// Plies to mate of a decided child (ties only, BKD-6.2).
    pub mate_in: Option<i64>,
    /// BKD-3.1.
    pub floor: bool,
    /// Set by the repetition walk (LOOP-1.1): the edge comes back here.
    pub cycles: bool,
    /// BKD-6.4: the parent's pointer through this move would close a loop.
    pub returns: bool,
    /// BKD-6.4: the child's pointers run into a loop elsewhere.
    pub circular: bool,
}

impl ChildValue {
    pub fn uninformed(mv: Move, id: PosId) -> ChildValue {
        ChildValue {
            mv,
            value: None,
            quality: 0,
            plies: 0,
            id,
            spine: Move::NONE,
            exact: false,
            mate_in: None,
            floor: false,
            cycles: false,
            returns: false,
            circular: false,
        }
    }
}

/// BKD-3.1 `is_floor`: a backed value on a child never searched and not
/// decided.
#[inline]
pub fn is_floor(status: Status, nodes_invested: u64, backed_eval: Option<i32>) -> bool {
    backed_eval.is_some() && nodes_invested == 0 && status_eval(status).is_none()
}

/// BKD-1.1 to BKD-1.3 `_child_contribution`.
pub fn child_contribution(mv: Move, id: PosId, child: &Core, ext: &Ext) -> ChildValue {
    let mut c = ChildValue::uninformed(mv, id);
    if let Some(exact) = status_eval(child.status()) {
        c.value = Some(exact);
        c.quality = PROVEN_QUALITY;
        c.exact = true;
        c.mate_in = child.mate().map(i64::from);
        return c;
    }
    let backed = child.backed().map(i32::from);
    if backed.is_some() && child.backed_plies <= BACKED_ECHO_PLIES {
        c.value = backed;
        c.quality = ext.backed_nodes.max(ext.nodes_invested);
        c.plies = child.backed_plies as i64;
        c.spine = child.backed_move;
        c.floor = is_floor(child.status(), ext.nodes_invested, backed);
        return c;
    }
    if let Some(e) = child.eval() {
        c.value = Some(e as i32);
        c.quality = ext.nodes_invested;
    }
    c
}

/// The children of `id` as the backed decision reads them, in edge order
/// (v1 `_backed_children_by_parent`; its order is the database's and only
/// orders the discrepancies, see `docs/rules-port-status.md`).
pub fn children_of(g: &dyn GraphRead, id: PosId, buf: &mut Vec<av2_model::Edge>) -> Vec<ChildValue> {
    buf.clear();
    g.children(id, buf);
    buf.iter().map(|e| child_contribution(e.mv, e.child, &g.core(e.child), &g.ext(e.child).unwrap_or_default())).collect()
}

// ---------------------------------------------------------------- showcase

/// The certificate of a multipv showcase: its bound (BKD-4.1) and the moves
/// heading its current lines (BKD-3.2).  Empty lines exactly when no bound.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Showcase {
    pub bound: Option<i32>,
    pub lines: Vec<Move>,
}

/// Parse `last_analysis` (a JSON list of lines).
pub fn showcase_of(last_analysis: Option<&Value>) -> Showcase {
    let Some(Value::Array(items)) = last_analysis else { return Showcase::default() };
    let current: Vec<&serde_json::Map<String, Value>> =
        items.iter().filter_map(|l| l.as_object()).filter(|l| !truthy(l.get("prior_pass"))).collect();
    if current.len() < 2 {
        return Showcase::default();
    }
    // `isinstance(worst, int)`: an integer JSON number (a bool is an int to
    // Python too; no worker sends one).
    let bound = match current.last().unwrap().get("eval_cp") {
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => n.as_i64().map(|v| v as i32),
        Some(Value::Bool(b)) => Some(*b as i32),
        _ => None,
    };
    if bound.is_none() {
        return Showcase::default();
    }
    let mut lines = Vec::new();
    for l in current {
        let head = match l.get("move") {
            Some(v) if truthy(Some(v)) => v.as_str().map(str::to_string),
            _ => match l.get("pv") {
                Some(Value::Array(pv)) if !pv.is_empty() => pv[0].as_str().map(str::to_string),
                _ => None,
            },
        };
        if let Some(Ok(m)) = head.filter(|s| !s.is_empty()).map(|s| Move::parse(&s))
            && !lines.contains(&m)
        {
            lines.push(m);
        }
    }
    Showcase { bound, lines }
}

/// Read and parse a position's `last_analysis` blob.
pub fn showcase_from_graph(g: &dyn GraphRead, id: PosId) -> Showcase {
    let Some(ext) = g.ext(id) else { return Showcase::default() };
    if ext.analysis.is_none() {
        return Showcase::default();
    }
    let Some(bytes) = g.blob(ext.analysis) else { return Showcase::default() };
    match serde_json::from_slice::<Value>(&bytes) {
        Ok(v) => showcase_of(Some(&v)),
        Err(_) => Showcase::default(),
    }
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// BKD-3.3 `_beats_certificate`: a floor, outside the lines, better for the
/// mover than their last.
#[inline]
pub fn beats_certificate(floor: bool, value: i32, mv: Move, show: &Showcase, stm_white: bool) -> bool {
    floor && show.bound.is_some_and(|b| !show.lines.contains(&mv) && better_for_mover(value, b, stm_white))
}

/// BKD-3.4 `refuses_floor`: the same predicate for one row of a page.
pub fn refuses_floor(row: &Core, show: &Showcase, child: &Core, child_ext: &Ext, mv: Move) -> bool {
    let backed = child.backed().map(i32::from);
    beats_certificate(is_floor(child.status(), child_ext.nodes_invested, backed), backed.unwrap_or(0), mv, show, stm_white(row))
        && backed.is_some()
}

/// BKD-4.2 `coverage_is_partial`.
pub fn coverage_is_partial(expanded: bool, children: &[ChildValue]) -> bool {
    let informed = children.iter().filter(|c| c.value.is_some()).count();
    if informed == 0 {
        return true;
    }
    !(expanded && informed == children.len())
}

// ---------------------------------------------------------------- ties

/// `float('-inf')` / `float('inf')` in the tie keys.
const NEG_INF: i64 = i64::MIN;
const POS_INF: i64 = i64::MAX;

/// BKD-6.4 `_grounding`.
#[inline]
pub fn grounding(c: &ChildValue) -> i64 {
    if c.returns {
        0
    } else if c.circular {
        1
    } else {
        2
    }
}

/// BKD-6.2 `_proof_rank`.
pub fn proof_rank(c: &ChildValue, stm_white: bool) -> (i64, i64) {
    let v = match (c.exact, c.value) {
        (true, Some(v)) if v != 0 => v,
        _ => return (0, 0),
    };
    let wins = (v > 0) == stm_white;
    match c.mate_in {
        None => (if wins { 1 } else { -1 }, NEG_INF),
        Some(m) => {
            if wins {
                (1, -m)
            } else {
                (-1, m)
            }
        }
    }
}

/// BKD-6.3 `_defence_moves`.
pub fn defence_moves(c: &ChildValue, stm_white: bool) -> Option<i64> {
    let v = c.value?;
    if (v < 0) != stm_white || v == 0 {
        return None;
    }
    if c.exact {
        return Some(match c.mate_in {
            None => NEG_INF,
            Some(m) => (m + 1).div_euclid(2),
        });
    }
    let a = v.unsigned_abs() as i64;
    if a >= 10_000 {
        return Some(POS_INF);
    }
    if a > TB_CLAMP_CEILING as i64 {
        return Some(10_000 - a);
    }
    None
}

type TieKey = (i64, i64, (i64, i64), u64, i64, bool);

/// BKD-6.1 `_best_child`: the index (into `children`) of the child the side
/// to move stands on, among informed `children` (all must be informed).
pub fn best_child(children: &[ChildValue], idx: &[usize], stm_white: bool, own_move: Move) -> usize {
    let sign: i64 = if stm_white { 1 } else { -1 };
    let mut order: Vec<usize> = idx.to_vec();
    order.sort_by_key(|&i| uci_order(children[i].mv));
    let defences: Vec<Option<i64>> = order.iter().map(|&i| defence_moves(&children[i], stm_white)).collect();
    let longest = !order.is_empty() && defences.iter().all(Option::is_some);
    let own = |c: &ChildValue| !own_move.is_none() && c.mv == own_move;
    let key = |k: usize| -> TieKey {
        let c = &children[order[k]];
        if longest {
            (grounding(c), defences[k].unwrap(), proof_rank(c, stm_white), c.quality, -c.plies, own(c))
        } else {
            (sign * c.value.unwrap() as i64, grounding(c), proof_rank(c, stm_white), c.quality, -c.plies, own(c))
        }
    };
    let mut best = 0;
    let mut best_key = key(0);
    for k in 1..order.len() {
        let kk = key(k);
        if kk > best_key {
            best = k;
            best_key = kk;
        }
    }
    order[best]
}

// ---------------------------------------------------------------- decision

/// The four backed columns: `(backed_eval, backed_move, backed_plies,
/// backed_nodes)`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub struct Backed {
    pub value: Option<i32>,
    pub mv: Move,
    pub plies: i64,
    pub quality: u64,
}

impl Backed {
    pub const NOTHING: Backed = Backed { value: None, mv: Move::NONE, plies: 0, quality: 0 };

    /// What a row stores (`_backed_stored`'s left side).
    pub fn stored(core: &Core, ext: &Ext) -> Backed {
        Backed { value: core.backed().map(i32::from), mv: core.backed_move, plies: core.backed_plies as i64, quality: ext.backed_nodes }
    }
}

/// What `_backed_for` reads of the row.
#[derive(Clone, Copy, Debug)]
pub struct Row {
    pub id: PosId,
    pub core: Core,
    pub nodes_invested: u64,
}

/// A question the decision leaves open, bought at the end of the level
/// (BKD-11): `(move, parent, quality)`.
pub type Discrepancy = (Move, PosId, u64);

/// BKD-5.1 `_backed_for`.  `children` are the `ChildValue`s of all the
/// row's edges; repetitions among them are set to 0 with no weight in place,
/// as v1 does to its child objects.
pub fn backed_for(
    row: &Row,
    children: &mut [ChildValue],
    mut discrepancies: Option<&mut Vec<Discrepancy>>,
    show: &Showcase,
    mut cycle_picks: Option<&mut Vec<Discrepancy>>,
) -> Backed {
    let core = &row.core;
    if let Some(exact) = status_eval(core.status()) {
        return Backed { value: Some(exact), mv: core.best_move, plies: 0, quality: PROVEN_QUALITY };
    }
    if !children.iter().any(|c| c.value.is_some()) {
        return Backed::NOTHING;
    }
    let stm = stm_white(core);
    for c in children.iter_mut() {
        if c.value.is_some() && c.cycles {
            c.value = Some(0);
            c.quality = 0;
        }
    }
    let own = core.eval().map(i32::from);
    let own_quality = row.nodes_invested;
    let own_back = |own: Option<i32>| match own {
        Some(o) => Backed { value: Some(o), mv: Move::NONE, plies: 0, quality: own_quality },
        None => Backed::NOTHING,
    };
    // BKD-3.3: the certificate holds against a floor.
    let floors: Vec<usize> = (0..children.len())
        .filter(|&i| {
            let c = &children[i];
            c.value.is_some() && !c.cycles && beats_certificate(c.floor, c.value.unwrap(), c.mv, show, stm)
        })
        .collect();
    let mut local: Vec<ChildValue>;
    let kids: &[ChildValue] = if floors.is_empty() {
        children
    } else {
        if let Some(d) = discrepancies.as_deref_mut() {
            let rung = own_quality.min(REQUEST_FIRST_RUNG);
            d.extend(floors.iter().map(|&i| (children[i].mv, row.id, rung)));
        }
        local = children.to_vec();
        for &i in &floors {
            local[i] = ChildValue::uninformed(local[i].mv, local[i].id);
        }
        if !local.iter().any(|c| c.value.is_some()) {
            return own_back(own);
        }
        local.as_mut_slice()
    };
    let informed: Vec<usize> = (0..kids.len()).filter(|&i| kids[i].value.is_some()).collect();
    let partial = coverage_is_partial(core.expanded(), kids);
    let bounded = show.bound.is_some() && (informed.len() < kids.len() || partial);
    let mut best = best_child(kids, &informed, stm, core.best_move);
    let complete = bounded || !partial;
    if kids[best].cycles {
        if let Some(p) = cycle_picks.as_deref_mut() {
            p.push((kids[best].mv, row.id, own_quality));
        }
        if !complete {
            return own_back(own);
        }
    }
    if let Some(o) = own {
        let b = &kids[best];
        if b.quality == 0 && (bounded || !complete) && better_for_mover(b.value.unwrap(), o, stm) {
            if let Some(d) = discrepancies.as_deref_mut() {
                d.push((b.mv, row.id, own_quality));
            }
            let vetoed: Vec<usize> =
                informed.iter().copied().filter(|&i| kids[i].quality == 0 && better_for_mover(kids[i].value.unwrap(), o, stm)).collect();
            let rest: Vec<usize> = if vetoed.iter().any(|&i| kids[i].cycles || kids[i].mv == core.best_move) {
                informed.iter().copied().filter(|&i| kids[i].quality > 0 && better_for_mover(kids[i].value.unwrap(), o, stm)).collect()
            } else {
                informed.iter().copied().filter(|i| !vetoed.contains(i)).collect()
            };
            if rest.is_empty() {
                return own_back(own);
            }
            best = best_child(kids, &rest, stm, core.best_move);
            if kids[best].cycles
                && let Some(p) = cycle_picks
            {
                p.push((kids[best].mv, row.id, own_quality));
            }
        }
    }
    let b = kids[best];
    let bv = b.value.unwrap();
    if !complete {
        if let Some(o) = own {
            if !better_for_mover(bv, o, stm) {
                return own_back(own);
            }
            if (b.quality as u128) * BACKED_QUALITY_TOLERANCE_DEN < own_quality as u128
                && let Some(d) = discrepancies
            {
                d.push((b.mv, row.id, own_quality));
            }
        } else if if stm { bv <= -MATE_BAND } else { bv >= MATE_BAND } {
            return Backed::NOTHING;
        }
    }
    let mut quality = b.quality;
    if (!complete || bounded) && quality >= PROVEN_QUALITY {
        quality = row.nodes_invested;
    }
    Backed { value: Some(bv), mv: b.mv, plies: 1 + b.plies, quality }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn cv(m: &str, value: Option<i32>, quality: u64) -> ChildValue {
        let mut c = ChildValue::uninformed(Move::parse(m).unwrap(), PosId(0));
        c.value = value;
        c.quality = quality;
        c
    }

    #[test]
    fn showcase_bound_and_lines() {
        // test_backprop_floor.py::FloorTests::test_the_bound_is_the_last_line_and_not_the_own_number (pure half)
        let a =
            json!([{"move": "e2e4", "eval_cp": 50}, {"move": "d2d4", "eval_cp": 20}, {"move": "c2c4", "eval_cp": -10, "prior_pass": true}]);
        let s = showcase_of(Some(&a));
        assert_eq!(s.bound, Some(20));
        assert_eq!(s.lines, vec![Move::parse("e2e4").unwrap(), Move::parse("d2d4").unwrap()]);
        // ::test_without_two_lines_there_is_no_certificate_to_hold
        let one = json!([{"move": "e2e4", "eval_cp": 50}]);
        assert_eq!(showcase_of(Some(&one)), Showcase::default());
        // a mate line without eval_cp bounds nothing; a float is not an int.
        let mate = json!([{"move": "e2e4", "eval_cp": 50}, {"move": "d2d4", "mate": 3}]);
        assert_eq!(showcase_of(Some(&mate)), Showcase::default());
        let float = json!([{"move": "e2e4", "eval_cp": 50}, {"move": "d2d4", "eval_cp": 2.5}]);
        assert_eq!(showcase_of(Some(&float)).bound, None);
        // the first move of the pv when `move` is missing.
        let pv = json!([{"pv": ["g1f3", "d7d5"], "eval_cp": 50}, {"move": "", "pv": ["b1c3"], "eval_cp": 1}]);
        assert_eq!(showcase_of(Some(&pv)).lines, vec![Move::parse("g1f3").unwrap(), Move::parse("b1c3").unwrap()]);
        assert_eq!(showcase_of(None), Showcase::default());
    }

    #[test]
    fn coverage() {
        let a = cv("a2a3", Some(1), 1);
        let n = cv("a2a4", None, 0);
        assert!(coverage_is_partial(true, &[n]));
        assert!(coverage_is_partial(false, &[a]));
        assert!(coverage_is_partial(true, &[a, n]));
        assert!(!coverage_is_partial(true, &[a]));
    }

    #[test]
    fn proof_rank_and_defence() {
        let mut win = cv("a2a3", Some(10_000), PROVEN_QUALITY);
        win.exact = true;
        win.mate_in = Some(3);
        assert_eq!(proof_rank(&win, true), (1, -3));
        assert_eq!(proof_rank(&win, false), (-1, 3));
        win.mate_in = None;
        assert_eq!(proof_rank(&win, true), (1, NEG_INF));
        let mut draw = cv("a2a3", Some(0), PROVEN_QUALITY);
        draw.exact = true;
        assert_eq!(proof_rank(&draw, true), (0, 0));
        assert_eq!(defence_moves(&draw, true), None);
        let mut loss = cv("a2a3", Some(-10_000), PROVEN_QUALITY);
        loss.exact = true;
        loss.mate_in = Some(13);
        assert_eq!(defence_moves(&loss, true), Some(7));
        loss.exact = false;
        assert_eq!(defence_moves(&loss, true), Some(POS_INF));
        let engine = cv("a2a3", Some(-9_995), 1);
        assert_eq!(defence_moves(&engine, true), Some(5));
        assert_eq!(defence_moves(&cv("a2a3", Some(-9_400), 1), true), None);
        assert_eq!(defence_moves(&cv("a2a3", Some(9_995), 1), true), None);
    }
}
