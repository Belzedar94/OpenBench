//! Lease protocol (Q-1, Q-2.1..Q-2.29).  Test names follow the v1 tests in
//! `test-map.csv` (file::Class::test) they port.

mod common;
use av2_model::Status;
use av2_queue::consts::*;
use av2_queue::*;
use common::*;

// ------------------------------------------------------------ windows and recycling

/// test_fencing.py::LeaseWindowTests::test_the_window_is_four_minutes (+ restart relief < window).
#[test]
fn the_window_is_four_minutes() {
    assert_eq!(LEASE_MINUTES, 4 * MINUTE);
    assert!(RESTART_RECYCLE_MINUTES < LEASE_MINUTES);
    assert_eq!(LEASES_PER_MACHINE, 32);
    assert_eq!(RELEASE_MAX_LEASES, LEASES_PER_MACHINE + 1);
}

/// test_fencing.py::LeaseWindowTests::test_five_silent_minutes_give_the_task_to_somebody_else
#[test]
fn five_silent_minutes_give_the_task_to_somebody_else() {
    let mut h = H::new();
    let id = h.auto(B128);
    assert_eq!(h.lease("alice", "box-a"), Some(id));
    let first = h.t(id).token.clone();
    h.advance(5 * MINUTE);
    assert_eq!(h.lease("bob", "box-b"), Some(id));
    assert_eq!(h.t(id).attempts, 2);
    assert_ne!(h.t(id).token, first);
    assert_eq!(h.t(id).machine, "box-b");
}

/// test_fencing.py::LeaseWindowTests::test_three_and_a_half_silent_minutes_do_not
#[test]
fn three_and_a_half_silent_minutes_do_not() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("alice", "box-a");
    let tok = h.t(id).token.clone();
    h.advance(3 * MINUTE + 30 * SECOND);
    assert_eq!(h.lease("bob", "box-b"), None);
    assert_eq!(h.t(id).token, tok);
    assert_eq!(h.t(id).attempts, 1);
}

/// test_fencing.py::LeaseWindowTests::test_the_edge_is_the_constant_and_nothing_else (and the exact edge, a gap in v1).
#[test]
fn the_edge_is_the_constant_and_nothing_else() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("alice", "box-a");
    h.advance(LEASE_MINUTES - MINUTE);
    assert_eq!(h.lease("bob", "box-b"), None);
    h.advance(MINUTE); // exactly the window: strict `<` keeps it
    assert_eq!(h.lease("bob", "box-c"), None);
    assert_eq!(h.t(id).state, TState::Leased);
    h.advance(1);
    assert_eq!(h.lease("bob", "box-d"), Some(id));
}

/// tests.py::LeaseReclaimTests::test_recent_heartbeat_prevents_expired_assignment_reclaim
#[test]
fn recent_heartbeat_prevents_expired_assignment_reclaim() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("alice", "box-a");
    for _ in 0..40 {
        h.advance(3 * MINUTE);
        let tok = h.t(id).token.clone();
        let r = HeartbeatReq { now: h.now, account: "alice".into(), machine: "box-a".into(), task: Some(id), token: tok, ..Default::default() };
        assert!(matches!(h.apply(Input::Heartbeat(r)).output, Output::Heartbeat(HeartbeatOut::Ok { .. })));
    }
    // Leased two hours ago, heartbeat fresh: nobody takes it.
    assert_eq!(h.lease("bob", "box-b"), None);
    assert_eq!(h.t(id).machine, "box-a");
}

/// tests.py::LeaseReclaimTests::test_stale_assignment_and_stale_heartbeat_are_reclaimed
#[test]
fn stale_assignment_and_stale_heartbeat_are_reclaimed() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("alice", "box-a");
    h.advance(2 * HOUR);
    assert_eq!(h.lease("bob", "box-b"), Some(id));
}

