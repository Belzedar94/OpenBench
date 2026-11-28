//! Proof numbers from scratch: the startup rebuild of D5 ("computed in
//! memory, not stored": after a restart nothing is loaded, so the numbers
//! must be a function of the graph), and the engine of the lazy alternative
//! (recompute on read, cached per commit epoch) that the bench compares with
//! the writer-maintained table.
//!
//! **What "from scratch" means** (the proofs file's Q3, recommendation (b)):
//! the fixpoint of the PN-4.1 recurrences over the rows, with the PN-3 cycle
//! rule, computed Gauss-Seidel in descending id order (children are created
//! after their parents, so this is nearly bottom-up) and then by a FIFO
//! worklist of the parents of every changed row, until nothing changes.
//! Rows are given to every position that has out-edges or is closed (and the
//! root), i.e. "the proof has expanded it or it is a leaf with a verdict".
//!
//! Where it can differ from v1's stored numbers (the bench and the property
//! tests measure it): (1) the sticky primary starts with no history, so a
//! primary held by hysteresis can move once; (2) v1's numbers that are stale
//! because a cap cut a refresh are fresh here; (3) row presence of
//! positions that were never seeded in v1.
//!
//! Cycle detection only runs for nodes inside a non-trivial strongly
//! connected component of the open rows: a carrier walk that returns to its
//! parent is a cycle through it, so for any other node the rule can claim
//! nothing (exact, and it keeps the rebuild linear on the DAG parts).

