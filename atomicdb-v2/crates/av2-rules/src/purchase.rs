//! Step 9 of the port: what the ascent buys (BKD-11), as intents through a
//! trait.
//!
//! v1 creates or raises `AnalysisTask` rows at the end of every level of the
//! ascent (`_queue_quality_convergence`, `_queue_cycle_disambiguation`).  The
//! task model belongs to another agent; the rules keep the decision of *what*
//! to buy and in which order (rungs, interior cap, server floor, the child's
//! own investment, the room), and hand each purchase to a [`TaskSink`], which
//! answers whether it counted (a task created, or a PENDING one raised).
//! The hourly room of the arm (a rolling one-hour window on `created` in v1)
//! is the sink's: under owner decision D1 it counts in the log's terms, not
//! the wall clock.

use crate::config::RulesConfig;
use crate::effects::{Effects, Event};
use av2_model::{Edge, GraphRead, PosId, Status};

/// `ingest.BUDGET_LADDER`.
pub const BUDGET_LADDER: [u64; 5] = [8_000_000, 32_000_000, 128_000_000, 512_000_000, 2_000_000_000];
/// `ingest.REQUEST_BUDGET_LADDER[0]` = `SERVER_PICKED_MIN_NODES` = `AUTO_INTERIOR_MAX_NODES`.
pub const SERVER_PICKED_MIN_NODES: u64 = 128_000_000;
pub const AUTO_INTERIOR_MAX_NODES: u64 = 128_000_000;
/// `ingest.QUALITY_QUEUE_CAP`.
pub const QUALITY_QUEUE_CAP: u64 = 50;
/// `ingest.QUALITY_ARM`.
pub const QUALITY_ARM: &str = "quality";
/// `ingest.ARM_RATE_SETTING[QUALITY_ARM]` default (`ATOMICDB_ARM_RATE_QUALITY`).
pub const ARM_RATE_QUALITY: u64 = 120;

/// BKD-11.2 `_rung_at_least`: the first rung `>= nodes`, else the top.
pub fn rung_at_least(nodes: u64) -> u64 {
    BUDGET_LADDER.iter().copied().find(|&r| r >= nodes).unwrap_or(BUDGET_LADDER[4])
}

/// BKD-11.2 `_rung_matching`: the first rung with `nodes <= rung * 1.01`.
pub fn rung_matching(nodes: u64) -> u64 {
    // nodes <= rung * (1 + 0.01)  <=>  100 * nodes <= 101 * rung (exact; v1's
    // float product is exact for every rung).
    BUDGET_LADDER.iter().copied().find(|&r| (nodes as u128) * 100 <= (r as u128) * 101).unwrap_or(BUDGET_LADDER[4])
}

/// `ingest.caps_interior`: would the automatic policy hold this purchase at
/// the first request rung?  Interior = the child's value already stands on a
/// child of its own (`backed_move` set).
pub fn caps_interior(child_backed_move_set: bool, budget: u64, cfg: &RulesConfig) -> bool {
    budget > AUTO_INTERIOR_MAX_NODES && child_backed_move_set && !cfg.auto_interior_deep_rungs
}

/// `ingest.server_picked_budget(budget, pos=...)` without a clamp (the ascent
/// never passes one).
pub fn server_picked_budget(budget: u64, child_backed_move_set: Option<bool>, cfg: &RulesConfig) -> u64 {
    let mut b = budget;
    if let Some(interior) = child_backed_move_set
        && caps_interior(interior, b, cfg)
    {
        b = AUTO_INTERIOR_MAX_NODES;
    }
    b.max(SERVER_PICKED_MIN_NODES)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum PurchaseKind {
    /// BKD-11.1.
    QualityConvergence,
    /// BKD-11.3.
    CycleDisambiguation,
}

/// One purchase: `get_or_create(position=child, generation=child.visits)`
/// with `budget_nodes`, `multipv_for(child.visits, budget)`, source `FILL`,
/// arm `quality`; an existing PENDING task with a smaller budget is raised to
/// it (compare-and-set on the old budget).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Purchase {
    pub kind: PurchaseKind,
    pub child: PosId,
    pub generation: i32,
    pub budget: u64,
}

impl Purchase {
    pub const SOURCE: &'static str = "FILL";
    pub const ARM: &'static str = QUALITY_ARM;
}

/// The task model, as the ascent's purchases see it.
pub trait TaskSink {
    /// How many more tasks the quality arm may buy now: `min(hourly room,
    /// QUALITY_QUEUE_CAP - PENDING tasks of the arm)`, 0 when the arm is off
    /// or full (v1 `_arm_budget_room` and the pending count; the receipt
    /// event of a full arm is the sink's).  Asked once per buyer per level,
    /// only when there is something to buy.
    fn quality_room(&mut self) -> u64;
    /// Create or raise; `true` when it counts (created, or raised).
    fn buy(&mut self, p: &Purchase) -> bool;
}

/// Buys nothing (an arm switched off).
#[derive(Default, Debug, Clone, Copy)]
pub struct NoPurchases;

impl TaskSink for NoPurchases {
    fn quality_room(&mut self) -> u64 {
        0
    }
    fn buy(&mut self, _: &Purchase) -> bool {
        false
    }
}

/// A sink with v1's task semantics in memory, for tests and benchmarks: one
/// task per `(child, generation)`, created PENDING, raised when PENDING and
/// smaller; the room is `min(rate - created, cap - pending)`.
#[derive(Default, Debug, Clone)]
pub struct MemTasks {
    pub tasks: std::collections::BTreeMap<(PosId, i32), (u64, bool)>,
    pub created: u64,
    pub rate: Option<u64>,
    pub log: Vec<Purchase>,
}

