//! Service order: bands, own first, named stratum, fair share, backers,
//! least recently served, campaigns, house and personal workers (Q-1.2,
//! Q-2.14..Q-2.19, Q-5).

mod common;
use av2_model::Status;
use av2_queue::consts::*;
use av2_queue::*;
use common::*;

fn owner(h: &H, ids: &[u64]) -> Vec<String> {
    ids.iter().map(|id| h.t(*id).requested_by.clone()).collect()
}

/// test_coverage_completion.py::CoverageIsServedAheadOfExplorationTests::test_the_queue_order_is_user_then_fill_then_auto
/// tests.py::RequestTests::test_user_requests_leased_first
#[test]
fn the_queue_order_is_user_then_fill_then_auto() {
    let mut h = H::new();
    let auto = h.auto(B128);
    let fill = h.row(B128, |t| t.source = Source::Fill);
    let user = h.user("x", B128);
    let pp = h.t(auto).pos;
    h.env.priority.insert(pp, 99.0);
    assert_eq!(h.drain("w", 3), vec![user, fill, auto]);
    assert!(Source::User.rank() > Source::Fill.rank() && Source::Fill.rank() > Source::Auto.rank());
}

/// test_queue_fairness.py::UserBandFifoTests::test_an_older_click_is_served_before_a_better_positioned_one
/// ::test_a_user_click_still_outranks_every_selector_task ::test_the_selector_bands_still_follow_position_priority
/// test_campaigns.py::SelectorBonusTests::test_the_user_band_of_the_lease_is_untouched
#[test]
fn user_band_fifo_and_selector_priority() {
    let mut h = H::new();
    let old = h.user("", B128);
    let new = h.user("", B128);
    let (po, pn) = (h.t(old).pos, h.t(new).pos);
    h.env.priority.insert(po, -73.0);
    h.env.priority.insert(pn, 8.0);
    let a_low = h.auto(B128);
    let a_high = h.auto(B128);
    let (pl, ph) = (h.t(a_low).pos, h.t(a_high).pos);
    h.env.priority.insert(pl, -73.0);
    h.env.priority.insert(ph, 8.0);
    assert_eq!(h.drain("w", 4), vec![old, new, a_high, a_low]);
}

/// test_queue_fairness.py::FairShareTests::test_the_first_request_of_each_account_precedes_every_second_one
/// ::test_within_one_account_the_order_of_arrival_is_untouched
#[test]
fn the_first_request_of_each_account_precedes_every_second_one() {
    let mut h = H::new();
    let a1 = h.user("a", B128);
    let a2 = h.user("a", B128);
    let b1 = h.user("b", B128);
    let b2 = h.user("b", B128);
    assert_eq!(h.serve("w", 4), vec![a1, b1, a2, b2]);
}

/// test_lanes.py::AccountShareTests::test_equal_accounts_alternate / test_every_account_advances_at_the_same_rate
#[test]
fn equal_accounts_alternate() {
    let mut h = H::new();
    for _ in 0..10 {
        h.user("a", B128);
        h.user("b", B128);
    }
    let got = h.serve("w", 8);
    let o = owner(&h, &got);
    assert_eq!(o.iter().filter(|x| *x == "a").count(), 4);
    let mut h = H::new();
    for acct in ["a", "b", "c", "d", "e", "f"] {
        for _ in 0..4 {
            h.user(acct, B128);
        }
    }
    let got = h.serve("w", 12);
    let o = owner(&h, &got);
    for acct in ["a", "b", "c", "d", "e", "f"] {
        assert_eq!(o.iter().filter(|x| *x == acct).count(), 2, "{acct}");
    }
}

/// test_queue_fairness.py::FairShareTests::test_a_ten_billion_request_yields_its_turn_to_the_cheap_ones
#[test]
fn a_ten_billion_request_yields_its_turn_to_the_cheap_ones() {
    let mut h = H::new();
    let big1 = h.user("big", B10);
    let big2 = h.user("big", B10);
    let cheap: Vec<u64> = (0..100).map(|_| h.user("cheap", B128)).collect();
    let order = h.serve("w", 90);
    assert_eq!(order[0], big1);
    assert_eq!(order[1], cheap[0]);
    // 10B / 128M = 78.125: the 79th cheap one starts at 9.984B < 10B, the
    // 80th at 10.112B > 10B; the second 10B starts at 10B.
    let pos = order.iter().position(|x| *x == big2).unwrap();
    assert_eq!(pos, 80);
}

