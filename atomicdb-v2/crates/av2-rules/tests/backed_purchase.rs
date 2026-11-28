//! Step 9 of the port: what the ascent buys (BKD-11), with the task model in
//! memory (`purchase::MemTasks`: one task per `(child, generation)`, the
//! quality arm's hourly room and queue cap).  v1's `AnalysisTask` assertions
//! become assertions on the sink.  Each test names its v1 original.

use av2_model::{GraphRead, Move, PosId};
use av2_rules::ascent::AscentOpts;
use av2_rules::config::RulesConfig;
use av2_rules::effects::{Event, Recorder};
use av2_rules::purchase::{
    BUDGET_LADDER, MemTasks, PurchaseKind, QUALITY_QUEUE_CAP, queue_cycle_disambiguation, queue_quality_convergence, rung_at_least,
    rung_matching,
};
use av2_rules::testkit::{Fixture, START_FEN};

fn task_of(tasks: &MemTasks, child: PosId) -> Option<u64> {
    tasks.tasks.iter().find(|((c, _), _)| *c == child).map(|(_, (b, _))| *b)
}

fn ascent(f: &mut Fixture, seeds: &[PosId], tasks: &mut MemTasks) -> Recorder {
    f.ascent_with(&RulesConfig::default(), seeds, AscentOpts::default(), tasks).1
}

fn value(f: &Fixture, id: PosId) -> (Option<i32>, String) {
    let b = f.stored(id);
    (b.value, b.mv.to_uci())
}

// ------------------------------------------------------------ test_backed_eval.py

/// test_backed_eval.py::BackedGuardTests::test_shallow_child_displaces_at_once_and_buys_convergence
#[test]
fn shallow_child_displaces_at_once_and_buys_convergence() {
    let mut f = Fixture::new();
    let p = f.named("QP", false);
    f.set(p, |c| {
        c.set_flag(av2_model::record::F_EXPANDED, true);
        c.eval_cp = 100;
    });
    f.set_ext(p, |e| e.nodes_invested = 10_000_000_000);
    let shallow = f.named("QSHALLOW", true);
    f.set(shallow, |c| c.eval_cp = 900);
    f.set_ext(shallow, |e| e.nodes_invested = 128_000_000);
    let blind = f.named("QBLIND", true);
    f.edge(p, "a1a2", shallow);
    f.edge(p, "a1a3", blind);
    let mut tasks = MemTasks::default();
    ascent(&mut f, &[p], &mut tasks);
    assert_eq!(f.stored(p).value, Some(900));
    assert!(task_of(&tasks, shallow).is_some());
    assert!(tasks.log.iter().all(|p| p.kind == PurchaseKind::QualityConvergence));
}

// ------------------------------------------------------------ test_quality_tolerance.py::QualityToleranceTests

/// `_wolfram`: the reported shape, White to move, partial coverage, one
/// informed child (d2d4) claiming a better value.
fn wolfram(child_quality: u64, child_value: i16, own: i16, own_quality: u64) -> (Fixture, PosId, PosId) {
    let mut f = Fixture::new();
    let parent = f.pos(START_FEN);
    f.expand_opening(parent);
    f.set(parent, |c| c.eval_cp = own);
    f.set_ext(parent, |e| e.nodes_invested = own_quality);
    let child = f.edge_child(parent, "d2d4");
    f.set(child, |c| {
        c.backed_eval = child_value;
        c.backed_plies = 2;
    });
    f.set_ext(child, |e| e.backed_nodes = child_quality);
    (f, parent, child)
}

/// test_quality_tolerance.py::QualityToleranceTests::test_the_reported_case_now_displaces
#[test]
fn the_reported_case_now_displaces() {
    let (mut f, parent, _) = wolfram(128_007_926, 416, 369, 128_111_808);
    ascent(&mut f, &[parent], &mut MemTasks::default());
    assert_eq!(value(&f, parent), (Some(416), "d2d4".into()));
}

/// test_quality_tolerance.py::QualityToleranceTests::test_a_genuinely_shallow_child_displaces_and_buys
#[test]
fn a_genuinely_shallow_child_displaces_and_buys() {
    let (mut f, parent, child) = wolfram(8_000_000, 416, 369, 10_000_000_000);
    let mut tasks = MemTasks::default();
    ascent(&mut f, &[parent], &mut tasks);
    assert_eq!(value(&f, parent), (Some(416), "d2d4".into()));
    assert!(task_of(&tasks, child).is_some());
}

