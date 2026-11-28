//! Requests: purchase, join, adopt, withdraw, undo, clear, bump, LIFO,
//! background priority, request time, the door (Q-5.10, Q-6) and the bulk
//! children button (Q-8.16..Q-8.20).

mod common;
use av2_model::Status;
use av2_queue::consts::*;
use av2_queue::*;
use common::*;

/// Serve a position's live task to completion and land it (visits + 1, EXP-5.2 absorption).
fn land(h: &mut H, pos: u64) -> u64 {
    let id = h.q.live_tasks_of(pos).iter().find(|t| t.state == TState::Pending).map(|t| t.id).unwrap();
    let mut r = h.lease_req("w", "lander");
    r.max_budget = None;
    // Lease exactly this one: it is the only PENDING row in these tests.
    let LeaseOut::Task { grant } = h.lease_out(r) else { panic!("nothing to land") };
    assert_eq!(grant.id, id);
    h.complete(id);
    let v = h.env.info(pos).visits;
    h.env.info(pos).visits = v + 1;
    let budget = h.t(id).budget;
    h.apply(Input::Landed { now: h.now, pos, task: Some(id), arbiter: budget });
    id
}

fn live(h: &H, pos: u64) -> Task {
    h.q.live_tasks_of(pos)[0].clone()
}

/// tests.py::RequestTests::test_request_queues_user_task_with_floor
/// test_frontier.py::UnchangedLadderTests::test_fresh_request_keeps_the_classic_single_task
/// test_request_time.py::RequestTimeTests::test_a_new_request_is_stamped_when_it_is_made
#[test]
fn a_fresh_request_buys_the_first_rung() {
    let mut h = H::new();
    let p = h.pos();
    assert_eq!(h.request("alice", p, |_| {}).status, "queued");
    let t = live(&h, p);
    assert_eq!((t.source, t.budget, t.multipv, t.generation, t.requested_by.as_str()), (Source::User, B128, 5, 0, "alice"));
    assert_eq!(t.requested_at, Some(h.now));
}

/// tests.py::RequestTests::test_reanalysis_uses_128m_512m_2b_staircase
/// test_frontier.py::UnchangedLadderTests::test_middle_rung_still_escalates_on_the_position_itself
/// tests.py::RequestTests::test_completed_recent_request_can_queue_next_rung
#[test]
fn the_staircase() {
    let mut h = H::new();
    let p = h.pos();
    for (i, rung) in REQUEST_BUDGET_LADDER.iter().enumerate() {
        h.advance(2 * HOUR);
        assert_eq!(h.request("alice", p, |_| {}).status, "queued");
        let t = live(&h, p);
        assert_eq!((t.budget, t.generation), (*rung, i as i32));
        land(&mut h, p);
    }
    // Spent: the one_search door says so; the plain door falls to the subtree.
    h.advance(2 * HOUR);
    assert_eq!(h.request("alice", p, |r| r.one_search = true).status, "ladder-spent");
    assert!(h.q.live_tasks_of(p).is_empty());
    assert!(budget::ladder_exhausted(true, h.q.completed_max_budget(p, &h.env)));
}

/// Retention: a delivered row below the position's visits, served more than
/// an hour ago, leaves memory and its rung goes to the ext ladder (owner
/// decision, 28-Nov); the next request still climbs from it.
#[test]
fn folded_rows_keep_the_ladder_in_the_ext_record() {
    let mut h = H::new();
    let p = h.pos();
    h.request("a", p, |_| {});
    let id = land(&mut h, p);
    h.advance(2 * HOUR);
    let a = h.apply(Input::Tick { now: h.now });
    assert_eq!(a.output, Output::Tick { recycled_solves: 0, pruned: 1 });
    assert_eq!(a.ladder, vec![(p, av2_queue::Ladder { bought: Some(B128), searched: Some(B128) })]);
    assert!(h.q.task(id).is_none());
    assert_eq!(h.q.completed_max_budget(p, &h.env), Some(B128));
    assert_eq!(h.request("a", p, |_| {}).status, "queued");
    assert_eq!(live(&h, p).budget, B512);
    // The ext encoding floors to 1,000 nodes and gives the rungs back exactly.
    let mut e = av2_model::Ext::default();
    av2_queue::Ladder { bought: Some(128_000_999), searched: None }.store_in(&mut e);
    assert_eq!(av2_queue::Ladder::from_ext(&e), av2_queue::Ladder { bought: Some(128_000_000), searched: None });
}

