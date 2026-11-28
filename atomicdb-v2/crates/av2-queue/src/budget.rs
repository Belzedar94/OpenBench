//! Budget sizing (PRI-5, also EXP-4): ladders, the visit ladder, the
//! short-mate clamp, MultiPV, the server-picked floor and the interior cap.
//! Pure functions of a position's columns.

use crate::consts::*;
use crate::env::PosInfo;

/// `(budget, multipv)` of a clamp (mate clamp or OR clamp).
pub type Clamp = (u64, u32);

/// `_mate_moves_to_plies(n, winner_is_white, stm_white)` (PRI-5.4).
pub fn mate_moves_to_plies(n: i64, winner_white: bool, stm_white: bool) -> Option<u32> {
    if n <= 0 {
        return Some(0);
    }
    let n = n as u64;
    Some(if winner_white == stm_white { (2 * n - 1) as u32 } else { (2 * n) as u32 })
}

/// `claimed_mate_plies(pos)` (PRI-5.4).
pub fn claimed_mate_plies(p: &PosInfo) -> Option<u32> {
    if let Some(m) = p.mate_in {
        return Some(m.max(0) as u32);
    }
    if let Some(mate) = p.first_line_mate {
        if mate != 0 {
            // Exactly as v1: `_mate_moves_to_plies(abs(mate), mate > 0, stm_white)`
            // (the sign is read as "White wins").
            return mate_moves_to_plies(mate.abs(), mate > 0, p.white_to_move);
        }
        return None;
    }
    if let Some(cp) = p.eval_cp {
        let a = cp.abs();
        if a > TB_CLAMP_CEILING && a < 10000 {
            return mate_moves_to_plies((10000 - a) as i64, cp > 0, p.white_to_move);
        }
    }
    None
}

/// `_short_mate_clamp(pos)` (PRI-5.4).
pub fn short_mate_clamp(p: &PosInfo) -> Option<Clamp> {
    let plies = claimed_mate_plies(p)?;
    if plies > MATE_CLAMP_PLIES {
        return None;
    }
    let budget = (MATE_CLAMP_NODES_PER_PLY * plies as u64).clamp(MATE_CLAMP_MIN_NODES, MATE_CLAMP_MAX_NODES);
    Some((budget, MATE_CLAMP_MULTIPV))
}

/// `budget_for(pos)` (PRI-5.3).
pub fn budget_for(p: &PosInfo) -> u64 {
    let mut budget = BUDGET_LADDER[(p.visits.max(0) as usize).min(4)];
    if let Some((c, _)) = short_mate_clamp(p) {
        return budget.max(c);
    }
    if p.eval_cp.unwrap_or(0).abs() >= MATE_BAND {
        budget = budget.max(128_000_000);
    }
    budget
}

/// `multipv_for(visits, budget_nodes, seeding, clamp)` (PRI-5.5).
pub fn multipv_for(visits: i32, budget: Option<u64>, seeding: bool, clamp: Option<Clamp>) -> u32 {
    if seeding {
        return 5;
    }
    if let (Some((cb, cm)), Some(b)) = (clamp, budget) {
        if b <= cb {
            return cm;
        }
    }
    if let Some(b) = budget {
        if visits >= 1 && b >= DEPTH_BUDGET_THRESHOLD {
            return DEPTH_MULTIPV;
        }
    }
    if visits < 3 { 5 } else { 3 }
}

/// `caps_interior(pos, budget)` (PRI-5.7).
pub fn caps_interior(p: Option<&PosInfo>, budget: u64, interior_deep_rungs: bool) -> bool {
    match p {
        Some(p) => budget > AUTO_INTERIOR_MAX_NODES && p.backed_move && !interior_deep_rungs,
        None => false,
    }
}

