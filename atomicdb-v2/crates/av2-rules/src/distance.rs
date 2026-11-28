//! Mate distance: steps 3 and 5 of the port.
//!
//! - DTM-4.1 the own witness (`read::own_witness`)
//! - DTM-1.1 the winner's distance (`ingest.mate_distance_refresh`, `_witness_rank`)
//! - DTM-2.1 the loser's distance (`ingest._loser_distance`)
//! - DTM-3.1 a repetition cannot lengthen a mate (`_support_next`,
//!   `support_reaches`, the loop check inside `_loser_distance`): a bounded
//!   walk of at most `MATE_SUPPORT_HOPS` positions, no clock.
//!
//! Distances are plies, as `i64` here and `u16` in the record.

use crate::conv::{is_decisive, is_verified, mate_of, mover_win, stm_white, uci_order};
use crate::exact::Kid;
use crate::read::own_witness;
use av2_model::{Core, Edge, GraphRead, Move, PosId, Status};
use std::collections::HashMap;

/// `ingest.MATE_SUPPORT_HOPS`.
pub const MATE_SUPPORT_HOPS: usize = 64;

/// When the loser rule looks for a loop (`loop_check` of v1).
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum LoopCheck {
    /// The cascade's: only when a loser node would lengthen.
    #[default]
    Raise,
    /// The listed repair's (`backfill_mate_distance`): whenever a child sets
    /// the number.
    Always,
    /// Any other value of v1's argument: never.
    Never,
}

/// `(mate_in, best_move)` a caller holds in memory and has not written yet
/// (the backfill's overlay).
pub type Overrides = HashMap<PosId, (Option<i64>, Move)>;

/// The arguments of the loop check (v1 `loop_check`, `overrides` and the
/// module constant `MATE_SUPPORT_HOPS`, which tests patch).
#[derive(Clone, Copy, Debug)]
pub struct DtmOpts<'a> {
    pub loop_check: LoopCheck,
    pub overrides: Option<&'a Overrides>,
    pub hops: usize,
}

impl Default for DtmOpts<'_> {
    fn default() -> Self {
        DtmOpts { loop_check: LoopCheck::Raise, overrides: None, hops: MATE_SUPPORT_HOPS }
    }
}

/// DTM-1.1 / DTM-2.1 `mate_distance_refresh`: `(mate_in, best_move)` of a
/// closed decisive node from its children (`kids` in edge order by move).
pub fn mate_distance_refresh(g: &dyn GraphRead, id: PosId, core: &Core, kids: &[Kid], opts: DtmOpts) -> (Option<i64>, Move) {
    let stm = stm_white(core);
    if core.status() != mover_win(stm) {
        return loser_distance(g, id, core, kids, opts);
    }
    // winners in edge order; `min` keeps the first of equal keys.
    let mut best: Option<&Kid> = None;
    let rank = |k: &Kid| (mate_of(&k.core).unwrap(), if is_verified(&k.core) { 0 } else { 1 });
    for k in kids.iter().filter(|k| k.core.status() == core.status() && k.core.mate().is_some()) {
        if best.is_none_or(|b| rank(k) < rank(b)) {
            best = Some(k);
        }
    }
    let own = own_witness(g, id, core);
    let derived = best.map(|b| 1 + mate_of(&b.core).unwrap());
    let new_mate = match (own.plies, derived) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (a, b) => a.or(b),
    };
    let stored = mate_of(core);
    let from_child = best.is_some() && derived == new_mate;
    if from_child {
        let b = best.unwrap();
        if new_mate != stored || (core.best_move != b.mv && is_verified(&b.core)) {
            return (new_mate, b.mv);
        }
    }
    if !from_child
        && new_mate != stored
        && own.plies.is_some()
        && let Some(first) = own.line_first
    {
        return (new_mate, first);
    }
    (new_mate, core.best_move)
}

