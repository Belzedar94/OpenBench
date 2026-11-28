//! Background arms (Q-8): engine debt, coverage completion, dn repair and
//! fragile mate claims.
//!
//! Each arm has two halves.  The **scan** reads the graph (a reader thread,
//! as v1's selector service does) and lists candidates in order; the
//! **enqueue** runs in the writer, applies the stock cap and the hourly room
//! to the queue as it is now, and mints.  The candidates travel in the input
//! (`Input::Arm`), so the log replays the same decision.

use crate::budget::*;
use crate::consts::*;
use crate::env::QueueEnv;
use crate::model::*;
use crate::request::unjudged_children;
use crate::state::QueueState;
use av2_model::{Closure, GraphRead, PosId, Proof, Status};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

/// A coverage candidate (Q-8.8): a node and the children its gap misses.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct CoverageGap {
    pub pos: u64,
    pub missing: Vec<u64>,
}

/// The candidates of one arm step (the scan half), as logged.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "arm", rename_all = "snake_case")]
pub enum ArmStep {
    /// Q-8.6/Q-8.7: `(pos, goal)` in candidate order.
    Debt { candidates: Vec<(u64, String)>, limit: Option<usize> },
    /// Q-8.9.
    Coverage { gaps: Vec<CoverageGap>, scanned: usize },
    /// Q-8.11/Q-8.12: per campaign, the frontier AND rows `(pos, pn, dn)`.
    DnRepair { frontier: Vec<Vec<(u64, u64, u64)>> },
    /// Q-8.13/Q-8.14: `(pos, goal)` in scan order.
    Fragile { candidates: Vec<(u64, String)> },
}

impl QueueState {
    /// Run the enqueue half of an arm step.  Returns how many rows it made.
    pub(crate) fn arm_step(&mut self, step: &ArmStep, now: Ms, env: &dyn QueueEnv) -> usize {
        match step {
            ArmStep::Debt { candidates, limit } => self.enqueue_engine_debt(candidates, *limit, now),
            ArmStep::Coverage { gaps, scanned } => self.enqueue_coverage(gaps, *scanned, now, env),
            ArmStep::DnRepair { frontier } => self.enqueue_dn_repair(frontier, now, env),
            ArmStep::Fragile { candidates } => self.enqueue_fragile(candidates, now),
        }
    }

    fn solve_positions(&self, pred: impl Fn(&SolveTask) -> bool) -> BTreeSet<u64> {
        self.solves.values().filter(|s| pred(s)).map(|s| s.pos).collect()
    }

    /// Q-8.7 `enqueue_engine_debt(cap, limit)`.
    fn enqueue_engine_debt(&mut self, candidates: &[(u64, String)], limit: Option<usize>, now: Ms) -> usize {
        let cap = self.config.debt_cap;
        let pending = self.solves.values().filter(|s| s.state == SolveState::Pending && s.arm == ARM_DEBT).count();
        let mut room = cap.saturating_sub(pending);
        if let Some(l) = limit {
            room = room.min(l);
        }
        if room == 0 {
            return 0;
        }
        // A FAILED debt task does not exclude its position (OQ-21, as in v1).
        let taken = self.solve_positions(|s| s.arm == ARM_DEBT && s.state != SolveState::Failed);
        let mut made = 0;
        for (pos, goal) in candidates.iter().filter(|(p, _)| !taken.contains(p)).take(room * 2) {
            if made >= room {
                break;
            }
            self.insert_solve_task(*pos, goal, "F0", DEBT_STAGE_NODES, ARM_DEBT, now);
            made += 1;
        }
        if made > 0 {
            self.event("DEBT_ENQUEUED", serde_json::json!({"created": made, "pending_before": pending, "cap": cap}));
        }
        made
    }

    /// A FILL arm buys a child: create, or promote a PENDING AUTO row of the
    /// slot to this arm (Q-8.9, Q-8.12).  Returns whether it counts.
    fn fill_child(&mut self, child: u64, budget: u64, multipv: u32, arm: &str, now: Ms, env: &dyn QueueEnv) -> bool {
        let visits = env.visits(child);
        match self.slot(child, visits).map(|t| (t.id, t.state, t.source)) {
            None => {
                let id = self.alloc_task_id();
                let mut t = Task::new(id, child, visits, budget, now);
                t.multipv = multipv;
                t.source = Source::Fill;
                t.arm = arm.to_string();
                self.insert_task(t);
                self.note_arm_created(arm, now);
                true
            }
            Some((id, TState::Pending, Source::Auto)) => {
                let arm = arm.to_string();
                self.update_task(id, |t| {
                    t.source = Source::Fill;
                    t.arm = arm;
                });
                true
            }
            Some(_) => false,
        }
    }