/// test_frontier.py::LadderExhaustionTests::test_ladder_exhausted_reports_only_the_spent_top_rung
#[test]
fn ladder_exhausted_reports_only_the_spent_top_rung() {
    assert!(!budget::ladder_exhausted(true, None));
    assert!(!budget::ladder_exhausted(true, Some(B512)));
    assert!(budget::ladder_exhausted(true, Some(B2)));
    assert!(!budget::ladder_exhausted(false, Some(B2)));
}

/// test_public_api.py::RequestApiTests::test_a_decided_position_buys_nothing
/// test_frontier.py::LadderExhaustionTests::test_solved_position_never_expands
#[test]
fn a_decided_position_buys_nothing() {
    let mut h = H::new();
    let p = h.pos();
    h.env.close(p, Status::Draw);
    assert_eq!(h.request("alice", p, |_| {}).status, "already-solved");
    assert_eq!(h.q.retained_count(), 0);
}

/// test_queue_control.py::DuplicateRequestTests (second person written down, no second task, twice yourself,
/// anonymous adds nothing, anonymous adopted) + test_adopted_request.py::AdoptedReceiptTests
/// test_request_time.py::RequestTimeTests::test_joining_a_request_keeps_its_authors_wait / ::test_an_adopted_anonymous_request_is_a_fresh_wait_for_its_new_owner
#[test]
fn joining_and_adopting() {
    let mut h = H::new();
    let p = h.pos();
    h.request("a", p, |_| {});
    let stamped = live(&h, p).requested_at;
    h.advance(DAY);
    let o = h.request("b", p, |r| r.ip = "2.2.2.2".into());
    assert_eq!(o.status, "already-queued");
    assert!(o.detail.get("adopted").is_none());
    // test_public_api.py::RequestApiTests::test_a_second_account_backs_the_request_instead_of_duplicating_it
    assert_eq!(o.detail["backed"], serde_json::json!(true));
    assert_eq!(o.detail["task_state"], serde_json::json!("waiting"));
    assert_eq!(o.detail["priority"], serde_json::json!("normal"));
    let t = live(&h, p);
    assert_eq!((t.requested_by.as_str(), t.also.clone(), t.backers()), ("a", vec!["b".to_string()], 1));
    assert_eq!(t.requested_at, stamped);
    assert_eq!(h.q.live_tasks_of(p).len(), 1);
    h.request("a", p, |r| r.ip = "3.3.3.3".into());
    h.request("", p, |r| r.ip = "4.4.4.4".into());
    assert_eq!(live(&h, p).backers(), 1);

    let mut h = H::new();
    let p = h.pos();
    h.request("", p, |_| {});
    h.advance(HOUR);
    let o = h.request("lesha", p, |r| r.ip = "9.9.9.9".into());
    assert_eq!(o.status, "already-queued");
    assert_eq!(o.detail.get("adopted"), Some(&serde_json::json!(true)));
    let t = live(&h, p);
    assert_eq!((t.requested_by.as_str(), t.backers(), t.requested_at), ("lesha", 0, Some(h.now)));
}

/// test_queue_control.py::DuplicateRequestTests::test_joining_moves_the_request_to_the_front_of_its_own_queue
#[test]
fn joining_moves_the_request_to_the_front_of_its_own_queue() {
    let mut h = H::new();
    let ps: Vec<u64> = (0..3).map(|_| h.pos()).collect();
    for p in &ps {
        h.request("a", *p, |_| {});
    }
    h.request("b", ps[2], |r| r.ip = "7.7.7.7".into());
    let first = h.serve("w", 1)[0];
    assert_eq!(h.t(first).pos, ps[2]);
}