/// `server_picked_budget(budget, clamp, pos)` (PRI-5.6).
pub fn server_picked_budget(budget: u64, clamp: Option<Clamp>, p: Option<&PosInfo>, interior_deep_rungs: bool) -> u64 {
    if let Some((cb, _)) = clamp {
        if budget <= cb {
            return budget;
        }
    }
    let mut b = budget;
    if caps_interior(p, b, interior_deep_rungs) {
        b = AUTO_INTERIOR_MAX_NODES;
    }
    b.max(SERVER_PICKED_MIN_NODES)
}

/// `_next_rung(completed_max)` (PRI-5.8, Q-6.3).
pub fn next_rung(completed_max: Option<u64>) -> u64 {
    match completed_max {
        None => REQUEST_BUDGET_LADDER[0],
        Some(c) => REQUEST_BUDGET_LADDER.iter().copied().find(|&r| r > c).unwrap_or(REQUEST_BUDGET_LADDER[2]),
    }
}

/// `_rung_at_least(nodes)` (PRI-5.8).
pub fn rung_at_least(nodes: u64) -> u64 {
    BUDGET_LADDER.iter().copied().find(|&r| r >= nodes).unwrap_or(BUDGET_LADDER[4])
}

/// `_rung_matching(nodes)` (PRI-5.8): first rung with `nodes <= rung * 1.01`.
pub fn rung_matching(nodes: u64) -> u64 {
    BUDGET_LADDER
        .iter()
        .copied()
        .find(|&r| (nodes as f64) <= (r as f64) * (1.0 + RUNG_OVERSHOOT_TOLERANCE))
        .unwrap_or(BUDGET_LADDER[4])
}

/// What the value walker quotes (PRI-4.8 `value_budget`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ValueArm {
    Rung,
    Cycle,
    Backup,
    Explore,
    Spine,
    Bottleneck,
    Other(String),
}

pub fn value_budget(arm: &ValueArm, floor_nodes: Option<u64>) -> u64 {
    match arm {
        ValueArm::Rung => next_rung(floor_nodes),
        // v1 raises on None; 0 gives the first rung.
        ValueArm::Cycle => rung_at_least(floor_nodes.unwrap_or(0)),
        ValueArm::Backup => VALUE_BACKUP_NODES,
        ValueArm::Explore => VALUE_EXPLORE_NODES,
        _ => VALUE_SPINE_NODES,
    }
}