    /// Q-8.9 `enqueue_coverage_completion`.
    fn enqueue_coverage(&mut self, gaps: &[CoverageGap], scanned: usize, now: Ms, env: &dyn QueueEnv) -> usize {
        let cap = self.config.coverage_cap;
        let pending = self.pending_of_arm(ARM_COVERAGE);
        let room = cap.saturating_sub(pending);
        if room == 0 {
            return 0;
        }
        let mut made = 0;
        'outer: for g in gaps {
            if made >= room {
                break;
            }
            for &child in &g.missing {
                if made >= room {
                    break 'outer;
                }
                let cp = env.pos(child);
                if !cp.exists || cp.status != Status::Unknown {
                    continue;
                }
                let budget = server_picked_budget(COVERAGE_SEED_NODES.max(budget_for(&cp)), None, Some(&cp), self.config.interior_deep_rungs);
                if self.fill_child(child, budget, DEPTH_MULTIPV, ARM_COVERAGE, now, env) {
                    made += 1;
                }
            }
        }
        if made > 0 {
            self.event(
                "COVERAGE_ENQUEUED",
                serde_json::json!({"created": made, "pending_before": pending, "cap": cap, "scanned": scanned}),
            );
        }
        made
    }

    /// Q-8.11/Q-8.12 `enqueue_dn_repair`.
    fn enqueue_dn_repair(&mut self, frontier: &[Vec<(u64, u64, u64)>], now: Ms, env: &dyn QueueEnv) -> usize {
        let cap = self.config.dn_repair_cap;
        let pending = self.pending_of_arm(ARM_DN);
        let room = cap.saturating_sub(pending).min(DN_REPAIR_MAX_PER_CYCLE);
        if room == 0 || frontier.is_empty() {
            return 0;
        }
        let (mut made, mut scanned, mut nodes) = (0, 0, 0);
        'campaigns: for rows in frontier {
            for &(pos, _pn, dn) in rows {
                if made >= room || scanned >= DN_REPAIR_MAX_NODES {
                    break 'campaigns;
                }
                if dn > DN_REPAIR_FLOOR {
                    continue;
                }
                scanned += 1;
                let p = env.pos(pos);
                if !p.exists || p.status != Status::Unknown || !p.expanded {
                    continue;
                }
                let mut any = false;
                for (_, child) in unjudged_children(env, pos).into_iter().take(DN_REPAIR_REPLIES) {
                    if made >= room {
                        break;
                    }
                    let cp = env.pos(child);
                    let budget = server_picked_budget(DN_REPAIR_SEED_NODES.max(budget_for(&cp)), None, Some(&cp), self.config.interior_deep_rungs);
                    let mpv = multipv_for(cp.visits, Some(budget), false, None);
                    if self.fill_child(child, budget, mpv, ARM_DN, now, env) {
                        made += 1;
                        any = true;
                    }
                }
                if any {
                    nodes += 1;
                }
            }
        }
        if made > 0 {
            self.event(
                "DN_REPAIR",
                serde_json::json!({"queued": made, "nodes": nodes, "examined": scanned, "floor": DN_REPAIR_FLOOR,
                    "pending_before": pending, "cap": cap, "per_cycle": DN_REPAIR_MAX_PER_CYCLE}),
            );
        }
        made
    }

    /// Q-8.14 `enqueue_fragile_mate_solves`.
    fn enqueue_fragile(&mut self, candidates: &[(u64, String)], now: Ms) -> usize {
        let cap = self.config.fragile_cap;
        let pending = self.solves.values().filter(|s| s.state == SolveState::Pending && s.arm == ARM_FRAGILE).count();
        let room = cap.saturating_sub(pending);
        if room == 0 || candidates.is_empty() {
            return 0;
        }
        let mut taken = self.solve_positions(|s| {
            (s.arm == ARM_FRAGILE && s.state != SolveState::Failed) || matches!(s.state, SolveState::Pending | SolveState::Leased)
        });
        let (mut made, mut white, mut black) = (0, 0, 0);
        for (pos, goal) in candidates {
            if made >= room {
                break;
            }
            if !taken.insert(*pos) {
                continue;
            }
            self.insert_solve_task(*pos, goal, "F0", FRAGILE_STAGE_NODES, ARM_FRAGILE, now);
            made += 1;
            if goal == "WHITE_WIN" {
                white += 1;
            } else {
                black += 1;
            }
        }
        if made > 0 {
            self.event(
                "FRAGILE_ENQUEUED",
                serde_json::json!({"created": made, "pending_before": pending, "cap": cap,
                    "candidates": candidates.len(), "white": white, "black": black}),
            );
        }
        made
    }
}

// ------------------------------------------------------------ scans (reader side)