/// test_mate_clamp.py::RequestedTaskTests::test_a_pending_request_for_more_is_never_lowered
/// test_request_time.py::RequestTimeTests::test_server_work_promoted_to_a_request_is_a_fresh_wait
#[test]
fn a_pending_request_for_more_is_never_lowered() {
    let mut h = H::new();
    let id = h.row(B512, |t| t.multipv = 2);
    let p = h.t(id).pos;
    h.advance(2 * DAY);
    assert_eq!(h.request("a", p, |_| {}).status, "queued");
    let t = h.t(id);
    assert_eq!((t.source, t.budget, t.multipv, t.requested_at), (Source::User, B512, 2, Some(h.now)));
    // A deeper choice lifts a request that is still waiting (test_depth_slider ChosenRungTests).
    h.request("a", p, |r| {
        r.budget_floor = Some(B2);
        r.ip = "1.2.3.4".into();
    });
    assert_eq!(h.t(id).budget, B2);
}

/// test_mate_clamp.py::RequestedTaskTests::test_a_click_on_a_short_mate_verifies_instead_of_digging
/// ::test_a_click_on_the_band_without_distance_keeps_the_floor ::test_the_ladder_recovers_control_once_the_check_ran
#[test]
fn short_mate_clamp_on_requests() {
    let mut h = H::new();
    let p = h.pos();
    h.env.info(p).first_line_mate = Some(2); // M2 with White to move: 3 plies
    h.request("a", p, |_| {});
    let t = live(&h, p);
    assert_eq!((t.budget, t.multipv), (3 * MATE_CLAMP_NODES_PER_PLY, 1));
    land(&mut h, p);
    h.advance(2 * HOUR);
    h.request("a", p, |_| {});
    let t = live(&h, p);
    assert_eq!((t.budget, t.multipv), (B128, 5), "the ladder recovers control once the check ran");

    let mut h = H::new();
    let p = h.pos();
    h.env.info(p).eval_cp = Some(9500);
    h.request("a", p, |_| {});
    let t = live(&h, p);
    assert_eq!((t.budget, t.multipv), (B128, 5));
}

/// test_queue_control.py::AbsorptionStillWorksTests::test_a_delivered_search_still_to_be_applied_is_not_a_dead_row
/// ::test_the_position_can_still_be_requested_after_an_absorption
#[test]
fn absorbed_and_delivered_rows_on_the_slot() {
    let mut h = H::new();
    let id = h.row(B128, |t| {
        t.state = TState::Completed;
        t.machine = "m".into();
        t.nodes_searched = 1;
        t.completed = Some(T0);
    });
    let p = h.t(id).pos;
    assert_eq!(h.request("a", p, |_| {}).status, "already-queued");
    assert_eq!(h.t(id).state, TState::Completed);

    let mut h = H::new();
    let id = h.row(B512, |t| {
        t.state = TState::Completed;
        t.completed = Some(T0);
    });
    let p = h.t(id).pos;
    assert_eq!(h.request("a", p, |_| {}).status, "queued");
    let t = h.t(id);
    // Bought 512M (absorbed): the next rung is 2B.
    assert_eq!((t.state, t.budget, t.source, t.requested_by.as_str()), (TState::Pending, B2, Source::User, "a"));
}

