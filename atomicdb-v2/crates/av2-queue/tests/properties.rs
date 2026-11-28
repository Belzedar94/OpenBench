//! Property tests: random workloads, deterministic seeds (`av2_model::Rng`).
//!
//! After every input (through `H::apply`): the invariants hold and the deltas
//! rebuild the state.  On top of that, per input:
//! - **no double lease**: a grant is either a replay of the same token, or a
//!   row that was PENDING just before; a superseded token can never submit;
//! - **expiry**: after any lease call no LEASED row is silent for more than
//!   `LEASE_MINUTES`, and no slot holds two live leases;
//! - **replay**: rebuilding from a snapshot plus the logged facts gives the
//!   same digest as the live state.
//!
//! And two fairness bounds on random queues.

mod common;
use av2_model::{Rng, Status};
use av2_queue::consts::*;
use av2_queue::*;
use common::*;
use std::collections::BTreeMap;

const ACCOUNTS: [&str; 5] = ["", "alice", "bob", "carol", "dave"];
const MACHINES: [&str; 6] = ["m1#0", "m1#1", "m2", "m3#0", "m3#1", "m4"];

fn workload(seed: u64, steps: usize) {
    let mut rng = Rng::new(seed);
    let mut h = H::new();
    let positions: Vec<u64> = (0..40).map(|_| h.pos()).collect();
    for &p in &positions {
        if rng.chance(0.1) {
            h.env.info(p).tb_applicable = true;
        }
    }
    let mut sessions: BTreeMap<&str, String> = BTreeMap::new();
    let mut superseded: Vec<(u64, String, String)> = Vec::new(); // (task, machine, old token)
    let mut snapshot = h.q.snapshot();
    let mut log: Vec<av2_facts::Fact> = Vec::new();
    for step in 0..steps {
        h.advance(rng.below(90) as Ms * SECOND);
        let pick = rng.below(100);
        let input = if pick < 30 {
            let m = MACHINES[rng.below(MACHINES.len() as u64) as usize];
            let account = ACCOUNTS[1 + rng.below(4) as usize];
            let mut r = h.lease_req(account, m);
            if rng.chance(0.7) {
                if let Some(s) = sessions.get(m) {
                    r.session = s.clone();
                }
            }
            sessions.insert(m, r.session.clone());
            if rng.chance(0.2) {
                r.max_budget = Some([B128, B512][rng.below(2) as usize]);
            }
            r.tb = rng.chance(0.5);
            r.telemetry.threads = [0, 4, 8, 32][rng.below(4) as usize];
            Input::Lease(r)
        } else if pick < 50 {
            let p = positions[rng.below(positions.len() as u64) as usize];
            let account = ACCOUNTS[rng.below(5) as usize];
            let mut r = RequestReq { now: h.now, account: account.into(), pos: p, ip: format!("10.0.0.{}", rng.below(3)), ..Default::default() };
            r.background = rng.chance(0.3);
            if rng.chance(0.2) {
                r.budget_floor = Some(REQUEST_BUDGET_LADDER[rng.below(3) as usize]);
            }
            r.only_if_missing = rng.chance(0.1);
            Input::Request(r)
        } else if pick < 62 {
            // Submit or heartbeat a leased task, sometimes with a stale token.
            let leased: Vec<u64> = h.q.tasks().filter(|t| t.state == TState::Leased).map(|t| t.id).collect();
            if leased.is_empty() {
                Input::Tick { now: h.now }
            } else {
                let id = leased[rng.below(leased.len() as u64) as usize];
                let t = h.t(id).clone();
                let token = if rng.chance(0.8) { t.token.clone() } else { "forged".into() };
                if rng.chance(0.5) {
                    let mut r = h.submit_req(id);
                    r.token = token;
                    r.gate_rejects = rng.chance(0.05);
                    Input::Submit(r)
                } else {
                    Input::Heartbeat(HeartbeatReq { now: h.now, account: "w".into(), machine: t.machine.clone(), task: Some(id), token, ..Default::default() })
                }
            }
        } else if pick < 66 {
            let leased: Vec<(u64, String, String)> =
                h.q.tasks().filter(|t| t.state == TState::Leased).map(|t| (t.id, t.machine.clone(), t.token.clone())).collect();
            let entries = leased
                .iter()
                .filter(|_| rng.chance(0.5))
                .map(|(id, m, tok)| ReleaseEntry { kind: LeaseKind::Analysis, task: *id, token: tok.clone(), owner: m.clone() })
                .collect::<Vec<_>>();
            let caller = entries.first().map(|e| e.owner.clone()).unwrap_or_else(|| "m1#0".into());
            Input::Release(ReleaseReq { now: h.now, account: "w".into(), machine: caller, entries })
        } else if pick < 74 {
            let ids: Vec<u64> = h.q.tasks().filter(|t| t.source == Source::User).map(|t| t.id).collect();
            if ids.is_empty() {
                Input::Tick { now: h.now }
            } else {
                let id = ids[rng.below(ids.len() as u64) as usize];
                let author = h.t(id).requested_by.clone();
                match rng.below(3) {
                    0 => Input::Withdraw { now: h.now, account: author, task: id },
                    1 => Input::Undo { now: h.now, account: author, task: id },
                    _ => Input::Bump { now: h.now, account: author, task: id },
                }
            }
        } else if pick < 79 {
            // A landing: the position's visit count moves on and covered requests are absorbed.
            let done: Vec<u64> = h.q.tasks().filter(|t| t.state == TState::Completed && !t.machine.is_empty()).map(|t| t.id).collect();
            if done.is_empty() {
                Input::Tick { now: h.now }
            } else {
                let id = done[rng.below(done.len() as u64) as usize];
                let (p, generation, budget) = (h.t(id).pos, h.t(id).generation, h.t(id).budget);
                if h.env.info(p).visits <= generation {
                    h.env.info(p).visits = generation + 1;
                }
                Input::Landed { now: h.now, pos: p, task: Some(id), arbiter: budget }
            }
        } else if pick < 82 {
            let p = positions[rng.below(positions.len() as u64) as usize];
            if rng.chance(0.5) {
                h.env.close(p, Status::WhiteWin);
                Input::PositionClosed { now: h.now, pos: p }
            } else {
                h.env.close(p, Status::Unknown);
                Input::Tick { now: h.now }
            }
        } else if pick < 90 {
            let p = positions[rng.below(positions.len() as u64) as usize];
            let arm = ["", ARM_REPAIR, ARM_SWEEP16, ARM_COVERAGE][rng.below(4) as usize];
            let source = if arm == ARM_SWEEP16 || arm == ARM_COVERAGE { Source::Fill } else { Source::Auto };
            let plan = MintPlan { tasks: vec![MintTask { pos: p, budget: B128, multipv: 5, source, arm: arm.into() }], ..Default::default() };
            Input::Mint { now: h.now, plan }
        } else if pick < 93 {
            Input::Tick { now: h.now }
        } else if pick < 96 {
            let account = ACCOUNTS[1 + rng.below(4) as usize];
            Input::ClearQueue { now: h.now, account: account.into() }
        } else {
            let account = ACCOUNTS[1 + rng.below(4) as usize];
            let pref = Pref {
                lifo_queue: rng.chance(0.5),
                serve_order: [ServeOrder::Own, ServeOrder::Queue, ServeOrder::Others][rng.below(3) as usize],
                serve_for: if rng.chance(0.5) { vec!["Alice".into(), "anonymous".into()] } else { vec![] },
                serve_campaign: None,
            };
            Input::SetPref { now: h.now, account: account.into(), pref }
        };

        let before = h.q.clone();
        let applied = h.apply(input.clone());
        log.push(av2_queue::fact::record(&input, &applied));

        // No double lease.
        if let Output::Lease(LeaseOut::Task { grant }) = &applied.output {
            let was = before.task(grant.id).unwrap();
            if grant.replayed {
                assert_eq!((was.state, &was.token), (TState::Leased, &grant.token), "seed {seed} step {step}");
            } else {
                // Either PENDING, or a lease that was dead (silent past the
                // window) or relieved (same slot, silent past the restart window).
                if was.state == TState::Leased {
                    let Input::Lease(r) = &input else { unreachable!() };
                    let beat = was.heartbeat_at.or(was.leased_at).unwrap();
                    let dead = beat < h.now - LEASE_MINUTES;
                    let relieved = was.machine == r.machine && beat < h.now - RESTART_RECYCLE_MINUTES;
                    assert!(dead || relieved, "seed {seed} step {step}: granted a live lease of {}", was.machine);
                } else {
                    assert_eq!(was.state, TState::Pending, "seed {seed} step {step}: granted a non-PENDING row");
                }
                if !was.machine.is_empty() || was.leased_at.is_some() {
                    superseded.push((grant.id, String::new(), String::new()));
                }
            }
        }
        for t in before.tasks() {
            if t.state == TState::Leased {
                let now = h.q.task(t.id);
                if let Some(n) = now {
                    if n.state == TState::Leased && n.token != t.token {
                        superseded.push((t.id, t.machine.clone(), t.token.clone()));
                    }
                }
            }
        }
        // Expiry after a lease call; one live lease per slot.
        if matches!(input, Input::Lease(_)) {
            if let Output::Lease(LeaseOut::UpgradeRequired) = applied.output {
            } else {
                for t in h.q.tasks().filter(|t| t.state == TState::Leased) {
                    let beat = t.heartbeat_at.unwrap_or(0).max(t.leased_at.unwrap_or(0));
                    assert!(beat >= h.now - LEASE_MINUTES, "seed {seed} step {step}: task {} silent since {}", t.id, h.now - beat);
                }
            }
        }
        let mut live_by_slot: BTreeMap<&str, u32> = BTreeMap::new();
        for t in h.q.tasks().filter(|t| t.state == TState::Leased) {
            let beat = t.heartbeat_at.or(t.leased_at).unwrap();
            if beat >= h.now - LEASE_MINUTES {
                *live_by_slot.entry(t.machine.as_str()).or_default() += 1;
            }
        }
        assert!(live_by_slot.values().all(|&n| n <= 1), "seed {seed} step {step}: {live_by_slot:?}");
        // Superseded tokens are fenced.
        for (id, machine, token) in &superseded {
            if machine.is_empty() {
                continue;
            }
            let mut probe = h.q.clone();
            let r = SubmitReq { now: h.now, account: "w".into(), machine: machine.clone(), task: *id, token: token.clone(), ..Default::default() };
            let out = probe.apply(&Input::Submit(r), &h.env, &mut NoRefill).output;
            assert!(
                !matches!(out, Output::Submit(SubmitOut::Accepted { .. })),
                "seed {seed} step {step}: a superseded token submitted task {id}"
            );
        }
        // Snapshot + log replay = live state (every 50 inputs a new snapshot).
        if step % 50 == 49 {
            let mut rebuilt = QueueState::restore(&snapshot).unwrap();
            av2_queue::fact::replay(&mut rebuilt, &log).unwrap();
            assert_eq!(rebuilt.digest(), h.q.digest(), "seed {seed} step {step}: replay differs");
            snapshot = h.q.snapshot();
            log.clear();
        }
    }
}

