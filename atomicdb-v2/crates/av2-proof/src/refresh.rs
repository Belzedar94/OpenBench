//! The incremental refresh of one campaign: an exact port of
//! `proof._refresh_campaign` (PN-4.3) with `compute_numbers` (PN-4.1), the
//! cycling-edge rule (PN-3.1 to PN-3.4) and the caps (PN-4.4), over the
//! in-memory [`ProofTable`] instead of `ProofNode` rows.
//!
//! What is reproduced on purpose, because it decides stored values:
//!
//! - **Gauss-Seidel inside a level for updated rows, Jacobi for created
//!   rows.**  v1 mutates the `ProofNode` objects of `existing` in place, so a
//!   parent computed later in the same level reads the NEW numbers of a child
//!   updated earlier in that level; a row created in the level is not in
//!   `existing`, so a later parent still prices that child from its edge.
//!   Here: the table is updated in place, and rows created in the current
//!   level are hidden from the level's later computations.
//! - **The row order of a level.**  v1 reads the level with no ORDER BY.
//!   Here the order is `ProofGraph::order_key` (default: id, D3; the
//!   `KeyOrdered` adapter gives v1's usual key order).
//! - The caps (64 levels, 2 re-entries per key after the seed level,
//!   50,000 processed rows checked after a level that changed something),
//!   the `PROOF_GUARD` event, the sticky primary, the cycle walks' tie rules.
//!
//! What disappears with one writer: skip-locked (PN-4.5), `ignore_conflicts`
//! losses, READ COMMITTED reads; the carrier cache of PN-3.3 is the table
//! itself (with one writer every number the cache holds equals the table's).

use crate::campaign::Campaign;
use crate::gate::{GateConfig, GateLog, py_round};
use crate::graph::{Node, ProofGraph};
use crate::numbers::{self, Disagreement, Goal, LeafInput, PROOF_MAX_LEAF};
use crate::table::{IdMap, IdSet, ProofTable, Row};
use av2_model::{Edge, Move, PosId, Status};
use std::collections::HashSet;

pub const PROOF_MAX_PLIES: u32 = 64;
pub const PROOF_MAX_REVISITS: u8 = 2;
pub const PROOF_MAX_NODES: u64 = 50_000;
pub const PROOF_CYCLE_MAX_PLIES: u32 = 32;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RefreshConfig {
    pub max_plies: u32,
    pub max_revisits: u8,
    pub max_nodes: u64,
    pub cycle_max_plies: u32,
    pub gate: GateConfig,
}

impl Default for RefreshConfig {
    fn default() -> Self {
        RefreshConfig {
            max_plies: PROOF_MAX_PLIES,
            max_revisits: PROOF_MAX_REVISITS,
            max_nodes: PROOF_MAX_NODES,
            cycle_max_plies: PROOF_CYCLE_MAX_PLIES,
            gate: GateConfig::default(),
        }
    }
}

