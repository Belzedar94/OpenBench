//! The two descents of the pn selector: df-pn (PN-6) and the value walker
//! (PN-9) of `docs/rules-spec-proofs.md`.  Port of `atomicdb/proof.py`
//! (`_point`, `_bucket`, `campaign_roots`, `campaign_start`,
//! `_ranked_children`, `_primary_index`, `descend`, and the value walker from
//! `_value_ranked` to `descend_value`).
//!
//! Read-only over the graph ([`ProofGraph`]) and one campaign's
//! [`ProofTable`].  The queue (`av2-queue`) drives them from its minting loop
//! (PN-6.6) and passes what only it knows: the batch reservation and which
//! positions already have a live task.
//!
//! Gate: the children are priced from their edge exactly like
//! `_ranked_children` does (`shallow_annoyance`), with the switch the caller
//! passes in [`DescentConfig::gate`].  Owner decision (28-Nov): the stored and
//! served numbers are computed gate-off; the gate is on only here, where the
//! selector ranks children.
//!
//! Not ported: `shared_alt.observe` (a shadow logger, off by default in v1,
//! whose result the walk never reads).

use crate::campaign::{Campaign, normalized_policy};
use crate::gate::GateConfig;
use crate::graph::{Node, ProofGraph, ValueInfo};
use crate::numbers::{self, LeafInput};
use crate::priority::known_eval_of;
use crate::table::ProofTable;
use av2_model::{Edge, Move, PosId, Status};
use std::collections::HashSet;

/// `proof.DESCENT_MAX_PLIES`.
pub const DESCENT_MAX_PLIES: u32 = 96;
/// `proof.CAMPAIGN_DESCENT_SHARE` (PN-6.3).
pub const CAMPAIGN_DESCENT_SHARE: f64 = 0.35;
/// `proof.CAMPAIGN_ACTIVATION_VOTES` (PN-6.3).
pub const CAMPAIGN_ACTIVATION_VOTES: i64 = 1;
/// `proof.VALUE_ALTERNATIVE_MARGIN_CP` (PN-9.6).
pub const VALUE_ALTERNATIVE_MARGIN_CP: i64 = 150;
/// `proof.VALUE_BACKTRACK_MAX` (PN-9.7).
pub const VALUE_BACKTRACK_MAX: usize = 64;
/// `proof.LONG_LINE_PLIES` (PN-9.10).
pub const LONG_LINE_PLIES: i64 = 200;
/// `proof.LONG_LINE_HALF_LIFE_PLIES`.
pub const LONG_LINE_HALF_LIFE_PLIES: i64 = 40;
/// `proof.LONG_LINE_PLAN_DECAY` (PN-9.11).
pub const LONG_LINE_PLAN_DECAY: f64 = 0.5;
/// `proof.LONG_LINE_SCAN_PLANS`.
pub const LONG_LINE_SCAN_PLANS: usize = 8;

// ------------------------------------------------------------ draws (PN-6.2, PN-6.3)

/// `proof._point(counter, salt)`: a uniform point in `[0, 1)` from the
/// counter, by domain.
pub fn point(counter: u64, salt: &str) -> f64 {
    use sha2::{Digest, Sha256};
    let d = Sha256::digest(format!("{salt}{counter}").as_bytes());
    let x = u64::from_be_bytes(d[..8].try_into().expect("8 bytes"));
    // Python: int / float converts the int to the nearest double first.
    x as f64 / 18_446_744_073_709_551_616.0
}

/// The repertoire bucket of a draw.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Bucket {
    Primary,
    Backup,
    Explore,
}

/// `proof._bucket(counter, policy)` with the normalised policy.
pub fn bucket(counter: u64, policy: (f64, f64, f64)) -> Bucket {
    let p = point(counter, "");
    if p < policy.0 {
        Bucket::Primary
    } else if p < policy.0 + policy.1 {
        Bucket::Backup
    } else {
        Bucket::Explore
    }
}

