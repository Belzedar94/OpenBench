//! Heartbeat, submit, release and the engine gate (Q-2.30..Q-2.51), and the
//! proof leases (Q-2.52..Q-2.61, SOL-5).

mod common;
use av2_model::Status;
use av2_queue::consts::*;
use av2_queue::*;
use common::*;

fn hb(h: &mut H, id: u64, machine: &str, token: &str) -> HeartbeatOut {
    let r = HeartbeatReq {
        now: h.now,
        account: "w".into(),
        machine: machine.into(),
        task: Some(id),
        token: token.into(),
        nps: 1234,
        ..Default::default()
    };
    match h.apply(Input::Heartbeat(r)).output {
        Output::Heartbeat(o) => o,
        o => panic!("{o:?}"),
    }
}

/// tests.py::MachineVisibilityTests::test_heartbeat_tracks_current_task_and_keeps_original_lease_time
#[test]
fn heartbeat_tracks_current_task_and_keeps_original_lease_time() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("w", "m");
    let leased_at = h.t(id).leased_at;
    h.advance(MINUTE);
    let tok = h.t(id).token.clone();
    assert_eq!(hb(&mut h, id, "m", &tok), HeartbeatOut::Ok { machine: "m".into(), current: Some(id) });
    assert_eq!(h.t(id).leased_at, leased_at);
    assert_eq!(h.t(id).heartbeat_at, Some(h.now));
    let p = h.q.ping("m", "w").unwrap();
    assert_eq!(p.current_task, Some(id));
    assert_eq!(p.last_nps, 1234);
    // Idle heartbeat clears the live view.
    let r = HeartbeatReq { now: h.now, account: "w".into(), machine: "m".into(), nps: 99, ..Default::default() };
    h.apply(Input::Heartbeat(r));
    let p = h.q.ping("m", "w").unwrap();
    assert_eq!((p.current_task, p.last_nps, p.nps_updated), (None, 0, None));
}

/// tests.py::LeaseReclaimTests::test_assignment_token_fences_stale_same_machine_process
#[test]
fn assignment_token_fences_stale_same_machine_process() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("w", "m");
    let old = h.t(id).token.clone();
    h.advance(2 * HOUR);
    h.lease("w", "m");
    assert_ne!(h.t(id).token, old);
    assert_eq!(hb(&mut h, id, "m", &old), HeartbeatOut::StaleLease);
    let mut r = h.submit_req(id);
    r.token = old;
    assert_eq!(h.submit(r), SubmitOut::StaleLease);
    // Another machine with the right token is not the holder either.
    let tok = h.t(id).token.clone();
    assert_eq!(hb(&mut h, id, "other", &tok), HeartbeatOut::StaleLease);
    // A completed task cannot be heartbeated.
    h.complete(id);
    assert_eq!(hb(&mut h, id, "m", &tok), HeartbeatOut::StaleLease);
}

/// test_fencing.py::SubmitFencingTests::test_pending_task_is_rejected / test_other_machine_is_rejected
/// test_ingest_queue.py::SubmitEnqueueTests::test_the_definitive_rejections_are_unchanged
#[test]
fn submit_fencing() {
    let mut h = H::new();
    let pending = h.auto(B128);
    let mut r = h.submit_req(pending);
    r.machine = "m".into();
    assert_eq!(h.submit(r.clone()), SubmitOut::NotLeased);
    r.task = 999;
    assert_eq!(h.submit(r), SubmitOut::UnknownTask);
    let id = h.auto(B128);
    h.lease("w", "m");
    let leased = if h.t(pending).state == TState::Leased { pending } else { id };
    let mut r = h.submit_req(leased);
    r.machine = "other".into();
    assert_eq!(h.submit(r.clone()), SubmitOut::NotYourLease);
    r.machine = String::new();
    assert_eq!(h.submit(r), SubmitOut::NotYourLease, "the default machine of a submit is ''");
    // A cancelled task is not leased.
    let c = h.user("a", B128);
    h.status(Input::Withdraw { now: h.now, account: "a".into(), task: c });
    let mut r = h.submit_req(c);
    r.machine = "m".into();
    assert_eq!(h.submit(r), SubmitOut::NotLeased);
}

