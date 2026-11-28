//! The selector (PRI-1, PRI-2.1, PRI-4): what the server buys on its own.
//!
//! The selector reads the graph in memory (plan 3.1) and proposes tasks; the
//! writer mints them (`apply_mint`), so a proposal computed on a slightly
//! older graph can never break a queue invariant: slots are checked again
//! when the proposal is applied, and only the result goes into the log.
//!
//! Two engines, as in v1 (PRI-4.1):
//! - **regret** (`RegretSelector`): the global regret Dijkstra from the root
//!   (PRI-2.1, exact) and the priority formula (PRI-1.5), computed in memory
//!   (owner decision D5: priority is not stored).  Production does not run
//!   it (`--no-priorities`, PRI-6.2); kept for the rollback path (OQ-22).
//! - **pn** (`PnSelector`): the proof/value minting loop (PRI-4.4..PRI-4.7)
//!   over a [`Descent`] provided by the proof-number crate (`av2-proof`,
//!   rules-pn): df-pn descent, value walker, OR clamp and campaign roots.

use crate::budget::*;
use crate::consts::*;
use crate::env::QueueEnv;
use crate::input::MintTask;
use crate::model::*;
use crate::state::QueueState;
use av2_model::{GraphRead, PosId, Status};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, BinaryHeap};

/// What a selector proposes.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct MintPlan {
    pub tasks: Vec<MintTask>,
    /// Positions to tombstone (PRI-1.7: every parent closed).
    pub tombstones: Vec<u64>,
    /// Arm-rate receipts to write (Q-8.4), `(arm, rate, made)`.
    #[serde(default)]
    pub receipts: Vec<(String, u32, usize)>,
}

/// `next_tasks(n)` as the lease and the pool call it (PRI-4.1).
pub trait Refill {
    fn next_tasks(&mut self, q: &QueueState, env: &dyn QueueEnv, n: usize, now: Ms) -> MintPlan;
}

/// No selector: the lease's refill pass mints nothing.
pub struct NoRefill;
impl Refill for NoRefill {
    fn next_tasks(&mut self, _q: &QueueState, _env: &dyn QueueEnv, _n: usize, _now: Ms) -> MintPlan {
        MintPlan::default()
    }
}

/// `_still_reachable(pos)` (PRI-1.8).
pub fn still_reachable(env: &dyn QueueEnv, pos: u64) -> bool {
    let parents = env.parents(pos);
    parents.is_empty() || parents.iter().any(|&p| env.status(p) == Status::Unknown)
}

impl QueueState {
    /// Arm hourly room (Q-8.3): `(room, Some((rate, made)) if the ceiling is reached)`.
    /// `None` room means the arm has no ceiling.
    pub fn arm_budget_room(&self, arm: &str, now: Ms) -> (Option<usize>, Option<(u32, usize)>) {
        let Some(rate) = self.config.arm_rate(arm) else { return (None, None) };
        if rate == 0 {
            return (Some(0), None);
        }
        let made = self.arm_log.get(arm).map(|d| d.iter().filter(|&&c| c >= now - ARM_RATE_WINDOW).count()).unwrap_or(0);
        if made >= rate as usize {
            return (Some(0), Some((rate, made)));
        }
        (Some(rate as usize - made), None)
    }

    /// Q-8.4: one `ARM_RATE_BUDGET` receipt per arm and ten minutes.
    pub(crate) fn arm_rate_receipt(&mut self, arm: &str, rate: u32, made: usize, now: Ms) {
        let key = format!("arm_rate:{arm}");
        if self.receipts.get(&key).map(|&at| at >= now - ARM_RATE_RECEIPT_WINDOW).unwrap_or(false) {
            return;
        }
        self.receipts.insert(key.clone(), now);
        self.tx.receipts.insert(key);
        self.event("ARM_RATE_BUDGET", serde_json::json!({"arm": arm, "rate": rate, "made": made, "window_minutes": 60}));
    }

    /// `room` with the receipt written (the writer-side `_arm_budget_room`).
    pub(crate) fn take_arm_room(&mut self, arm: &str, now: Ms) -> Option<usize> {
        let (room, receipt) = self.arm_budget_room(arm, now);
        if let Some((rate, made)) = receipt {
            self.arm_rate_receipt(arm, rate, made, now);
        }
        room
    }