impl MemTasks {
    pub fn with_rate(rate: u64) -> MemTasks {
        MemTasks { rate: Some(rate), ..Default::default() }
    }
}

impl TaskSink for MemTasks {
    fn quality_room(&mut self) -> u64 {
        let rate = self.rate.unwrap_or(ARM_RATE_QUALITY);
        if rate == 0 || self.created >= rate {
            return 0;
        }
        let pending = self.tasks.values().filter(|(_, p)| *p).count() as u64;
        (rate - self.created).min(QUALITY_QUEUE_CAP.saturating_sub(pending))
    }
    fn buy(&mut self, p: &Purchase) -> bool {
        self.log.push(*p);
        match self.tasks.get_mut(&(p.child, p.generation)) {
            None => {
                self.tasks.insert((p.child, p.generation), (p.budget, true));
                self.created += 1;
                true
            }
            Some((budget, pending)) if *pending && *budget < p.budget => {
                *budget = p.budget;
                true
            }
            Some(_) => false,
        }
    }
}

fn edge_child(g: &dyn GraphRead, parent: PosId, mv: av2_model::Move, buf: &mut Vec<Edge>) -> Option<PosId> {
    buf.clear();
    g.children(parent, buf);
    buf.iter().find(|e| e.mv == mv).map(|e| e.child)
}

/// BKD-11.1 `_queue_quality_convergence`.
pub fn queue_quality_convergence(
    g: &dyn GraphRead,
    discrepancies: &[crate::backed::Discrepancy],
    cfg: &RulesConfig,
    sink: &mut dyn TaskSink,
    effects: &mut dyn Effects,
) -> u64 {
    if discrepancies.is_empty() {
        return 0;
    }
    let room = sink.quality_room();
    if room == 0 {
        return 0;
    }
    let mut made = 0;
    let mut buf = Vec::new();
    for &(mv, parent, own_quality) in discrepancies {
        if made >= room {
            break;
        }
        let Some(child) = edge_child(g, parent, mv, &mut buf) else { continue };
        let core = g.core(child);
        if core.status() != Status::Unknown {
            continue;
        }
        let ext = g.ext(child).unwrap_or_default();
        let interior = !core.backed_move.is_none();
        let mut budget = rung_matching(own_quality);
        if caps_interior(interior, budget, cfg) {
            budget = AUTO_INTERIOR_MAX_NODES;
        }
        if ext.nodes_invested >= budget {
            continue;
        }
        budget = server_picked_budget(budget, Some(interior), cfg);
        if sink.buy(&Purchase { kind: PurchaseKind::QualityConvergence, child, generation: ext.visits, budget }) {
            made += 1;
        }
    }
    if made > 0 {
        effects.event(Event::QualityConvergence { queued: made, discrepancies: discrepancies.len() as u64 });
    }
    made
}

/// BKD-11.3 `_queue_cycle_disambiguation`.
pub fn queue_cycle_disambiguation(
    g: &dyn GraphRead,
    picks: &[crate::backed::Discrepancy],
    cfg: &RulesConfig,
    sink: &mut dyn TaskSink,
    effects: &mut dyn Effects,
) -> u64 {
    if picks.is_empty() {
        return 0;
    }
    let room = sink.quality_room();
    if room == 0 {
        return 0;
    }
    let mut made = 0;
    let mut buf = Vec::new();
    for &(mv, parent, floor_quality) in picks {
        if made >= room {
            break;
        }
        let Some(child) = edge_child(g, parent, mv, &mut buf) else { continue };
        let core = g.core(child);
        if core.status() != Status::Unknown {
            continue;
        }
        let ext = g.ext(child).unwrap_or_default();
        let budget = rung_at_least(floor_quality.max(ext.nodes_invested + 1));
        // No `pos` on purpose (v1): no interior cap, only the floor.
        let budget = server_picked_budget(budget, None, cfg);
        if sink.buy(&Purchase { kind: PurchaseKind::CycleDisambiguation, child, generation: ext.visits, budget }) {
            made += 1;
        }
    }
    if made > 0 {
        effects.event(Event::CycleDisambiguation { queued: made, chosen_cycles: picks.len() as u64 });
    }
    made
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rungs() {
        // test_quality_tolerance.py / test_idle_repair.py RUNG_OVERSHOOT
        assert_eq!(rung_matching(512_000_099), 512_000_000);
        assert_eq!(rung_at_least(512_000_099), 2_000_000_000);
        assert_eq!(rung_matching(512_000_000 * 101 / 100), 512_000_000);
        assert_eq!(rung_matching(512_000_000 * 101 / 100 + 1), 2_000_000_000);
        assert_eq!(rung_matching(0), 8_000_000);
        assert_eq!(rung_at_least(8_000_001), 32_000_000);
        assert_eq!(rung_at_least(u64::MAX), 2_000_000_000);
    }

    #[test]
    fn server_floor_and_interior_cap() {
        let cfg = RulesConfig::default();
        assert_eq!(server_picked_budget(8_000_000, None, &cfg), 128_000_000);
        assert_eq!(server_picked_budget(512_000_000, Some(true), &cfg), 128_000_000);
        assert_eq!(server_picked_budget(512_000_000, Some(false), &cfg), 512_000_000);
        let deep = RulesConfig { auto_interior_deep_rungs: true, ..RulesConfig::default() };
        assert_eq!(server_picked_budget(512_000_000, Some(true), &deep), 512_000_000);
    }
}