impl RefreshConfig {
    /// No caps: the refresh runs to rest (what D2 would allow once the exact
    /// port is validated).  Used by the rebuild and by property tests.
    pub fn uncapped() -> RefreshConfig {
        RefreshConfig { max_plies: u32::MAX, max_revisits: u8::MAX, max_nodes: u64::MAX, ..RefreshConfig::default() }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GuardReason {
    NodeBudget,
    PlyGuard,
}

impl GuardReason {
    pub fn v1(self) -> &'static str {
        match self {
            GuardReason::NodeBudget => "node-budget",
            GuardReason::PlyGuard => "ply-guard",
        }
    }
}

/// Events v1 writes as `DBEvent` rows; the writer turns them into facts.
#[derive(Clone, Debug, PartialEq)]
pub enum ProofEvent {
    /// `PROOF_GUARD` (PN-4.4).
    Guard { campaign: String, reason: GuardReason, processed: u64, plies: u32, seed_count: usize },
    /// `SOLVE_GATE_DISAGREE` (PN-2.10): payload already rounded like v1's.
    SolveGateDisagree { position: PosId, eval_cp: Option<i32>, annoyance: f64, factor: f64 },
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct RefreshReport {
    /// v1's return value: rows created + rows updated.
    pub written: u64,
    pub created: u64,
    pub updated: u64,
    pub processed: u64,
    pub plies: u32,
    /// Parents not re-entered because of the revisit cap (v1 drops them
    /// silently; counted here so that tests can tell "at rest" from "cut").
    pub revisits_dropped: u64,
    pub guard: Option<GuardReason>,
    pub events: Vec<ProofEvent>,
}

impl RefreshReport {
    pub fn absorb(&mut self, o: RefreshReport) {
        self.written += o.written;
        self.created += o.created;
        self.updated += o.updated;
        self.processed += o.processed;
        self.plies += o.plies;
        self.revisits_dropped += o.revisits_dropped;
        self.guard = self.guard.or(o.guard);
        self.events.extend(o.events);
    }
    /// Nothing was cut: the refreshed cone is at rest.
    pub fn at_rest(&self) -> bool {
        self.guard.is_none() && self.revisits_dropped == 0
    }
}

/// Per-call context: the batch's sequence number (row birth) and logical
/// time (gate log window).
pub struct RefreshCtx<'a> {
    pub seq: u64,
    pub now: f64,
    pub gate_log: &'a mut GateLog,
}

/// One out-edge with what the recurrences read of its child (v1's tuple
/// `(move_uci, child_id, child.status, child.eval_cp, child.fen)`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ChildEdge {
    pub mv: Move,
    pub child: PosId,
    pub node: Node,
}

/// Result of PN-4.1 for one node.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Computed {
    pub pn: u64,
    pub dn: u64,
    pub expanded: bool,
    pub selected: Move,
}

/// Edges `(parent, move)` whose value comes back into the parent (PN-3.1).
pub type Cycling = HashSet<(u64, u16)>;

fn leaf_of(n: Node) -> LeafInput {
    LeafInput { black_to_move: n.black_to_move, status: n.status, eval_cp: n.eval_cp }
}

/// PN-4.1 `compute_numbers`.
///
/// `child_row` answers "the child's ProofNode as the level sees it";
/// `previous` is this node's stored `selected_child` (`Move::NONE` = NULL).
#[allow(clippy::too_many_arguments)]
pub fn compute_numbers<G: ProofGraph + ?Sized>(
    g: &G,
    goal: Goal,
    gate: GateConfig,
    id: PosId,
    node: Node,
    children: &[ChildEdge],
    child_row: impl Fn(PosId) -> Option<Row>,
    previous: Move,
    cycling: &Cycling,
) -> (Computed, Option<Disagreement>) {
    if let Some((pn, dn)) = numbers::terminal_numbers(node.status, goal) {
        return (Computed { pn, dn, expanded: false, selected: Move::NONE }, None);
    }
    if children.is_empty() {
        let annoyance = gate.full(|| g.gate_row(id));
        let ((pn, dn), why) = numbers::leaf_numbers(leaf_of(node), goal, None, annoyance, true);
        return (Computed { pn, dn, expanded: false, selected: Move::NONE }, why);
    }
    let mut nums: Vec<(u64, u64)> = Vec::with_capacity(children.len());
    for e in children {
        let cycles = !cycling.is_empty() && cycling.contains(&(id.0, e.mv.0));
        let row = if cycles { None } else { child_row(e.child) };
        nums.push(match row {
            Some(r) => (r.pn, r.dn),
            None => {
                let annoyance = gate.shallow(|| g.fen_of(e.child), e.node.eval_cp);
                let ((mut pn, dn), _) = numbers::leaf_numbers(leaf_of(e.node), goal, None, annoyance, false);
                if cycles {
                    pn = PROOF_MAX_LEAF;
                }
                (pn, dn)
            }
        });
    }
    let or_node = goal.is_or_node(node.black_to_move);
    let (pn, dn) = numbers::internal_numbers(or_node, &nums).expect("children is not empty");
    let index = if or_node {
        let best = argmin(&nums, |n| n.0);
        let held = if previous.is_none() { None } else { children.iter().position(|e| e.mv == previous) };
        numbers::sticky_index(best, held, &nums)
    } else {
        argmin(&nums, |n| n.1)
    };
    (Computed { pn, dn, expanded: true, selected: children[index].mv }, None)
}