/// DTM-2.1 and DTM-3.1 `_loser_distance`.
pub fn loser_distance(g: &dyn GraphRead, id: PosId, core: &Core, kids: &[Kid], opts: DtmOpts) -> (Option<i64>, Move) {
    let DtmOpts { loop_check, overrides, hops } = opts;
    let full = !kids.is_empty() && kids.iter().all(|k| k.core.mate().is_some());
    let own = own_witness(g, id, core);
    let stored = mate_of(core);
    if !full && own.plies.is_none() {
        return (stored, core.best_move);
    }
    let (counted, mut floor): (Vec<&Kid>, Option<i64>) = if full {
        (kids.iter().collect(), None)
    } else {
        (kids.iter().filter(|k| k.core.status() == core.status() && k.core.mate().is_some()).collect(), own.plies)
    };
    let len = |k: &Kid| 1 + mate_of(&k.core).unwrap();
    let candidate = counted.iter().map(|k| len(k)).chain(floor).max().unwrap();
    let looks = match loop_check {
        LoopCheck::Always => true,
        LoopCheck::Raise => stored.is_none_or(|m| candidate > m),
        LoopCheck::Never => false,
    };
    let mut excluded: Vec<Move> = Vec::new();
    if looks {
        let mut order: Vec<&Kid> = counted.clone();
        order.sort_by_key(|k| (-mate_of(&k.core).unwrap(), uci_order(k.mv)));
        for k in order {
            let length = len(k);
            if floor.is_some_and(|f| length <= f) {
                break;
            }
            if loop_check == LoopCheck::Raise && stored.is_some_and(|m| length <= m) {
                break;
            }
            if !support_reaches(g, k.id, id, hops, overrides) {
                break;
            }
            excluded.push(k.mv);
        }
    }
    if excluded.is_empty() {
        return (Some(candidate), core.best_move);
    }
    let rest: Vec<&Kid> = counted.iter().copied().filter(|k| !excluded.contains(&k.mv)).collect();
    if full {
        floor = own.plies;
    }
    let value = rest.iter().map(|k| len(k)).chain(floor).max();
    let mut mv = core.best_move;
    if excluded.contains(&mv) {
        // max by (mate_in, move): the first maximum (keys are unique).
        let source = rest.iter().copied().fold(None::<&Kid>, |acc, k| match acc {
            Some(a) if (mate_of(&a.core), uci_order(a.mv)) >= (mate_of(&k.core), uci_order(k.mv)) => Some(a),
            _ => Some(k),
        });
        mv = if source.is_some_and(|s| Some(len(s)) == value) {
            source.unwrap().mv
        } else if own.plies.is_some() && own.plies == value && own.line_first.is_some() {
            own.line_first.unwrap()
        } else {
            Move::NONE
        };
    }
    (value, mv)
}

/// What `_support_next` reads of a child: `(move, child, status, mate_in)`.
#[derive(Clone, Copy, Debug)]
pub struct SupportChild {
    pub mv: Move,
    pub id: PosId,
    pub status: Status,
    pub mate_in: Option<i64>,
}

/// DTM-3.1 `_support_next`: the child the distance of a node stands on.
pub fn support_next(
    g: &dyn GraphRead,
    id: PosId,
    core: &Core,
    mate_in: Option<i64>,
    best_move: Move,
    children: &[SupportChild],
) -> Option<PosId> {
    let status = core.status();
    if !is_decisive(status) {
        return None;
    }
    let mw = mover_win(stm_white(core));
    let own = own_witness(g, id, core).plies;
    let same: Vec<&SupportChild> = children.iter().filter(|c| c.status == status && c.mate_in.is_some()).collect();
    if mate_in.is_none() || (status != mw && own.is_none() && same.is_empty()) {
        if own.is_some() {
            return None;
        }
        return children.iter().find(|c| c.mv == best_move && c.status == status).map(|c| c.id);
    }
    let mate_in = mate_in.unwrap();
    if status == mw {
        let best = same.iter().min_by_key(|c| (c.mate_in.unwrap(), c.mv != best_move, uci_order(c.mv)))?;
        if own.is_some_and(|o| o <= 1 + best.mate_in.unwrap()) {
            return None;
        }
        return Some(best.id);
    }
    let mut fitting: Vec<&&SupportChild> = same.iter().filter(|c| c.mate_in.unwrap() < mate_in).collect();
    if fitting.is_empty() {
        if own.is_some() || same.is_empty() {
            return None;
        }
        fitting = same.iter().collect();
    }
    let top = fitting.iter().max_by_key(|c| (c.mate_in.unwrap(), c.mv == best_move, uci_order(c.mv))).unwrap();
    if own.is_some_and(|o| o > top.mate_in.unwrap()) {
        return None;
    }
    Some(top.id)
}

/// DTM-3.1 `support_reaches`: does the chain of moves the distance of
/// `start` stands on reach `target` within `hops` positions?
pub fn support_reaches(g: &dyn GraphRead, start: PosId, target: PosId, hops: usize, overrides: Option<&Overrides>) -> bool {
    let mut key = Some(start);
    let mut seen: Vec<PosId> = Vec::new();
    let mut buf: Vec<Edge> = Vec::new();
    for _ in 0..hops {
        let Some(k) = key else { return false };
        if k == target {
            return true;
        }
        if seen.contains(&k) {
            return false;
        }
        seen.push(k);
        buf.clear();
        g.children(k, &mut buf);
        if buf.is_empty() {
            return false;
        }
        let core = g.core(k);
        let (mut mate_in, mut best_move) = (mate_of(&core), core.best_move);
        if let Some(&(m, b)) = overrides.and_then(|o| o.get(&k)) {
            mate_in = m;
            best_move = b;
        }
        let children: Vec<SupportChild> = buf
            .iter()
            .map(|e| {
                let c = g.core(e.child);
                let m = match overrides.and_then(|o| o.get(&e.child)) {
                    Some(&(m, _)) => m,
                    None => mate_of(&c),
                };
                SupportChild { mv: e.mv, id: e.child, status: c.status(), mate_in: m }
            })
            .collect();
        key = support_next(g, k, &core, mate_in, best_move, &children);
    }
    false
}