/// test_lease_release.py::SharedRecyclerTests::test_the_analysis_recycler_leaves_live_leases_alone
#[test]
fn the_analysis_recycler_leaves_live_leases_alone() {
    let mut h = H::new();
    let id = h.auto(B128);
    let other = h.auto(B128);
    h.lease("alice", "box-a");
    h.advance(MINUTE);
    assert_eq!(h.lease("bob", "box-b"), Some(other));
    assert_eq!(h.t(id).state, TState::Leased);
}

/// test_fencing.py::DeadLeaseOnClosedPositionTests::test_a_dead_lease_on_a_closed_position_is_absorbed_not_requeued
#[test]
fn a_dead_lease_on_a_closed_position_is_absorbed_not_requeued() {
    let mut h = H::new();
    let closed = h.auto(B128);
    let open = h.auto(B128);
    h.lease("alice", "box-a");
    h.lease("alice", "box-b");
    let p = h.t(closed).pos;
    h.env.close(p, Status::WhiteWin);
    h.advance(LEASE_MINUTES + 5 * MINUTE);
    h.lease("carol", "box-c");
    let c = h.t(closed);
    assert_eq!(c.state, TState::Completed);
    assert_eq!(c.nodes_searched, 0);
    assert_eq!(c.machine, "");
    assert!(c.is_absorbed());
    // The open one was recycled and handed to carol.
    assert_eq!(h.t(open).state, TState::Leased);
    assert_eq!(h.t(open).machine, "box-c");
}

/// test_ingest_queue.py::ShadowBackfillTests::test_a_tombstoned_position_is_not_a_zombie_and_is_still_served
#[test]
fn a_tombstoned_position_is_not_a_zombie_and_is_still_served() {
    let mut h = H::new();
    let id = h.auto(B128);
    let p = h.t(id).pos;
    h.q.import_dead(p);
    assert_eq!(h.lease("alice", "box-a"), Some(id));
}

// ------------------------------------------------------------ one slot, one lease

/// tests.py::LeaseReclaimTests::test_same_machine_does_not_steal_healthy_lease
/// test_worker_jobs.py::LeaseConcurrencyTests::test_one_slot_still_gets_only_one_task_at_a_time
#[test]
fn same_machine_does_not_steal_healthy_lease() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.auto(B128);
    let r = h.lease_req("alice", "box");
    h.lease_out(r);
    let tok = h.t(id).token.clone();
    let mut again = h.lease_req("alice", "box");
    again.session = "another-session".into();
    assert_eq!(h.lease_out(again), LeaseOut::Busy { held: id });
    assert_eq!(h.t(id).attempts, 1);
    assert_eq!(h.t(id).token, tok);
    assert_eq!(h.q.ping("box", "alice").unwrap().current_task, Some(id));
}

/// tests.py::LeaseReclaimTests::test_same_session_replays_lost_lease_response_idempotently
/// test_worker_jobs.py::LeaseConcurrencyTests::test_a_lost_response_still_replays_within_its_slot
#[test]
fn same_session_replays_lost_lease_response_idempotently() {
    let mut h = H::new();
    let id = h.auto(B128);
    let r = h.lease_req("alice", "box#0");
    let session = r.session.clone();
    let LeaseOut::Task { grant } = h.lease_out(r) else { panic!() };
    let before = h.t(id).clone();
    h.advance(10 * SECOND);
    let mut again = h.lease_req("alice", "box#0");
    again.session = session;
    let LeaseOut::Task { grant: g2 } = h.lease_out(again) else { panic!() };
    assert!(g2.replayed);
    assert_eq!(g2.id, grant.id);
    assert_eq!(g2.token, grant.token);
    assert_eq!(h.t(id), &before);
}

/// tests.py::LeaseReclaimTests::test_same_machine_recycles_only_stale_lease
#[test]
fn same_machine_recycles_only_stale_lease() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("alice", "box");
    let tok = h.t(id).token.clone();
    h.advance(2 * HOUR);
    assert_eq!(h.lease("alice", "box"), Some(id));
    assert_eq!(h.t(id).attempts, 2);
    assert_ne!(h.t(id).token, tok);
}