/// test_public_api.py::OnlyIfMissingTests (buys nothing once searched, missing queued at the first rung,
/// bought-but-never-searched does not count, deeper search covers, live deeper request joined as it stands)
/// test_public_api.py::OnlyIfMissingOnAnAbsorbedSlotTests::test_a_target_above_what_ran_brings_the_row_back_at_the_target
#[test]
fn only_if_missing() {
    let mut h = H::new();
    let p = h.pos();
    assert_eq!(h.request("a", p, |r| r.only_if_missing = true).status, "queued");
    land(&mut h, p);
    h.advance(2 * HOUR);
    let o = h.request("a", p, |r| r.only_if_missing = true);
    assert_eq!(o.status, "already-analyzed");
    assert_eq!(o.detail["completed_budget"], serde_json::json!(B128));
    // The plain call climbs.
    assert_eq!(h.request("a", p, |_| {}).status, "queued");
    assert_eq!(live(&h, p).budget, B512);

    // A rung bought but never searched: the floor, not above it.
    let mut h = H::new();
    let id = h.row(B512, |t| {
        t.state = TState::Completed;
        t.completed = Some(T0);
    });
    let p = h.t(id).pos;
    assert_eq!(h.request("a", p, |r| r.only_if_missing = true).status, "queued");
    assert_eq!(h.t(id).budget, B128);
    // ...and a target above what ran brings it back at the target.
    let mut h = H::new();
    let id = h.row(B128, |t| {
        t.state = TState::Completed;
        t.completed = Some(T0);
    });
    let p = h.t(id).pos;
    h.request("a", p, |r| {
        r.only_if_missing = true;
        r.budget_floor = Some(B512);
    });
    assert_eq!(h.t(id).budget, B512);

    // A live deeper request of somebody else is joined as it stands.
    let mut h = H::new();
    let p = h.pos();
    h.request("other", p, |r| r.budget_floor = Some(B2));
    let o = h.request("a", p, |r| {
        r.only_if_missing = true;
        r.ip = "5.5.5.5".into();
    });
    assert_eq!(o.status, "already-queued");
    let t = live(&h, p);
    assert_eq!((t.budget, t.also.clone()), (B2, vec!["a".to_string()]));
}

/// tests.py::RequestTests::test_request_during_shallow_lease_preserves_deep_follow_up (purchase side)
/// tests.py::RequestTests::test_sufficient_auto_lease_is_promoted_and_deduplicated
#[test]
fn requests_on_a_leased_row() {
    let mut h = H::new();
    let id = h.auto(8_000_000);
    let p = h.t(id).pos;
    h.lease("w", "m");
    assert_eq!(h.request("a", p, |_| {}).status, "queued");
    let f = h.q.live_tasks_of(p).into_iter().find(|t| t.state == TState::Pending).unwrap().clone();
    assert_eq!((f.generation, f.budget, f.source), (1, B128, Source::User));
    assert_eq!(h.t(id).state, TState::Leased);
    // A second, deeper click raises the follow-up.
    h.request("a", p, |r| {
        r.budget_floor = Some(B2);
        r.ip = "8.8.8.8".into();
    });
    assert_eq!(h.t(f.id).budget, B2);

    let mut h = H::new();
    let id = h.auto(B512);
    let p = h.t(id).pos;
    h.lease("w", "m");
    assert_eq!(h.request("a", p, |_| {}).status, "already-queued");
    let t = h.t(id);
    assert_eq!((t.source, t.state), (Source::User, TState::Leased));
    assert_eq!(h.q.live_tasks_of(p).len(), 1);
}

/// test_queue_control.py::WithdrawOwnRequestTests::* / WithdrawCostsNothingTests::* / WithdrawKeepsOtherBackersTests::*
/// test_queue_control.py::ScriptedQueueDoorTests::test_a_scripted_cancel_withdraws_and_undo_restores_the_same_row
#[test]
fn withdraw_and_undo() {
    let mut h = H::new();
    let p = h.pos();
    h.request("a", p, |_| {});
    let id = live(&h, p).id;
    assert_eq!(h.status(Input::Withdraw { now: h.now, account: "b".into(), task: id }), "not-yours");
    assert_eq!(h.status(Input::Withdraw { now: h.now, account: "a".into(), task: id }), "cancelled");
    assert_eq!(h.t(id).state, TState::Cancelled);
    assert_eq!(h.lease("w", "m"), None, "a cancelled row is never served");
    assert_eq!(h.status(Input::Undo { now: h.now, account: "a".into(), task: id }), "restored");
    assert_eq!((h.t(id).state, h.t(id).budget, h.t(id).completed), (TState::Pending, B128, None));
    assert_eq!(h.status(Input::Undo { now: h.now, account: "a".into(), task: id }), "nothing-to-undo");
    // Withdraw then ask again: the same row comes back (the ladder does not advance).
    h.status(Input::Withdraw { now: h.now, account: "a".into(), task: id });
    h.advance(2 * HOUR);
    assert_eq!(h.request("c", p, |_| {}).status, "queued");
    let t = h.t(id);
    assert_eq!((t.state, t.requested_by.as_str(), t.budget), (TState::Pending, "c", B128));
    // Undo is refused once the position has a verdict.
    h.status(Input::Withdraw { now: h.now, account: "c".into(), task: id });
    h.env.close(p, Status::WhiteWin);
    assert_eq!(h.status(Input::Undo { now: h.now, account: "c".into(), task: id }), "cannot-restore");
    // A running request cannot be withdrawn.
    let mut h = H::new();
    let r = h.user("a", B128);
    h.lease("w", "m");
    assert_eq!(h.status(Input::Withdraw { now: h.now, account: "a".into(), task: r }), "not-yours");
}

