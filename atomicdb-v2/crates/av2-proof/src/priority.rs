//! The selector's `priority` (PRI-1.2 to PRI-1.6, PRI-2.1, PRI-2.3), computed
//! in memory (owner decision D5: not stored).
//!
//! In production (28-Nov-2026) the column is not recomputed at all:
//! `refresh_selector` runs with `--no-priorities` since 29-Aug and
//! `ATOMICDB_SELECTOR=pn`, so `Position.priority` only carries tombstones
//! (`DEAD`, PRI-1.7) and the values an old pass left, which still order
//! AUTO/FILL leases (queue file, lease order key 8).  The tombstone is
//! state, not a function of the graph: here it is an input
//! (`is_tombstone`), kept by the queue port (OQ-26: a boolean).
//!
//! This module is the v1 engine (PRI-2.1 global regret Dijkstra, PRI-3.3
//! scoring), which the bounded engine (PRI-2.2) only approximates; in memory
//! the global walk is cheap (see the bench).  Constants mirror
//! `av2-queue/src/consts.rs` (queue agent); one of the two copies should go
//! when both crates land.

use av2_model::{GraphRead, PosId, Status};
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};

pub const DEAD: f64 = -1e9;
pub const REGRET_WEIGHT: f64 = 3.0;
pub const DISCONNECTED_REGRET: f64 = 5.0;
pub const CAMPAIGN_BONUS: f64 = 40.0;
pub const EVAL_PROXIMITY_CAP: i64 = 1500;
pub const MATE_BAND_BONUS: f64 = 50.0;
pub const UNEXPANDED_BONUS: f64 = 2.0;
pub const VISIT_WEIGHT: f64 = 1.5;
pub const REGRET_CAP: f64 = 3000.0;
pub const FAR_REGRET: f64 = REGRET_CAP / 100.0;
pub const MATE_BAND: i64 = 9_000;
/// `SELECTOR_HORIZON_FLOOR` / `selector_horizon_width()` = max(4*8, 1000).
pub const SELECTOR_HORIZON: usize = 1_000;

/// `{campaign id: votes}` of ACTIVE campaigns (PRI-1.4).
pub type Votes = HashMap<u32, i64>;

/// `_status_eval` / PRI-1.2 `known_eval_of`.
pub fn known_eval_of(status: Status, backed_eval: Option<i32>, eval_cp: Option<i32>) -> Option<i32> {
    match status {
        Status::WhiteWin => Some(10_000),
        Status::BlackWin => Some(-10_000),
        Status::Draw => Some(0),
        Status::Unknown => backed_eval.or(eval_cp),
    }
}

/// PRI-1.3 `_runits`.
pub fn runits(regret: f64) -> f64 {
    if regret == f64::INFINITY { DISCONNECTED_REGRET } else { regret.min(REGRET_CAP) / 100.0 }
}

/// PRI-1.4 `_campaign_bonus`.
pub fn campaign_bonus(campaign: Option<u32>, votes: &Votes) -> f64 {
    let Some(c) = campaign else { return 0.0 };
    match votes.get(&c) {
        None => 0.0,
        Some(&v) => CAMPAIGN_BONUS * (v.max(0) as f64).ln_1p(),
    }
}

/// PRI-1.5 `priority_of`, evaluated left to right like v1.
pub fn priority_of(known: Option<i32>, expanded: bool, visits: i64, campaign: Option<u32>, runits: f64, votes: &Votes) -> f64 {
    let e = known.map(|k| (k as i64).abs()).unwrap_or(0);
    (e.min(EVAL_PROXIMITY_CAP) as f64) / 100.0
        + if e >= MATE_BAND { MATE_BAND_BONUS } else { 0.0 }
        + if expanded { 0.0 } else { UNEXPANDED_BONUS }
        - REGRET_WEIGHT * runits
        - VISIT_WEIGHT * visits as f64
        + campaign_bonus(campaign, votes)
}

/// PRI-1.6 `_priority_ceiling`.
pub fn priority_ceiling(votes: &Votes) -> f64 {
    let best = votes.values().copied().max().unwrap_or(0);
    EVAL_PROXIMITY_CAP as f64 / 100.0 + MATE_BAND_BONUS + UNEXPANDED_BONUS + CAMPAIGN_BONUS * (best.max(0) as f64).ln_1p()
}