/// A community campaign as `campaign_roots` reads it (`Campaign(state,
/// votes, root)`; the relational side owns the rows).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CommunityCampaign {
    pub id: u64,
    pub root: PosId,
    /// `state == ACTIVE`.
    pub active: bool,
    pub votes: i64,
}

/// `proof.campaign_roots()`: `(root, ln(1 + max(1, votes)))` of the ACTIVE
/// campaigns whose root is still open, by id.  The caller passes `[]` when
/// campaign descent is off.
pub fn campaign_roots<G: ProofGraph + ?Sized>(g: &G, rows: &[CommunityCampaign]) -> Vec<(PosId, f64)> {
    let mut rows: Vec<&CommunityCampaign> =
        rows.iter().filter(|c| c.active && c.root.0 < g.positions() && g.node(c.root).status == Status::Unknown).collect();
    rows.sort_by_key(|c| c.id);
    rows.into_iter().map(|c| (c.root, (c.votes.max(CAMPAIGN_ACTIVATION_VOTES) as f64).ln_1p())).collect()
}

/// `proof.campaign_start(counter, roots)`: the root this descent starts at,
/// `None` for the campaign's own root.
pub fn campaign_start(counter: u64, roots: &[(PosId, f64)]) -> Option<PosId> {
    let total: f64 = roots.iter().map(|r| r.1).sum();
    if roots.is_empty() || total <= 0.0 {
        return None;
    }
    let p = point(counter, "campaign:");
    let mut edge = 0.0;
    for &(root, w) in roots {
        edge += CAMPAIGN_DESCENT_SHARE * w / total;
        if p < edge {
            return Some(root);
        }
    }
    None
}

/// `proof.long_line_weight(line_plies)` (PN-9.10).
pub fn long_line_weight(line_plies: i64) -> f64 {
    let excess = line_plies - LONG_LINE_PLIES;
    if excess <= 0 {
        return 1.0;
    }
    0.5f64.powf(excess as f64 / LONG_LINE_HALF_LIFE_PLIES as f64)
}

// ------------------------------------------------------------ the walker

/// What the caller sets per call site.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct DescentConfig {
    /// `max_plies` (`DESCENT_MAX_PLIES` in production).
    pub max_plies: u32,
    /// The solve gate for the edge-view pricing of children.
    pub gate: GateConfig,
    /// `proof._saturated_nodes()` = `ingest.VALUE_SPINE_NODES` (128M), read
    /// from the queue's budget ladder, not copied (PN-9.5).
    pub saturated_nodes: u64,
}

impl Default for DescentConfig {
    fn default() -> DescentConfig {
        DescentConfig { max_plies: DESCENT_MAX_PLIES, gate: GateConfig { enabled: true }, saturated_nodes: 128_000_000 }
    }
}

/// The value walker's arms (`proof.VALUE_ARM_*`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ValueArm {
    Spine,
    Bottleneck,
    Rung,
    Cycle,
    Backup,
    Explore,
}

impl ValueArm {
    pub fn v1(self) -> &'static str {
        match self {
            ValueArm::Spine => "spine",
            ValueArm::Bottleneck => "bottleneck",
            ValueArm::Rung => "rung",
            ValueArm::Cycle => "cycle",
            ValueArm::Backup => "backup",
            ValueArm::Explore => "explore",
        }
    }
}

/// `proof.ValueTarget`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ValueTarget {
    pub pos: PosId,
    pub plies: usize,
    pub arm: ValueArm,
    pub floor_nodes: Option<u64>,
    pub line_plies: i64,
}

/// One campaign's descents over a graph.
pub struct Walker<'a, G: ProofGraph + ?Sized> {
    pub g: &'a G,
    pub table: &'a ProofTable,
    pub campaign: &'a Campaign,
    pub cfg: DescentConfig,
}

/// `_ValueChild`.
#[derive(Clone, Debug)]
struct VChild {
    index: usize,
    mv: Move,
    key: PosId,
    node: Node,
    v: ValueInfo,
    /// `best_known` (White POV), set for the ranked (open) children only.
    value: Option<i32>,
}