use crate::campaign::Campaign;
use crate::gate::GateConfig;
use crate::graph::ProofGraph;
use crate::refresh::{ChildEdge, Cycling, PROOF_CYCLE_MAX_PLIES, compute_numbers, cycling_edges};
use crate::table::{ProofTable, Row};
use av2_model::{Edge, Move, PosId, Status};
use std::collections::VecDeque;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scope {
    /// Every position.
    All,
    /// Positions reachable from this root through open positions (the lazy
    /// "numbers of this node" computation; the campaign root gives the whole
    /// proof tree).
    Below(PosId),
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RebuildReport {
    pub rows: u64,
    pub computations: u64,
    pub cyclic_nodes: u64,
    /// Nodes whose recomputation hit the per-node cap (non-convergence).
    pub unconverged: u64,
}

/// Per-node recomputation cap of the worklist (a safety net: v1's own caps
/// are 2 re-entries; cycles are already cut by PN-3).
pub const REBUILD_MAX_RECOMPUTE: u16 = 256;

struct Bits(Vec<u64>);
impl Bits {
    fn new(n: u64) -> Bits {
        Bits(vec![0; n.div_ceil(64) as usize])
    }
    #[inline]
    fn get(&self, i: u64) -> bool {
        self.0[(i / 64) as usize] >> (i % 64) & 1 == 1
    }
    #[inline]
    fn set(&mut self, i: u64, v: bool) {
        let w = &mut self.0[(i / 64) as usize];
        if v { *w |= 1 << (i % 64) } else { *w &= !(1 << (i % 64)) }
    }
}

fn children_of<G: ProofGraph + ?Sized>(g: &G, id: PosId, buf: &mut Vec<Edge>, out: &mut Vec<ChildEdge>) {
    buf.clear();
    out.clear();
    g.out_edges(id, buf);
    out.extend(buf.iter().map(|e| ChildEdge { mv: e.mv, child: e.child, node: g.node(e.child) }));
}

/// The row set of a scope, in ascending id order.
fn rows_of<G: ProofGraph + ?Sized>(g: &G, scope: Scope, campaign: &Campaign) -> (Vec<PosId>, Bits) {
    let n = g.positions();
    let mut in_r = Bits::new(n);
    let mut out = Vec::new();
    let mut buf = Vec::new();
    match scope {
        Scope::All => {
            for i in 0..n {
                let id = PosId(i);
                buf.clear();
                g.out_edges(id, &mut buf);
                if !buf.is_empty() || g.node(id).status != Status::Unknown || id == campaign.root {
                    in_r.set(i, true);
                    out.push(id);
                }
            }
        }
        Scope::Below(root) => {
            let mut seen = Bits::new(n);
            let mut q = VecDeque::new();
            if root.0 < n {
                seen.set(root.0, true);
                q.push_back(root);
            }
            while let Some(id) = q.pop_front() {
                let open = g.node(id).status == Status::Unknown;
                buf.clear();
                if open {
                    g.out_edges(id, &mut buf);
                }
                if !buf.is_empty() || !open || id == root {
                    in_r.set(id.0, true);
                    out.push(id);
                }
                for e in &buf {
                    if !seen.get(e.child.0) {
                        seen.set(e.child.0, true);
                        q.push_back(e.child);
                    }
                }
            }
            out.sort_unstable();
        }
    }
    (out, in_r)
}

/// Nodes of non-trivial SCCs among open rows (iterative Tarjan).
fn cyclic_nodes<G: ProofGraph + ?Sized>(g: &G, rows: &[PosId], in_r: &Bits) -> Bits {
    let n = g.positions();
    let mut cyc = Bits::new(n);
    let open = |id: PosId| in_r.get(id.0) && g.node(id).status == Status::Unknown;
    let mut index: Vec<u32> = Vec::new();
    let mut low: Vec<u32> = Vec::new();
    // Dense index maps only for the rows (position -> slot).
    let mut slot = std::collections::HashMap::<u64, u32>::with_capacity(rows.len());
    for (i, id) in rows.iter().enumerate() {
        slot.insert(id.0, i as u32);
    }
    index.resize(rows.len(), u32::MAX);
    low.resize(rows.len(), 0);
    let mut on_stack = vec![false; rows.len()];
    let mut stack: Vec<u32> = Vec::new();
    let mut counter = 0u32;
    let mut succ: Vec<Vec<u32>> = vec![Vec::new(); rows.len()];
    let mut buf = Vec::new();
    for (i, &id) in rows.iter().enumerate() {
        if !open(id) {
            continue;
        }
        buf.clear();
        g.out_edges(id, &mut buf);
        for e in &buf {
            if open(e.child) {
                succ[i].push(slot[&e.child.0]);
            }
        }
    }
    for start in 0..rows.len() as u32 {
        if index[start as usize] != u32::MAX || !open(rows[start as usize]) {
            continue;
        }
        let mut call: Vec<(u32, usize)> = vec![(start, 0)];
        index[start as usize] = counter;
        low[start as usize] = counter;
        counter += 1;
        stack.push(start);
        on_stack[start as usize] = true;
        while let Some(&(v, k)) = call.last() {
            if k < succ[v as usize].len() {
                let w = succ[v as usize][k];
                call.last_mut().unwrap().1 += 1;
                if index[w as usize] == u32::MAX {
                    index[w as usize] = counter;
                    low[w as usize] = counter;
                    counter += 1;
                    stack.push(w);
                    on_stack[w as usize] = true;
                    call.push((w, 0));
                } else if on_stack[w as usize] {
                    low[v as usize] = low[v as usize].min(index[w as usize]);
                }
            } else {
                call.pop();
                if let Some(&(u, _)) = call.last() {
                    low[u as usize] = low[u as usize].min(low[v as usize]);
                }
                if low[v as usize] == index[v as usize] {
                    let mut members = Vec::new();
                    loop {
                        let w = stack.pop().unwrap();
                        on_stack[w as usize] = false;
                        members.push(w);
                        if w == v {
                            break;
                        }
                    }
                    let self_loop = succ[v as usize].contains(&v);
                    if members.len() > 1 || self_loop {
                        for w in members {
                            cyc.set(rows[w as usize].0, true);
                        }
                    }
                }
            }
        }
    }
    cyc
}

/// Compute a campaign's table from scratch.  `seq` is stamped as the rows'
/// birth.
pub fn rebuild<G: ProofGraph + ?Sized>(
    g: &G,
    campaign: &Campaign,
    gate: GateConfig,
    scope: Scope,
    seq: u64,
) -> (ProofTable, RebuildReport) {
    let n = g.positions();
    let goal = campaign.goal;
    let (rows, in_r) = rows_of(g, scope, campaign);
    let cyc = cyclic_nodes(g, &rows, &in_r);
    let mut rep = RebuildReport { rows: rows.len() as u64, ..Default::default() };
    let mut table = ProofTable::new();
    let mut queued = Bits::new(n);
    let mut count: Vec<u16> = vec![0; n as usize];
    let mut q: VecDeque<PosId> = rows.iter().rev().copied().collect();
    for id in &rows {
        queued.set(id.0, true);
    }
    let mut buf = Vec::new();
    let mut kids = Vec::new();
    let mut parents = Vec::new();
    let empty = Cycling::new();
    while let Some(id) = q.pop_front() {
        queued.set(id.0, false);
        let c = &mut count[id.index()];
        if *c >= REBUILD_MAX_RECOMPUTE {
            rep.unconverged += 1;
            continue;
        }
        *c += 1;
        rep.computations += 1;
        let node = g.node(id);
        children_of(g, id, &mut buf, &mut kids);
        let cycling = if cyc.get(id.0) && node.status == Status::Unknown {
            cycling_edges(g, goal, &[id], &[&kids], &table, PROOF_CYCLE_MAX_PLIES)
        } else {
            Cycling::new()
        };
        let old = table.get(id);
        let previous = old.map(|r| r.selected).unwrap_or(Move::NONE);
        let (cmp, _) = compute_numbers(
            g,
            goal,
            gate,
            id,
            node,
            &kids,
            |cid| table.get(cid),
            previous,
            if cycling.is_empty() { &empty } else { &cycling },
        );
        let row = Row { pn: cmp.pn, dn: cmp.dn, expanded: cmp.expanded, selected: cmp.selected, born: seq };
        let changed = match old {
            None => true,
            Some(o) => !o.same_values(&row),
        };
        if changed {
            table.put(id, row);
            parents.clear();
            g.in_parents(id, &mut parents);
            for &p in &parents {
                if in_r.get(p.0) && !queued.get(p.0) {
                    queued.set(p.0, true);
                    q.push_back(p);
                }
            }
        }
    }
    rep.cyclic_nodes = rows.iter().filter(|id| cyc.get(id.0)).count() as u64;
    (table, rep)
}

/// The lazy alternative: numbers recomputed on read, cached until the next
/// commit (epoch = the store's applied sequence number).
#[derive(Default)]
pub struct EpochCache {
    pub epoch: Option<u64>,
    pub table: ProofTable,
    pub recomputes: u64,
}

impl EpochCache {
    pub fn get<G: ProofGraph + ?Sized>(&mut self, g: &G, campaign: &Campaign, gate: GateConfig, epoch: u64) -> &ProofTable {
        if self.epoch != Some(epoch) {
            self.table = rebuild(g, campaign, gate, Scope::Below(campaign.root), epoch).0;
            self.epoch = Some(epoch);
            self.recomputes += 1;
        }
        &self.table
    }
}