fn known_of<G: GraphRead + ?Sized>(g: &G, id: PosId) -> Option<i32> {
    let c = g.core(id);
    known_eval_of(c.status(), c.backed().map(i32::from), c.eval().map(i32::from))
}

#[derive(PartialEq)]
struct HeapItem(f64, u64);
impl Eq for HeapItem {}
impl Ord for HeapItem {
    fn cmp(&self, o: &Self) -> Ordering {
        // min-heap on (regret, id)
        o.0.total_cmp(&self.0).then_with(|| o.1.cmp(&self.1))
    }
}
impl PartialOrd for HeapItem {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}

/// PRI-2.1 `_regret_from_root`: regret in cp of every position from the
/// root (`INFINITY` = unreached).  Gaps are >= 0 so Dijkstra is exact; a
/// closed node does not relax its children; an unvalued child inherits its
/// parent's regret.
pub fn regret_from_root<G: GraphRead + ?Sized>(g: &G, root: PosId) -> Vec<f64> {
    let n = g.len() as usize;
    let mut regret = vec![f64::INFINITY; n];
    if root.index() >= n {
        return regret;
    }
    regret[root.index()] = 0.0;
    let mut heap = BinaryHeap::new();
    heap.push(HeapItem(0.0, root.0));
    let mut kids = Vec::new();
    let mut vals: Vec<Option<i32>> = Vec::new();
    while let Some(HeapItem(r, k)) = heap.pop() {
        if r > regret[k as usize] {
            continue;
        }
        let c = g.core(PosId(k));
        if c.status() != Status::Unknown {
            continue;
        }
        kids.clear();
        g.children(PosId(k), &mut kids);
        vals.clear();
        vals.extend(kids.iter().map(|e| known_of(g, e.child)));
        let white = !c.black_to_move();
        let best = vals.iter().flatten().copied().reduce(|a, b| if white { a.max(b) } else { a.min(b) });
        for (e, v) in kids.iter().zip(vals.iter()) {
            let gap = match (v, best) {
                (Some(v), Some(b)) => (if white { b - v } else { v - b }) as f64,
                _ => 0.0,
            };
            let nr = r + gap;
            if nr < regret[e.child.index()] {
                regret[e.child.index()] = nr;
                heap.push(HeapItem(nr, e.child.0));
            }
        }
    }
    regret
}

/// PRI-3.3 scoring of every live open position: `(id, priority)` in id order.
/// `votes` are the ACTIVE campaigns' votes; `is_tombstone` the DEAD flag.
pub fn priorities<G: GraphRead + ?Sized>(g: &G, root: PosId, votes: &Votes, is_tombstone: impl Fn(PosId) -> bool) -> Vec<(PosId, f64)> {
    let regret = regret_from_root(g, root);
    let mut out = Vec::new();
    for i in 0..g.len() {
        let id = PosId(i);
        let c = g.core(id);
        if c.status() != Status::Unknown || is_tombstone(id) {
            continue;
        }
        let ext = g.ext(id).unwrap_or_default();
        let campaign = (ext.campaign != 0).then_some(ext.campaign);
        let p = priority_of(known_of(g, id), c.expanded(), ext.visits as i64, campaign, runits(regret[id.index()]), votes);
        out.push((id, p));
    }
    out
}

/// PRI-2.3 `_TopK`: the K best `(priority, id)`, replacing only on a strictly
/// greater priority.
pub fn top_k(scored: impl IntoIterator<Item = (PosId, f64)>, k: usize) -> Vec<(PosId, f64)> {
    #[derive(PartialEq)]
    struct Min(f64, u64);
    impl Eq for Min {}
    impl Ord for Min {
        fn cmp(&self, o: &Self) -> Ordering {
            o.0.total_cmp(&self.0).then_with(|| o.1.cmp(&self.1))
        }
    }
    impl PartialOrd for Min {
        fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
            Some(self.cmp(o))
        }
    }
    let mut heap: BinaryHeap<Min> = BinaryHeap::new();
    if k == 0 {
        return Vec::new();
    }
    for (id, p) in scored {
        if heap.len() < k {
            heap.push(Min(p, id.0));
        } else if p > heap.peek().unwrap().0 {
            heap.pop();
            heap.push(Min(p, id.0));
        }
    }
    let mut v: Vec<(PosId, f64)> = heap.into_iter().map(|m| (PosId(m.1), m.0)).collect();
    v.sort_by(|a, b| b.1.total_cmp(&a.1).then(a.0.cmp(&b.0)));
    v
}

