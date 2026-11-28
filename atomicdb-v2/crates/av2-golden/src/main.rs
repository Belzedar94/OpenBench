//! `av2-golden`: replay v1's golden cases on v2.
//!
//! ```text
//! av2-golden [--mode v2|echo|noop] [--kind drain|stage_a] [--limit N]
//!            [--json REPORT.json] [--cases CASES.jsonl] [--examples N]
//!            [--fail-on-diff] PATH...
//! ```
//!
//! PATH is a case file (`.jsonl`, `.jsonl.gz`, `.jsonl.zst`) or a directory
//! of them (the recorder's output directory, copied from production).
//! `--cases` writes one line per case (status, counts, first differences).
//! `--fail-on-diff` exits 1 if any case is not `clean` (echo mode in CI).

use av2_golden::replay::{self, Mode};
use std::io::Write;
use std::path::PathBuf;

fn usage() -> ! {
    eprintln!(
        "usage: av2-golden [--mode v2|echo|noop] [--kind drain|stage_a] [--limit N] [--json REPORT] [--cases CASES.jsonl] [--examples N] [--fail-on-diff] PATH..."
    );
    std::process::exit(2)
}

fn main() {
    let mut mode = Mode::V2;
    let mut kind: Option<String> = None;
    let mut limit = None;
    let mut json_out: Option<PathBuf> = None;
    let mut cases_out: Option<PathBuf> = None;
    let mut examples = 5usize;
    let mut fail_on_diff = false;
    let mut paths = Vec::new();
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        let mut val = || it.next().unwrap_or_else(|| usage());
        match a.as_str() {
            "--mode" => {
                mode = val().parse().unwrap_or_else(|e| {
                    eprintln!("{e}");
                    usage()
                })
            }
            "--kind" => kind = Some(val()),
            "--limit" => limit = Some(val().parse().unwrap_or_else(|_| usage())),
            "--json" => json_out = Some(val().into()),
            "--cases" => cases_out = Some(val().into()),
            "--examples" => examples = val().parse().unwrap_or_else(|_| usage()),
            "--fail-on-diff" => fail_on_diff = true,
            "-h" | "--help" => usage(),
            _ if a.starts_with("--") => usage(),
            _ => paths.push(PathBuf::from(a)),
        }
    }
    if paths.is_empty() {
        usage();
    }
    let files = av2_golden::read::case_files(&paths);
    let mut cases_file =
        cases_out.map(|p| std::io::BufWriter::new(std::fs::File::create(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()))));
    let started = std::time::Instant::now();
    let sum = replay::run_files(&files, mode, kind.as_deref(), limit, examples, |r| {
        if let Some(f) = cases_file.as_mut() {
            let mut v = serde_json::to_value(r).unwrap_or_default();
            if let Some(d) = v.get_mut("diffs").and_then(|d| d.as_array_mut()) {
                d.truncate(20);
            }
            let _ = writeln!(f, "{v}");
        }
    });
    print!("{}", sum.text());
    println!("  wall {:.2}s", started.elapsed().as_secs_f64());
    if let Some(p) = json_out {
        let mut v = sum.json();
        v["wall_s"] = serde_json::json!(started.elapsed().as_secs_f64());
        std::fs::write(&p, serde_json::to_string_pretty(&v).unwrap()).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    }
    if fail_on_diff {
        let bad: u64 = sum.by_status.values().flat_map(|m| m.iter()).filter(|(k, _)| **k != "clean").map(|(_, n)| *n).sum();
        if bad > 0 || sum.cases == 0 {
            eprintln!("av2-golden: {bad} case(s) not clean");
            std::process::exit(1);
        }
    }
}
