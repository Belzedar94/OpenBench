//! The selector (PRI-1, PRI-2.1, PRI-4) and the background arms (Q-8) on an
//! in-memory graph.

mod common;
use av2_model::{Closure, Core, Edge, Ext, Fingerprint, GraphRead, GraphWrite, Move, PosId, Proof, Status};
use av2_queue::consts::*;
use av2_queue::selector::*;
use av2_queue::*;
use av2_store::mem::MemGraph;
use common::*;
use std::collections::{BTreeMap, BTreeSet};

fn fp(i: u64) -> Fingerprint {
    let mut b = [0u8; 12];
    b[..8].copy_from_slice(&i.to_le_bytes());
    b[11] = 0x5a;
    Fingerprint(b)
}

/// A graph builder: positions with evals, side to move and edges.
struct G {
    g: MemGraph,
}

impl G {
    fn new() -> G {
        G { g: MemGraph::new() }
    }
    fn node(&mut self, eval: Option<i16>, white: bool) -> PosId {
        let n = self.g.len();
        let mut c = Core::default();
        if let Some(e) = eval {
            c.eval_cp = e;
        }
        c.set_flag(av2_model::record::F_BLACK_TO_MOVE, !white);
        let fen = if white { "8/8/8/8/8/8/8/K6k w - - 0 1" } else { "8/8/8/8/8/8/8/K6k b - - 0 1" };
        self.g.create(fp(n + 1), Some(fen), c).unwrap()
    }
    fn edges(&mut self, parent: PosId, kids: &[PosId]) {
        let edges: Vec<Edge> = kids.iter().enumerate().map(|(i, k)| Edge { child: *k, mv: Move::new(i as u8, i as u8 + 8, 0) }).collect();
        self.g.set_children(parent, &edges).unwrap();
        let mut c = self.g.core(parent);
        c.set_flag(av2_model::record::F_EXPANDED, true);
        self.g.put_core(parent, c).unwrap();
    }
    fn close(&mut self, id: PosId, s: Status) {
        let mut c = self.g.core(id);
        c.set_status(s, Closure::Minimax, Proof::None);
        self.g.put_core(id, c).unwrap();
    }
    fn visits(&mut self, id: PosId, v: i32) {
        let mut e = self.g.ext(id).unwrap_or_default();
        e.visits = v;
        self.g.put_ext(id, e).unwrap();
    }
}

/// tests.py::SelectorTests::test_priority_prefers_root_relevant_lines
/// test_selector_leak.py::RegretUsesBestKnownEvalTests::test_a_closed_parent_no_longer_hands_out_a_zero_gap_path
/// ::test_a_closed_sibling_still_creates_the_gap_it_should
#[test]
fn regret_dijkstra() {
    let mut g = G::new();
    let root = g.node(Some(20), true);
    let best = g.node(Some(20), false);
    let bad = g.node(Some(-300), false);
    let blind = g.node(None, false);
    g.edges(root, &[best, bad, blind]);
    let below_bad = g.node(Some(-300), true);
    g.edges(bad, &[below_bad]);
    let orphan = g.node(Some(0), true);
    let r = regret_from_root(&g.g, root);
    assert_eq!(r[root.index()], 0.0);
    assert_eq!(r[best.index()], 0.0);
    assert_eq!(r[bad.index()], 320.0);
    assert_eq!(r[blind.index()], 0.0, "an unvalued child inherits the parent's regret");
    assert_eq!(r[below_bad.index()], 320.0);
    assert_eq!(r[orphan.index()], f64::INFINITY);
    // A closed node does not relax its children.
    g.close(bad, Status::WhiteWin);
    let r = regret_from_root(&g.g, root);
    assert_eq!(r[below_bad.index()], f64::INFINITY);
    // ...but as a child it still creates the gap (value +10000 for White).
    assert_eq!(r[best.index()], (10000 - 20) as f64);
}