/// test_worker_jobs.py::LeaseConcurrencyTests::test_a_restarted_worker_reclaims_its_dead_predecessors_lease
/// test_fencing.py::LeaseWindowTests::test_a_restarted_worker_is_relieved_before_a_stranger_is
#[test]
fn a_restarted_worker_reclaims_its_dead_predecessors_lease() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("alice", "box#3");
    let tok = h.t(id).token.clone();
    h.advance(RESTART_RECYCLE_MINUTES + SECOND);
    // A stranger cannot take it yet (inside the 4-minute window)...
    assert_eq!(h.lease("bob", "other"), None);
    // ...but the same slot with a new session is relieved and gets it again.
    assert_eq!(h.lease("alice", "box#3"), Some(id));
    assert_eq!(h.t(id).attempts, 2);
    assert_ne!(h.t(id).token, tok);
}

/// test_worker_jobs.py::LeaseConcurrencyTests::test_two_slots_of_one_machine_hold_leases_at_the_same_time
#[test]
fn two_slots_of_one_machine_hold_leases_at_the_same_time() {
    let mut h = H::new();
    let a = h.auto(B128);
    let b = h.auto(B128);
    assert_eq!(h.lease("alice", "box#0"), Some(a));
    assert_eq!(h.lease("alice", "box#1"), Some(b));
    assert_eq!(h.t(a).machine, "box#0");
    assert_eq!(h.t(b).machine, "box#1");
}

/// test_worker_jobs.py::LeaseConcurrencyTests::test_the_cap_is_per_physical_machine
/// ::test_a_capped_machine_is_told_nothing_rather_than_failing ::test_the_cap_does_not_penalise_a_different_machine
#[test]
fn the_cap_is_per_physical_machine() {
    let mut h = H::new();
    for _ in 0..40 {
        h.auto(B128);
    }
    for k in 0..LEASES_PER_MACHINE {
        assert!(h.lease("alice", &format!("box#{k}")).is_some());
    }
    let r = h.lease_req("alice", "box#99");
    assert_eq!(h.lease_out(r), LeaseOut::Empty);
    let r = h.lease_req("alice", "box");
    assert_eq!(h.lease_out(r), LeaseOut::Empty);
    assert!(h.lease("bob", "box2#0").is_some(), "a different machine is not capped");
}

/// test_worker_jobs.py::MachineIdentityTests::test_a_slot_resolves_to_its_physical_machine
/// ::test_a_lookalike_machine_is_not_the_same_machine
#[test]
fn machine_identity() {
    assert_eq!(machine_base("box#3"), "box");
    assert_eq!(machine_base("box"), "box");
    assert_eq!(machine_base(""), "");
    let mut h = H::new();
    for _ in 0..LEASES_PER_MACHINE + 1 {
        h.auto(B128);
    }
    for k in 0..LEASES_PER_MACHINE {
        assert!(h.lease("alice", &format!("box-atomicdb#{k}")).is_some());
    }
    assert!(h.lease("alice", "box-atomicdb2#0").is_some());
}

/// test_worker_jobs.py::LeaseConcurrencyTests::test_a_recycled_lease_frees_a_slot_of_the_cap
#[test]
fn a_recycled_lease_frees_a_slot_of_the_cap() {
    let mut h = H::new();
    for _ in 0..LEASES_PER_MACHINE + 1 {
        h.auto(B128);
    }
    for k in 0..LEASES_PER_MACHINE {
        h.lease("alice", &format!("box#{k}"));
    }
    h.advance(HOUR);
    assert!(h.lease("alice", "box#99").is_some());
}

// ------------------------------------------------------------ D8