/// `_ValueStep`.
#[derive(Clone, Debug)]
struct VStep {
    key: PosId,
    black_to_move: bool,
    nodes: u64,
    backed_plies: u32,
    children: Vec<VChild>,
    /// Indexes into `children`, best for the mover first.
    ranked: Vec<usize>,
    /// The child the spine went on by (index into `children`).
    primary: Option<usize>,
}

/// A plan: `(key, arm, floor_nodes, plies, line)`.
#[derive(Clone, Copy, Debug)]
struct Plan {
    key: PosId,
    arm: ValueArm,
    floor: Option<u64>,
    plies: usize,
    line: i64,
}

impl<'a, G: ProofGraph + ?Sized> Walker<'a, G> {
    pub fn new(g: &'a G, table: &'a ProofTable, campaign: &'a Campaign, cfg: DescentConfig) -> Walker<'a, G> {
        Walker { g, table, campaign, cfg }
    }

    /// A child's `(pn, dn)`: its row, or the edge-view leaf numbers.
    fn child_numbers(&self, child: PosId, n: Node) -> (u64, u64) {
        if let Some(r) = self.table.get(child) {
            return r.numbers();
        }
        let annoyance = self.cfg.gate.shallow(|| self.g.fen_of(child), n.eval_cp);
        let leaf = LeafInput { black_to_move: n.black_to_move, status: n.status, eval_cp: n.eval_cp };
        numbers::leaf_numbers(leaf, self.campaign.goal, None, annoyance, false).0
    }

    /// PN-6.5 `descend(campaign, counter, max_plies, avoid, start)` →
    /// `(position or None, plies)`.
    pub fn descend(&self, counter: u64, avoid: &dyn Fn(PosId) -> bool, start: Option<PosId>) -> (Option<PosId>, u32) {
        let policy = normalized_policy(self.campaign);
        let goal = self.campaign.goal;
        let mut node = start.unwrap_or(self.campaign.root);
        let mut visited: HashSet<PosId> = HashSet::from([node]);
        let mut plies = 0u32;
        let mut edges: Vec<Edge> = Vec::new();
        let mut ranked: Vec<((u64, usize), Move, PosId)> = Vec::new();
        while plies < self.cfg.max_plies {
            let n = self.g.node(node);
            if n.status != Status::Unknown {
                return (None, plies);
            }
            edges.clear();
            self.g.out_edges(node, &mut edges);
            if edges.is_empty() {
                return (if avoid(node) { None } else { Some(node) }, plies);
            }
            // PN-6.4 `_ranked_children`.
            let or_node = goal.is_or_node(n.black_to_move);
            ranked.clear();
            for (index, e) in edges.iter().enumerate() {
                let cn = self.g.node(e.child);
                if cn.status != Status::Unknown {
                    continue;
                }
                let (pn, dn) = self.child_numbers(e.child, cn);
                ranked.push(((if or_node { pn } else { dn }, index), e.mv, e.child));
            }
            ranked.sort_by_key(|r| r.0);
            ranked.retain(|r| !visited.contains(&r.2) && !avoid(r.2));
            if ranked.is_empty() {
                return (if avoid(node) { None } else { Some(node) }, plies);
            }
            let b = bucket(counter.wrapping_add(u64::from(plies)), policy);
            // `_primary_index`.
            let selected = self.table.get(node).map(|r| r.selected).unwrap_or(Move::NONE);
            let primary = if selected.is_none() { 0 } else { ranked.iter().position(|r| r.1 == selected).unwrap_or(0) };
            let chosen = match b {
                Bucket::Primary => ranked[primary].2,
                Bucket::Backup => ranked.iter().enumerate().find(|(i, _)| *i != primary).map(|(_, r)| r.2).unwrap_or(ranked[primary].2),
                Bucket::Explore => ranked[ranked.len() - 1].2,
            };
            visited.insert(chosen);
            node = chosen;
            plies += 1;
        }
        (Some(node), plies)
    }

    // ---------------------------------------------------------------- PN-9

    /// `_value_children` + `_value_ranked` for one node.
    fn value_step(&self, key: PosId, black_to_move: bool, nodes: u64, backed_plies: u32) -> VStep {
        let mut edges = Vec::new();
        self.g.out_edges(key, &mut edges);
        let mut children: Vec<VChild> = edges
            .iter()
            .enumerate()
            .map(|(index, e)| VChild { index, mv: e.mv, key: e.child, node: self.g.node(e.child), v: self.g.value(e.child), value: None })
            .collect();
        let mut order: Vec<((u8, i64, u64, usize), usize)> = Vec::with_capacity(children.len());
        for (ci, c) in children.iter_mut().enumerate() {
            if c.node.status != Status::Unknown {
                continue;
            }
            c.value = known_eval_of(c.node.status, c.v.backed_eval, c.node.eval_cp);
            let pn = self.child_numbers(c.key, c.node).0;
            let k = match c.value {
                None => (1, 0, pn, c.index),
                Some(v) => (0, if black_to_move { i64::from(v) } else { -i64::from(v) }, pn, c.index),
            };
            order.push((k, ci));
        }
        order.sort_by_key(|o| o.0);
        VStep { key, black_to_move, nodes, backed_plies, children, ranked: order.into_iter().map(|o| o.1).collect(), primary: None }
    }

    /// `_spine_step` (with `_value_primary` as the fallback).
    fn spine_step(&self, backed_move: Move, step: &VStep) -> Option<usize> {
        if backed_move.is_none() || step.ranked.is_empty() {
            return None;
        }
        if let Some(&ci) = step.ranked.iter().find(|&&ci| step.children[ci].mv == backed_move) {
            return Some(ci);
        }
        let selected = self.table.get(step.key).map(|r| r.selected).unwrap_or(Move::NONE);
        if !selected.is_none()
            && let Some(&ci) = step.ranked.iter().find(|&&ci| step.children[ci].mv == selected)
        {
            return Some(ci);
        }
        Some(step.ranked[0])
    }

    /// PN-9.4 `_walk_spine` → `(path, child that cycles)`.
    #[allow(clippy::too_many_arguments)]
    fn walk_spine(
        &self,
        mut key: PosId,
        mut black: bool,
        mut backed_move: Move,
        mut nodes: u64,
        visited: &mut HashSet<PosId>,
        max_plies: usize,
        mut backed_plies: u32,
    ) -> (Vec<VStep>, Option<VChild>) {
        let mut path: Vec<VStep> = Vec::new();
        let mut plies = 0usize;
        loop {
            let step = self.value_step(key, black, nodes, backed_plies);
            path.push(step);
            if plies >= max_plies {
                return (path, None);
            }
            let step = path.last_mut().expect("pushed");
            let Some(ci) = self.spine_step(backed_move, step) else { return (path, None) };
            let chosen = step.children[ci].clone();
            if visited.contains(&chosen.key) {
                return (path, Some(chosen));
            }
            step.primary = Some(ci);
            visited.insert(chosen.key);
            key = chosen.key;
            black = chosen.node.black_to_move;
            backed_move = chosen.v.backed_move;
            nodes = chosen.v.nodes_invested;
            backed_plies = chosen.v.backed_plies;
            plies += 1;
        }
    }

    /// PN-9.5 `_tip_plan` over `prefix + branch` (the path of a detour is the
    /// main path's prefix followed by the detour's own walk).
    fn tip_plan(&self, prefix: &[VStep], branch: &[VStep], arm: ValueArm, plies: usize) -> Plan {
        let tip = branch.last().or(prefix.last()).expect("a path has a step");
        let line = plies as i64 + i64::from(tip.backed_plies);
        if tip.nodes < self.cfg.saturated_nodes {
            return Plan { key: tip.key, arm, floor: None, plies, line };
        }
        for step in branch.iter().rev().chain(prefix.iter().rev()) {
            if step.children.iter().any(unjudged) {
                return Plan { key: step.key, arm: ValueArm::Bottleneck, floor: None, plies, line };
            }
        }
        Plan { key: tip.key, arm: ValueArm::Rung, floor: Some(tip.nodes), plies, line }
    }

    /// PN-9.7 `_value_detours`, one at a time (the generator's state).
    fn next_detour(&self, path: &[VStep], st: &mut Detours) -> Option<(usize, Vec<VStep>)> {
        loop {
            if st.depth < 0 {
                return None;
            }
            let depth = st.depth as usize;
            if st.rivals.is_none() {
                st.rivals = Some(competitive(&path[depth]));
                st.at = 0;
            }
            let rivals = st.rivals.as_ref().expect("set");
            if st.at >= rivals.len() {
                st.depth -= 1;
                st.rivals = None;
                continue;
            }
            let child = path[depth].children[rivals[st.at]].clone();
            st.at += 1;
            if st.tried >= VALUE_BACKTRACK_MAX {
                st.depth = -1;
                return None;
            }
            if path[..=depth].iter().any(|s| s.key == child.key) {
                continue;
            }
            st.tried += 1;
            let mut visited: HashSet<PosId> = path[..=depth].iter().map(|s| s.key).collect();
            visited.insert(child.key);
            let (branch, _cycled) = self.walk_spine(
                child.key,
                child.node.black_to_move,
                child.v.backed_move,
                child.v.nodes_invested,
                &mut visited,
                (self.cfg.max_plies as usize).saturating_sub(depth + 1),
                child.v.backed_plies,
            );
            return Some((depth, branch));
        }
    }

    /// PN-9.11 `descend_value(campaign, counter, max_plies, avoid, start)`.
    ///
    /// `avoid` is the batch reservation; `live` answers `_has_live_task`
    /// (a PENDING or LEASED task on the position, any generation).
    pub fn descend_value(
        &self,
        counter: u64,
        avoid: &dyn Fn(PosId) -> bool,
        live: &dyn Fn(PosId) -> bool,
        start: Option<PosId>,
    ) -> Option<ValueTarget> {
        let node = start.unwrap_or(self.campaign.root);
        let n = self.g.node(node);
        if n.status != Status::Unknown {
            return None;
        }
        let b = bucket(counter, normalized_policy(self.campaign));
        let v = self.g.value(node);
        let mut visited = HashSet::from([node]);
        let (path, cycled) = self.walk_spine(
            node,
            n.black_to_move,
            v.backed_move,
            v.nodes_invested,
            &mut visited,
            self.cfg.max_plies as usize,
            v.backed_plies,
        );
        let mut pick = Pick { best: None, best_score: 0.0, usable: 0 };
        let usable = |key: PosId| !avoid(key) && !live(key) && self.g.node(key).status == Status::Unknown;
        // PN-9.9 `_value_plans`, consumed lazily.
        let last = path.last().expect("a walk has a step");
        if let Some(c) = &cycled {
            let p = path.len() - 1;
            let plan = Plan {
                key: last.key,
                arm: ValueArm::Cycle,
                floor: Some(c.v.nodes_invested + 1),
                plies: p,
                line: p as i64 + i64::from(last.backed_plies),
            };
            if pick.offer(plan, &usable) {
                return pick.best;
            }
        }
        let mut detours = Detours { depth: path.len() as isize - 1, rivals: None, at: 0, tried: 0 };
        match b {
            Bucket::Backup => {
                if pick.closed() {
                    return pick.best;
                }
                if let Some((depth, branch)) = self.next_detour(&path, &mut detours) {
                    let plies = depth + branch.len();
                    let plan = self.tip_plan(&path[..=depth], &branch, ValueArm::Backup, plies);
                    if pick.offer(plan, &usable) {
                        return pick.best;
                    }
                }
            }
            Bucket::Explore => {
                if pick.closed() {
                    return pick.best;
                }
                if let Some((child, p)) = best_unexplored(&path) {
                    let plan = Plan {
                        key: child.key,
                        arm: ValueArm::Explore,
                        floor: None,
                        plies: p,
                        line: p as i64 + i64::from(child.v.backed_plies),
                    };
                    if pick.offer(plan, &usable) {
                        return pick.best;
                    }
                }
            }
            Bucket::Primary => {}
        }
        if pick.closed() {
            return pick.best;
        }
        let plan = self.tip_plan(&[], &path, ValueArm::Spine, path.len() - 1);
        if pick.offer(plan, &usable) {
            return pick.best;
        }
        loop {
            if pick.closed() {
                return pick.best;
            }
            let Some((depth, branch)) = self.next_detour(&path, &mut detours) else { return pick.best };
            let plies = depth + branch.len();
            let plan = self.tip_plan(&path[..=depth], &branch, ValueArm::Spine, plies);
            if pick.offer(plan, &usable) {
                return pick.best;
            }
        }
    }
}

/// The generator state of `_value_detours`.
struct Detours {
    depth: isize,
    rivals: Option<Vec<usize>>,
    at: usize,
    tried: usize,
}

/// The selection loop's accumulator (PN-9.11).
struct Pick {
    best: Option<ValueTarget>,
    best_score: f64,
    usable: usize,
}

impl Pick {
    /// The loop's break test, made before the next plan: nothing further
    /// down the order can beat the best.
    fn closed(&self) -> bool {
        self.best.is_some() && (LONG_LINE_PLAN_DECAY.powi(self.usable as i32) <= self.best_score || self.usable > LONG_LINE_SCAN_PLANS)
    }
    /// One plan; true when the loop is over.
    fn offer(&mut self, plan: Plan, usable: &dyn Fn(PosId) -> bool) -> bool {
        if self.closed() {
            return true;
        }
        if !usable(plan.key) {
            return false;
        }
        let score = LONG_LINE_PLAN_DECAY.powi(self.usable as i32) * long_line_weight(plan.line);
        self.usable += 1;
        if self.best.is_none() || score > self.best_score {
            self.best =
                Some(ValueTarget { pos: plan.key, plies: plan.plies, arm: plan.arm, floor_nodes: plan.floor, line_plies: plan.line });
            self.best_score = score;
        }
        self.best_score >= 1.0
    }
}

/// `ingest.is_unjudged`: no status, no eval of any origin, no backing.
fn unjudged(c: &VChild) -> bool {
    c.node.status == Status::Unknown && c.node.eval_cp.is_none() && c.v.backed_eval.is_none()
}

/// `ingest.is_unexplored`: no search of its own, no search weight behind
/// its backing.
fn unexplored(c: &VChild) -> bool {
    c.node.status == Status::Unknown && c.v.nodes_invested == 0 && c.v.backed_nodes == 0
}

/// PN-9.6 `_competitive(step)`: the ranked alternatives within 150 cp of the
/// primary for the mover (indexes into `children`, in ranked order).
fn competitive(step: &VStep) -> Vec<usize> {
    let Some(primary) = step.primary.or(step.ranked.first().copied()) else { return Vec::new() };
    let Some(pv) = step.children[primary].value else { return Vec::new() };
    let pv = i64::from(pv);
    step.ranked
        .iter()
        .copied()
        .filter(|&ci| {
            if ci == primary {
                return false;
            }
            let Some(v) = step.children[ci].value else { return false };
            let v = i64::from(v);
            let loss = if step.black_to_move { v - pv } else { pv - v };
            loss < VALUE_ALTERNATIVE_MARGIN_CP
        })
        .collect()
}

/// PN-9.8 `_best_unexplored(path)` → `(child, plies)`.
fn best_unexplored(path: &[VStep]) -> Option<(VChild, usize)> {
    for (plies, step) in path.iter().enumerate().rev() {
        let mut best: Option<(&VChild, i64)> = None;
        for c in step.children.iter().filter(|c| unexplored(c)) {
            let Some(e) = c.node.eval_cp else { continue };
            let score = if step.black_to_move { -i64::from(e) } else { i64::from(e) };
            // Python `max` keeps the first maximum.
            if best.map(|(_, s)| score > s).unwrap_or(true) {
                best = Some((c, score));
            }
        }
        if let Some((c, _)) = best {
            return Some((c.clone(), plies + 1));
        }
    }
    None
}
