//! The proof frontier and its statistics (PN-10.1 to PN-10.4).
//!
//! v1 pages `ProofNode` by `(pn, position_id)` and joins `Position` in
//! chunks (PR #296) only to get the same rows as one JOIN; in memory the
//! JOIN is a filter.  Only explicit rows can be frontier (an implicit row is
//! `(0, INF)` or `(INF, 0)`, never finite on both sides).

use crate::graph::ProofGraph;
use crate::numbers::{Goal, PROOF_INFINITY};
use crate::table::{ProofTable, Row};
use av2_model::{PosId, Status};

/// `PROOF_FRONTIER_SCAN`.
pub const PROOF_FRONTIER_SCAN: usize = 20_000;

/// One frontier row: v1 returns `(position_id, fen, pn, dn)`; here the id and
/// the side to move (the only thing callers read of the FEN).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FrontierRow {
    pub id: PosId,
    pub black_to_move: bool,
    pub pn: u64,
    pub dn: u64,
}

/// PN-10.1 `frontier_rows`: rows with an open position and both numbers
/// finite, by `(pn, key)`, first `limit`.
pub fn frontier_rows<G: ProofGraph + ?Sized>(g: &G, t: &ProofTable, limit: usize) -> Vec<FrontierRow> {
    let mut v: Vec<(u64, u128, FrontierRow)> = Vec::new();
    for (id, r) in t.explicit() {
        if r.pn < PROOF_INFINITY && r.dn < PROOF_INFINITY {
            let n = g.node(id);
            if n.status == Status::Unknown {
                v.push((r.pn, g.order_key(id), FrontierRow { id, black_to_move: n.black_to_move, pn: r.pn, dn: r.dn }));
            }
        }
    }
    let by = |a: &(u64, u128, FrontierRow), b: &(u64, u128, FrontierRow)| (a.0, a.1, a.2.id.0).cmp(&(b.0, b.1, b.2.id.0));
    if v.len() > limit {
        if limit == 0 {
            return Vec::new();
        }
        v.select_nth_unstable_by(limit - 1, by);
        v.truncate(limit);
    }
    v.sort_unstable_by(by);
    v.into_iter().map(|x| x.2).collect()
}

/// PN-10.2 `frontier_and_rows`: the frontier restricted to AND nodes.
pub fn frontier_and_rows<G: ProofGraph + ?Sized>(g: &G, t: &ProofTable, goal: Goal, limit: usize) -> Vec<FrontierRow> {
    frontier_rows(g, t, limit).into_iter().filter(|r| !goal.is_or_node(r.black_to_move)).collect()
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Saturation {
    /// `pn >= INF or dn >= INF` (v1 `column=None`).
    Any,
    Dn,
    Pn,
    /// `dn >= INF and 0 < pn < INF`: the state sound recurrences cannot produce.
    Impossible,
}

fn saturated(r: &Row, s: Saturation) -> bool {
    match s {
        Saturation::Any => r.pn >= PROOF_INFINITY || r.dn >= PROOF_INFINITY,
        Saturation::Dn => r.dn >= PROOF_INFINITY,
        Saturation::Pn => r.pn >= PROOF_INFINITY,
        Saturation::Impossible => r.dn >= PROOF_INFINITY && r.pn > 0 && r.pn < PROOF_INFINITY,
    }
}

/// PN-10.3 `saturated_open_count` for one campaign's table.
pub fn saturated_open_count<G: ProofGraph + ?Sized>(g: &G, t: &ProofTable, s: Saturation) -> u64 {
    let mut n = 0;
    for id in t.ids() {
        if let Some(r) = t.get(id)
            && saturated(&r, s)
            && g.node(id).status == Status::Unknown
        {
            n += 1;
        }
    }
    n
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DnStats {
    pub and_nodes: u64,
    pub dn_median: u64,
    pub thin: u64,
    pub saturated: u64,
}

/// PN-10.2 `frontier_dn_stats`.
pub fn frontier_dn_stats<G: ProofGraph + ?Sized>(g: &G, t: &ProofTable, goal: Goal, floor: u64, limit: usize) -> DnStats {
    let saturated = saturated_open_count(g, t, Saturation::Impossible);
    let mut dns: Vec<u64> = frontier_and_rows(g, t, goal, limit).iter().map(|r| r.dn).collect();
    if dns.is_empty() {
        return DnStats { and_nodes: 0, dn_median: 0, thin: 0, saturated };
    }
    dns.sort_unstable();
    let m = dns.len() / 2;
    let median = if dns.len() % 2 == 1 { dns[m] } else { (dns[m - 1] + dns[m]) / 2 };
    DnStats { and_nodes: dns.len() as u64, dn_median: median, thin: dns.iter().filter(|&&d| d <= floor).count() as u64, saturated }
}

/// PN-10.4 `most_proving_frontier`: open rows by `(pn, key)`, no finiteness filter.
pub fn most_proving_frontier<G: ProofGraph + ?Sized>(g: &G, t: &ProofTable, limit: usize) -> Vec<(PosId, Row)> {
    let mut v: Vec<(u64, u128, PosId, Row)> = t
        .ids()
        .filter(|&id| g.node(id).status == Status::Unknown)
        .filter_map(|id| t.get(id).map(|r| (r.pn, g.order_key(id), id, r)))
        .collect();
    v.sort_unstable_by_key(|x| (x.0, x.1, x.2.0));
    v.truncate(limit);
    v.into_iter().map(|x| (x.2, x.3)).collect()
}

/// PN-10.4 `open_and_obligations`: open rows whose position has the defender
/// to move, oldest first (v1 orders by `ProofNode.updated`, the creation time;
/// here the creation sequence number, ties by key).
pub fn open_and_obligations<G: ProofGraph + ?Sized>(g: &G, t: &ProofTable, goal: Goal, limit: usize) -> Vec<(PosId, Row)> {
    let mut v: Vec<(u64, u128, PosId, Row)> = t
        .ids()
        .filter(|&id| {
            let n = g.node(id);
            n.status == Status::Unknown && !goal.is_or_node(n.black_to_move)
        })
        .filter_map(|id| t.get(id).map(|r| (r.born, g.order_key(id), id, r)))
        .collect();
    v.sort_unstable_by_key(|x| (x.0, x.1, x.2.0));
    v.truncate(limit);
    v.into_iter().map(|x| (x.2, x.3)).collect()
}