/// test_queue_control.py::WithdrawCostsNothingTests::test_asking_again_does_not_inherit_the_withdrawn_rung
/// ::test_a_deeper_rung_can_still_be_picked_on_the_way_back
/// test_request_priority.py::UpgradeTests::test_a_withdrawn_api_row_comes_back_at_the_level_asked_now
#[test]
fn a_revived_row_is_the_request_made_now() {
    let mut h = H::new();
    let p = h.pos();
    h.request("a", p, |r| {
        r.budget_floor = Some(B2);
        r.background = true;
    });
    let id = live(&h, p).id;
    h.status(Input::Withdraw { now: h.now, account: "a".into(), task: id });
    h.advance(2 * HOUR);
    h.request("a", p, |_| {});
    let t = h.t(id);
    assert_eq!((t.budget, t.background), (B128, false));
    h.status(Input::Withdraw { now: h.now, account: "a".into(), task: id });
    h.advance(2 * HOUR);
    h.request("a", p, |r| r.budget_floor = Some(B512));
    assert_eq!(h.t(id).budget, B512);
}

/// test_queue_control.py::WithdrawKeepsOtherBackersTests::test_the_request_survives_under_the_first_of_the_others
/// ::test_the_last_person_waiting_closes_it; test_lanes.py::HandoverLaneTests::test_a_handover_to_the_queue_lender_drops_the_loan
#[test]
fn a_shared_request_is_handed_over() {
    let mut h = H::new();
    let id = h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "a".into();
        t.also = vec!["b".into(), "c".into()];
        t.lane_account = "b".into();
        t.queue_seq = -4;
    });
    assert_eq!(h.status(Input::Withdraw { now: h.now, account: "a".into(), task: id }), "handed-over");
    let t = h.t(id);
    assert_eq!((t.state, t.requested_by.as_str(), t.also.clone(), t.queue_seq), (TState::Pending, "b", vec!["c".to_string()], 0));
    assert_eq!(t.lane_account, "c", "re-resolved: c has the lighter queue");
    h.status(Input::Withdraw { now: h.now, account: "b".into(), task: id });
    assert_eq!(h.t(id).requested_by, "c");
    assert_eq!(h.status(Input::Withdraw { now: h.now, account: "c".into(), task: id }), "cancelled");

    // test_lanes.py::HandoverLaneTests: the lender becomes the author, the loan is dropped.
    let mut h = H::new();
    let id = h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "a".into();
        t.also = vec!["b".into()];
        t.lane_account = "b".into();
    });
    assert_eq!(h.status(Input::Withdraw { now: h.now, account: "a".into(), task: id }), "handed-over");
    assert_eq!((h.t(id).requested_by.as_str(), h.t(id).lane_account.as_str()), ("b", ""));
}