/// `ladder_exhausted` (Q-6.3) given the bought rung.
pub fn ladder_exhausted(open: bool, completed_max: Option<u64>) -> bool {
    open && completed_max.map(|c| c >= REQUEST_BUDGET_LADDER[2]).unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pos() -> PosInfo {
        PosInfo { exists: true, white_to_move: true, ..Default::default() }
    }

    #[test]
    fn ladders_and_rungs() {
        assert_eq!(next_rung(None), 128_000_000);
        assert_eq!(next_rung(Some(128_000_000)), 512_000_000);
        assert_eq!(next_rung(Some(200_000_000)), 512_000_000);
        assert_eq!(next_rung(Some(2_000_000_000)), 2_000_000_000);
        assert_eq!(rung_at_least(9_000_000), 32_000_000);
        assert_eq!(rung_at_least(3_000_000_000), 2_000_000_000);
        // test_backlog.py::Adopting::test_the_price_beats_the_search_it_redoes
        assert_eq!(rung_matching(512_000_099), 512_000_000);
        assert_eq!(rung_matching(517_120_000), 512_000_000);
        assert_eq!(rung_matching(517_120_001), 2_000_000_000);
    }

    #[test]
    fn multipv_policy() {
        // test_multipv_policy.py::MultipvPolicyTests
        assert_eq!(multipv_for(0, Some(2_000_000_000), false, None), 5);
        assert_eq!(multipv_for(1, Some(512_000_000), false, None), 2);
        assert_eq!(multipv_for(1, Some(128_000_000), false, None), 5);
        assert_eq!(multipv_for(3, Some(128_000_000), false, None), 3);
        assert_eq!(multipv_for(3, None, true, None), 5);
        assert_eq!(multipv_for(2, Some(8_000_000), false, Some((8_000_000, 1))), 1);
        assert_eq!(multipv_for(2, Some(128_000_000), false, Some((8_000_000, 1))), 5);
    }

    #[test]
    fn short_mate_clamp_shape() {
        // test_mate_clamp.py::ShortMateClampShapeTests
        let mut p = pos();
        p.mate_in = Some(1);
        assert_eq!(short_mate_clamp(&p), Some((4_000_000, 1)));
        p.mate_in = Some(0);
        assert_eq!(short_mate_clamp(&p), Some((2_000_000, 1)));
        p.mate_in = Some(6);
        assert_eq!(short_mate_clamp(&p), Some((24_000_000, 1)));
        p.mate_in = Some(7);
        assert_eq!(short_mate_clamp(&p), None);
        // A first line `mate 2` with the side to move winning: 3 plies.
        let mut q = pos();
        q.first_line_mate = Some(2);
        assert_eq!(claimed_mate_plies(&q), Some(3));
        q.first_line_mate = Some(-2);
        assert_eq!(claimed_mate_plies(&q), Some(4));
        // The first current line has no mate: no distance, even with a mate eval.
        q.first_line_mate = Some(0);
        q.eval_cp = Some(9_997);
        assert_eq!(claimed_mate_plies(&q), None);
        // Band above the tables, below a proven win.
        let mut r = pos();
        r.eval_cp = Some(9_998);
        assert_eq!(claimed_mate_plies(&r), Some(3));
        // test_mate_clamp.py::MateDistanceUnitTests::test_a_proven_win_without_a_stated_distance_is_not_a_claim
        r.eval_cp = Some(10_000);
        assert_eq!(claimed_mate_plies(&r), None);
    }

    #[test]
    fn budget_for_carve_out() {
        // test_mate_clamp.py::BudgetForCarveOutTests
        let mut p = pos();
        p.eval_cp = Some(9_998);
        assert_eq!(budget_for(&p), 12_000_000); // short claim replaces the band jump
        p.eval_cp = Some(9_200);
        assert_eq!(budget_for(&p), 128_000_000); // band without distance keeps the jump
        p.eval_cp = Some(50);
        assert_eq!(budget_for(&p), 8_000_000);
        p.visits = 4;
        p.eval_cp = Some(9_998);
        assert_eq!(budget_for(&p), 2_000_000_000); // the visit ladder takes over
    }

    #[test]
    fn server_floor_and_interior() {
        // test_idle_pool_budget.py::RuleTests, test_sweep.py::InteriorRungRuleTests
        let mut p = pos();
        assert_eq!(server_picked_budget(8_000_000, None, Some(&p), false), 128_000_000);
        assert_eq!(server_picked_budget(4_000_000, Some((4_000_000, 1)), Some(&p), false), 4_000_000);
        assert_eq!(server_picked_budget(32_000_000, Some((4_000_000, 1)), Some(&p), false), 128_000_000);
        p.backed_move = true;
        assert_eq!(server_picked_budget(2_000_000_000, None, Some(&p), false), 128_000_000);
        assert_eq!(server_picked_budget(2_000_000_000, None, None, false), 2_000_000_000);
        assert_eq!(server_picked_budget(2_000_000_000, None, Some(&p), true), 2_000_000_000);
        p.backed_move = false;
        assert_eq!(server_picked_budget(512_000_000, None, Some(&p), false), 512_000_000);
    }

    #[test]
    fn value_budgets() {
        assert_eq!(value_budget(&ValueArm::Rung, None), 128_000_000);
        assert_eq!(value_budget(&ValueArm::Rung, Some(128_000_000)), 512_000_000);
        assert_eq!(value_budget(&ValueArm::Cycle, Some(100_000_000)), 128_000_000);
        assert_eq!(value_budget(&ValueArm::Backup, None), 32_000_000);
        assert_eq!(value_budget(&ValueArm::Explore, None), 8_000_000);
        assert_eq!(value_budget(&ValueArm::Spine, None), 128_000_000);
    }
}