/// test_quality_tolerance.py::QualityToleranceTests::test_exactly_at_the_tolerance_qualifies
#[test]
fn exactly_at_the_tolerance_qualifies() {
    let (mut f, parent, child) = wolfram(50_000_000, 416, 369, 100_000_000);
    let mut tasks = MemTasks::default();
    ascent(&mut f, &[parent], &mut tasks);
    assert_eq!(f.stored(parent).value, Some(416));
    // At the tolerance there is no discrepancy to buy.
    assert!(task_of(&tasks, child).is_none());
}

/// test_quality_tolerance.py::QualityToleranceTests::test_just_under_the_tolerance_displaces_and_buys
#[test]
fn just_under_the_tolerance_displaces_and_buys() {
    let (mut f, parent, child) = wolfram(49_000_000, 416, 369, 100_000_000);
    let mut tasks = MemTasks::default();
    ascent(&mut f, &[parent], &mut tasks);
    assert_eq!(f.stored(parent).value, Some(416));
    assert!(task_of(&tasks, child).is_some());
}

/// test_quality_tolerance.py::QualityToleranceTests::test_the_directional_guard_is_untouched_at_any_quality
#[test]
fn the_directional_guard_is_untouched_at_any_quality() {
    let (mut f, parent, _) = wolfram(10_000_000_000, 120, 369, 1_000);
    ascent(&mut f, &[parent], &mut MemTasks::default());
    assert_eq!(f.stored(parent).value, Some(369));
    assert_eq!(f.stored(parent).mv, Move::NONE);
}

/// test_quality_tolerance.py::QualityToleranceTests::test_the_tolerance_is_a_named_documented_constant
#[test]
fn the_tolerance_is_a_named_documented_constant() {
    // 0.5, compared exactly as `2 * quality < own_quality`.
    assert_eq!(av2_rules::backed::BACKED_QUALITY_TOLERANCE_DEN, 2);
}

// ------------------------------------------------------------ QualityConvergenceTests

/// A start position at 369 / `parent_nodes` with d2d4 backed at 416.
fn blocked(parent_nodes: u64, child_backed_nodes: u64, child_nodes: u64) -> (Fixture, PosId, PosId) {
    let mut f = Fixture::new();
    let parent = f.pos(START_FEN);
    f.expand_opening(parent);
    f.set(parent, |c| c.eval_cp = 369);
    f.set_ext(parent, |e| e.nodes_invested = parent_nodes);
    let child = f.edge_child(parent, "d2d4");
    f.set(child, |c| c.backed_eval = 416);
    f.set_ext(child, |e| {
        e.backed_nodes = child_backed_nodes;
        e.nodes_invested = child_nodes;
    });
    (f, parent, child)
}

/// test_quality_tolerance.py::QualityConvergenceTests::test_a_weak_displacement_buys_the_child_the_missing_depth
#[test]
fn a_weak_displacement_buys_the_child_the_missing_depth() {
    let (mut f, parent, child) = blocked(2_000_000_000, 8_000_000, 8_000_000);
    let mut tasks = MemTasks::default();
    let rec = ascent(&mut f, &[parent], &mut tasks);
    assert_eq!(f.stored(parent).value, Some(416));
    assert!(task_of(&tasks, child).unwrap() >= 2_000_000_000);
    assert!(rec.events.iter().any(|e| matches!(e, Event::QualityConvergence { .. })));
}

/// test_quality_tolerance.py::QualityConvergenceTests::test_a_displacement_buys_nothing
#[test]
fn a_displacement_buys_nothing() {
    let (mut f, parent, _) = blocked(128_111_808, 128_007_926, 0);
    let mut tasks = MemTasks::default();
    ascent(&mut f, &[parent], &mut tasks);
    assert!(tasks.tasks.is_empty());
}

/// test_quality_tolerance.py::QualityConvergenceTests::test_a_child_that_already_has_the_depth_is_not_re_bought
#[test]
fn a_child_that_already_has_the_depth_is_not_re_bought() {
    let (mut f, parent, child) = blocked(2_000_000_000, 8_000_000, 2_000_000_000);
    let mut tasks = MemTasks::default();
    ascent(&mut f, &[parent], &mut tasks);
    assert!(task_of(&tasks, child).is_none());
}