/// Q-8.6: engine-debt candidates, `(pos, goal = status)`.  v1 orders by
/// key; v2 orders by id (D3; the key is not a column of the graph).
pub fn scan_engine_debt<G: GraphRead + ?Sized>(graph: &G) -> Vec<(u64, String)> {
    let mut out = Vec::new();
    for id in 0..graph.len() {
        let c = graph.core(PosId(id));
        if c.closure() != Closure::MatePv || !matches!(c.status(), Status::WhiteWin | Status::BlackWin) {
            continue;
        }
        if !matches!(c.proof(), Proof::Engine | Proof::None) {
            continue;
        }
        let has_line = graph.ext(PosId(id)).map(|e| !e.won_line.is_none()).unwrap_or(false);
        if has_line {
            out.push((id, c.status().v1().to_string()));
        }
    }
    out
}

/// Q-8.8 `_coverage_gap(fen, children, missing_max)` on child rows
/// `(child, status, known)`, `known` White's point of view.
pub fn coverage_gap(white_to_move: bool, children: &[(u64, Status, Option<i32>)], missing_max: usize) -> Option<Vec<u64>> {
    let (win, loss) = if white_to_move { (Status::WhiteWin, Status::BlackWin) } else { (Status::BlackWin, Status::WhiteWin) };
    let mut informed = 0;
    let mut missing = Vec::new();
    for &(child, status, known) in children {
        if status == win || status == Status::Draw {
            return None;
        }
        if status == loss {
            informed += 1;
            continue;
        }
        match known {
            None => missing.push(child),
            Some(k) => {
                let mover = if white_to_move { k } else { -k };
                if mover > -COVERAGE_DECISIVE_CP {
                    return None;
                }
                informed += 1;
            }
        }
    }
    if missing.is_empty() || missing.len() > missing_max || informed == 0 { None } else { Some(missing) }
}

/// Q-8.9 scan: up to `scan` open expanded positions, most recently changed
/// first (`-updated` in v1; the record `version` here, ties by id, D3).
pub fn scan_coverage<G: GraphRead + ?Sized>(graph: &G, scan: usize) -> (Vec<CoverageGap>, usize) {
    let mut cands: Vec<(u64, u64)> = Vec::new();
    for id in 0..graph.len() {
        let c = graph.core(PosId(id));
        if c.status() == Status::Unknown && c.expanded() {
            cands.push((c.version, id));
        }
    }
    cands.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    cands.truncate(scan);
    let mut gaps = Vec::new();
    let mut edges = Vec::new();
    for &(_, id) in &cands {
        edges.clear();
        graph.children(PosId(id), &mut edges);
        let rows: Vec<(u64, Status, Option<i32>)> = edges
            .iter()
            .map(|e| {
                let cc = graph.core(e.child);
                (e.child.0, cc.status(), cc.backed().or(cc.eval()).map(i32::from))
            })
            .collect();
        let white = !graph.core(PosId(id)).black_to_move();
        if let Some(missing) = coverage_gap(white, &rows, COVERAGE_MISSING_MAX) {
            gaps.push(CoverageGap { pos: id, missing });
        }
    }
    (gaps, cands.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn coverage_gap_shapes() {
        // test_coverage_completion.py::CoverageGapTests::test_the_wolfram_shape_asks_for_exactly_the_missing_move
        let rows = vec![(1, Status::Unknown, Some(-900)), (2, Status::Unknown, None), (3, Status::BlackWin, None)];
        assert_eq!(coverage_gap(true, &rows, 3), Some(vec![2]));
        // ::test_a_balanced_node_is_left_alone / test_one_survivable_reply_disqualifies_the_node
        let rows = vec![(1, Status::Unknown, Some(-100)), (2, Status::Unknown, None)];
        assert_eq!(coverage_gap(true, &rows, 3), None);
        // ::test_a_winning_reply_for_the_mover_disqualifies_it
        let rows = vec![(1, Status::WhiteWin, None), (2, Status::Unknown, None)];
        assert_eq!(coverage_gap(true, &rows, 3), None);
        // ::test_too_many_blanks_is_ordinary_exploration_not_this
        let rows = vec![(1, Status::BlackWin, None), (2, Status::Unknown, None), (3, Status::Unknown, None), (4, Status::Unknown, None), (5, Status::Unknown, None)];
        assert_eq!(coverage_gap(true, &rows, 3), None);
        // -800 exactly qualifies (counted as informed).
        let rows = vec![(1, Status::Unknown, Some(-800)), (2, Status::Unknown, None)];
        assert_eq!(coverage_gap(true, &rows, 3), Some(vec![2]));
        // A draw disqualifies.
        let rows = vec![(1, Status::Draw, None), (2, Status::Unknown, None)];
        assert_eq!(coverage_gap(true, &rows, 3), None);
        // Black to move: values flip.
        let rows = vec![(1, Status::Unknown, Some(900)), (2, Status::Unknown, None)];
        assert_eq!(coverage_gap(false, &rows, 3), Some(vec![2]));
    }
}