    /// Pending tasks of an arm (stock caps, Q-8.1).
    pub fn pending_of_arm(&self, arm: &str) -> usize {
        self.idx.pending_arm.get(arm).map(|s| s.len()).unwrap_or(0)
    }

    /// PRI-4.11: mint a proposal on `(pos, visits)` unless the slot is
    /// taken.  Returns `(created, PENDING ids)`.
    pub(crate) fn apply_mint(&mut self, plan: &MintPlan, now: Ms, env: &dyn QueueEnv) -> (usize, Vec<u64>) {
        for &p in &plan.tombstones {
            self.set_dead(p, true);
        }
        for (arm, rate, made) in &plan.receipts {
            self.arm_rate_receipt(arm, *rate, *made, now);
        }
        let mut created = 0;
        let mut pending = Vec::new();
        for m in &plan.tasks {
            let visits = env.visits(m.pos);
            match self.slot(m.pos, visits).map(|t| (t.id, t.state)) {
                Some((id, TState::Pending)) => pending.push(id),
                Some(_) => {}
                // A proposal that only pointed at an existing PENDING row
                // (budget 0) never mints.
                None if m.budget == 0 => {}
                None => {
                    let id = self.alloc_task_id();
                    let mut t = Task::new(id, m.pos, visits, m.budget, now);
                    t.multipv = m.multipv;
                    t.source = m.source;
                    t.arm = m.arm.clone();
                    self.insert_task(t);
                    if !m.arm.is_empty() {
                        self.note_arm_created(&m.arm, now);
                    }
                    created += 1;
                    pending.push(id);
                }
            }
        }
        (created, pending)
    }

    /// `top_up_analysis_pool(target)` (PRI-4.3): AUTO PENDING rows that are
    /// not idle repairs; `next_tasks(target - pending)` when short.
    pub(crate) fn top_up_pool(&mut self, env: &dyn QueueEnv, refill: &mut dyn Refill, now: Ms, target: Option<usize>) -> usize {
        let target = target.unwrap_or(self.config.pool_target);
        let pending = self.pool_pending();
        if pending >= target {
            return 0;
        }
        let plan = refill.next_tasks(self, env, target - pending, now);
        self.apply_mint(&plan, now, env).1.len()
    }

    pub fn pool_pending(&self) -> usize {
        self.idx.pending.iter().filter(|id| {
            let t = &self.tasks[id];
            t.source == Source::Auto && t.arm != ARM_REPAIR
        }).count()
    }

    /// `_queue_value_cascade(pos)` (PRI-4.9).  `open_before` is the status of
    /// the landed position before the cascade (OQ-29: kept as in v1).
    pub(crate) fn queue_value_cascade(&mut self, pos: u64, open_before: bool, now: Ms, env: &dyn QueueEnv) -> usize {
        if !self.config.descent_value || !open_before {
            return 0;
        }
        let Some(hourly) = self.take_arm_room(ARM_CASCADE, now) else { return 0 };
        if hourly == 0 {
            return 0;
        }
        let pending = self.pending_of_arm(ARM_CASCADE);
        let room = VALUE_CASCADE_CAP.min(hourly).min(VALUE_CASCADE_CAP.saturating_sub(pending));
        if room == 0 {
            return 0;
        }
        let budget = server_picked_budget(VALUE_CASCADE_NODES, None, None, self.config.interior_deep_rungs);
        let mut made = 0;
        for (_, child) in crate::request::unjudged_children(env, pos) {
            if made == room {
                break;
            }
            let visits = env.visits(child);
            if self.slot(child, visits).is_some() {
                continue;
            }
            let id = self.alloc_task_id();
            let mut t = Task::new(id, child, visits, budget, now);
            t.multipv = multipv_for(visits, Some(budget), false, None);
            t.arm = ARM_CASCADE.to_string();
            self.insert_task(t);
            self.note_arm_created(ARM_CASCADE, now);
            made += 1;
        }
        if made > 0 {
            self.event("VALUE_CASCADE", serde_json::json!({"pos": pos, "queued": made}));
        }
        made
    }
}

