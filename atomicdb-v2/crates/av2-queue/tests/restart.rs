//! A restart must not waste a running 2B-node search.
//!
//! The queue facts go into real store frames (`av2_store::Store::commit`);
//! the process "dies" (the store is dropped without a queue snapshot of the
//! latest state); recovery restores the last queue snapshot and replays the
//! queue facts of the log after it.  The worker that held the lease then
//! heartbeats and submits with the token it was given before the restart.

mod common;
use av2_queue::consts::*;
use av2_queue::*;
use av2_store::{Config, Store};
use common::*;

fn tmp(name: &str) -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!("av2-queue-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    d
}

fn cfg() -> Config {
    Config { durable: false, background_checkpoint: false, initial_positions: 1024, shard_bits: 2, ..Config::default() }
}

/// Commit one input's fact as its own frame (as the writer does per batch).
fn commit(store: &mut Store, input: &Input, applied: &Applied) -> u64 {
    let fact = av2_queue::fact::record(input, applied);
    let bytes = av2_facts::encode_all(&[fact]);
    let batch = store.batch();
    store.commit(batch, &bytes).unwrap()
}

/// Read every queue fact of the store's log and archive, in order, with seq.
fn queue_facts(dir: &std::path::Path, after: u64) -> Vec<av2_facts::Fact> {
    let mut out = Vec::new();
    let mut last = 0;
    for d in [dir.join("archive"), dir.join("log")] {
        if !d.exists() {
            continue;
        }
        av2_store::log::for_each_frame(&d, |_, fr| {
            if fr.seq > last && fr.seq > after {
                last = fr.seq;
                out.extend(av2_facts::decode_all(fr.facts).unwrap());
            }
            Ok(())
        })
        .unwrap();
    }
    out
}

#[test]
fn a_restart_does_not_waste_a_running_search() {
    let dir = tmp("restart");
    let mut store = Store::create(&dir, cfg()).unwrap();
    let mut h = H::new();
    for _ in 0..5 {
        h.user("alice", B128);
    }
    let deep = h.user("bob", B2);
    let mut snap_seq = 0;
    let mut snapshot = h.q.snapshot();
    // Some traffic, a snapshot in the middle, more traffic, then the 2B lease.
    for i in 0..4 {
        let mut r = h.lease_req("w", &format!("box#{i}"));
        r.max_budget = Some(B128);
        let input = Input::Lease(r);
        let applied = h.apply(input.clone());
        let seq = commit(&mut store, &input, &applied);
        if i == 1 {
            snapshot = h.q.snapshot();
            snap_seq = seq;
        }
        h.advance(SECOND);
    }
    let mut r = h.lease_req("bob", "bigbox");
    r.session = "bob-session".into();
    let input = Input::Lease(r);
    let applied = h.apply(input.clone());
    commit(&mut store, &input, &applied);
    let Output::Lease(LeaseOut::Task { grant }) = applied.output else { panic!() };
    assert_eq!(grant.id, deep, "bob's own 2B request");
    let live_digest = h.q.digest();
    // kill: no snapshot of the latest state, the store is dropped.
    drop(store);

    // Recovery.
    let store = Store::open(&dir, cfg()).unwrap();
    let mut q = QueueState::restore(&snapshot).unwrap();
    let facts = queue_facts(&dir, snap_seq);
    let n = av2_queue::fact::replay(&mut q, &facts).unwrap();
    assert_eq!(n, 3);
    assert_eq!(q.digest(), live_digest);
    let t = q.task(deep).unwrap();
    assert_eq!((t.state, t.token.as_str(), t.machine.as_str()), (TState::Leased, grant.token.as_str(), "bigbox"));

    // The worker keeps going: a lost lease response is replayed, the
    // heartbeat is accepted, and the result lands.
    h.q = q;
    h.advance(2 * MINUTE);
    let mut again = h.lease_req("bob", "bigbox");
    again.session = "bob-session".into();
    let LeaseOut::Task { grant: g2 } = h.lease_out(again) else { panic!() };
    assert!(g2.replayed && g2.token == grant.token);
    let hb = HeartbeatReq {
        now: h.now,
        account: "bob".into(),
        machine: "bigbox".into(),
        task: Some(deep),
        token: grant.token.clone(),
        ..Default::default()
    };
    assert!(matches!(h.apply(Input::Heartbeat(hb)).output, Output::Heartbeat(HeartbeatOut::Ok { .. })));
    h.advance(3 * MINUTE);
    let sub = SubmitReq {
        now: h.now,
        account: "bob".into(),
        machine: "bigbox".into(),
        task: deep,
        token: grant.token.clone(),
        nodes: B2,
        elapsed: 4000.0,
        lines: "[]".into(),
        ..Default::default()
    };
    assert!(matches!(h.submit(sub), SubmitOut::Accepted { .. }));
    drop(store);
    let _ = std::fs::remove_dir_all(&dir);
}

/// The snapshot is canonical and restores to the same state.
#[test]
fn snapshot_round_trip() {
    let mut h = H::new();
    for i in 0..20 {
        h.user(if i % 3 == 0 { "" } else { "alice" }, B128);
    }
    h.serve("w", 5);
    h.apply(Input::SetPref { now: h.now, account: "alice".into(), pref: Pref { lifo_queue: true, ..Default::default() } });
    let bytes = h.q.snapshot();
    let q = QueueState::restore(&bytes).unwrap();
    assert_eq!(q.snapshot(), bytes);
    q.check_invariants().unwrap();
}