/// Owner decision D8 (replaces tests.py::LeaseReclaimTests::test_recycled_task_waits_for_token_capable_worker).
#[test]
fn a_build_without_lease_tokens_is_refused() {
    let mut h = H::new();
    h.auto(B128);
    let mut r = h.lease_req("old", "relic");
    r.build = 2026072202;
    assert_eq!(h.lease_out(r), LeaseOut::UpgradeRequired);
    assert!(h.q.ping("relic", "old").is_none());
    let mut r = h.lease_req("old", "relic");
    r.build = 0;
    assert_eq!(h.lease_out(r), LeaseOut::UpgradeRequired);
}

// ------------------------------------------------------------ max_budget

/// test_max_budget.py::CappedLeaseTests::test_the_cap_is_inclusive / test_a_capped_lease_skips_the_larger_task_ahead
/// / test_the_larger_task_keeps_its_place_for_the_others / test_with_only_larger_tasks_the_capped_worker_gets_none
#[test]
fn max_budget_rules() {
    let mut h = H::new();
    let big = h.user("x", B512);
    let small = h.user("x", B128);
    assert_eq!(h.lease_with("w", "capped", |r| r.max_budget = Some(B128)), Some(small));
    assert_eq!(h.lease("w", "free"), Some(big));
    let mut h = H::new();
    let exact = h.auto(B512);
    assert_eq!(h.lease_with("w", "m", |r| r.max_budget = Some(B512)), Some(exact));
    let mut h = H::new();
    let only = h.auto(B512);
    assert_eq!(h.lease_with("w", "m", |r| r.max_budget = Some(B128)), None);
    assert_eq!(h.t(only).state, TState::Pending);
    assert_eq!(h.t(only).attempts, 0);
    assert_eq!(h.t(only).machine, "");
}

/// test_max_budget.py::CappedLeaseTests::test_nothing_above_the_cap_is_ever_returned
#[test]
fn nothing_above_the_cap_is_ever_returned() {
    let mut h = H::new();
    for b in [8_000_000, 32_000_000, B128, B512, B2, B10] {
        h.auto(b);
    }
    let mut got = Vec::new();
    for i in 0..6 {
        if let Some(id) = h.lease_with("w", &format!("m{i}"), |r| r.max_budget = Some(B128)) {
            got.push(h.t(id).budget);
        }
    }
    got.sort();
    assert_eq!(got, vec![8_000_000, 32_000_000, B128]);
    assert_eq!(h.q.pending_count(), 3);
}

// ------------------------------------------------------------ routing gates

/// tests.py::TbRoutingTests::test_tb_task_waits_for_tb_worker / test_tb_task_served_when_nothing_else
/// / test_non_tb_worker_scans_past_more_than_four_tb_tasks
#[test]
fn tablebase_routing() {
    let mut h = H::new();
    let mut tbs = Vec::new();
    for _ in 0..5 {
        let id = h.user("x", B128);
        let p = h.t(id).pos;
        h.env.info(p).tb_applicable = true;
        tbs.push(id);
    }
    let normal = h.auto(B128);
    assert_eq!(h.lease("w", "no-tb"), Some(normal));
    assert_eq!(h.lease_with("w", "tb", |r| r.tb = true), Some(tbs[0]));
    // Only TB rows left: the first one is the fallback.
    assert_eq!(h.lease("w", "no-tb2"), Some(tbs[1]));
}

/// test_tb_on_pick.py::TablesSettleQueuedPositionsTests::test_a_covered_position_is_served_to_no_worker
/// ::test_the_lease_goes_on_to_the_next_task
#[test]
fn positions_the_server_tables_cover_are_never_served() {
    let mut h = H::new();
    let covered = h.user("x", B128);
    let p = h.t(covered).pos;
    h.env.tables.insert(p);
    let next = h.auto(B128);
    assert_eq!(h.lease("w", "m"), Some(next));
    assert_eq!(h.lease("w", "m2"), None);
    assert_eq!(h.t(covered).state, TState::Pending);
}