/// PRI-1.5 / PRI-1.3 / PRI-1.4 (test_selector_v2.py::HorizonTests::test_the_two_prices_are_not_the_same_number,
/// test_campaigns.py::SelectorBonusTests::test_more_votes_weigh_more_but_with_diminishing_returns)
#[test]
fn priority_formula() {
    assert_eq!(runits(f64::INFINITY), DISCONNECTED_REGRET);
    assert_eq!(runits(5000.0), 30.0);
    assert_eq!(runits(150.0), 1.5);
    assert_ne!(FAR_REGRET, DISCONNECTED_REGRET);
    // 15 (cap) + 50 (mate band) + 2 (unexpanded) - 3*1.5 - 1.5*2
    let none = Votes::new();
    assert_eq!(priority_of(Some(-9500), false, 2, None, 1.5, &none), 15.0 + 50.0 + 2.0 - 4.5 - 3.0);
    assert_eq!(priority_of(None, true, 0, None, 0.0, &none), 0.0);
    let votes = Votes::from([(1, 1), (2, 10), (3, 0)]);
    let b1 = campaign_bonus(Some(1), &votes);
    let b10 = campaign_bonus(Some(2), &votes);
    assert!(b10 > b1 && b10 < 10.0 * b1);
    assert_eq!(campaign_bonus(None, &votes), 0.0);
    assert_eq!(campaign_bonus(Some(3), &votes), 0.0);
}

/// tests.py::SelectorTests::test_global_best_first_by_eval / test_dead_branch_not_selected
/// test_idle_pool_budget.py::RegretSelectorTests::test_a_first_look_is_minted_at_the_floor
/// ::test_the_visit_ladder_above_the_floor_still_climbs
/// test_campaigns.py::SelectorBonusTests::test_a_tombstone_is_not_resurrected_by_any_number_of_votes
#[test]
fn regret_minting() {
    let mut g = G::new();
    let root = g.node(Some(0), true);
    let a = g.node(Some(200), false); // closer to the mate band: higher priority
    let b = g.node(Some(0), false);
    g.edges(root, &[a, b]);
    let lonely = g.node(Some(0), true);
    let dead_parent = g.node(Some(0), true);
    g.edges(dead_parent, &[lonely]);
    g.close(dead_parent, Status::Draw);
    g.visits(b, 3);
    let votes = Votes::new();
    let sel = RegretSelector::compute(&g.g, root, &votes, &BTreeSet::new());
    assert!(sel.ranked.iter().position(|x| x.1 == a.0) < sel.ranked.iter().position(|x| x.1 == b.0));

    let mut h = H::new();
    let env_graph = GraphEnv::new(&g.g);
    let mut q = QueueState::new(QueueConfig::default());
    let mut refill = sel.clone();
    let plan = refill.next_tasks(&q, &env_graph, 8, T0);
    assert!(plan.tombstones.contains(&lonely.0), "every parent closed: tombstoned");
    let applied = q.apply(&Input::Mint { now: T0, plan }, &env_graph, &mut NoRefill);
    let Output::Minted { created, .. } = applied.output else { panic!() };
    assert!(created >= 3);
    let task_of = |q: &QueueState, p: PosId| q.live_tasks_of(p.0)[0].clone();
    assert_eq!(task_of(&q, a).budget, B128, "a first look is minted at the floor");
    assert_eq!(task_of(&q, b).budget, B512, "visit ladder above the floor (visits 3)");
    assert!(q.is_dead(lonely.0));
    // Tombstones are not rescored by a pass, whatever the votes.
    let sel2 = RegretSelector::compute(&g.g, root, &Votes::from([(1, 1000)]), &BTreeSet::from([lonely.0]));
    assert_eq!(sel2.ranked.last().unwrap(), &(DEAD, lonely.0));
    let _ = &mut h;
}

struct FakeDescent {
    picks: Vec<u64>,
    campaigns: Vec<u64>,
    roots: Vec<(u64, f64)>,
    or_clamp: Option<u64>,
}