// ------------------------------------------------------------ priority (PRI-1)

/// `_runits(regret)` (PRI-1.3): `None` = no path from the root.
pub fn runits(regret: Option<f64>) -> f64 {
    match regret {
        None => DISCONNECTED_REGRET,
        Some(r) => r.min(REGRET_CAP) / 100.0,
    }
}

/// `_campaign_bonus` (PRI-1.4).
pub fn campaign_bonus(votes: Option<i64>) -> f64 {
    match votes {
        None => 0.0,
        Some(v) => CAMPAIGN_BONUS * (v.max(0) as f64).ln_1p(),
    }
}

/// `priority_of` (PRI-1.5), evaluated left to right as v1.
pub fn priority_of(known: Option<i32>, expanded: bool, visits: i32, runits: f64, bonus: f64) -> f64 {
    let e = known.map(|k| k.unsigned_abs() as i64).unwrap_or(0);
    let mut p = (e.min(EVAL_PROXIMITY_CAP) as f64) / 100.0;
    p += if e >= MATE_BAND as i64 { MATE_BAND_BONUS } else { 0.0 };
    p += if expanded { 0.0 } else { UNEXPANDED_BONUS };
    p -= REGRET_WEIGHT * runits;
    p -= VISIT_WEIGHT * visits as f64;
    p += bonus;
    p
}

/// PRI-1.2 on a core record.
fn known_of(core: &av2_model::Core) -> Option<i32> {
    match core.status() {
        Status::WhiteWin => Some(10000),
        Status::BlackWin => Some(-10000),
        Status::Draw => Some(0),
        Status::Unknown => core.backed().or(core.eval()).map(i32::from),
    }
}

/// Regret saturates here: every regret above `REGRET_CAP` prices the same.
const SAT: u32 = u32::MAX - 1;
pub const UNREACHED: u32 = u32::MAX;

/// `_regret_from_root` (PRI-2.1): regret in cp of every position, dense by
/// id, `UNREACHED` when no path from the root exists.  Exact v1 semantics:
/// closed nodes do not relax their children, children without a value
/// inherit the parent's regret, transpositions keep the minimum.  Values are
/// saturated at `u32::MAX - 1` (far above `REGRET_CAP`, so prices are equal).
pub fn regret_from_root<G: GraphRead + ?Sized>(graph: &G, root: PosId) -> Vec<u32> {
    let n = graph.len() as usize;
    let mut regret = vec![UNREACHED; n];
    if root.index() >= n {
        return regret;
    }
    regret[root.index()] = 0;
    let mut heap = BinaryHeap::new();
    heap.push(std::cmp::Reverse((0u32, root.0)));
    let mut kids = Vec::new();
    let mut vals: Vec<Option<i32>> = Vec::new();
    while let Some(std::cmp::Reverse((r, k))) = heap.pop() {
        if r > regret[k as usize] {
            continue;
        }
        let core = graph.core(PosId(k));
        if core.status() != Status::Unknown {
            continue;
        }
        kids.clear();
        graph.children(PosId(k), &mut kids);
        if kids.is_empty() {
            continue;
        }
        vals.clear();
        vals.extend(kids.iter().map(|e| known_of(&graph.core(e.child))));
        let white = !core.black_to_move();
        let best = if white { vals.iter().flatten().max().copied() } else { vals.iter().flatten().min().copied() };
        for (e, v) in kids.iter().zip(vals.iter()) {
            let gap = match (best, v) {
                (Some(b), Some(v)) => {
                    if white { (b - v) as u32 } else { (v - b) as u32 }
                }
                _ => 0,
            };
            let nr = r.saturating_add(gap).min(SAT);
            let c = e.child.index();
            if c < n && nr < regret[c] {
                regret[c] = nr;
                heap.push(std::cmp::Reverse((nr, e.child.0)));
            }
        }
    }
    regret
}

/// The regret engine (PRI-1.5 + PRI-2.1 + PRI-4.2), priorities in memory.
#[derive(Clone, Debug, Default)]
pub struct RegretSelector {
    /// Open positions by priority (descending), ties by id (D3).
    pub ranked: Vec<(f64, u64)>,
}