/// test_fencing.py::SubmitFencingTests::test_duplicate_submit_is_idempotent
/// test_ingest_queue.py::SubmitEnqueueTests::test_a_replayed_submit_is_still_a_duplicate
#[test]
fn duplicate_submit_is_idempotent() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("w", "m");
    let r = h.submit_req(id);
    assert!(matches!(h.submit(r.clone()), SubmitOut::Accepted { .. }));
    let before = h.q.digest();
    let applied = h.apply(Input::Submit(r));
    assert_eq!(applied.output, Output::Submit(SubmitOut::Dup));
    assert!(applied.deltas.is_empty());
    assert_eq!(h.q.digest(), before);
}

/// test_fencing.py::SubmitFencingTests::test_reported_nodes_are_clamped_to_twice_budget
/// ::test_zero_reported_nodes_remain_zero; tests.py::NodesAccountingTests::test_actual_nodes_recorded_not_budget
/// test_lanes.py::DeliveryEvidenceTests::test_submitting_a_result_stamps_one
/// tests.py::MachineVisibilityTests::test_lease_and_submit_update_ping
#[test]
fn submit_completion_fields() {
    let mut h = H::new();
    let id = h.auto(1000);
    h.lease("w", "m");
    let mut r = h.submit_req(id);
    r.nodes = 999_999;
    r.account = "delivering".into();
    let SubmitOut::Accepted { claim } = h.submit(r) else { panic!() };
    assert_eq!(claim.nodes, 2000);
    let t = h.t(id);
    assert_eq!((t.state, t.nodes_searched, t.delivered_by.as_str(), t.machine.as_str()), (TState::Completed, 2000, "delivering", "m"));
    assert!(!t.token.is_empty(), "lease columns stay after completion");
    // No ping of (m, delivering): the submit does not create one.
    assert!(h.q.ping("m", "delivering").is_none());

    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("w", "m");
    let mut r = h.submit_req(id);
    r.nodes = 1000;
    r.elapsed = 2.5;
    h.submit(r);
    let p = h.q.ping("m", "w").unwrap();
    assert_eq!((p.tasks_done, p.last_nps, p.current_task), (1, 400, None));
    assert_eq!(p.last_result_at, Some(h.now));
    assert_eq!(h.t(id).nodes_searched, 1000);
    assert_eq!(h.t(id).elapsed, 2.5);

    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("w", "m");
    let mut r = h.submit_req(id);
    r.nodes = 0;
    r.elapsed = 0.0;
    h.submit(r);
    assert_eq!(h.t(id).nodes_searched, 0);
    assert!(!h.t(id).is_absorbed(), "a zero-node delivery keeps its machine");
    assert_eq!(h.q.ping("m", "w").unwrap().last_nps, 0);
}

/// test_engine_policy.py::EngineGateTests::test_a_foreign_engine_is_refused_and_the_task_goes_back
/// ::test_another_machine_cannot_push_a_lease_back_with_a_bad_sha (+ the absorbed case, a v1 gap)
#[test]
fn engine_gate_at_submit() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("w", "m");
    let mut r = h.submit_req(id);
    r.gate_rejects = true;
    let mut stranger = r.clone();
    stranger.machine = "evil".into();
    assert_eq!(h.submit(stranger), SubmitOut::NotYourLease);
    assert_eq!(h.t(id).state, TState::Leased);
    assert_eq!(h.submit(r), SubmitOut::EngineRefused);
    let t = h.t(id);
    assert_eq!((t.state, t.machine.as_str(), t.token.as_str(), t.attempts), (TState::Pending, "", "", 1));
    assert!(t.leased_at.is_some());
    assert_eq!(h.q.ping("m", "w").unwrap().current_task, None);

    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("w", "m");
    let p = h.t(id).pos;
    h.env.close(p, Status::WhiteWin);
    let mut r = h.submit_req(id);
    r.gate_rejects = true;
    assert_eq!(h.submit(r), SubmitOut::EngineRefused);
    assert!(h.t(id).is_absorbed());
}

// ------------------------------------------------------------ release

fn release(h: &mut H, machine: &str, entries: Vec<ReleaseEntry>) -> usize {
    let r = ReleaseReq { now: h.now, account: "w".into(), machine: machine.into(), entries };
    match h.apply(Input::Release(r)).output {
        Output::Release { released } => released,
        o => panic!("{o:?}"),
    }
}