impl Descent for FakeDescent {
    fn active_campaigns(&self) -> Vec<u64> {
        self.campaigns.clone()
    }
    fn campaign_roots(&self) -> Vec<(u64, f64)> {
        self.roots.clone()
    }
    fn descend(&self, _c: u64, counter: u64, avoid: &BTreeSet<u64>, _start: Option<u64>) -> Option<u64> {
        self.picks.iter().copied().find(|p| !avoid.contains(p)).or_else(|| self.picks.get(counter as usize % self.picks.len()).copied())
    }
    fn descend_value(&self, c: u64, counter: u64, avoid: &BTreeSet<u64>, start: Option<u64>, _live: &dyn Fn(u64) -> bool) -> Option<ValueTarget> {
        self.descend(c, counter, avoid, start).map(|pos| ValueTarget { pos, arm: av2_queue::budget::ValueArm::Explore, floor_nodes: None })
    }
    fn proved_or_clamp(&self, pos: u64, _c: &[u64]) -> Option<(u64, u32)> {
        self.or_clamp.filter(|&p| p == pos).map(|_| (OR_CLAMP_NODES, OR_CLAMP_MULTIPV))
    }
}

/// test_proof_manager.py::SelectorFlagTests::test_pn_selector_without_campaigns_returns_nothing
/// test_bug_hunt_findings.py::ProofSelectorAttemptCapTests::test_the_descent_loop_stops_at_its_attempt_cap
/// test_solver_allocation.py::CampaignMintingTests::test_a_normal_descent_is_not_marked / test_a_campaign_descent_marks_its_tasks
/// test_solver_allocation.py::OrClampMintingTests::test_the_clamp_beats_the_ladder_and_the_mate_carve_out
/// test_value_descent.py::ReservationTests::test_the_pool_never_hands_the_same_position_twice
#[test]
fn pn_minting_loop() {
    let mut h = H::new();
    let ps: Vec<u64> = (0..4).map(|_| h.pos()).collect();
    let mut none = FakeDescent { picks: ps.clone(), campaigns: vec![], roots: vec![], or_clamp: None };
    let mut sel = PnSelector { descent: &none };
    assert!(sel.next_tasks(&h.q, &h.env, 8, h.now).tasks.is_empty());

    none.campaigns = vec![1];
    none.or_clamp = Some(ps[1]);
    let mut sel = PnSelector { descent: &none };
    let plan = sel.next_tasks(&h.q, &h.env, 8, h.now);
    // Only 4 distinct positions in 32 attempts: never the same one twice.
    assert_eq!(plan.tasks.len(), 4);
    let mut seen = BTreeSet::new();
    assert!(plan.tasks.iter().all(|t| seen.insert(t.pos)));
    assert!(plan.tasks.iter().all(|t| t.arm.is_empty() && t.source == Source::Auto));
    let or = plan.tasks.iter().find(|t| t.pos == ps[1]).unwrap();
    assert_eq!((or.budget, or.multipv), (OR_CLAMP_NODES, OR_CLAMP_MULTIPV), "the OR clamp keeps its price");
    let other = plan.tasks.iter().find(|t| t.pos == ps[0]).unwrap();
    assert_eq!(other.budget, B128);

    // Campaign roots: a descent started at a campaign root marks its task.
    let camp = FakeDescent { picks: ps.clone(), campaigns: vec![1], roots: vec![(ps[3], 1.0)], or_clamp: None };
    let mut sel = PnSelector { descent: &camp };
    let plan = sel.next_tasks(&h.q, &h.env, 40, h.now);
    let started: Vec<bool> = (0..plan.tasks.len() as u64).map(|i| campaign_start(h.q.max_task_id() + i, &camp.roots).is_some()).collect();
    assert!(plan.tasks.iter().zip(&started).all(|(t, s)| (t.arm == ARM_CAMPAIGN) == *s));
}

/// proof.campaign_start / _point: a deterministic, domain-separated draw.
#[test]
fn campaign_start_draw() {
    let roots = vec![(10, 1.0), (20, 1.0)];
    let mut counts = BTreeMap::new();
    for c in 0..10_000u64 {
        *counts.entry(campaign_start(c, &roots)).or_insert(0) += 1;
    }
    // 35% of the draws start at a campaign root, split by weight.
    let at_roots = counts.get(&Some(10)).unwrap() + counts.get(&Some(20)).unwrap();
    assert!((3200..3800).contains(&at_roots), "{at_roots}");
    assert_eq!(campaign_start(7, &[]), None);
    assert_eq!(point(1, "campaign:"), point(1, "campaign:"));
    assert_ne!(point(1, "campaign:"), point(1, ""));
}