/// test_quality_tolerance.py::QualityConvergenceTests::test_bought_depth_closes_the_loop_instead_of_going_silent
#[test]
fn bought_depth_closes_the_loop_instead_of_going_silent() {
    let (mut f, parent, _) = blocked(2_000_000_000, 8_000_000, 2_000_000_000);
    ascent(&mut f, &[parent], &mut MemTasks::default());
    assert_eq!(value(&f, parent), (Some(416), "d2d4".into()));
}

/// test_quality_tolerance.py::QualityConvergenceTests::test_the_rung_matches_the_support_it_has_to_equal
#[test]
fn the_rung_matches_the_support_it_has_to_equal() {
    assert_eq!(rung_at_least(1), BUDGET_LADDER[0]);
    assert_eq!(rung_at_least(200_000_000), 512_000_000);
    assert_eq!(rung_at_least(1_000_000_000_000), BUDGET_LADDER[4]);
}

/// test_quality_tolerance.py::QualityConvergenceTests::test_the_engines_overshoot_still_matches_its_own_rung
#[test]
fn the_engines_overshoot_still_matches_its_own_rung() {
    assert_eq!(rung_matching(512_000_099), 512_000_000);
    assert_eq!(rung_matching(2_000_000_950), 2_000_000_000);
    assert_eq!(rung_matching(640_000_000), 2_000_000_000);
    assert_eq!(rung_matching(200_000_000), 512_000_000);
}

/// test_quality_tolerance.py::QualityConvergenceTests::test_a_parent_searched_at_512m_buys_its_child_512m_not_2b
#[test]
fn a_parent_searched_at_512m_buys_its_child_512m_not_2b() {
    let (mut f, parent, child) = blocked(512_000_099, 8_000_000, 8_000_000);
    let mut tasks = MemTasks::default();
    ascent(&mut f, &[parent], &mut tasks);
    assert_eq!(task_of(&tasks, child), Some(512_000_000));
    assert_eq!(av2_rules::purchase::Purchase::ARM, av2_rules::purchase::QUALITY_ARM);
}

/// `_overshoot_pair`: a parent searched at 512,000,099 and its d2d4 child.
fn overshoot_pair(child_nodes: u64) -> (Fixture, PosId, PosId) {
    let mut f = Fixture::new();
    let parent = f.named("overshoot-parent", false);
    f.set_ext(parent, |e| e.nodes_invested = 512_000_099);
    let child = f.named("overshoot-child", true);
    f.set_ext(child, |e| e.nodes_invested = child_nodes);
    f.edge(parent, "d2d4", child);
    (f, parent, child)
}

/// test_quality_tolerance.py::QualityConvergenceTests::test_a_child_searched_at_the_parents_rung_is_not_re_bought
#[test]
fn a_child_searched_at_the_parents_rung_is_not_re_bought() {
    let (f, parent, child) = overshoot_pair(512_000_099);
    let mut tasks = MemTasks::default();
    let made = queue_quality_convergence(
        &f.g,
        &[(Move::parse("d2d4").unwrap(), parent, 512_000_099)],
        &RulesConfig::default(),
        &mut tasks,
        &mut Recorder::default(),
    );
    assert_eq!(made, 0);
    assert!(task_of(&tasks, child).is_none());
}

/// test_quality_tolerance.py::QualityConvergenceTests::test_the_cycle_disambiguation_still_buys_one_rung_deeper
#[test]
fn the_cycle_disambiguation_still_buys_one_rung_deeper() {
    let (f, parent, child) = overshoot_pair(128_000_099);
    let mut tasks = MemTasks::default();
    let made = queue_cycle_disambiguation(
        &f.g,
        &[(Move::parse("d2d4").unwrap(), parent, 8_000_000)],
        &RulesConfig::default(),
        &mut tasks,
        &mut Recorder::default(),
    );
    assert_eq!(made, 1);
    assert_eq!(task_of(&tasks, child), Some(512_000_000));
}

/// `_fill_queue(rows, QUALITY_ARM)`: PENDING quality tasks on another
/// position, created this hour.
fn fill_queue(f: &mut Fixture, tasks: &mut MemTasks, rows: u64) {
    let filler = f.pos(&av2_rules::testkit::play(START_FEN, "a2a3"));
    for i in 0..rows {
        tasks.tasks.insert((filler, i as i32), (1, true));
        tasks.created += 1;
    }
}