/// test_queue_fairness.py::FairShareTests::test_the_burst_of_the_sixth_of_august_no_longer_blocks_a_newcomer
#[test]
fn the_burst_of_the_sixth_of_august_no_longer_blocks_a_newcomer() {
    let mut h = H::new();
    for _ in 0..1562 {
        h.user("burst", B128);
    }
    let newcomer = h.user("newcomer", B128);
    assert_eq!(h.q.queue_ahead(newcomer, h.now, &h.env), Some((1, 0)));
    let order = h.serve("w", 2);
    assert_eq!(order[1], newcomer);
}

/// test_queue_fairness.py::FairShareTests::test_the_estimator_agrees_with_the_order_actually_served
#[test]
fn the_estimator_agrees_with_the_order_actually_served() {
    let mut h = H::new();
    let mut ids = Vec::new();
    for (acct, b) in [("a", B128), ("b", B2), ("a", B2), ("c", B128), ("b", B128), ("c", B2), ("a", B128)] {
        ids.push(h.user(acct, b));
    }
    let mut predicted: Vec<(usize, u64)> = ids.iter().map(|id| (h.q.queue_ahead(*id, h.now, &h.env).unwrap().0, *id)).collect();
    predicted.sort();
    // Leased rows keep counting in their account's share (Q-5.6), so a
    // queue served lease by lease follows the estimate.
    let served = h.serve("w", ids.len());
    assert_eq!(predicted.iter().map(|x| x.1).collect::<Vec<_>>(), served);
    assert_eq!(predicted.iter().map(|x| x.0).collect::<Vec<_>>(), (0..ids.len()).collect::<Vec<_>>());
}

/// test_queue_fairness.py::NamedBeforeAnonymousTests::test_a_named_request_jumps_the_anonymous_flood
/// test_queue_fairness.py::StarvedRequestTests::test_the_fresh_anonymous_flood_still_waits_its_turn
#[test]
fn a_named_request_jumps_the_anonymous_flood() {
    let mut h = H::new();
    let anon = h.user("", B128);
    let named = h.user("lesha", B128);
    assert_eq!(h.drain("w", 2), vec![named, anon]);
}

/// test_queue_fairness.py::StarvedRequestTests::test_a_starved_anonymous_click_beats_a_fresh_named_one
/// test_queue_fairness.py::AnonymousBucketTests::test_a_starved_anonymous_click_still_reaches_the_front
#[test]
fn a_starved_anonymous_click_beats_a_fresh_named_one() {
    let mut h = H::new();
    let starved = h.user("", B128);
    h.advance(30 * HOUR);
    let named1 = h.user("n1", B128);
    let named2 = h.user("n2", B128);
    assert_eq!(h.drain("w", 3), vec![starved, named1, named2]);
}

/// test_queue_fairness.py::AnonymousBucketTests::test_the_anonymous_flood_is_shared_out_as_a_single_account
#[test]
fn the_anonymous_flood_is_shared_out_as_a_single_account() {
    let mut h = H::new();
    let f0 = h.user("", B128);
    let f1 = h.user("", B128);
    let n0 = h.user("named", B128);
    let n1 = h.user("named", B128);
    assert_eq!(h.serve("w", 4), vec![n0, n1, f0, f1]);
}

/// test_queue_fairness.py::AnonymousBucketTests::test_the_starved_tier_never_inherits_the_deficit_of_the_fresh_one
#[test]
fn the_starved_tier_never_inherits_the_deficit_of_the_fresh_one() {
    let mut h = H::new();
    let p1 = h.user("", B2);
    let p2 = h.user("", B2);
    h.advance(30 * HOUR);
    let named = h.user("named", B2);
    let order = h.serve("w", 3);
    // The first promoted row ties the named one at 0 and wins by id; the
    // second promoted row carries the first's 2B.
    assert_eq!(order, vec![p1, named, p2]);
}