/// test_idle_pool_budget.py::RegretSelectorTests::test_the_pool_top_up_mints_at_the_floor_and_counts_tasks
/// test_idle_repair.py::PoolTests::test_waiting_repairs_do_not_shrink_the_pool
/// test_arm_rate_budget.py::UnbudgetedDoorsTests::test_the_top_up_is_never_rate_limited
#[test]
fn pool_top_up() {
    let mut h = H::new();
    let ps: Vec<u64> = (0..10).map(|_| h.pos()).collect();
    h.row(B128, |t| t.arm = ARM_REPAIR.into());
    h.row(B128, |t| t.source = Source::Fill);
    let d = FakeDescent { picks: ps, campaigns: vec![1], roots: vec![], or_clamp: None };
    let mut sel = PnSelector { descent: &d };
    let a = h.q.apply(&Input::Pool { now: h.now, target: Some(5) }, &h.env, &mut sel);
    assert_eq!(a.output, Output::Made { count: 5 });
    assert_eq!(h.q.pool_pending(), 5);
    let a = h.q.apply(&Input::Pool { now: h.now, target: Some(5) }, &h.env, &mut sel);
    assert_eq!(a.output, Output::Made { count: 0 });
}

/// test_arm_rate_budget.py::CascadeRateTests (hourly count, window rolls, clips below the burst brake,
/// rate 0 turns the arm off) + ArmRateReceiptTests + test_value_descent.py::ValueCascadeTests
#[test]
fn value_cascade_and_hourly_room() {
    let mut h = H::new();
    h.config(|c| {
        c.descent_value = true;
        c.arm_rates.insert(ARM_CASCADE.into(), 5);
    });
    let parent = h.pos();
    for i in 0..30 {
        h.env.edge(parent, &format!("m{i}"), 1000 + i);
    }
    h.env.info(1001).eval_cp = Some(12); // judged: not bought
    let cascade = |h: &mut H| {
        let a = h.apply(Input::Cascade { now: h.now, pos: parent, open_before: true });
        let Output::Made { count } = a.output else { panic!() };
        (count, a.events)
    };
    let (n, ev) = cascade(&mut h);
    assert_eq!(n, 5, "the hourly room clips the batch below the brake of 16");
    assert_eq!(ev[0].kind, "VALUE_CASCADE");
    assert!(h.q.live_tasks_of(1001).is_empty());
    let t = h.q.live_tasks_of(1000)[0].clone();
    assert_eq!((t.budget, t.arm.as_str(), t.source), (B128, ARM_CASCADE, Source::Auto));
    let (n, ev) = cascade(&mut h);
    assert_eq!(n, 0);
    assert_eq!(ev[0].kind, "ARM_RATE_BUDGET");
    assert_eq!(ev[0].payload["made"], 5);
    h.advance(5 * MINUTE);
    let (_, ev) = cascade(&mut h);
    assert!(ev.is_empty(), "one receipt per arm and ten minutes");
    h.advance(HOUR);
    let (n, _) = cascade(&mut h);
    assert_eq!(n, 5, "the window rolls");
    // Rate 0 turns the arm off, without a receipt.
    h.config(|c| {
        c.arm_rates.insert(ARM_CASCADE.into(), 0);
    });
    h.advance(2 * HOUR);
    let (n, ev) = cascade(&mut h);
    assert_eq!((n, ev.len()), (0, 0));
    // The proof descent does not cascade; a closed landing does not either.
    h.config(|c| {
        c.descent_value = false;
        c.arm_rates.insert(ARM_CASCADE.into(), 240);
    });
    assert_eq!(cascade(&mut h).0, 0);
    let a = h.apply(Input::Cascade { now: h.now, pos: parent, open_before: false });
    assert_eq!(a.output, Output::Made { count: 0 });
}