/// test_quality_tolerance.py::QualityConvergenceTests::test_the_fill_cap_still_bounds_it
#[test]
fn the_fill_cap_still_bounds_it() {
    let (mut f, parent, child) = blocked(2_000_000_000, 8_000_000, 0);
    let mut tasks = MemTasks::default();
    fill_queue(&mut f, &mut tasks, QUALITY_QUEUE_CAP);
    ascent(&mut f, &[parent], &mut tasks);
    assert!(task_of(&tasks, child).is_none());
}

/// test_quality_tolerance.py::QualityConvergenceTests::test_another_arms_backlog_does_not_spend_this_arms_quota
/// (the sink is the quality arm's: another arm's backlog is not in it, so
/// the purchase goes through, into the quality arm).
#[test]
fn another_arms_backlog_does_not_spend_this_arms_quota() {
    let (mut f, parent, child) = blocked(2_000_000_000, 8_000_000, 0);
    let mut tasks = MemTasks::default();
    ascent(&mut f, &[parent], &mut tasks);
    assert!(task_of(&tasks, child).is_some());
    assert_eq!(av2_rules::purchase::Purchase::ARM, "quality");
}

// ------------------------------------------------------------ test_arm_rate_budget.py::QualityRateTests

/// `setUp` with the tight settings (`ATOMICDB_ARM_RATE_QUALITY = 2`).
fn quality_rate() -> (Fixture, PosId, Vec<(Move, PosId, u64)>) {
    let mut f = Fixture::new();
    let parent = f.named("QR-parent", false);
    f.set(parent, |c| {
        c.eval_cp = 100;
        c.set_flag(av2_model::record::F_EXPANDED, true);
    });
    f.set_ext(parent, |e| e.nodes_invested = 512_000_000);
    let child = f.named("QR-child", true);
    f.set(child, |c| c.eval_cp = -900);
    f.edge(parent, "a1a2", child);
    (f, child, vec![(Move::parse("a1a2").unwrap(), parent, 8_000_000)])
}

/// `_spend(QUALITY_ARM, 2)`: two purchases this hour, already completed.
fn spent(rate: u64, count: u64) -> MemTasks {
    let mut t = MemTasks::with_rate(rate);
    t.created = count;
    t
}

/// test_arm_rate_budget.py::QualityRateTests::test_the_convergence_stops_at_its_hourly_count
#[test]
fn the_convergence_stops_at_its_hourly_count() {
    let (f, child, picks) = quality_rate();
    let mut tasks = spent(2, 2);
    assert_eq!(queue_quality_convergence(&f.g, &picks, &RulesConfig::default(), &mut tasks, &mut Recorder::default()), 0);
    assert!(task_of(&tasks, child).is_none());
}

/// test_arm_rate_budget.py::QualityRateTests::test_the_cycle_disambiguation_shares_the_same_bag
#[test]
fn the_cycle_disambiguation_shares_the_same_bag() {
    let (f, _child, picks) = quality_rate();
    let mut tasks = spent(2, 2);
    assert_eq!(queue_cycle_disambiguation(&f.g, &picks, &RulesConfig::default(), &mut tasks, &mut Recorder::default()), 0);
}

/// test_arm_rate_budget.py::QualityRateTests::test_with_room_left_the_purchase_is_the_one_of_always
#[test]
fn with_room_left_the_purchase_is_the_one_of_always() {
    let (f, child, picks) = quality_rate();
    let mut tasks = MemTasks::with_rate(2);
    assert_eq!(queue_quality_convergence(&f.g, &picks, &RulesConfig::default(), &mut tasks, &mut Recorder::default()), 1);
    assert!(task_of(&tasks, child).is_some());
    assert_eq!(f.g.core(child).status(), av2_model::Status::Unknown);
}

/// test_arm_rate_budget.py::QualityRateTests::test_a_rate_of_zero_turns_the_arm_off
#[test]
fn a_rate_of_zero_turns_the_arm_off() {
    let (f, _child, picks) = quality_rate();
    let mut tasks = MemTasks::with_rate(0);
    assert_eq!(queue_cycle_disambiguation(&f.g, &picks, &RulesConfig::default(), &mut tasks, &mut Recorder::default()), 0);
    assert_eq!(queue_quality_convergence(&f.g, &picks, &RulesConfig::default(), &mut tasks, &mut Recorder::default()), 0);
    assert!(tasks.tasks.is_empty());
}