/// test_queue_control.py::ClearOwnQueueTests::*
#[test]
fn clear_own_queue() {
    let mut h = H::new();
    let mine: Vec<u64> = (0..4).map(|_| h.user("a", B128)).collect();
    let shared = h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "a".into();
        t.also = vec!["b".into()];
    });
    let theirs = h.user("b", B128);
    let running = mine[0];
    h.lease("w", "m");
    assert_eq!(h.t(running).state, TState::Leased);
    let out = h.apply(Input::ClearQueue { now: h.now, account: "a".into() }).output;
    assert_eq!(out, Output::Cleared { withdrawn: 3, handed: 1 });
    assert_eq!(h.t(running).state, TState::Leased, "what is already running keeps running");
    assert_eq!(h.t(shared).requested_by, "b");
    assert_eq!(h.t(theirs).state, TState::Pending);
    assert_eq!(h.apply(Input::ClearQueue { now: h.now, account: "a".into() }).output, Output::Cleared { withdrawn: 0, handed: 0 });
}

/// test_queue_control.py::OwnQueueBumpTests::* and BumpReadsTheLaneItActuallyRidesTests::*
#[test]
fn move_to_front() {
    let mut h = H::new();
    let a: Vec<u64> = (0..3).map(|_| h.user("a", B128)).collect();
    assert_eq!(h.status(Input::Bump { now: h.now, account: "a".into(), task: a[0] }), "already-first");
    assert_eq!(h.status(Input::Bump { now: h.now, account: "b".into(), task: a[1] }), "not-yours");
    assert_eq!(h.status(Input::Bump { now: h.now, account: "a".into(), task: a[1] }), "moved");
    assert_eq!(h.status(Input::Bump { now: h.now, account: "a".into(), task: a[2] }), "moved");
    assert_eq!(h.t(a[2]).queue_seq, -2);
    assert_eq!(h.serve("w", 3), vec![a[2], a[1], a[0]]);
    assert_eq!(h.status(Input::Bump { now: h.now, account: "a".into(), task: a[0] }), "not-yours", "a served request is no place");

    // The floor is the front of the LANE the row rides, not of its author.
    let mut h = H::new();
    h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "b".into();
        t.queue_seq = -5;
    });
    h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "a".into();
        t.queue_seq = -1;
    });
    let riding = h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "a".into();
        t.lane_account = "b".into();
    });
    assert_eq!(h.q.front_of_own_queue(riding), Some(-6));
    let ahead_in_author = h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "a".into();
        t.lane_account = "b".into();
        t.queue_seq = -1;
    });
    assert_eq!(h.q.front_of_own_queue(ahead_in_author), Some(-6));
}

/// test_queue_control.py::QueueApiAndLifoTests::test_lifo_places_each_new_request_at_the_front_of_only_its_queue
/// ::test_switching_lifo_off_leaves_the_next_request_at_the_tail
#[test]
fn newest_first() {
    let mut h = H::new();
    h.apply(Input::SetPref { now: h.now, account: "alice".into(), pref: Pref { lifo_queue: true, ..Default::default() } });
    let ps: Vec<u64> = (0..6).map(|_| h.pos()).collect();
    for (i, p) in ps.iter().enumerate() {
        h.request(if i % 2 == 0 { "alice" } else { "bob" }, *p, |_| {});
    }
    let served = h.serve("w", 6);
    let pos: Vec<u64> = served.iter().map(|id| h.t(*id).pos).collect();
    // Bob has the lower id at the first tie; then least-recently-served keeps
    // the accounts alternating, and alice's own turns go newest first.
    assert_eq!(pos, vec![ps[1], ps[4], ps[3], ps[2], ps[5], ps[0]]);

    let mut h = H::new();
    h.apply(Input::SetPref { now: h.now, account: "alice".into(), pref: Pref { lifo_queue: true, ..Default::default() } });
    let ps: Vec<u64> = (0..3).map(|_| h.pos()).collect();
    h.request("alice", ps[0], |_| {});
    h.request("alice", ps[1], |_| {});
    h.apply(Input::SetPref { now: h.now, account: "alice".into(), pref: Pref::default() });
    h.request("alice", ps[2], |_| {});
    let pos: Vec<u64> = h.serve("w", 3).iter().map(|id| h.t(*id).pos).collect();
    assert_eq!(pos, vec![ps[1], ps[0], ps[2]]);
}