impl RegretSelector {
    /// One priority pass (`refresh_priorities_v1` without the writes):
    /// every open, non-tombstoned position.  `votes` are the ACTIVE
    /// campaigns' votes; `dead` the tombstones.
    pub fn compute<G: GraphRead + ?Sized>(graph: &G, root: PosId, votes: &BTreeMap<u32, i64>, dead: &BTreeSet<u64>) -> RegretSelector {
        let regret = regret_from_root(graph, root);
        let mut ranked = Vec::new();
        let mut tomb = Vec::new();
        for id in 0..graph.len() {
            let core = graph.core(PosId(id));
            if core.status() != Status::Unknown {
                continue;
            }
            if dead.contains(&id) {
                tomb.push((DEAD, id));
                continue;
            }
            let ext = graph.ext(PosId(id)).unwrap_or_default();
            let r = regret[id as usize];
            let ru = runits(if r == UNREACHED { None } else { Some(r as f64) });
            let bonus = if ext.campaign == 0 { 0.0 } else { campaign_bonus(votes.get(&ext.campaign).copied()) };
            ranked.push((priority_of(known_of(&core), core.expanded(), ext.visits, ru, bonus), id));
        }
        ranked.sort_by(|a, b| b.0.total_cmp(&a.0).then(a.1.cmp(&b.1)));
        ranked.extend(tomb);
        RegretSelector { ranked }
    }

    /// The priority of a position (`-1e9` for a tombstone, `0.0` if unscored).
    pub fn priority_map(&self) -> std::collections::HashMap<u64, f64> {
        self.ranked.iter().map(|(p, id)| (*id, *p)).collect()
    }
}

impl Refill for RegretSelector {
    /// PRI-4.2: the best `4n` open rows; tombstone the unreachable ones.
    fn next_tasks(&mut self, q: &QueueState, env: &dyn QueueEnv, n: usize, _now: Ms) -> MintPlan {
        let mut plan = MintPlan::default();
        let mut got = 0;
        let window: Vec<u64> =
            self.ranked.iter().map(|x| x.1).filter(|&p| env.status(p) == Status::Unknown).take(4 * n).collect();
        for pos in window {
            if got >= n {
                break;
            }
            if !still_reachable(env, pos) {
                plan.tombstones.push(pos);
                continue;
            }
            let p = env.pos(pos);
            match q.slot(pos, p.visits).map(|t| t.state) {
                Some(TState::Pending) => {
                    got += 1;
                    plan.tasks.push(MintTask { pos, budget: 0, multipv: 0, source: Source::Auto, arm: String::new() });
                    continue;
                }
                Some(_) => continue,
                None => {}
            }
            let clamp = short_mate_clamp(&p);
            let budget = server_picked_budget(budget_for(&p), clamp, Some(&p), q.config.interior_deep_rungs);
            plan.tasks.push(MintTask {
                pos,
                budget,
                multipv: multipv_for(p.visits, Some(budget), false, clamp),
                source: Source::Auto,
                arm: String::new(),
            });
            got += 1;
        }
        plan
    }
}

// ------------------------------------------------------------ pn selector (PRI-4.4..4.7)

/// A value-walker target (`proof.ValueTarget`).
#[derive(Clone, Debug, PartialEq)]
pub struct ValueTarget {
    pub pos: u64,
    pub arm: ValueArm,
    pub floor_nodes: Option<u64>,
}

/// What the proof-number crate provides to the minting loop.  Implemented
/// by `av2-proof` (rules-pn); the queue only calls it.
pub trait Descent {
    /// `proof.active_campaigns()`: ProofCampaign ids, ordered by id.
    fn active_campaigns(&self) -> Vec<u64>;
    /// `proof.campaign_roots()`: community campaign roots `(pos, weight)`
    /// with `weight = ln(1 + max(1, votes))`, ACTIVE with an open root,
    /// ordered by campaign id; empty when campaign descent is off.
    fn campaign_roots(&self) -> Vec<(u64, f64)>;
    /// `proof.descend(campaign, counter, avoid, start)` (PN-6).
    fn descend(&self, campaign: u64, counter: u64, avoid: &BTreeSet<u64>, start: Option<u64>) -> Option<u64>;
    /// `proof.descend_value(...)` (PN-9).
    fn descend_value(&self, campaign: u64, counter: u64, avoid: &BTreeSet<u64>, start: Option<u64>) -> Option<ValueTarget>;
    /// `proof.proved_or_clamp(pos, campaigns)` (PN-7).
    fn proved_or_clamp(&self, pos: u64, campaigns: &[u64]) -> Option<Clamp>;
}

