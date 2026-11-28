//! PN-7.1 `proved_or_clamp`: the minimum budget of a node the proof no
//! longer needs (a sibling of a proved child under an OR parent).
//!
//! Called by the minting loop (queue catalog, PN-6.6) after the budget is
//! computed; reads parents and their children's statuses only.

use crate::campaign::Campaign;
use crate::graph::ProofGraph;
use av2_model::{PosId, Status};

pub const OR_CLAMP_NODES: u64 = 250_000;
pub const OR_CLAMP_MULTIPV: u32 = 1;
pub const OR_CLAMP_MAX_PARENTS: usize = 32;

/// `_settled_for`: no parent still needs the node for this goal.
fn settled_for<G: ProofGraph + ?Sized>(g: &G, campaign: &Campaign, parents: &[PosId], proved: &[PosId]) -> bool {
    parents.iter().all(|&p| {
        let n = g.node(p);
        n.status != Status::Unknown || (campaign.goal.is_or_node(n.black_to_move) && proved.contains(&p))
    })
}

/// PN-7.1.  `enabled` is `settings.ATOMICDB_OR_CLAMP` (default true);
/// `campaigns` is v1's argument (the active campaigns when the caller has
/// none to give).
pub fn proved_or_clamp<G: ProofGraph + ?Sized>(g: &G, position: PosId, campaigns: &[&Campaign], enabled: bool) -> Option<(u64, u32)> {
    if !enabled || campaigns.is_empty() {
        return None;
    }
    let mut parents = Vec::new();
    g.in_parents(position, &mut parents);
    parents.sort_unstable();
    parents.dedup();
    if parents.is_empty() || parents.len() > OR_CLAMP_MAX_PARENTS {
        return None;
    }
    let mut edges = Vec::new();
    for c in campaigns {
        let goal = c.goal.status();
        let proved: Vec<PosId> = parents
            .iter()
            .copied()
            .filter(|&p| {
                edges.clear();
                g.out_edges(p, &mut edges);
                edges.iter().any(|e| g.node(e.child).status == goal)
            })
            .collect();
        if proved.is_empty() || !settled_for(g, c, &parents, &proved) {
            return None;
        }
    }
    Some((OR_CLAMP_NODES, OR_CLAMP_MULTIPV))
}