/// test_arm_rate_budget.py::ArmRateKnobTests::test_the_defaults_are_the_ones_the_owner_agreed / test_a_mistyped_value_keeps_the_default
#[test]
fn arm_rate_knobs() {
    let c = QueueConfig::default();
    assert_eq!(c.arm_rate(ARM_SWEEP16), Some(1200));
    assert_eq!(c.arm_rate(ARM_CASCADE), Some(240));
    assert_eq!(c.arm_rate(ARM_QUALITY), Some(120));
    assert_eq!(c.arm_rate(ARM_DESCEND), Some(120));
    assert_eq!(c.arm_rate(ARM_REPAIR), Some(60));
    assert_eq!(c.arm_rate(ARM_AUTOSOLVE95), Some(60));
    assert_eq!(c.arm_rate(ARM_COVERAGE), None);
    assert_eq!(config::parse_arm_rate("30", 240), 30);
    assert_eq!(config::parse_arm_rate(" 0 ", 240), 0);
    assert_eq!(config::parse_arm_rate("-5", 240), 240);
    assert_eq!(config::parse_arm_rate("2.5", 240), 240);
    assert_eq!(config::parse_arm_rate("", 240), 240);
}

// ------------------------------------------------------------ arms

/// test_engine_debt.py::EnqueueDebtTests (one F0 task, goal follows status, never twice, cap counted over this arm,
/// tops up only the room left) + DEBT_ENQUEUED receipt; OQ-21: a FAILED debt task does not exclude.
#[test]
fn engine_debt() {
    let mut g = G::new();
    let mut debt = Vec::new();
    for i in 0..5 {
        let p = g.node(Some(9990), true);
        let mut c = g.g.core(p);
        c.set_status(
            if i % 2 == 0 { Status::WhiteWin } else { Status::BlackWin },
            Closure::MatePv,
            if i == 3 { Proof::None } else { Proof::Engine },
        );
        g.g.put_core(p, c).unwrap();
        let line = g.g.put_blob(b"e2e4 e7e5").unwrap();
        g.g.put_ext(p, Ext { won_line: line, ..Default::default() }).unwrap();
        debt.push(p.0);
    }
    let certified = g.node(None, true);
    let mut c = g.g.core(certified);
    c.set_status(Status::WhiteWin, Closure::MatePv, Proof::AndOr);
    g.g.put_core(certified, c).unwrap();
    let no_line = g.node(None, true);
    let mut c = g.g.core(no_line);
    c.set_status(Status::WhiteWin, Closure::MatePv, Proof::Engine);
    g.g.put_core(no_line, c).unwrap();
    let cands = arms::scan_engine_debt(&g.g);
    assert_eq!(cands.iter().map(|c| c.0).collect::<Vec<_>>(), debt);
    assert_eq!(cands[1].1, "BLACK_WIN");

    let mut h = H::new();
    h.config(|c| c.debt_cap = 3);
    // Another arm's pending row does not count against the debt cap.
    let p = h.pos();
    h.q.import_solve(SolveTask::new(1, p, "WHITE_WIN", "F0", 2_000_000, ARM_FRAGILE, h.now));
    let a = h.apply(Input::Arm { now: h.now, step: arms::ArmStep::Debt { candidates: cands.clone(), limit: None } });
    assert_eq!(a.output, Output::Made { count: 3 });
    assert_eq!(a.events[0].kind, "DEBT_ENQUEUED");
    let a = h.apply(Input::Arm { now: h.now, step: arms::ArmStep::Debt { candidates: cands.clone(), limit: None } });
    assert_eq!(a.output, Output::Made { count: 0 }, "the arm's own pending rows hold it back");
    let debt_tasks: Vec<SolveTask> = h.q.solves().filter(|s| s.arm == ARM_DEBT).cloned().collect();
    assert!(debt_tasks.iter().all(|s| s.stage == "F0" && s.budget == DEBT_STAGE_NODES));
    assert_eq!(debt_tasks[1].goal, "BLACK_WIN");
}