/// The lazy lease order (plain partitions merged by their heads) equals the
/// full sorted order, for random queues and every kind of worker.
#[test]
fn lazy_order_equals_full_order() {
    for seed in 1..=60 {
        let mut rng = Rng::new(seed);
        let mut h = H::new();
        let accounts = ["", "alice", "bob", "Carol", "carol", "dave", "eve"];
        for _ in 0..(20 + rng.below(200)) {
            let a = accounts[rng.below(accounts.len() as u64) as usize].to_string();
            let budget = [B128, B512, B2, B10][rng.below(4) as usize];
            let created_back = rng.below(60) as Ms * HOUR;
            let mut also = Vec::new();
            if rng.chance(0.1) {
                let b = accounts[1 + rng.below(6) as usize].to_string();
                if b != a {
                    also.push(b);
                }
            }
            let lane = if rng.chance(0.1) { accounts[1 + rng.below(6) as usize].to_string() } else { String::new() };
            let source = [Source::User, Source::User, Source::User, Source::Fill, Source::Auto][rng.below(5) as usize];
            let state = if rng.chance(0.15) { TState::Leased } else { TState::Pending };
            let bg = rng.chance(0.2);
            let seq = if rng.chance(0.2) { -(rng.below(5) as i64) } else { 0 };
            let id = h.row(budget, |t| {
                t.source = source;
                t.requested_by = a.clone();
                t.also = also;
                t.lane_account = if lane == a { String::new() } else { lane };
                t.state = state;
                t.background = bg;
                t.queue_seq = seq;
                t.created = T0 - created_back;
                if state == TState::Leased {
                    t.machine = "x".into();
                    t.leased_at = Some(T0 - rng.below(90) as Ms * MINUTE);
                    t.heartbeat_at = t.leased_at;
                    t.token = "t".into();
                    t.attempts = 1;
                }
            });
            if rng.chance(0.05) {
                let p = h.t(id).pos;
                h.env.close(p, Status::Draw);
            }
            let p = h.t(id).pos;
            h.env.priority.insert(p, rng.below(100) as f64 - 50.0);
        }
        for (i, worker) in ["alice", "bob", "carol", "zed", "codex_local_worker"].iter().enumerate() {
            let pref = Pref {
                serve_order: [ServeOrder::Own, ServeOrder::Queue, ServeOrder::Others][(seed as usize + i) % 3],
                serve_for: if i % 2 == 0 { vec!["CAROL".into(), "anonymous".into()] } else { vec![] },
                ..Default::default()
            };
            h.q.import_pref(worker, pref);
            let (full, lazy) = h.q.orders_for_test(worker, "m", h.now, &h.env);
            assert_eq!(full, lazy, "seed {seed} worker {worker}");
        }
    }
}

