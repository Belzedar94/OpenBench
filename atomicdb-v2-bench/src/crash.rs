//! Crash recovery: kill -9 the writer at a random moment, reopen, and compare
//! the whole state with a reference that applied exactly the recovered number
//! of batches.
//!
//! The writer is a child process that prints `ACK <seq>` after each durable
//! batch.  The parent kills it (TerminateProcess; for Postgres it kills the
//! server's process tree instead), reopens the store, times the recovery, and
//! checks three things: nothing acknowledged was lost, at most the one batch in
//! flight was added, and the state hash equals the in-memory reference's.

use crate::dataset::Dataset;
use crate::model::Rng;
use crate::store;
use crate::stores::mem::MemStore;
use crate::util::{emit, Args};
use crate::{dataset_counts, open_opts, step};
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

fn research(seq: u64) -> bool { seq % 4 == 0 }

pub fn child(a: &Args) {
    let kind = a.s("store", "mmap");
    let (dn, de) = dataset_counts(&a.s("data", "data"));
    let o = open_opts(a, dn, de);
    let (seed, bsize, q) = (a.n("seed", 7), a.n("batch", 8) as usize, a.f("q-pv", 0.95));
    let mut s = store::open(&kind, &o, None);
    let mut seq = s.last_seq();
    println!("OPEN {seq}");
    loop {
        seq += 1;
        step(&mut *s, seq, seed, bsize, q, research(seq));
        println!("ACK {seq}");
    }
}

fn pg_kill(pgdata: &str) {
    if let Ok(t) = std::fs::read_to_string(PathBuf::from(pgdata).join("postmaster.pid")) {
        if let Some(pid) = t.lines().next() {
            let mut cmd = if cfg!(windows) { let mut c = Command::new("taskkill"); c.args(["/F", "/T", "/PID", pid.trim()]); c }
                          else { let mut c = Command::new("kill"); c.args(["-9", pid.trim()]); c };
            let _ = cmd.stdout(Stdio::null()).stderr(Stdio::null()).status();
        }
    }
}
fn pg_start(pgctl: &str, pgdata: &str) {
    let log = PathBuf::from(pgdata).with_extension("log");
    let st = Command::new(pgctl).args(["-D", pgdata, "-l", log.to_str().unwrap(), "-w", "-t", "600", "start"]).stdout(Stdio::null()).stderr(Stdio::null()).status().unwrap();
    assert!(st.success(), "pg_ctl start failed");
}

pub fn run(a: &Args) {
    let kind = a.s("store", "mmap");
    let data = a.s("data", "data");
    let ds = Dataset::open(&PathBuf::from(&data), true);
    let o = open_opts(a, ds.n as u64, ds.e as u64);
    let (seed, bsize, q) = (a.n("seed", 7), a.n("batch", 8) as usize, a.f("q-pv", 0.95));
    let trials = a.n("trials", 20);
    let (min_ms, max_ms) = (a.n("min-ms", 200), a.n("max-ms", 2500));
    let out = a.get("out").map(PathBuf::from);
    let (pgctl, pgdata) = (a.get("pgctl"), a.get("pgdata"));
    let is_pg = kind.starts_with("pg");
    let mut reference = MemStore::from_dataset(&ds);
    let mut ref_seq = 0u64;
    let mut rng = Rng::new(seed ^ 0xC8A5);
    let exe = std::env::current_exe().unwrap();
    let pass: Vec<String> = std::env::args().skip(2).collect();
    let (mut ok, mut lost, mut differ) = (0u64, 0u64, 0u64);
    let mut recov = crate::util::Lat::new();
    for trial in 0..trials {
        let mut ch = Command::new(&exe).arg("crash-child").args(&pass).stdout(Stdio::piped()).stderr(Stdio::inherit()).spawn().unwrap();
        let acked = Arc::new(AtomicU64::new(0));
        let opened = Arc::new(AtomicU64::new(u64::MAX));
        let (a2, o2) = (acked.clone(), opened.clone());
        let so = ch.stdout.take().unwrap();
        let th = std::thread::spawn(move || {
            for l in BufReader::new(so).lines().map_while(Result::ok) {
                if let Some(v) = l.strip_prefix("ACK ") { a2.store(v.trim().parse().unwrap(), Ordering::SeqCst); }
                else if let Some(v) = l.strip_prefix("OPEN ") { o2.store(v.trim().parse().unwrap(), Ordering::SeqCst); }
            }
        });
        // Let it commit at least one batch, then kill at a random moment.
        let t0 = Instant::now();
        let start_ack = loop {
            let o_ = opened.load(Ordering::SeqCst);
            if o_ != u64::MAX && acked.load(Ordering::SeqCst) > o_ { break o_; }
            if t0.elapsed() > Duration::from_secs(600) { panic!("child never acknowledged a batch"); }
            if let Ok(Some(st)) = ch.try_wait() { panic!("child exited early: {st}"); }
            std::thread::sleep(Duration::from_millis(5));
        };
        std::thread::sleep(Duration::from_millis(min_ms + rng.below(max_ms - min_ms + 1)));
        if is_pg { pg_kill(pgdata.as_deref().expect("--pgdata")); }
        let _ = ch.kill();
        let _ = ch.wait();
        let _ = th.join();
        let last_ack = acked.load(Ordering::SeqCst);
        // Recovery.
        let t = Instant::now();
        if is_pg { pg_start(pgctl.as_deref().expect("--pgctl"), pgdata.as_deref().unwrap()); }
        let mut s = store::open(&kind, &o, None);
        let recovered = s.last_seq();
        let recovery_ms = t.elapsed().as_secs_f64() * 1e3;
        recov.add_us((recovery_ms * 1e3) as u64);
        while ref_seq < recovered { ref_seq += 1; step(&mut reference, ref_seq, seed, bsize, q, research(ref_seq)); }
        let durable_ok = recovered >= last_ack && recovered <= last_ack + 1;
        let identical = ref_seq == recovered && crate::workload::digest(&mut *s) == crate::workload::digest(&mut reference);
        if !durable_ok { lost += 1; }
        if !identical { differ += 1; }
        if durable_ok && identical { ok += 1; }
        emit(&out, &format!("{{\"stage\":\"crash_trial\",\"store\":\"{}\",\"kind\":\"{}\",\"trial\":{},\"started_at_seq\":{},\"last_ack\":{},\"recovered_seq\":{},\"recovery_ms\":{:.1},\"durable_ok\":{},\"identical\":{},\"positions\":{}}}",
            s.name(), kind, trial, start_ack, last_ack, recovered, recovery_ms, durable_ok, identical, s.node_count()));
        drop(s);
        if !identical && ref_seq > recovered { eprintln!("store went backwards: cannot continue"); break; }
    }
    emit(&out, &format!("{{\"stage\":\"crash_summary\",\"store\":\"{}\",\"trials\":{},\"ok\":{},\"lost_acknowledged\":{},\"state_differs\":{},\"final_seq\":{},\"recovery\":{}}}",
        kind, trials, ok, lost, differ, ref_seq, recov.json()));
}