/// `min(range(n), key=lambda i: (v_i, i))`: the first minimum.
fn argmin(nums: &[(u64, u64)], f: impl Fn(&(u64, u64)) -> u64) -> usize {
    let mut best = 0;
    for i in 1..nums.len() {
        if f(&nums[i]) < f(&nums[best]) {
            best = i;
        }
    }
    best
}

/// PN-3.2 `_carrier_next`: the open child with a row that carries `node`'s
/// dn (`want_dn`) or pn; ties keep the first in edge order.
fn carrier_next<G: ProofGraph + ?Sized>(
    g: &G,
    goal: Goal,
    table: &ProofTable,
    node: PosId,
    want_dn: bool,
    buf: &mut Vec<Edge>,
) -> Option<PosId> {
    let or_node = goal.is_or_node(g.node(node).black_to_move);
    buf.clear();
    g.out_edges(node, buf);
    let mut best: Option<(PosId, u64)> = None;
    for e in buf.iter() {
        if g.node(e.child).status != Status::Unknown {
            continue;
        }
        let Some(r) = table.get(e.child) else { continue };
        let value = if want_dn { r.dn } else { r.pn };
        match best {
            None => best = Some((e.child, value)),
            Some((_, bv)) => {
                let better = if want_dn {
                    if or_node { value > bv } else { value < bv }
                } else if or_node {
                    value < bv
                } else {
                    value > bv
                };
                if better {
                    best = Some((e.child, value));
                }
            }
        }
    }
    best.map(|b| b.0)
}

/// PN-3.1 `_cycling_edges` for the parents of one level.
///
/// `children[i]` are the edges of `parents[i]`.  Walks follow value carriers
/// (PN-3.2) for at most `cap` steps; every doubtful stop claims no cycle.
pub fn cycling_edges<G: ProofGraph + ?Sized>(
    g: &G,
    goal: Goal,
    parents: &[PosId],
    children: &[&[ChildEdge]],
    table: &ProofTable,
    cap: u32,
) -> Cycling {
    let mut cycling = Cycling::new();
    // The table does not change during detection, so a node's carrier is
    // memoised for the whole call (walks share their tails).
    let mut memo: IdMap<[Option<Option<PosId>>; 2]> = IdMap::default();
    let mut buf = Vec::new();
    let mut seen: Vec<PosId> = Vec::new();
    for (pi, &parent) in parents.iter().enumerate() {
        for e in children[pi] {
            if e.node.status != Status::Unknown || !table.has(e.child) {
                continue;
            }
            for want_dn in [true, false] {
                if cycling.contains(&(parent.0, e.mv.0)) {
                    break;
                }
                let mut node = e.child;
                seen.clear();
                seen.push(node);
                for _ in 0..cap {
                    let slot = &mut memo.entry(node.0).or_insert([None, None])[want_dn as usize];
                    let below = match slot {
                        Some(b) => *b,
                        None => {
                            let b = carrier_next(g, goal, table, node, want_dn, &mut buf);
                            *slot = Some(b);
                            b
                        }
                    };
                    let Some(below) = below else { break };
                    if below == parent {
                        cycling.insert((parent.0, e.mv.0));
                        break;
                    }
                    if seen.contains(&below) {
                        break;
                    }
                    seen.push(below);
                    node = below;
                }
            }
        }
    }
    cycling
}

/// Read the out-edges of `ids` with their children's facts.
pub fn load_children<G: ProofGraph + ?Sized>(g: &G, ids: &[PosId], edges: &mut Vec<ChildEdge>, offsets: &mut Vec<usize>) {
    edges.clear();
    offsets.clear();
    offsets.push(0);
    let mut buf = Vec::new();
    for &id in ids {
        buf.clear();
        g.out_edges(id, &mut buf);
        for e in &buf {
            edges.push(ChildEdge { mv: e.mv, child: e.child, node: g.node(e.child) });
        }
        offsets.push(edges.len());
    }
}

fn disagreement_event(ctx: &mut RefreshCtx<'_>, id: PosId, why: Option<Disagreement>, events: &mut Vec<ProofEvent>) {
    if let Some(d) = why
        && ctx.gate_log.admit(id.0, ctx.now)
    {
        events.push(ProofEvent::SolveGateDisagree {
            position: id,
            eval_cp: d.eval_cp,
            annoyance: py_round(d.annoyance, 4),
            factor: py_round(d.factor, 3),
        });
    }
}