#[cfg(test)]
mod tests {
    use super::*;
    use av2_model::{Core, Edge, Fingerprint, GraphWrite, Move};
    use av2_store::mem::MemGraph;

    fn pos(g: &mut MemGraph, n: u64, black: bool, status: Status, eval: Option<i16>, backed: Option<i16>) -> PosId {
        let mut c = Core::default();
        c.set_status(status, Default::default(), Default::default());
        c.set_flag(av2_model::record::F_BLACK_TO_MOVE, black);
        c.eval_cp = eval.unwrap_or(av2_model::record::NO_CP);
        c.backed_eval = backed.unwrap_or(av2_model::record::NO_CP);
        let mut fp = [0u8; 12];
        fp[..8].copy_from_slice(&n.to_be_bytes());
        g.create(Fingerprint(fp), None, c).unwrap()
    }

    #[test]
    fn the_formula_is_evaluated_in_v1_order() {
        let votes: Votes = [(3, 4)].into_iter().collect();
        // e = 9500 -> 15 + 50 + 2 - 3*1.5 - 1.5*2 + 40*ln(5)
        let p = priority_of(Some(-9_500), false, 2, Some(3), 1.5, &votes);
        let expected = 15.0 + 50.0 + 2.0 - 3.0 * 1.5 - 1.5 * 2.0 + 40.0 * 4f64.ln_1p();
        assert_eq!(p.to_bits(), expected.to_bits());
        assert_eq!(campaign_bonus(Some(9), &votes), 0.0);
        assert_eq!(campaign_bonus(None, &votes), 0.0);
        assert_eq!(runits(f64::INFINITY), DISCONNECTED_REGRET);
        assert_eq!(runits(4_000.0), 30.0);
        assert_eq!(FAR_REGRET, 30.0);
    }

    // test_selector_leak.py::RegretUsesBestKnownEvalTests (shape): the blind
    // parent contributes its gap through the backed value, a closed node is
    // not a motorway, a closed sibling still creates a gap.
    #[test]
    fn regret_uses_best_known_values_and_stops_at_closed_nodes() {
        let mut g = MemGraph::new();
        let root = pos(&mut g, 0, false, Status::Unknown, Some(0), None);
        let good = pos(&mut g, 1, true, Status::Unknown, None, Some(300)); // backed only
        let bad = pos(&mut g, 2, true, Status::Unknown, Some(-200), None);
        let won = pos(&mut g, 3, true, Status::WhiteWin, None, None);
        let under_won = pos(&mut g, 4, false, Status::Unknown, Some(0), None);
        g.set_children(root, &[Edge { child: good, mv: Move(1) }, Edge { child: bad, mv: Move(2) }, Edge { child: won, mv: Move(3) }])
            .unwrap();
        g.set_children(won, &[Edge { child: under_won, mv: Move(4) }]).unwrap();
        let r = regret_from_root(&g, root);
        // best for White is the closed win (10000): every sibling pays its gap.
        assert_eq!(r[good.index()], 10_000.0 - 300.0);
        assert_eq!(r[bad.index()], 10_200.0);
        assert_eq!(r[won.index()], 0.0);
        assert_eq!(r[under_won.index()], f64::INFINITY);
        let pr = priorities(&g, root, &Votes::new(), |_| false);
        assert!(pr.iter().all(|(id, _)| *id != won));
        let top = top_k(pr.clone(), 1);
        assert_eq!(top.len(), 1);
        assert!(top[0].1 >= pr.iter().map(|x| x.1).fold(f64::MIN, f64::max));
        // Tombstones are never scored.
        assert!(priorities(&g, root, &Votes::new(), |id| id == bad).iter().all(|(id, _)| *id != bad));
    }
}