/// test_queue_fairness.py::OwnRequestAffinityTests::test_a_workers_own_request_jumps_its_band_queue
/// ::test_after_its_own_the_worker_rejoins_the_fifo
/// test_queue_fairness.py::NamedBeforeAnonymousTests::test_own_first_still_beats_a_named_stranger
#[test]
fn a_workers_own_request_jumps_its_band_queue() {
    let mut h = H::new();
    let stranger = h.user("stranger", B128);
    let own = h.user("lesha", B128);
    assert_eq!(h.lease("lesha", "m1"), Some(own));
    assert_eq!(h.lease("lesha", "m2"), Some(stranger));
}

/// test_serve_others_first.py::ServeOthersFirstLeaseTests (on/off/queue/only own/selector bands)
#[test]
fn serve_orders() {
    let set = |h: &mut H, order: ServeOrder, serve_for: &str| {
        let pref = Pref { serve_order: order, serve_for: parse_serve_for(serve_for), ..Default::default() };
        h.apply(Input::SetPref { now: h.now, account: "me".into(), pref });
    };
    // Others first: the waiting stranger first, then own.
    let mut h = H::new();
    let own = h.user("me", B128);
    let stranger = h.user("stranger", B128);
    set(&mut h, ServeOrder::Others, "");
    assert_eq!(h.lease("me", "a"), Some(stranger));
    assert_eq!(h.lease("me", "b"), Some(own));
    // ...and own still beats the selector bands.
    let mut h = H::new();
    let auto = h.auto(B128);
    let own = h.user("me", B128);
    set(&mut h, ServeOrder::Others, "");
    assert_eq!(h.lease("me", "a"), Some(own));
    assert_eq!(h.lease("me", "b"), Some(auto));
    // Queue order: the older request whoever asked.
    let mut h = H::new();
    let stranger = h.user("stranger", B128);
    h.user("me", B128);
    set(&mut h, ServeOrder::Queue, "ally");
    assert_eq!(h.lease("me", "a"), Some(stranger));
    // A pref row with the default order keeps own first.
    let mut h = H::new();
    h.user("stranger", B128);
    let own = h.user("me", B128);
    let pref = Pref { lifo_queue: true, ..Default::default() };
    h.apply(Input::SetPref { now: h.now, account: "me".into(), pref });
    assert_eq!(h.lease("me", "a"), Some(own));
    // Another account's setting changes nothing here.
    let mut h = H::new();
    h.user("stranger", B128);
    let own = h.user("me", B128);
    let pref = Pref { serve_order: ServeOrder::Others, ..Default::default() };
    h.apply(Input::SetPref { now: h.now, account: "other".into(), pref });
    assert_eq!(h.lease("me", "a"), Some(own));
}

/// test_serve_others_first.py::ServeForListTests (listed first, anonymous, mine-first band, queue ignores)
/// test_lease_heads.py::HeadLeaseEquivalenceTests::test_listed_accounts_with_others_first
#[test]
fn serve_for_lists() {
    let mut h = H::new();
    let stranger = h.user("stranger", B128);
    h.user("me", B128);
    let ally = h.user("ally", B128);
    let pref = Pref { serve_order: ServeOrder::Others, serve_for: parse_serve_for("ALLY"), ..Default::default() };
    h.apply(Input::SetPref { now: h.now, account: "me".into(), pref });
    assert_eq!(h.lease("me", "a"), Some(ally));
    assert_eq!(h.lease("me", "b"), Some(stranger));

    let mut h = H::new();
    h.user("stranger", B128);
    let anon = h.user("", B128);
    let pref = Pref { serve_order: ServeOrder::Others, serve_for: parse_serve_for("Anonymous"), ..Default::default() };
    h.apply(Input::SetPref { now: h.now, account: "me".into(), pref });
    assert_eq!(h.lease("me", "a"), Some(anon));

    let mut h = H::new();
    h.user("stranger", B128);
    let ally = h.user("ally", B128);
    let pref = Pref { serve_order: ServeOrder::Own, serve_for: parse_serve_for("ally"), ..Default::default() };
    h.apply(Input::SetPref { now: h.now, account: "me".into(), pref });
    assert_eq!(h.lease("me", "a"), Some(ally));
    assert_eq!(parse_serve_for("a, b  A,c"), vec!["a", "b", "c"]);
}