/// test_idle_repair.py::LeaseRoutingTests (narrow slot / wide slot / jobs slot / no threads / visitor repair)
/// test_idle_repair.py::ShallowRuleTests::test_only_a_server_picked_repair_is_routed
#[test]
fn idle_repair_slot_routing() {
    let mut h = H::new();
    let repair = h.row(B128, |t| t.arm = ARM_REPAIR.into());
    let other = h.auto(B128);
    let pp = h.t(repair).pos; h.env.priority.insert(pp, 10.0);
    assert_eq!(h.lease_with("w", "wide", |r| r.telemetry.threads = 32), Some(other));
    assert_eq!(h.t(repair).state, TState::Pending);
    assert_eq!(h.lease_with("w", "none", |r| r.telemetry.threads = 0), None);
    assert_eq!(h.lease_with("w", "box#2", |r| r.telemetry.threads = 8), Some(repair));
    let mut t = Task::new(1, 1, 0, 1, 0);
    t.arm = ARM_REPAIR.into();
    t.source = Source::User;
    assert!(lease::fits_slot(&t, 64));
    t.source = Source::Auto;
    assert!(!lease::fits_slot(&t, 64));
    assert!(lease::fits_slot(&t, 1));
    t.arm = ARM_COVERAGE.into();
    assert!(lease::fits_slot(&t, 64));
}

/// tests.py::RequestTests::test_request_during_shallow_lease_preserves_deep_follow_up (lease side)
/// tests.py::RequestTests::test_deep_follow_up_is_not_run_after_shallow_visit_solves_position
#[test]
fn a_follow_up_waits_for_its_visit() {
    let mut h = H::new();
    let shallow = h.auto(8_000_000);
    let p = h.t(shallow).pos;
    let follow = h.row(B128, |t| {
        t.pos = p;
        t.generation = 1;
        t.source = Source::User;
        t.requested_by = "x".into();
    });
    assert_eq!(h.lease("w", "a"), Some(shallow));
    assert_eq!(h.lease("w", "b"), None, "generation 1 waits for visit 1");
    h.complete(shallow);
    h.env.info(p).visits = 1;
    assert_eq!(h.lease("w", "b"), Some(follow));
    // Closed by the shallow visit: never run.
    let mut h2 = H::new();
    let s = h2.auto(8_000_000);
    let p = h2.t(s).pos;
    h2.row(B128, |t| {
        t.pos = p;
        t.generation = 1;
        t.source = Source::User;
    });
    h2.lease("w", "a");
    h2.complete(s);
    h2.env.info(p).visits = 1;
    h2.env.close(p, Status::WhiteWin);
    assert_eq!(h2.lease("w", "b"), None);
}

// ------------------------------------------------------------ deep cap

fn live_slots(h: &mut H, n: usize) {
    for i in 0..n {
        h.ping("hog", &format!("live{i}"));
    }
}

/// test_queue_fairness.py::DeepLeaseCapTests::test_one_account_cannot_hold_more_deep_leases_than_its_share
/// ::test_the_rest_are_served_as_soon_as_a_deep_lease_frees_up ::test_without_the_cap_that_same_slot_would_have_gone_deep
#[test]
fn deep_lease_cap() {
    // The hog's own worker (own first) would take its deep rows before the
    // other account's; the slots are the 9 live ones, so the pool stays 9.
    let mut h = H::new();
    live_slots(&mut h, 9);
    let deep: Vec<u64> = (0..4).map(|_| h.user("hog", B10)).collect();
    let cheap = h.user("other", B128);
    for i in 0..3 {
        assert_eq!(h.lease("hog", &format!("live{i}")), Some(deep[i]));
    }
    // Fourth slot: the hog holds 3 = 9 // 3 deep leases, so the other account goes.
    assert_eq!(h.lease("hog", "live3"), Some(cheap));
    // One deep lease frees up: the waiting deep task is next.
    h.complete(deep[0]);
    assert_eq!(h.lease("hog", "live4"), Some(deep[3]));

    let mut h = H::new();
    live_slots(&mut h, 90);
    let deep: Vec<u64> = (0..4).map(|_| h.user("hog", B10)).collect();
    h.user("other", B128);
    for i in 0..4 {
        assert_eq!(h.lease("hog", &format!("live{i}")), Some(deep[i]));
    }
}