/// test_request_priority.py::UpgradeTests (background never lowers a normal row, click lifts, same-address
/// click lifts too, server row promoted by the API becomes background)
#[test]
fn background_priority_merge() {
    let mut h = H::new();
    let p = h.pos();
    h.request("bot", p, |r| r.background = true);
    assert!(live(&h, p).background);
    let o = h.request("human", p, |r| r.ip = "11.11.11.11".into());
    assert_eq!(o.status, "already-queued");
    assert!(!live(&h, p).background);
    h.request("bot", p, |r| {
        r.background = true;
        r.ip = "12.12.12.12".into();
    });
    assert!(!live(&h, p).background, "a background request never lowers a normal row");

    let mut h = H::new();
    let p = h.pos();
    h.request("bot", p, |r| r.background = true);
    let o = h.request("bot", p, |_| {});
    assert_eq!(o.status, "already-requested");
    assert!(!live(&h, p).background, "the same-address click lifts it too");

    let mut h = H::new();
    let id = h.auto(B128);
    let p = h.t(id).pos;
    h.request("bot", p, |r| r.background = true);
    assert_eq!((h.t(id).source, h.t(id).background), (Source::User, true));
}

/// tests.py::RequestTests::test_request_dedup_same_ip_position / test_receipts_no_longer_rate_limit_an_ip
#[test]
fn ip_and_position_dedup() {
    let mut h = H::new();
    let p = h.pos();
    let q = h.pos();
    assert_eq!(h.request("", p, |_| {}).status, "queued");
    assert_eq!(h.request("", p, |_| {}).status, "already-requested");
    assert_eq!(h.request("", q, |_| {}).status, "queued", "no hourly gate per address");
    // A chosen rung bypasses the dedup.
    assert_eq!(h.request("", p, |r| r.budget_floor = Some(B2)).status, "already-queued");
    // An hour later the dedup is gone.
    h.advance(HOUR + 1);
    assert_eq!(h.request("", p, |_| {}).status, "already-queued");
}

/// test_lanes.py::AccountQueueCapTests (refused, blocks nobody else, anonymous shares one allowance,
/// running work does not spend it, receipt once per account and minute)
#[test]
fn per_account_cap() {
    let mut h = H::new();
    for _ in 0..REQUEST_QUEUE_MAX {
        h.user("flood", 1);
    }
    let p = h.pos();
    let a = h.apply(Input::Request(RequestReq { now: h.now, account: "flood".into(), pos: p, ip: "1.1.1.1".into(), ..Default::default() }));
    let Output::Request(o) = &a.output else { panic!() };
    assert_eq!(o.status, "queue-full-account");
    assert_eq!(o.detail["retry_after"], serde_json::json!(300));
    assert_eq!(a.events[0].kind, "LANE_CAP_HIT");
    let a2 = h.apply(Input::Request(RequestReq {
        now: h.now + 10 * SECOND,
        account: "flood".into(),
        pos: p,
        ip: "1.1.1.1".into(),
        ..Default::default()
    }));
    assert!(a2.events.is_empty(), "one receipt per account and minute");
    assert_eq!(h.request("other", p, |_| {}).status, "queued");
    // Running work does not spend the allowance.
    h.lease("w", "m");
    let q = h.pos();
    assert_eq!(h.request("flood", q, |_| {}).status, "queued");
}

/// test_request_time.py::RequestTimeTests::test_a_withdrawn_request_asked_again_is_a_fresh_wait
#[test]
fn a_withdrawn_request_asked_again_is_a_fresh_wait() {
    let mut h = H::new();
    let p = h.pos();
    h.request("a", p, |_| {});
    let id = live(&h, p).id;
    h.status(Input::Withdraw { now: h.now, account: "a".into(), task: id });
    h.advance(2 * DAY);
    h.request("a", p, |_| {});
    assert_eq!(h.t(id).created, T0);
    assert_eq!(h.t(id).requested_at, Some(h.now));
}

