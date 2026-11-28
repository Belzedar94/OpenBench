//! Throughput of the rules library, per thread and with threads together.
//!
//! ```text
//! cargo run --release -p av2-chess --example bench -- golden.jsonl [max_positions] [threads,...]
//! ```
//!
//! Reads the `canon` FENs of a golden file and measures, for each thread
//! count: `legal_moves`, `key_of` (canonical FEN + sha256), and `expansion`
//! (every legal move with child FEN, key and terminal status: what one
//! expansion costs the preparers).

use av2_chess as chess;
use serde_json::Value;
use std::io::{BufRead, BufReader};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;

fn run<F: Fn(&str) -> usize + Sync>(fens: &[String], threads: usize, f: F) -> (f64, usize) {
    let next = AtomicUsize::new(0);
    let total = AtomicUsize::new(0);
    let t0 = Instant::now();
    std::thread::scope(|s| {
        for _ in 0..threads {
            s.spawn(|| {
                let mut local = 0;
                loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    let Some(fen) = fens.get(i) else { break };
                    local += f(fen);
                }
                total.fetch_add(local, Ordering::Relaxed);
            });
        }
    });
    (t0.elapsed().as_secs_f64(), total.load(Ordering::Relaxed))
}

fn main() {
    let mut args = std::env::args().skip(1);
    let file = args.next().expect("golden.jsonl");
    let max: usize = args.next().map(|s| s.parse().unwrap()).unwrap_or(20_000);
    let threads: Vec<usize> = args.next().map(|s| s.split(',').map(|x| x.parse().unwrap()).collect()).unwrap_or(vec![1, 2, 4, 8]);
    let mut fens = Vec::new();
    for l in BufReader::new(std::fs::File::open(&file).unwrap()).lines() {
        let v: Value = serde_json::from_str(&l.unwrap()).unwrap();
        fens.push(v["canon"].as_str().unwrap().to_string());
        if fens.len() >= max {
            break;
        }
    }
    chess::pf::init();
    println!("{} positions from {file}", fens.len());
    println!("{:>8} {:>14} {:>12} {:>14} {:>14} {:>12}", "threads", "what", "secs", "per pos (us)", "pos/s total", "scaling");
    for (name, which) in [("legal_moves", 0), ("key_of", 1), ("expansion", 2)] {
        let mut base = 0.0;
        for &t in &threads {
            let (secs, _n) = run(&fens, t, |fen| match which {
                0 => chess::legal_moves(fen).unwrap().len(),
                1 => chess::key_of(fen).0[0] as usize,
                _ => chess::expansion(fen).unwrap().len(),
            });
            let rate = fens.len() as f64 / secs;
            if t == threads[0] {
                base = rate / t as f64;
            }
            println!(
                "{:>8} {:>14} {:>12.3} {:>14.2} {:>14.0} {:>11.2}x",
                t,
                name,
                secs,
                secs * 1e6 * t as f64 / fens.len() as f64,
                rate,
                rate / base
            );
        }
    }
}