fn entry(h: &H, id: u64) -> ReleaseEntry {
    let t = h.t(id);
    ReleaseEntry { kind: LeaseKind::Analysis, task: id, token: t.token.clone(), owner: t.machine.clone() }
}

/// test_lease_release.py::ReleaseOwnLeasesTests::test_a_released_lease_is_back_in_the_queue_at_once
/// ::test_the_row_is_left_exactly_as_the_recycler_leaves_an_expired_one ::test_releasing_twice_changes_nothing_the_second_time
/// ::test_a_released_task_goes_to_the_next_worker_that_asks ::test_a_late_submit_of_a_released_task_is_refused
#[test]
fn release_own_leases() {
    let mut h = H::new();
    let a = h.auto(B128);
    let b = h.auto(B128);
    h.lease("w", "box#0");
    h.lease("w", "box#1");
    h.advance(LEASE_MINUTES + MINUTE);
    // b expires through the recycler; a is released by hand.
    let e = entry(&h, a);
    let late = h.submit_req(a);
    assert_eq!(release(&mut h, "box#5", vec![e.clone()]), 1);
    let ta = h.t(a).clone();
    assert_eq!(
        (ta.state, ta.machine.as_str(), ta.token.as_str(), ta.session.as_str(), ta.heartbeat_at, ta.attempts),
        (TState::Pending, "", "", "", None, 1)
    );
    assert!(ta.leased_at.is_some());
    // The recycler leaves b exactly in the same shape (a lease call runs it;
    // the slot below has nothing else to take than a and b).
    let mut r = h.lease_req("w", "probe");
    r.max_budget = Some(1);
    h.lease_out(r);
    let tb = h.t(b).clone();
    assert_eq!(
        (tb.state, tb.machine.as_str(), tb.token.as_str(), tb.session.as_str(), tb.heartbeat_at, tb.attempts),
        (TState::Pending, "", "", "", None, 1)
    );
    assert_eq!(release(&mut h, "box#5", vec![e]), 0, "releasing twice changes nothing");
    assert_eq!(h.submit(late), SubmitOut::NotLeased);
    assert_eq!(h.lease("w", "fresh"), Some(a));
    assert_eq!(h.t(a).attempts, 2);
}

/// test_lease_release.py::ReleaseRefusalTests::test_another_machines_lease_is_left_alone / test_a_lookalike_machine_is_another_machine
/// ::test_a_wrong_or_stale_token_releases_nothing / test_a_tokenless_lease_is_never_released / test_finished_and_waiting_tasks_are_left_alone
#[test]
fn release_refusals() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("w", "box-atomicdb#0");
    let mut e = entry(&h, id);
    assert_eq!(release(&mut h, "box-atomicdb2", vec![e.clone()]), 0, "lookalike machine");
    assert_eq!(release(&mut h, "other", vec![e.clone()]), 0, "another machine");
    e.token = "nope".into();
    assert_eq!(release(&mut h, "box-atomicdb", vec![e.clone()]), 0);
    e.token = String::new();
    assert_eq!(release(&mut h, "box-atomicdb", vec![e]), 0, "a tokenless entry never releases");
    let good = entry(&h, id);
    assert_eq!(release(&mut h, "", vec![good]), 0, "an empty caller machine releases nothing");
    assert_eq!(h.t(id).state, TState::Leased);
    let p = h.auto(B128);
    let e = ReleaseEntry { kind: LeaseKind::Analysis, task: p, token: "x".into(), owner: "box-atomicdb#0".into() };
    assert_eq!(release(&mut h, "box-atomicdb", vec![e]), 0);
}

/// test_lease_release.py::ReleaseOwnLeasesTests::test_a_task_on_a_closed_position_is_absorbed_as_the_recycler_does
/// ::test_the_machine_stops_showing_the_task_it_gave_back
#[test]
fn release_absorbs_on_closed_positions() {
    let mut h = H::new();
    let id = h.auto(B128);
    h.lease("w", "m");
    let p = h.t(id).pos;
    h.env.close(p, Status::BlackWin);
    let e = entry(&h, id);
    assert_eq!(release(&mut h, "m", vec![e]), 1);
    assert!(h.t(id).is_absorbed());
    assert_eq!(h.q.ping("m", "w").unwrap().current_task, None);
}

// ------------------------------------------------------------ proof leases