/// test_queue_fairness.py::DeepLeaseCapTests::test_no_slot_is_left_idle_when_only_deep_work_remains
/// ::test_the_cheap_rungs_are_not_capped ::test_the_cap_follows_the_pool_and_never_drops_below_one
#[test]
fn deep_cap_never_idles_a_slot() {
    let mut h = H::new();
    live_slots(&mut h, 9);
    for _ in 0..9 {
        h.user("hog", B10);
    }
    assert_eq!(h.serve("w", 9).len(), 9);
    let mut h = H::new();
    live_slots(&mut h, 9);
    for _ in 0..9 {
        h.user("hog", B2);
    }
    assert_eq!(h.serve("w", 9).len(), 9);
    let h = H::new();
    assert!(h.q.deep_capped_requesters(h.now).is_empty());
}

// ------------------------------------------------------------ pass sequence and refill

struct OneShot(Vec<u64>);
impl Refill for OneShot {
    fn next_tasks(&mut self, _q: &QueueState, env: &dyn QueueEnv, n: usize, _now: Ms) -> MintPlan {
        let tasks = self
            .0
            .drain(..)
            .take(n)
            .map(|pos| MintTask { pos, budget: B128, multipv: multipv_for_test(env.visits(pos)), source: Source::Auto, arm: String::new() })
            .collect();
        MintPlan { tasks, ..Default::default() }
    }
}
fn multipv_for_test(visits: i32) -> u32 {
    av2_queue::budget::multipv_for(visits, Some(B128), false, None)
}

/// test_idle_pool_budget.py::EmptyQueueLeaseTests::test_the_task_a_worker_gets_from_an_empty_queue_is_128m
/// test_max_budget.py::RefillTests (uncapped / capped at / capped below the server floor)
#[test]
fn refill_from_an_empty_queue() {
    let mut h = H::new();
    let p = h.pos();
    h.refill = Box::new(OneShot(vec![p]));
    let id = h.lease("w", "m").unwrap();
    assert_eq!(h.t(id).budget, B128);
    assert_eq!(h.t(id).source, Source::Auto);
    assert_eq!(h.t(id).state, TState::Leased);

    let mut h = H::new();
    let p = h.pos();
    h.refill = Box::new(OneShot(vec![p]));
    assert!(h.lease_with("w", "m", |r| r.max_budget = Some(SERVER_PICKED_MIN_NODES)).is_some());

    let mut h = H::new();
    let p = h.pos();
    h.refill = Box::new(OneShot(vec![p]));
    assert!(h.lease_with("w", "m", |r| r.max_budget = Some(SERVER_PICKED_MIN_NODES - 1)).is_none());
    assert_eq!(h.q.retained_count(), 0, "a worker capped below the floor does not refill");
}

// ------------------------------------------------------------ searchmoves

/// tests.py::SearchmovesTests::test_lease_restricts_to_unsolved_moves / test_no_restriction_when_nothing_solved
#[test]
fn searchmoves_restriction() {
    let mut h = H::new();
    let p = h.pos();
    h.env.info(p).expanded = true;
    h.env.edge(p, "e2e4", 100);
    h.env.edge(p, "d2d4", 101);
    h.env.edge(p, "g1f3", 102);
    assert!(lease::searchmoves(&h.env, p).is_empty());
    h.env.close(101, Status::WhiteWin);
    assert_eq!(lease::searchmoves(&h.env, p), vec!["e2e4".to_string(), "g1f3".to_string()]);
    h.env.close(100, Status::WhiteWin);
    h.env.close(102, Status::Draw);
    assert!(lease::searchmoves(&h.env, p).is_empty(), "everything solved: no restriction");
    h.env.info(p).expanded = false;
    assert!(lease::searchmoves(&h.env, p).is_empty());
}