#[test]
fn random_workloads_keep_every_invariant() {
    for seed in 1..=12 {
        workload(seed, 400);
    }
}

/// Fairness, deficit round robin by cost: with running work kept (leases
/// not submitted), the nodes served to two accounts that both still have
/// queue never differ by more than the largest budget.
#[test]
fn fair_share_bound_by_cost() {
    for seed in 1..=20 {
        let mut rng = Rng::new(seed);
        let mut h = H::new();
        let n_accounts = 2 + rng.below(4) as usize;
        let accounts: Vec<String> = (0..n_accounts).map(|i| format!("acct{i}")).collect();
        for _ in 0..200 {
            let a = &accounts[rng.below(n_accounts as u64) as usize];
            let b = [B128, B512, B2][rng.below(3) as usize];
            h.user(a, b);
        }
        let mut served: BTreeMap<String, u64> = BTreeMap::new();
        for _ in 0..80 {
            let Some(id) = h.serve("w", 1).first().copied() else { break };
            let t = h.t(id);
            *served.entry(t.requested_by.clone()).or_default() += t.budget;
            let waiting: Vec<&String> = accounts.iter().filter(|a| h.q.tasks().any(|t| &t.requested_by == *a && t.state == TState::Pending)).collect();
            for a in &waiting {
                for b in &waiting {
                    let (x, y) = (served.get(*a).copied().unwrap_or(0), served.get(*b).copied().unwrap_or(0));
                    assert!(x.abs_diff(y) <= B2, "seed {seed}: {a} {x} vs {b} {y}");
                }
            }
        }
    }
}

/// Fairness with one worker submitting each search (no running debt): the
/// least-recently-served key makes it a round robin by count.
#[test]
fn single_worker_round_robin() {
    for seed in 1..=10 {
        let mut rng = Rng::new(seed);
        let mut h = H::new();
        let n_accounts = 2 + rng.below(4) as usize;
        for i in 0..n_accounts {
            for _ in 0..(10 + rng.below(10)) {
                h.user(&format!("acct{i}"), [B128, B2][rng.below(2) as usize]);
            }
        }
        let mut count: BTreeMap<String, i64> = BTreeMap::new();
        for _ in 0..40 {
            let Some(id) = h.drain("w", 1).first().copied() else { break };
            *count.entry(h.t(id).requested_by.clone()).or_default() += 1;
            let waiting: Vec<String> = (0..n_accounts)
                .map(|i| format!("acct{i}"))
                .filter(|a| h.q.tasks().any(|t| &t.requested_by == a && t.state == TState::Pending))
                .collect();
            for a in &waiting {
                for b in &waiting {
                    let d = count.get(a).copied().unwrap_or(0) - count.get(b).copied().unwrap_or(0);
                    assert!(d.abs() <= 1, "seed {seed}: {count:?}");
                }
            }
        }
    }
}