fn solve_task(h: &mut H, budget: u64, arm: &str) -> u64 {
    let p = h.pos();
    let id = h.q.solves().map(|s| s.id).max().unwrap_or(0) + 1;
    h.q.import_solve(SolveTask::new(id, p, "WHITE_WIN", "F0", budget, arm, h.now));
    id
}

fn acquire(h: &mut H, machine: &str, session: &str, max: Option<u64>) -> Option<SolveGrant> {
    let token = h.token();
    let r = SolveAcquireReq {
        now: h.now,
        account: "s".into(),
        machine: machine.into(),
        session: session.into(),
        token,
        max_budget: max,
        ..Default::default()
    };
    match h.apply(Input::SolveAcquire(r)).output {
        Output::SolveAcquire { task } => task,
        o => panic!("{o:?}"),
    }
}

fn solve_submit(h: &mut H, id: u64, machine: &str, token: &str, outcome: &str, verified: bool) -> SolveSubmitOut {
    let r = SolveSubmitReq {
        now: h.now,
        account: "s".into(),
        machine: machine.into(),
        task: id,
        token: token.into(),
        outcome: outcome.into(),
        verdict: SolveVerdict { verified, reason: if verified { String::new() } else { "bad certificate".into() } },
        ..Default::default()
    };
    match h.apply(Input::SolveSubmit(r)).output {
        Output::SolveSubmit(o) => o,
        o => panic!("{o:?}"),
    }
}

/// test_solve.py::SolveProtocolTests::test_the_same_session_replays_its_assignment
/// ::test_acquire_leases_and_submit_closes ::test_a_second_submit_is_a_duplicate_not_a_reclosure
/// ::test_a_stale_token_cannot_submit ::test_another_machine_cannot_submit ::test_heartbeat_keeps_the_lease_and_rejects_a_stranger
#[test]
fn solve_protocol() {
    let mut h = H::new();
    let id = solve_task(&mut h, 10_000_000, "");
    let g = acquire(&mut h, "solver", "sess", None).unwrap();
    assert_eq!((g.id, g.goal.as_str(), g.stage.as_str()), (id, "WHITE_WIN", "F0"));
    let g2 = acquire(&mut h, "solver", "sess", None).unwrap();
    assert!(g2.replayed);
    assert_eq!(g2.token, g.token);
    assert_eq!(h.q.solve(id).unwrap().attempts, 1);
    assert!(acquire(&mut h, "solver", "other-session", None).is_none(), "one live proof lease per identity");
    let r = SolveHeartbeatReq {
        now: h.now,
        account: "s".into(),
        machine: "solver".into(),
        task: id,
        token: g.token.clone(),
        ..Default::default()
    };
    assert_eq!(h.apply(Input::SolveHeartbeat(r.clone())).output, Output::SolveHeartbeat { ok: true });
    let mut stranger = r;
    stranger.machine = "x".into();
    assert_eq!(h.apply(Input::SolveHeartbeat(stranger)).output, Output::SolveHeartbeat { ok: false });
    assert_eq!(solve_submit(&mut h, id, "solver", "stale", "PROVED", true), SolveSubmitOut::StaleLease);
    assert_eq!(solve_submit(&mut h, id, "other", &g.token, "PROVED", true), SolveSubmitOut::NotYourLease);
    let out = solve_submit(&mut h, id, "solver", &g.token, "PROVED", true);
    assert!(matches!(out, SolveSubmitOut::Completed { verified: true, .. }));
    assert_eq!(solve_submit(&mut h, id, "anyone", "", "PROVED", true), SolveSubmitOut::Dup);
}

/// Q-2.61: PROVED without a verifying certificate fails, with SOLVE_REJECTED.
#[test]
fn an_unverified_proof_fails() {
    let mut h = H::new();
    let id = solve_task(&mut h, 2_000_000, ARM_DEBT);
    let g = acquire(&mut h, "solver", "s", None).unwrap();
    let r = SolveSubmitReq {
        now: h.now,
        account: "s".into(),
        machine: "solver".into(),
        task: id,
        token: g.token.clone(),
        outcome: "PROVED".into(),
        verdict: SolveVerdict { verified: false, reason: "x".repeat(3000) },
        ..Default::default()
    };
    let applied = h.apply(Input::SolveSubmit(r));
    assert!(matches!(applied.output, Output::SolveSubmit(SolveSubmitOut::Rejected { .. })));
    assert_eq!(applied.events[0].kind, "SOLVE_REJECTED");
    let s = h.q.solve(id).unwrap();
    assert_eq!((s.state, s.reject_reason.len()), (SolveState::Failed, 2000));
    assert_eq!(solve_submit(&mut h, id, "solver", &g.token, "UNKNOWN", true), SolveSubmitOut::NotLeased);
}

