//! shakmaty-syzygy against python-chess (plan open question 5): reads the
//! JSON lines `tools/tb_golden.py` writes and probes every position again
//! with av2-tb.  Reports every disagreement, split by whether v1 could ever
//! probe the position (v1 probes only open, non-terminal positions).
//!
//! ```text
//! cargo run --release -p av2-tb --example tbequiv -- "DIR[;DIR]" [--mmap] [--threads N] golden.jsonl...
//! ```

use av2_tb::Tablebases;
use serde_json::Value;
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader};
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;

fn main() {
    let mut args = std::env::args().skip(1);
    let paths = args.next().expect("tables");
    let mut threads = 1;
    let mut access = av2_tb::Access::Read;
    let mut files = Vec::new();
    while let Some(a) = args.next() {
        if a == "--mmap" {
            access = av2_tb::Access::Mmap;
        } else if a == "--threads" {
            threads = args.next().unwrap().parse().unwrap();
        } else {
            files.push(a);
        }
    }
    let t0 = Instant::now();
    let tb = Tablebases::open_list_with(&paths, access);
    println!("opened {} files in {:.2} s ({access:?})", tb.files(), t0.elapsed().as_secs_f64());
    let mut recs = Vec::new();
    for f in &files {
        for l in BufReader::new(std::fs::File::open(f).unwrap()).lines() {
            let l = l.unwrap();
            if !l.is_empty() {
                recs.push(serde_json::from_str::<Value>(&l).unwrap());
            }
        }
    }
    let tally: Mutex<BTreeMap<String, (u64, Vec<String>)>> = Mutex::new(BTreeMap::new());
    let next = AtomicUsize::new(0);
    let by_men: Mutex<BTreeMap<u64, (u64, f64)>> = Mutex::new(BTreeMap::new());
    let t0 = Instant::now();
    std::thread::scope(|s| {
        for _ in 0..threads {
            s.spawn(|| {
                loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    let Some(r) = recs.get(i) else { break };
                    let fen = r["fen"].as_str().unwrap();
                    let men = r["men"].as_u64().unwrap();
                    // The first probe opens and maps the table; the second,
                    // timed, is the warm latency (python-chess: tools/tb_time.py).
                    let ours = tb.probe_wdl(fen, 6);
                    let t = Instant::now();
                    let again = tb.probe_wdl(fen, 6);
                    let dt = t.elapsed().as_secs_f64();
                    assert_eq!(ours, again);
                    if r["terminal"].is_null() {
                        let mut m = by_men.lock().unwrap();
                        let e = m.entry(men).or_default();
                        e.0 += 1;
                        e.1 += dt;
                    }
                    let theirs = r["wdl"].as_i64().map(|v| v as i32);
                    let terminal = !r["terminal"].is_null();
                    let class = if ours == theirs {
                        format!("agree {}", if theirs.is_some() { "value" } else { "none" })
                    } else {
                        format!(
                            "DISAGREE {} ours={ours:?} python-chess={theirs:?} err={} shakmaty={:?}",
                            if terminal { "terminal(v1 never probes)" } else { "open" },
                            r["err"],
                            av2_tb::position_error(fen)
                        )
                    };
                    let mut m = tally.lock().unwrap();
                    let e = m.entry(class).or_default();
                    e.0 += 1;
                    if e.1.len() < 4 && !e.1.iter().any(|x| x == fen) {
                        e.1.push(fen.to_string());
                    }
                }
            });
        }
    });
    println!("{} positions, {threads} threads, {:.1} s", recs.len(), t0.elapsed().as_secs_f64());
    let mut disagree = 0;
    for (k, (n, ex)) in tally.lock().unwrap().iter() {
        println!("  {k}: {n}");
        if k.starts_with("DISAGREE") {
            disagree += n;
            for e in ex {
                println!("      {e}");
            }
        }
    }
    for (men, (n, secs)) in by_men.lock().unwrap().iter() {
        println!("  {men} men: {n} probes, {:.1} us/probe (wall per thread)", secs * 1e6 / *n as f64);
    }
    if disagree > 0 {
        std::process::exit(1);
    }
}