/// test_worker_serving.py::BackedCountsAsOwnTests::test_mine_first_takes_the_backed_request_before_the_queue
/// ::test_a_request_backed_by_someone_else_is_not_mine ::test_others_first_puts_the_backed_request_behind_the_rest
/// ::test_queue_order_gives_the_backed_request_no_preference ::test_the_owner_is_still_the_one_charged
#[test]
fn backed_counts_as_own() {
    let mut h = H::new();
    let stranger = h.user("stranger", B128);
    let backed = h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "w".into();
        t.also = vec!["me".into()];
    });
    assert_eq!(h.lease("me", "a"), Some(backed));
    assert_eq!(h.t(backed).requested_by, "w");
    assert_eq!(h.t(backed).also, vec!["me".to_string()]);
    assert_eq!(h.lease("me", "b"), Some(stranger));

    // Backed by someone else: not mine.  (Its backer still wins the tie
    // against the plain stranger row, Q-5.7, but not against own work.)
    let mut h = H::new();
    let stranger = h.user("stranger", B128);
    let other_backed = h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "w".into();
        t.also = vec!["someone".into()];
    });
    let own = h.user("me", B128);
    assert_eq!(h.lease("me", "a"), Some(own));
    assert_eq!(h.lease("me", "b"), Some(other_backed));
    assert_eq!(h.lease("me", "c"), Some(stranger));
}

/// test_queue_fairness.py::HouseWorkerPolicyTests (skips foreign click / own click / community worker)
#[test]
fn house_worker_policy() {
    let mut h = H::new();
    let foreign = h.user("someone", B128);
    let auto = h.auto(B128);
    assert_eq!(h.lease("codex_local_worker", "house"), Some(auto));
    assert_eq!(h.t(foreign).state, TState::Pending);
    let own = h.user("codex_local_worker", B128);
    assert_eq!(h.lease("codex_local_worker", "house2"), Some(own));
    assert_eq!(h.lease("community", "c"), Some(foreign));
}

/// test_queue_fairness.py::LeastRecentlyServedTests::test_a_single_worker_does_not_starve_the_small_account
/// ::test_a_lease_older_than_the_window_does_not_count
#[test]
fn least_recently_served() {
    let mut h = H::new();
    let moky: Vec<u64> = (0..8).map(|_| h.user("moky", B128)).collect();
    let small: Vec<u64> = (0..4).map(|_| h.user("soothdest", B128)).collect();
    let served = h.drain("w", 12);
    assert_eq!(
        owner(&h, &served),
        ["moky", "soothdest", "moky", "soothdest", "moky", "soothdest", "moky", "soothdest", "moky", "moky", "moky", "moky"]
    );
    assert_eq!(served[1], small[0]);
    assert_eq!(served[0], moky[0]);

    let mut h = H::new();
    let m0 = h.user("moky", B128);
    let m1 = h.user("moky", B128);
    let s0 = h.user("soothdest", B128);
    assert_eq!(h.drain("w", 1), vec![m0]);
    h.advance(2 * HOUR);
    // moky's lease is older than the window: ids decide.
    assert_eq!(h.drain("w", 1), vec![m1]);
    let _ = s0;
}

/// test_queue_fairness.py::LeastRecentlyServedTests::test_the_estimator_sees_the_same_turn
#[test]
fn the_estimator_sees_the_same_turn() {
    let mut h = H::new();
    let m0 = h.user("moky", B128);
    let m1 = h.user("moky", B128);
    let s = h.user("small", B128);
    assert_eq!(h.drain("w", 1), vec![m0]);
    assert_eq!(h.q.queue_ahead(s, h.now, &h.env).unwrap().0, 0);
    assert_eq!(h.q.queue_ahead(m1, h.now, &h.env).unwrap().0, 1);
    assert_eq!(h.drain("w", 1), vec![s]);
}

/// test_queue_control.py::DuplicateRequestTests::test_more_people_win_the_tie_between_two_first_requests
#[test]
fn more_people_win_the_tie_between_two_first_requests() {
    let mut h = H::new();
    let lone = h.user("a", B128);
    let popular = h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "b".into();
        t.also = vec!["c".into(), "d".into()];
    });
    assert_eq!(h.drain("w", 2), vec![popular, lone]);
}

/// test_queue_control.py::BackersStayInTheVisitorBandTests::test_a_degraded_request_does_not_outrank_position_priority
#[test]
fn a_degraded_request_does_not_outrank_position_priority() {
    let mut h = H::new();
    let degraded = h.row(B128, |t| {
        t.requested_by = "a".into();
        t.also = vec!["b".into()];
    });
    let better = h.auto(B128);
    let (pd, pb) = (h.t(degraded).pos, h.t(better).pos);
    h.env.priority.insert(pd, 1.0);
    h.env.priority.insert(pb, 9.0);
    assert_eq!(h.drain("w", 2), vec![better, degraded]);
}