/// `proof.CAMPAIGN_DESCENT_SHARE`.
pub const CAMPAIGN_DESCENT_SHARE: f64 = 0.35;

/// `proof._point(counter, salt)`.
pub fn point(counter: u64, salt: &str) -> f64 {
    use sha2::{Digest, Sha256};
    let d = Sha256::digest(format!("{salt}{counter}").as_bytes());
    let x = u64::from_be_bytes(d[..8].try_into().unwrap());
    x as f64 / (1u128 << 64) as f64
}

/// `proof.campaign_start(counter, roots)`.
pub fn campaign_start(counter: u64, roots: &[(u64, f64)]) -> Option<u64> {
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

/// The production selector (`ATOMICDB_SELECTOR=pn`).
pub struct PnSelector<'d, D: Descent + ?Sized> {
    pub descent: &'d D,
}

impl<D: Descent + ?Sized> Refill for PnSelector<'_, D> {
    fn next_tasks(&mut self, q: &QueueState, env: &dyn QueueEnv, n: usize, now: Ms) -> MintPlan {
        let mut plan = MintPlan::default();
        let campaigns = self.descent.active_campaigns();
        if campaigns.is_empty() {
            return plan;
        }
        let by_value = q.config.descent_value && {
            let (room, receipt) = q.arm_budget_room(ARM_DESCEND, now);
            if let Some((rate, made)) = receipt {
                plan.receipts.push((ARM_DESCEND.to_string(), rate, made));
            }
            room.map(|r| r > 0).unwrap_or(true)
        };
        let roots = if q.config.campaign_descent { self.descent.campaign_roots() } else { Vec::new() };
        let base = q.max_task_id();
        let mut seen: BTreeSet<u64> = BTreeSet::new();
        let mut got = 0;
        let mut attempts = 0usize;
        let attempt_budget = 4 * n.max(1);
        while got < n && attempts < attempt_budget {
            let campaign = campaigns[attempts % campaigns.len()];
            let counter = base + attempts as u64;
            let start = campaign_start(counter, &roots);
            let (pos, target) = if by_value {
                match self.descent.descend_value(campaign, counter, &seen, start) {
                    Some(t) => (Some(t.pos), Some(t)),
                    None => (None, None),
                }
            } else {
                (self.descent.descend(campaign, counter, &seen, start), None)
            };
            attempts += 1;
            let Some(pos) = pos else { continue };
            if !seen.insert(pos) {
                continue;
            }
            if !still_reachable(env, pos) {
                plan.tombstones.push(pos);
                continue;
            }
            let p = env.pos(pos);
            match q.slot(pos, p.visits).map(|t| t.state) {
                Some(TState::Pending) => {
                    got += 1;
                    plan.tasks.push(MintTask { pos, budget: 0, multipv: 0, source: Source::Auto, arm: String::new() });
                    continue;
                }
                Some(_) => continue,
                None => {}
            }
            let mut clamp = short_mate_clamp(&p);
            let mut budget = match (&target, clamp) {
                (Some(t), None) => value_budget(&t.arm, t.floor_nodes),
                _ => budget_for(&p),
            };
            if q.config.or_clamp {
                if let Some(oc) = self.descent.proved_or_clamp(pos, &campaigns) {
                    budget = budget.min(oc.0);
                    clamp = Some(oc);
                }
            }
            let budget = server_picked_budget(budget, clamp, Some(&p), q.config.interior_deep_rungs);
            let arm = if start.is_some() {
                ARM_CAMPAIGN
            } else if by_value {
                ARM_DESCEND
            } else {
                ARM_NONE
            };
            plan.tasks.push(MintTask {
                pos,
                budget,
                multipv: multipv_for(p.visits, Some(budget), false, clamp),
                source: Source::Auto,
                arm: arm.to_string(),
            });
            got += 1;
        }
        plan
    }
}