// ------------------------------------------------------------ sweep share

/// test_sweep.py::SweepShareLeaseTests::test_a_share_of_one_serves_the_sweep_before_a_request
/// ::test_a_share_of_zero_keeps_the_old_order ::test_with_the_sweep_empty_the_request_is_served
/// test_sweep.py::SweepServiceOrderTests::test_it_goes_before_the_selectors_own_work
#[test]
fn sweep_share() {
    let mut h = H::new();
    h.config(|c| c.sweep16_share = 1.0);
    let req = h.user("x", B128);
    let sweep = h.row(B128, |t| {
        t.source = Source::Fill;
        t.arm = ARM_SWEEP16.into();
    });
    assert_eq!(h.lease("w", "a"), Some(sweep));
    assert_eq!(h.lease("w", "b"), Some(req));

    let mut h = H::new();
    h.config(|c| c.sweep16_share = 0.0);
    let req = h.user("x", B128);
    let sweep = h.row(B128, |t| {
        t.source = Source::Fill;
        t.arm = ARM_SWEEP16.into();
    });
    let auto = h.auto(B128);
    let pp = h.t(auto).pos; h.env.priority.insert(pp, 50.0);
    assert_eq!(h.lease("w", "a"), Some(req));
    assert_eq!(h.lease("w", "b"), Some(sweep), "FILL before AUTO");
    assert_eq!(h.lease("w", "c"), Some(auto));
}

/// test_sweep.py::SweepShareLeaseTests::test_over_many_leases_the_share_is_kept
#[test]
fn over_many_leases_the_share_is_kept() {
    let mut h = H::new();
    h.config(|c| c.sweep16_share = 0.25);
    for _ in 0..40 {
        h.user("x", B128);
        h.row(B128, |t| {
            t.source = Source::Fill;
            t.arm = ARM_SWEEP16.into();
        });
    }
    let got = h.drain("w", 40);
    let sweeps = got.iter().filter(|id| h.t(**id).arm == ARM_SWEEP16).count();
    assert_eq!(sweeps, 10);
}

/// test_sweep.py::SweepShareLeaseTests::test_a_closed_position_is_not_served_by_the_share
#[test]
fn a_closed_position_is_not_served_by_the_share() {
    let mut h = H::new();
    h.config(|c| c.sweep16_share = 1.0);
    let req = h.user("x", B128);
    let sweep = h.row(B128, |t| {
        t.source = Source::Fill;
        t.arm = ARM_SWEEP16.into();
    });
    let p = h.t(sweep).pos;
    h.env.close(p, Status::BlackWin);
    assert_eq!(h.lease("w", "a"), Some(req));
}

// ------------------------------------------------------------ worker ping

/// tests.py::LeaseReclaimTests::test_lease_stores_the_worker_build / test_request_without_build_keeps_the_stored_build
/// tests.py::MachineVisibilityTests::test_reported_threads_are_capped_at_what_the_engine_can_use
#[test]
fn lease_stores_the_worker_build() {
    let mut h = H::new();
    h.lease_with("w", "m", |r| r.telemetry.worker_build = Some(2026111704));
    assert_eq!(h.q.ping("m", "w").unwrap().worker_build, Some(2026111704));
    h.lease_with("w", "m", |r| r.telemetry.worker_build = None);
    assert_eq!(h.q.ping("m", "w").unwrap().worker_build, Some(2026111704));
    h.lease_with("w", "m", |r| r.telemetry.threads = 4096);
    assert_eq!(h.q.ping("m", "w").unwrap().threads, MAX_REPORTED_THREADS);
    assert_eq!(h.q.ping("m", "w").unwrap().current_task, None);
}
