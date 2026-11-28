//! The two per-node rules the benchmark climb applies.
//!
//! They are NOT AtomicDB's value semantics (those are ~40 rules, see the plan);
//! they are the smallest rules with the same read set and the same write
//! pattern: to decide a position you read its record, its out-edge block and
//! every child record; you write the position when the decision differs from
//! what it stores; and a change wakes its parents.

use crate::model::*;

/// Status cascade for one open position: `Some((status, mate_in, best_move))` when it closes.
pub fn close_rule(n: &Node, edges: &[Edge], kids: &[Node]) -> Option<(u16, u16, u16)> {
    if !n.is_open() || edges.is_empty() { return None; }
    let win = win_status_for(n.sign());
    let mut best_win: Option<(u16, u16)> = None; // (mate_in, mv), shortest
    let mut all_closed = true;
    let mut any_draw: Option<u16> = None;
    let mut longest_loss: Option<(u16, u16)> = None;
    for (e, k) in edges.iter().zip(kids) {
        let st = k.status();
        if st == ST_UNKNOWN { all_closed = false; continue; }
        if st == win {
            let cand = (k.mate_in, e.mv);
            if best_win.map_or(true, |b| cand < b) { best_win = Some(cand); }
        } else if st == ST_DRAW {
            if any_draw.is_none() { any_draw = Some(e.mv); }
        } else {
            let cand = (k.mate_in, e.mv);
            if longest_loss.map_or(true, |b| cand.0 > b.0 || (cand.0 == b.0 && cand.1 < b.1)) { longest_loss = Some(cand); }
        }
    }
    if let Some((m, mv)) = best_win { return Some((win, m.saturating_add(1), mv)); }
    if n.expanded() && all_closed {
        if let Some(mv) = any_draw { return Some((ST_DRAW, 0, mv)); }
        if let Some((m, mv)) = longest_loss { return Some((win_status_for(-n.sign()), m.saturating_add(1), mv)); }
    }
    None
}

/// One level of the backed-value negamax for one open position.
/// Returns the four backed columns it should hold.
pub fn decide(n: &Node, edges: &[Edge], kids: &[Node]) -> (i16, u16, u16, u64) {
    if !n.is_open() { return n.backed_tuple(); }
    let sign = n.sign();
    let mut best: Option<(i32, u64, u16, i16, u16)> = None; // (score for mover, quality, mv, white value, plies)
    let mut informed = 0usize;
    for (e, k) in edges.iter().zip(kids) {
        let (v, plies, q) = if !k.is_open() {
            (proven_value(k.status()), 1u16, PROVEN_QUALITY)
        } else if k.backed != NO_EVAL && k.backed_plies <= 256 {
            (k.backed, k.backed_plies.saturating_add(1), k.backed_nodes.max(k.nodes))
        } else if k.eval != NO_EVAL {
            (k.eval, 1u16, k.nodes)
        } else {
            continue;
        };
        informed += 1;
        let score = sign * v as i32;
        let better = match best {
            None => true,
            Some((bs, bq, bmv, _, _)) => score > bs || (score == bs && (q > bq || (q == bq && e.mv < bmv))),
        };
        if better { best = Some((score, q, e.mv, v, plies)); }
    }
    let Some((score, q, mv, v, plies)) = best else { return (NO_EVAL, 0, 0, 0) };
    // A searched position holds its engine's word about the moves nobody opened
    // (the MultiPV bound), so its valued moves decide; so does a fully valued one.
    let complete = n.expanded() && (informed == edges.len() || n.nodes > 0);
    // Otherwise coverage is partial: an own search stands unless a move beats it.
    if !complete && n.nodes > 0 && n.eval != NO_EVAL && sign * n.eval as i32 >= score {
        return (NO_EVAL, 0, 0, 0);
    }
    (v, mv, plies, q)
}

/// The noise cut of the ascent: a small drift on the same move wakes only the
/// parents that stand on this edge.
#[inline] pub fn is_small_drift(old: (i16, u16, u16, u64), new: (i16, u16, u16, u64)) -> bool {
    old.0 != NO_EVAL && new.0 != NO_EVAL && old.1 == new.1 && (old.0 as i32 - new.0 as i32).abs() < EPSILON_CP
}
