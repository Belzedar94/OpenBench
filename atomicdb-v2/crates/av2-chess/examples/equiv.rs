//! Equivalence check against v1: reads the JSON lines `tools/golden.py`
//! writes (v1's `logic.py` over pyffish) and recomputes every field with
//! av2-chess.  Any difference is a failure.
//!
//! ```text
//! cargo run --release -p av2-chess --example equiv -- [--threads N] golden1.jsonl [golden2.jsonl ...]
//! ```
//!
//! With `--threads N` the records are checked by N threads at once, all
//! calling the library concurrently (plan open question 3).

use av2_chess::{self as chess, pf};
use av2_model::Status;
use serde_json::Value;
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

fn status_name(s: Option<Status>) -> Value {
    match s {
        None => Value::Null,
        Some(s) => Value::String(s.v1().to_string()),
    }
}

#[derive(Default)]
struct Tally {
    positions: AtomicU64,
    children: AtomicU64,
    mismatches: Mutex<BTreeMap<&'static str, (u64, Vec<String>)>>,
}

impl Tally {
    fn bad(&self, field: &'static str, detail: String) {
        let mut m = self.mismatches.lock().unwrap();
        let e = m.entry(field).or_default();
        e.0 += 1;
        if e.1.len() < 5 {
            e.1.push(detail);
        }
    }
}

fn check(rec: &Value, t: &Tally) {
    let fen = rec["fen"].as_str().unwrap();
    let canon = chess::canonical_fen(fen);
    if canon != rec["canon"].as_str().unwrap() {
        t.bad("canonical_fen", format!("{fen}: {canon} != {}", rec["canon"]));
    }
    let key = chess::key_of(fen).to_hex();
    if key != rec["key"].as_str().unwrap() {
        t.bad("key_of", format!("{fen}: {key} != {}", rec["key"]));
    }
    if let Some(db) = rec.get("db_key").and_then(Value::as_str)
        && key != db
    {
        t.bad("db_key", format!("{fen}: {key} != stored {db}"));
    }
    let canon = rec["canon"].as_str().unwrap();
    let moves: Vec<&str> = rec["moves"].as_array().unwrap().iter().map(|m| m.as_str().unwrap()).collect();
    let ours = chess::legal_moves(canon).unwrap();
    if ours != moves {
        t.bad("legal_moves", format!("{canon}: {ours:?} != {moves:?}"));
    }
    let raw: Vec<&str> = rec["raw"].as_array().unwrap().iter().map(|m| m.as_str().unwrap()).collect();
    let kids = pf::children(canon).unwrap();
    let kid_pairs: Vec<(&str, &str)> = kids.iter().map(|(m, f)| (m.as_str(), f.as_str())).collect();
    let want_pairs: Vec<(&str, &str)> = moves.iter().copied().zip(raw.iter().copied()).collect();
    if kid_pairs != want_pairs {
        t.bad("children", format!("{canon}: {kid_pairs:?} != {want_pairs:?}"));
    }
    let expansion = chess::expansion(canon).unwrap();
    let kids_v = rec["kids"].as_array().unwrap();
    let kterm = rec["kid_terminal"].as_array().unwrap();
    if expansion.len() != moves.len() {
        t.bad("expansion_len", canon.to_string());
    }
    for (i, m) in moves.iter().enumerate() {
        t.children.fetch_add(1, Ordering::Relaxed);
        let want = &kids_v[i];
        let (wfen, wkey, wzero) = (want[0].as_str().unwrap(), want[1].as_str().unwrap(), want[2].as_bool().unwrap());
        let raw1 = pf::get_fen(canon, &[m]).unwrap();
        if raw1 != raw[i] {
            t.bad("get_fen", format!("{canon} {m}: {raw1} != {}", raw[i]));
        }
        let child = chess::apply_move(canon, m).unwrap();
        if child != wfen {
            t.bad("apply_move", format!("{canon} {m}: {child} != {wfen}"));
        }
        if chess::key_of(&child).to_hex() != wkey {
            t.bad("child_key", format!("{canon} {m}"));
        }
        if chess::is_zeroing(canon, m).unwrap() != wzero {
            t.bad("is_zeroing", format!("{canon} {m}"));
        }
        let ts = status_name(chess::terminal_status(&child).unwrap());
        if ts != kterm[i] {
            t.bad("child_terminal", format!("{child}: {ts} != {}", kterm[i]));
        }
        if let Some(e) = expansion.get(i)
            && (e.uci != *m || e.fen != wfen || e.key.to_hex() != wkey || status_name(e.terminal) != kterm[i])
        {
            t.bad("expansion", format!("{canon} {m}: {e:?}"));
        }
    }
    let ts = status_name(chess::terminal_status(canon).unwrap());
    if ts != rec["terminal"] {
        t.bad("terminal_status", format!("{canon}: {ts} != {}", rec["terminal"]));
    }
    // When the game does not end, pyffish returns an uninitialised value (0
    // or a stale -32000 from the stack, both seen on production); only the
    // flag and the value of an ended game are answers.
    let (imm, val) = pf::is_immediate_game_end(canon).unwrap();
    let want_imm = rec["imm"][0].as_bool().unwrap();
    if imm != want_imm || (want_imm && i64::from(val) != rec["imm"][1].as_i64().unwrap()) {
        t.bad("is_immediate_game_end", format!("{canon}: ({imm},{val}) != {}", rec["imm"]));
    }
    if !rec["result"].is_null() {
        let r = pf::game_result(canon).unwrap();
        if i64::from(r) != rec["result"].as_i64().unwrap() {
            t.bad("game_result", format!("{canon}: {r} != {}", rec["result"]));
        }
    }
    if chess::tb_applicable(canon, 6) != rec["tb6"].as_bool().unwrap() {
        t.bad("tb_applicable", canon.to_string());
    }
    t.positions.fetch_add(1, Ordering::Relaxed);
}

fn main() {
    let mut threads = 1usize;
    let mut files = Vec::new();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        if a == "--threads" {
            threads = args.next().unwrap().parse().unwrap();
        } else {
            files.push(a);
        }
    }
    println!("library: {}", pf::info());
    let mut lines = Vec::new();
    for f in &files {
        let r = BufReader::new(std::fs::File::open(f).unwrap_or_else(|e| panic!("{f}: {e}")));
        for l in r.lines() {
            let l = l.unwrap();
            if !l.is_empty() {
                lines.push(l);
            }
        }
    }
    let tally = Tally::default();
    let next = AtomicU64::new(0);
    let t0 = Instant::now();
    std::thread::scope(|s| {
        for _ in 0..threads {
            s.spawn(|| {
                loop {
                    let i = next.fetch_add(1, Ordering::Relaxed) as usize;
                    let Some(l) = lines.get(i) else { break };
                    let rec: Value = serde_json::from_str(l).unwrap();
                    check(&rec, &tally);
                }
            });
        }
    });
    let secs = t0.elapsed().as_secs_f64();
    let m = tally.mismatches.lock().unwrap();
    println!(
        "{} positions, {} children, {} threads, {:.1} s; mismatching fields: {}",
        tally.positions.load(Ordering::Relaxed),
        tally.children.load(Ordering::Relaxed),
        threads,
        secs,
        m.len()
    );
    for (field, (n, examples)) in m.iter() {
        println!("  {field}: {n}");
        for e in examples {
            println!("    {e}");
        }
    }
    if !m.is_empty() {
        std::process::exit(1);
    }
}
