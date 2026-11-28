//! av2-replay: read and replay the log.
//!
//! ```text
//! av2-replay dump    --store DIR | --log DIR [--from SEQ] [--to SEQ]
//!                                                        facts as JSON lines, one per fact
//! av2-replay stats   --store DIR | --log DIR             frames, bytes, facts by kind
//!
//! `--store DIR` reads `DIR/archive` (the facts of released segments) and
//! then `DIR/log`; `--log` reads one directory of segments.
//! av2-replay restore --db DIR                            open (= replay the log over the last
//!                                                        checkpoint), report, checkpoint
//! av2-replay facts   --log DIR --out DIR                 rebuild a store by feeding every fact
//!                                                        of the log through the rules (today the
//!                                                        `LogOnly` placeholder: the new store gets
//!                                                        the same facts and no graph changes)
//! ```
//!
//! `restore` is how a backup is brought up to date and how crash recovery
//! is run by hand.  `facts` is the shape of the recascade after a rule
//! change: it becomes useful when `av2-rules` holds the real rules.

use av2_facts::Fact;
use av2_rules::Rules;
use av2_store::{Config, Store};
use av2_tools::{Args, die, emit};
use std::collections::BTreeMap;
use std::time::Instant;

/// The segment directories to read, oldest first.
fn log_dirs(a: &Args) -> Vec<std::path::PathBuf> {
    match a.get("store") {
        Some(s) => {
            let s = std::path::PathBuf::from(s);
            [s.join("archive"), s.join("log")].into_iter().filter(|d| d.exists()).collect()
        }
        None => vec![a.path("log")],
    }
}

/// Walk the frames of several directories; a frame seen twice (an archived
/// segment whose log copy was not deleted yet) is visited once.
fn frames(dirs: &[std::path::PathBuf], mut f: impl FnMut(av2_store::log::Frame<'_>)) {
    let mut last = 0u64;
    for d in dirs {
        av2_store::log::for_each_frame(d, |_, fr| {
            if fr.seq > last {
                last = fr.seq;
                f(fr);
            }
            Ok(())
        })
        .unwrap_or_else(|e| die(&e.to_string()));
    }
}

fn main() {
    let a = Args::parse();
    let cmd = a.positional.first().map(String::as_str).unwrap_or("");
    let out = a.get("json").map(std::path::PathBuf::from);
    match cmd {
        "dump" => {
            let dirs = log_dirs(&a);
            let (from, to) = (a.n("from", 0), a.n("to", u64::MAX));
            frames(&dirs, |fr| {
                if fr.seq < from || fr.seq > to {
                    return;
                }
                match av2_facts::decode_all(fr.facts) {
                    Ok(facts) => {
                        for f in facts {
                            println!("{{\"seq\":{},\"fact\":{}}}", fr.seq, f.to_json_line());
                        }
                    }
                    Err(e) => println!("{{\"seq\":{},\"error\":{}}}", fr.seq, serde_json::to_string(&e.to_string()).unwrap()),
                }
            });
        }
        "stats" => {
            let dirs = log_dirs(&a);
            let (mut nframes, mut fact_bytes, mut redo_bytes, mut first, mut last) = (0u64, 0u64, 0u64, u64::MAX, 0u64);
            let mut kinds: BTreeMap<String, u64> = BTreeMap::new();
            frames(&dirs, |fr| {
                nframes += 1;
                fact_bytes += fr.facts.len() as u64;
                redo_bytes += fr.redo.len() as u64;
                first = first.min(fr.seq);
                last = last.max(fr.seq);
                for f in av2_facts::decode_all(fr.facts).unwrap_or_default() {
                    *kinds.entry(f.name().to_string()).or_default() += 1;
                }
            });
            emit(
                &out,
                &format!(
                    "{{\"stage\":\"log_stats\",\"frames\":{nframes},\"first_seq\":{},\"last_seq\":{last},\"fact_bytes\":{fact_bytes},\"redo_bytes\":{redo_bytes},\"facts\":{}}}",
                    if first == u64::MAX { 0 } else { first },
                    serde_json::to_string(&kinds).unwrap()
                ),
            );
        }
        "restore" => {
            let db = a.path("db");
            let t0 = Instant::now();
            let s = Store::open(&db, Config { background_checkpoint: false, ..Config::default() }).unwrap_or_else(|e| die(&e.to_string()));
            let r = s.recovery.clone();
            let t1 = Instant::now();
            s.checkpoint().unwrap_or_else(|e| die(&e.to_string()));
            emit(
                &out,
                &format!(
                    "{{\"stage\":\"restore\",\"checkpoint_seq\":{},\"last_seq\":{},\"frames_replayed\":{},\"bytes_replayed\":{},\"torn_tail_bytes\":{},\"open_ms\":{:.2},\"checkpoint_ms\":{:.2}}}",
                    r.checkpoint_seq,
                    r.last_seq,
                    r.frames_replayed,
                    r.bytes_replayed,
                    r.torn_tail_bytes,
                    (t1 - t0).as_secs_f64() * 1e3,
                    t1.elapsed().as_secs_f64() * 1e3
                ),
            );
            s.close().unwrap_or_else(|e| die(&e.to_string()));
        }
        "facts" => {
            let log = a.path("log");
            let dst = a.path("out");
            let mut s = Store::create(&dst, Config::default()).unwrap_or_else(|e| die(&e.to_string()));
            let mut rules = av2_rules::LogOnly;
            let t0 = Instant::now();
            let mut n = 0u64;
            av2_store::log::for_each_frame(&log, |_, fr| {
                let inputs: Vec<Fact> = av2_facts::decode_all(fr.facts).map_err(|e| av2_store::StoreError::Corrupt(e.to_string()))?;
                let mut b = s.batch();
                let mut facts = Vec::new();
                rules.apply(&mut b, &inputs, &mut facts).map_err(|e| av2_store::StoreError::Corrupt(e.to_string()))?;
                s.commit(b, &av2_facts::encode_all(&facts))?;
                n += 1;
                Ok(())
            })
            .unwrap_or_else(|e| die(&e.to_string()));
            emit(
                &out,
                &format!(
                    "{{\"stage\":\"replay_facts\",\"frames\":{n},\"last_seq\":{},\"secs\":{:.2}}}",
                    s.last_seq(),
                    t0.elapsed().as_secs_f64()
                ),
            );
            s.close().unwrap_or_else(|e| die(&e.to_string()));
        }
        _ => die("usage: av2-replay <dump|stats|restore|facts> ..."),
    }
}