/// test_then_verify.py::ThenVerifyRequestTests::test_the_box_marks_the_live_request / test_without_the_box_nothing_is_marked
#[test]
fn then_verify() {
    let mut h = H::new();
    let p = h.pos();
    let o = h.request("a", p, |r| r.then_verify = true);
    assert_eq!(o.detail.get("then_verify"), Some(&serde_json::json!(true)));
    assert!(live(&h, p).then_verify);
    let q = h.pos();
    let o = h.request("a", q, |_| {});
    assert!(o.detail.get("then_verify").is_none());
    assert!(!live(&h, q).then_verify);
}

/// test_queue_fairness.py::NamedBeforeAnonymousTests::test_the_click_receipt_tells_how_many_are_ahead
/// test_queue_fairness.py::StarvedRequestTests::test_the_receipt_counts_the_starved_tier_ahead
#[test]
fn the_receipt_tells_how_many_are_ahead() {
    let mut h = H::new();
    h.user("", B128);
    h.advance(30 * HOUR);
    let p = h.pos();
    let o = h.request("named", p, |r| r.with_place = true);
    assert_eq!(o.status, "queued");
    assert_eq!(h.q.user_queue_ahead(p, h.now, &h.env), Some(1));
}

// ------------------------------------------------------------ bulk children

/// test_mate_clamp.py::UnexploredChildrenTests::test_children_without_information_keep_the_request_floor
/// ::test_a_child_that_already_claims_a_short_mate_is_verified
/// test_explorer_proof_ui.py::UnexploredButtonTests::test_it_queues_every_reply_nobody_looked_at / test_a_second_click_queues_nothing_new
/// test_queue_fairness.py::BulkRouteInheritanceTests::test_bulk_children_inherit_route_plus_their_move
#[test]
fn unexplored_children_button() {
    let mut h = H::new();
    let p = h.pos();
    h.env.edge(p, "e2e4", 50);
    h.env.edge(p, "d2d4", 51);
    h.env.edge(p, "g1f3", 52);
    h.env.info(51).mate_in = Some(3);
    h.env.info(52).nodes_invested = 1; // explored
    let bulk = |h: &mut H| match h
        .apply(Input::BulkChildren { now: h.now, account: "w".into(), pos: p, route: "a2a3".into(), source: Source::User, children: None })
        .output
    {
        Output::Made { count } => count,
        o => panic!("{o:?}"),
    };
    assert_eq!(bulk(&mut h), 2);
    let a = live(&h, 50);
    assert_eq!((a.budget, a.multipv, a.route.as_str(), a.source), (B128, 5, "a2a3,e2e4", Source::User));
    let b = live(&h, 51);
    assert_eq!((b.budget, b.multipv), (3 * MATE_CLAMP_NODES_PER_PLY, 1));
    assert!(h.q.live_tasks_of(52).is_empty());
    assert_eq!(bulk(&mut h), 0, "a second click queues nothing new");
}

/// test_queue_control.py::WithdrawCostsNothingTests::test_the_bulk_button_can_buy_a_withdrawn_reply
/// Q-8.19: a system caller demotes a PENDING USER row (OQ-30, kept as in v1).
#[test]
fn bulk_children_withdrawn_and_demotion() {
    let mut h = H::new();
    let p = h.pos();
    h.env.edge(p, "e2e4", 60);
    h.request("x", 60, |_| {});
    let id = live(&h, 60).id;
    h.status(Input::Withdraw { now: h.now, account: "x".into(), task: id });
    h.apply(Input::BulkChildren {
        now: h.now,
        account: "wolfram".into(),
        pos: p,
        route: String::new(),
        source: Source::User,
        children: None,
    });
    let t = h.t(id);
    assert_eq!((t.state, t.requested_by.as_str(), t.budget), (TState::Pending, "wolfram", B128));
    h.apply(Input::BulkChildren {
        now: h.now,
        account: String::new(),
        pos: p,
        route: String::new(),
        source: Source::Auto,
        children: Some(vec![60]),
    });
    assert_eq!(h.t(id).source, Source::Auto);
}