/// test_request_priority.py::OrderTests::test_the_accounts_clicks_go_before_its_older_api_requests
/// ::test_other_accounts_keep_their_turns
#[test]
fn background_rows_go_behind_the_accounts_normal_ones() {
    let mut h = H::new();
    let l0 = h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "jean".into();
        t.background = true;
    });
    let l1 = h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "jean".into();
        t.background = true;
    });
    let n0 = h.user("jean", B128);
    let n1 = h.user("jean", B128);
    assert_eq!(h.serve("w", 4), vec![n0, n1, l0, l1]);

    // Other accounts keep their turns: the background row only changes
    // which of jean's rows fills jean's last turn.
    let mut h = H::new();
    let n0 = h.user("jean", B128);
    let o0 = h.user("other", B128);
    let l0 = h.row(B128, |t| {
        t.source = Source::User;
        t.requested_by = "jean".into();
        t.background = true;
    });
    let o1 = h.user("other", B128);
    let n1 = h.user("jean", B128);
    let served = h.serve("w", 5);
    assert_eq!(owner(&h, &served), ["jean", "other", "jean", "other", "jean"]);
    assert_eq!(served, vec![n0, o0, n1, o1, l0]);
}

/// test_queue_control.py::OwnQueueBumpTests::test_a_bump_does_not_erase_the_debt_of_your_running_work
#[test]
fn a_bump_does_not_erase_the_debt_of_your_running_work() {
    let mut h = H::new();
    let running = h.user("a", B10);
    assert_eq!(h.lease("w", "m"), Some(running));
    let a1 = h.user("a", B128);
    let a2 = h.user("a", B128);
    let b = h.user("b", B128);
    assert_eq!(h.status(Input::Bump { now: h.now, account: "a".into(), task: a2 }), "moved");
    assert_eq!(h.serve("w", 3), vec![b, a2, a1]);
}

/// test_worker_serving.py::WorkerServingLeaseTests (campaign before selector work, requests before
/// the campaign, empty campaign falls back, campaign 0 overrides, account campaign reaches a worker, personal)
#[test]
fn worker_campaigns_and_modes() {
    let mut h = H::new();
    let camp = h.auto(B128);
    let pc = h.t(camp).pos;
    h.env.info(pc).campaign = 7;
    h.env.priority.insert(pc, -73.0);
    let high = h.auto(B128);
    let ph = h.t(high).pos;
    h.env.priority.insert(ph, 50.0);
    let fill = h.row(B128, |t| t.source = Source::Fill);
    let req = h.user("stranger", B128);
    let serving = Serving { mode: WorkerMode::Account, campaign: Some(7) };
    h.apply(Input::SetServing { now: h.now, account: "me".into(), machine: "box#1".into(), serving });
    assert_eq!(h.lease("me", "box#0"), Some(req), "requests still go before the campaign");
    assert_eq!(h.lease("me", "box#1"), Some(camp));
    assert_eq!(h.lease("other", "x"), Some(fill), "another account's campaign does not reorder this one");

    // The account's campaign reaches a worker without a row; 0 on the worker overrides it.
    let mut h = H::new();
    let camp = h.auto(B128);
    let pc = h.t(camp).pos;
    h.env.info(pc).campaign = 3;
    let high = h.auto(B128);
    let ph = h.t(high).pos;
    h.env.priority.insert(ph, 50.0);
    let pref = Pref { serve_campaign: Some(3), ..Default::default() };
    h.apply(Input::SetPref { now: h.now, account: "me".into(), pref });
    h.apply(Input::SetServing { now: h.now, account: "me".into(), machine: "nocamp".into(), serving: Serving { mode: WorkerMode::Account, campaign: Some(0) } });
    let r = h.lease_req("me", "nocamp");
    assert_eq!(h.q.peek_lease(&r, &h.env), Some(high));
    assert_eq!(h.lease("me", "anybox"), Some(camp));

    // Personal: own first, then the campaign, then idle.
    let mut h = H::new();
    h.user("stranger", B128);
    h.auto(B128);
    let own = h.user("me", B128);
    let camp = h.auto(B128);
    let pc = h.t(camp).pos;
    h.env.info(pc).campaign = 9;
    h.apply(Input::SetServing { now: h.now, account: "me".into(), machine: "p".into(), serving: Serving { mode: WorkerMode::Personal, campaign: Some(9) } });
    assert_eq!(h.drain_as("me", "p", 3), vec![own, camp]);
}