/// PN-4.3 `_refresh_campaign`.  `seeds` must already be deduplicated in
/// first-seen order (PN-4.2 does it once for all campaigns).
pub fn refresh_campaign<G: ProofGraph + ?Sized>(
    g: &G,
    table: &mut ProofTable,
    campaign: &Campaign,
    seeds: &[PosId],
    cfg: &RefreshConfig,
    ctx: &mut RefreshCtx<'_>,
) -> RefreshReport {
    let goal = campaign.goal;
    let n = g.positions();
    let mut rep = RefreshReport::default();
    let mut frontier: Vec<PosId> = seeds.to_vec();
    let mut visits: IdMap<u8> = IdMap::default();
    let mut created_level = IdSet::default();
    let mut level: Vec<PosId> = Vec::new();
    let mut edges: Vec<ChildEdge> = Vec::new();
    let mut offsets: Vec<usize> = Vec::new();
    let mut propagate: Vec<PosId> = Vec::new();
    let mut parents_buf: Vec<PosId> = Vec::new();
    let mut broke = false;
    while !frontier.is_empty() && rep.plies < cfg.max_plies {
        rep.plies += 1;
        level.clear();
        level.extend(frontier.iter().copied().filter(|id| id.0 < n));
        level.sort_unstable_by_key(|&id| (g.order_key(id), id.0));
        level.dedup();
        if level.is_empty() {
            broke = true;
            break;
        }
        load_children(g, &level, &mut edges, &mut offsets);
        let per_node: Vec<&[ChildEdge]> = (0..level.len()).map(|i| &edges[offsets[i]..offsets[i + 1]]).collect();
        let cycling = cycling_edges(g, goal, &level, &per_node, table, cfg.cycle_max_plies);
        created_level.clear();
        propagate.clear();
        for (i, &id) in level.iter().enumerate() {
            rep.processed += 1;
            let old = table.get(id);
            let previous = old.map(|r| r.selected).unwrap_or(Move::NONE);
            let (c, why) = {
                let t: &ProofTable = table;
                let hidden = &created_level;
                compute_numbers(
                    g,
                    goal,
                    cfg.gate,
                    id,
                    g.node(id),
                    per_node[i],
                    |cid| if hidden.contains(&cid.0) { None } else { t.get(cid) },
                    previous,
                    &cycling,
                )
            };
            disagreement_event(ctx, id, why, &mut rep.events);
            let row = Row { pn: c.pn, dn: c.dn, expanded: c.expanded, selected: c.selected, born: ctx.seq };
            match old {
                None => {
                    table.put(id, row);
                    created_level.insert(id.0);
                    rep.created += 1;
                    rep.written += 1;
                    propagate.push(id);
                }
                Some(o) if o.same_values(&row) => {}
                Some(o) => {
                    table.put(id, Row { born: o.born, ..row });
                    rep.updated += 1;
                    rep.written += 1;
                    propagate.push(id);
                }
            }
        }
        if propagate.is_empty() {
            broke = true;
            break;
        }
        if rep.processed >= cfg.max_nodes {
            rep.guard = Some(GuardReason::NodeBudget);
            broke = true;
            break;
        }
        // set(parent ids of edges whose child was propagated)
        let mut next = IdSet::default();
        for &c in &propagate {
            parents_buf.clear();
            g.in_parents(c, &mut parents_buf);
            next.extend(parents_buf.iter().map(|p| p.0));
        }
        let mut next: Vec<u64> = next.into_iter().collect();
        next.sort_unstable();
        frontier.clear();
        for k in next {
            let seen = visits.entry(k).or_insert(0);
            if *seen < cfg.max_revisits {
                *seen += 1;
                frontier.push(PosId(k));
            } else {
                rep.revisits_dropped += 1;
            }
        }
    }
    if !broke && !frontier.is_empty() {
        rep.guard = Some(GuardReason::PlyGuard);
    }
    if let Some(reason) = rep.guard {
        rep.events.push(ProofEvent::Guard {
            campaign: campaign.name.clone(),
            reason,
            processed: rep.processed,
            plies: rep.plies,
            seed_count: seeds.len(),
        });
    }
    rep
}