/// test_coverage_completion.py::CoverageGapTests::test_the_global_cap_is_respected / test_it_records_what_it_did
/// ::test_an_existing_auto_task_is_promoted_not_duplicated; test_idle_pool_budget.py::CoverageAndDnTests::test_coverage_completion_is_floored
#[test]
fn coverage_completion() {
    let mut h = H::new();
    h.config(|c| c.coverage_cap = 2);
    let promoted = h.auto(B128);
    let pp = h.t(promoted).pos;
    let gaps = vec![arms::CoverageGap { pos: 1000, missing: vec![pp] }, arms::CoverageGap { pos: 1001, missing: vec![2001, 2002] }];
    h.env.add(2001);
    h.env.add(2002);
    let a = h.apply(Input::Arm { now: h.now, step: arms::ArmStep::Coverage { gaps, scanned: 2 } });
    assert_eq!(a.output, Output::Made { count: 2 });
    assert_eq!(a.events[0].payload["scanned"], 2);
    let t = h.t(promoted);
    assert_eq!((t.source, t.arm.as_str(), t.budget), (Source::Fill, ARM_COVERAGE, B128));
    let c = h.q.live_tasks_of(2001)[0].clone();
    assert_eq!((c.source, c.budget, c.multipv), (Source::Fill, B128, DEPTH_MULTIPV));
    assert!(h.q.live_tasks_of(2002).is_empty(), "the cap bites");
}

/// test_adversarial_explorer.py::DnRepairTests (thin obligation gets unlooked replies, floor decides,
/// unexpanded node is not a claim, a judged reply is not bought again, never disguised as a visitor, receipt)
#[test]
fn dn_repair() {
    let mut h = H::new();
    let node = h.pos();
    h.env.info(node).expanded = true;
    h.env.edge(node, "a", 3000);
    h.env.edge(node, "b", 3001);
    h.env.info(3001).eval_cp = Some(-50); // judged
    let fat = h.pos();
    h.env.info(fat).expanded = true;
    h.env.edge(fat, "c", 3002);
    let unexpanded = h.pos();
    h.env.edge(unexpanded, "d", 3003);
    h.env.info(unexpanded).expanded = false;
    let frontier = vec![vec![(node, 1, 2), (fat, 1, 5), (unexpanded, 1, 1)]];
    let a = h.apply(Input::Arm { now: h.now, step: arms::ArmStep::DnRepair { frontier } });
    assert_eq!(a.output, Output::Made { count: 1 });
    assert_eq!(a.events[0].payload["examined"], 2);
    let t = h.q.live_tasks_of(3000)[0].clone();
    assert_eq!((t.source, t.arm.as_str(), t.budget), (Source::Fill, ARM_DN, B128));
    assert!(h.q.live_tasks_of(3002).is_empty());
    assert!(h.q.live_tasks_of(3003).is_empty());
}

/// test_adversarial_explorer.py::FragileMateClaimTests (asks at the cheap rung, not twice, live solve left alone,
/// its own cap, receipt with both signs)
#[test]
fn fragile_mate_claims() {
    let mut h = H::new();
    let ps: Vec<u64> = (0..4).map(|_| h.pos()).collect();
    h.q.import_solve(SolveTask::new(1, ps[2], "WHITE_WIN", "F0", 8_000_000, ARM_AUTOSOLVE95, h.now));
    let cands = vec![(ps[0], "WHITE_WIN".to_string()), (ps[1], "BLACK_WIN".to_string()), (ps[2], "WHITE_WIN".to_string())];
    let a = h.apply(Input::Arm { now: h.now, step: arms::ArmStep::Fragile { candidates: cands.clone() } });
    assert_eq!(a.output, Output::Made { count: 2 });
    assert_eq!((a.events[0].payload["white"].clone(), a.events[0].payload["black"].clone()), (serde_json::json!(1), serde_json::json!(1)));
    let a = h.apply(Input::Arm { now: h.now, step: arms::ArmStep::Fragile { candidates: cands } });
    assert_eq!(a.output, Output::Made { count: 0 });
    assert!(h.q.solves().filter(|s| s.arm == ARM_FRAGILE).all(|s| s.budget == FRAGILE_STAGE_NODES && s.stage == "F0"));
}