/// test_worker_serving.py::WorkerServingLeaseTests::test_server_mode_serves_the_older_request_over_its_own
/// ::test_personal_mode_is_idle_without_own_requests
#[test]
fn server_and_personal_modes() {
    let mut h = H::new();
    let stranger = h.user("stranger", B128);
    h.user("me", B128);
    h.apply(Input::SetServing { now: h.now, account: "me".into(), machine: "srv#4".into(), serving: Serving { mode: WorkerMode::Server, campaign: None } });
    assert_eq!(h.lease("me", "srv#0"), Some(stranger), "every slot of the machine shares the row");

    let mut h = H::new();
    h.user("stranger", B128);
    h.auto(B128);
    h.apply(Input::SetServing { now: h.now, account: "me".into(), machine: "p".into(), serving: Serving { mode: WorkerMode::Personal, campaign: None } });
    assert_eq!(h.lease("me", "p"), None);
}

/// test_lanes.py::BackedRequestLaneTests::test_a_backed_request_rides_the_backers_empty_queue
#[test]
fn a_backed_request_rides_the_backers_empty_queue() {
    let mut h = H::new();
    for _ in 0..5 {
        h.user("author", B128);
    }
    let p = h.pos();
    assert_eq!(h.request("author", p, |_| {}).status, "queued");
    let buried = h.q.live_tasks_of(p)[0].id;
    assert_eq!(h.request("backer", p, |r| r.ip = "10.9.9.9".into()).status, "already-queued");
    assert_eq!(h.t(buried).lane_account, "backer");
    assert_eq!(h.t(buried).requested_by, "author");
    let first = h.serve("w", 2);
    assert!(first.contains(&buried));
}

/// test_lanes.py::ContributorPredicateTests::*
#[test]
fn contributor_predicate() {
    let mut h = H::new();
    let id = h.user("x", B128);
    h.lease("alice", "m");
    h.complete_as(id, "alice");
    assert!(h.q.ran_a_worker("alice", h.now));
    assert!(!h.q.ran_a_worker("bob", h.now));
    h.ping("bob", "bm");
    assert!(!h.q.ran_a_worker("bob", h.now), "saying hello forever earns nothing");
    h.advance(3 * DAY);
    assert!(h.q.ran_a_worker("alice", h.now));
    h.advance(4 * DAY + HOUR);
    assert!(!h.q.ran_a_worker("alice", h.now));
    assert!(!h.q.ran_a_worker("", h.now));
}

/// test_lanes.py::CancelledWorkCostsNothingTests::test_cancelled_rows_are_not_charged_to_an_account
/// test_lanes.py::QueuePageTests::test_a_closed_position_is_not_queue_anybody_is_waiting_behind
#[test]
fn charged_loads() {
    let mut h = H::new();
    let a = h.user("a", B128);
    h.user("a", B128);
    let closed = h.user("a", B128);
    let pc = h.t(closed).pos;
    h.env.close(pc, Status::Draw);
    assert_eq!(h.status(Input::Withdraw { now: h.now, account: "a".into(), task: a }), "cancelled");
    let loads = h.q.charged_loads(&["a".to_string()], &h.env);
    assert_eq!(loads["a"], B128);
}

trait HExt {
    fn drain_as(&mut self, account: &str, machine: &str, n: usize) -> Vec<u64>;
    fn complete_as(&mut self, id: u64, account: &str);
}
impl HExt for H {
    fn drain_as(&mut self, account: &str, machine: &str, n: usize) -> Vec<u64> {
        let mut out = Vec::new();
        for _ in 0..n {
            match self.lease(account, machine) {
                Some(id) => {
                    self.complete(id);
                    out.push(id);
                }
                None => break,
            }
        }
        out
    }
    fn complete_as(&mut self, id: u64, account: &str) {
        let mut r = self.submit_req(id);
        r.account = account.into();
        assert!(matches!(self.submit(r), SubmitOut::Accepted { .. }));
    }
}