/// test_engine_debt.py::DebtIsServedLastTests / test_max_budget.py::SolveCapTests
#[test]
fn solve_selection_order() {
    let mut h = H::new();
    let debt = solve_task(&mut h, 4_000_000_000, ARM_DEBT);
    let small = solve_task(&mut h, 2_000_000, "fragile");
    let mid = solve_task(&mut h, 64_000_000, "autosolve95");
    let huge = solve_task(&mut h, 4_000_000_000, "");
    assert_eq!(acquire(&mut h, "a", "", Some(B128)).unwrap().id, mid);
    assert_eq!(acquire(&mut h, "b", "", None).unwrap().id, huge);
    assert_eq!(acquire(&mut h, "c", "", None).unwrap().id, small);
    assert!(acquire(&mut h, "d", "", Some(B128)).is_none(), "only the 4B debt is left");
    assert_eq!(acquire(&mut h, "e", "", None).unwrap().id, debt);
}

/// test_engine_debt.py::RecycleStaleSolveLeasesTests::* (dead heartbeat, live lease, servable again, event)
/// test_bug_hunt_findings.py::SolveLeaseRestartTests::test_a_restarted_solver_is_relieved_like_an_analysis_worker
#[test]
fn solve_lease_recycling() {
    let mut h = H::new();
    let id = solve_task(&mut h, 2_000_000, ARM_DEBT);
    acquire(&mut h, "solver", "s1", None).unwrap();
    h.advance(10 * MINUTE);
    let tick = h.apply(Input::Tick { now: h.now });
    assert_eq!(tick.output, Output::Tick { recycled_solves: 0, pruned: 0 });
    assert!(tick.events.is_empty(), "a clean pass writes no event");
    h.advance(11 * MINUTE);
    let tick = h.apply(Input::Tick { now: h.now });
    assert_eq!(tick.output, Output::Tick { recycled_solves: 1, pruned: 0 });
    assert_eq!(tick.events[0].kind, "SOLVE_LEASE_RECYCLED");
    assert_eq!(tick.events[0].payload["minutes"], 20);
    assert_eq!(h.q.solve(id).unwrap().state, SolveState::Pending);
    assert_eq!(acquire(&mut h, "other", "", None).unwrap().id, id);
    // Restart relief: same identity, other session, holder silent > 3 minutes.
    h.advance(RESTART_RECYCLE_MINUTES + SECOND);
    let g = acquire(&mut h, "other", "new-process", None).unwrap();
    assert_eq!(g.id, id);
    assert_eq!(h.q.solve(id).unwrap().attempts, 3);
}

/// test_lease_release.py::ReleaseOwnLeasesTests::test_every_slot_and_the_solver_go_back_in_one_call
/// test_lease_release.py::SharedRecyclerTests::test_the_solve_recycler_and_a_release_leave_the_same_row
#[test]
fn every_slot_and_the_solver_go_back_in_one_call() {
    let mut h = H::new();
    let tasks: Vec<u64> = (0..3).map(|_| h.auto(B128)).collect();
    for k in 0..3 {
        h.lease("w", &format!("box#{k}"));
    }
    let sid = solve_task(&mut h, 2_000_000, "");
    let g = acquire(&mut h, "box#9", "", None).unwrap();
    let mut entries: Vec<ReleaseEntry> = tasks.iter().map(|id| entry(&h, *id)).collect();
    entries.push(ReleaseEntry { kind: LeaseKind::Solve, task: sid, token: g.token, owner: "box#9".into() });
    assert_eq!(release(&mut h, "box", entries), 4);
    assert!(tasks.iter().all(|id| h.t(*id).state == TState::Pending));
    let s = h.q.solve(sid).unwrap();
    assert_eq!((s.state, s.machine.as_str(), s.token.as_str(), s.attempts), (SolveState::Pending, "", "", 1));
}
