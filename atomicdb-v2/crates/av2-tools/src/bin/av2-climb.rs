//! av2-climb: the ported value rules (av2-rules, steps 1-9) on a loaded store.
//!
//! ```text
//! av2-climb --src DIR --work DIR [--b32 300] [--seed 1] [--json out.jsonl]
//! ```
//!
//! `--src` is copied to `--work` with an online backup and every workload
//! runs on the copy, 32 climbs per commit, one fsync per commit:
//!
//! - `reclimb`: a re-search lands on an open, expanded, searched position:
//!   its `eval_cp` moves by up to +-60 cp and it gains 128M nodes; then the
//!   climb of a landing (EXP-6.1): cascade from the position, backed ascent
//!   from the position and its parents.  This is the shape of the plan's
//!   `climb_b32` with the real rules instead of the stand-in.
//! - `closure`: a mate line closes an open child of an open position
//!   (`MATE_PV`, a random distance, slack 90, the winner the side to move of
//!   the parent); cascade from the parent, the child and the child's other
//!   parents, ascent from the parent, its parents and the child's parents.
//!
//! Each workload runs with v1's settings and again with the noise cut off
//! (D2).  Reported: climbs per second with commits, the rules' own time per
//! climb (no commit), rows written, levels, rows processed, events.
//!
//! The production slice has no FEN, no `won_line` and no `last_analysis`
//! (sizes only): no move counts as zeroing (the fresh-context rule then
//! decides by the children's slack alone), a `MATE_PV` row's own witness is
//! its stored distance, no row has a showcase bound.  The workload shape is
//! the plan's; the numbers are an upper bound on the rules' cost per climb
//! only in that the JSON of showcases is not parsed.

use av2_facts::Fact;
use av2_model::{Closure, GraphRead, GraphWrite, PosId, Proof, Rng, Status};
use av2_rules::climb::{ClimbEnv, climb};
use av2_rules::config::RulesConfig;
use av2_rules::effects::{Attribution, Recorder};
use av2_rules::exact::BoardZeroing;
use av2_rules::loops::NoLoops;
use av2_rules::purchase::MemTasks;
use av2_store::{Batch, Config, Store};
use av2_tools::{Args, Lat, die, emit};
use std::path::PathBuf;
use std::time::Instant;

fn cfg() -> Config {
    Config { ckpt_bytes: 256 << 20, ..Config::default() }
}

#[derive(Default)]
struct Totals {
    climbs: u64,
    rules_ns: u128,
    cascade_writes: u64,
    backed_writes: u64,
    levels: u64,
    processed: u64,
    events: u64,
    closed: u64,
    bought: u64,
    max_processed: u64,
}

fn pick(b: &Batch, rng: &mut Rng, ok: impl Fn(&Batch, PosId) -> bool) -> Option<PosId> {
    let n = b.len();
    for _ in 0..10_000 {
        let id = PosId(rng.below(n));
        if ok(b, id) {
            return Some(id);
        }
    }
    None
}

fn parents(b: &Batch, id: PosId) -> Vec<PosId> {
    let mut v = Vec::new();
    b.parents(id, &mut v);
    v
}

fn run_climb(b: &mut Batch, rcfg: &RulesConfig, cascade: &[PosId], backed: &[PosId], t: &mut Totals, facts: &mut Vec<Fact>) {
    let mut rec = Recorder::default();
    let mut z = BoardZeroing;
    let mut tasks = MemTasks::default();
    let mut loops = NoLoops;
    let attribution = Attribution::default();
    let t0 = Instant::now();
    let r = {
        let mut env =
            ClimbEnv { cfg: rcfg, effects: &mut rec, zeroing: &mut z, tasks: &mut tasks, loops: &mut loops, attribution: &attribution };
        climb(b, cascade, backed, &mut env).unwrap_or_else(|e| die(&e.to_string()))
    };
    t.rules_ns += t0.elapsed().as_nanos();
    t.climbs += 1;
    t.cascade_writes += r.cascade.changed.len() as u64;
    t.backed_writes += r.ascent.changed;
    t.levels += r.ascent.levels;
    t.processed += r.ascent.processed;
    t.max_processed = t.max_processed.max(r.ascent.processed);
    t.events += rec.events.len() as u64;
    t.closed += rec.closed.len() as u64;
    t.bought += r.ascent.bought;
    let key = |id: PosId| format!("{}", id.0);
    facts.extend(rec.events.iter().map(|e| e.to_fact(&key)));
}

fn reclimb(b: &mut Batch, rng: &mut Rng, rcfg: &RulesConfig, t: &mut Totals, facts: &mut Vec<Fact>) {
    let Some(p) = pick(b, rng, |b, id| {
        let c = b.core(id);
        c.is_open() && c.expanded() && c.eval().is_some()
    }) else {
        return;
    };
    let mut c = b.core(p);
    let delta = rng.below(121) as i16 - 60;
    c.eval_cp = (c.eval_cp as i32 + delta as i32).clamp(-8999, 8999) as i16;
    b.put_core(p, c).unwrap();
    let mut e = b.ext(p).unwrap_or_default();
    e.nodes_invested += 128_000_000;
    e.visits += 1;
    b.put_ext(p, e).unwrap();
    let mut backed = vec![p];
    backed.extend(parents(b, p));
    run_climb(b, rcfg, &[p], &backed, t, facts);
}

fn closure(b: &mut Batch, rng: &mut Rng, rcfg: &RulesConfig, t: &mut Totals, facts: &mut Vec<Fact>) {
    let mut es = Vec::new();
    let Some(p) = pick(b, rng, |b, id| {
        let c = b.core(id);
        c.is_open() && c.expanded()
    }) else {
        return;
    };
    b.children(p, &mut es);
    let open: Vec<PosId> = es.iter().map(|e| e.child).filter(|&k| b.core(k).is_open()).collect();
    if open.is_empty() {
        return;
    }
    let child = open[rng.below(open.len() as u64) as usize];
    let winner = if b.core(p).black_to_move() { Status::BlackWin } else { Status::WhiteWin };
    let mut c = b.core(child);
    c.set_status(winner, Closure::MatePv, Proof::Engine);
    c.mate_in = 1 + rng.below(20) as u16;
    c.clock_slack = 90;
    b.put_core(child, c).unwrap();
    let others: Vec<PosId> = parents(b, child).into_iter().filter(|&q| q != p).collect();
    let mut cascade = vec![p, child];
    cascade.extend(&others);
    let mut backed = vec![p];
    backed.extend(parents(b, p));
    backed.extend(&others);
    run_climb(b, rcfg, &cascade, &backed, t, facts);
}

fn workload(s: &mut Store, name: &str, rcfg: &RulesConfig, commits: u64, seed: u64, out: &Option<PathBuf>) {
    let mut rng = Rng::new(seed);
    let mut t = Totals::default();
    let mut lat = Lat::new();
    let t0 = Instant::now();
    for _ in 0..commits {
        let tc = Instant::now();
        let mut b = s.batch();
        let mut facts = Vec::new();
        for _ in 0..32 {
            if name.starts_with("reclimb") {
                reclimb(&mut b, &mut rng, rcfg, &mut t, &mut facts);
            } else {
                closure(&mut b, &mut rng, rcfg, &mut t, &mut facts);
            }
        }
        s.commit(b, &av2_facts::encode_all(&facts)).unwrap_or_else(|e| die(&e.to_string()));
        lat.add(tc.elapsed().as_nanos() as f64 / 1e6);
    }
    let secs = t0.elapsed().as_secs_f64();
    let n = t.climbs.max(1) as f64;
    emit(
        out,
        &format!(
            "{{\"stage\":\"{name}\",\"noise_cut\":{},\"commits\":{commits},\"climbs\":{},\"climbs_per_sec\":{:.0},\"rules_us_per_climb\":{:.1},\"rules_only_climbs_per_sec\":{:.0},\"cascade_writes_per_climb\":{:.2},\"backed_writes_per_climb\":{:.2},\"levels_per_climb\":{:.2},\"processed_per_climb\":{:.1},\"max_processed\":{},\"closures\":{},\"events\":{},\"bought\":{},\"commit_ms\":{}}}",
            rcfg.noise_cut,
            t.climbs,
            t.climbs as f64 / secs,
            t.rules_ns as f64 / 1e3 / n,
            n / (t.rules_ns as f64 / 1e9),
            t.cascade_writes as f64 / n,
            t.backed_writes as f64 / n,
            t.levels as f64 / n,
            t.processed as f64 / n,
            t.max_processed,
            t.closed,
            t.events,
            t.bought,
            lat.json()
        ),
    );
}

fn main() {
    let a = Args::parse();
    let src = a.path("src");
    let work = a.path("work");
    let out: Option<PathBuf> = a.get("json").map(PathBuf::from);
    let commits = a.n("b32", 300);
    let seed = a.n("seed", 1);
    if work.exists() {
        std::fs::remove_dir_all(&work).unwrap_or_else(|e| die(&e.to_string()));
    }
    let s0 = Store::open(&src, cfg()).unwrap_or_else(|e| die(&e.to_string()));
    s0.backup(&work).unwrap_or_else(|e| die(&e.to_string()));
    let n0 = s0.reader().read(|v| v.len());
    drop(s0);
    let mut s = Store::open(&work, cfg()).unwrap_or_else(|e| die(&e.to_string()));
    emit(&out, &format!("{{\"stage\":\"open\",\"positions\":{n0}}}"));
    let v1 = RulesConfig::default();
    let d2 = RulesConfig { noise_cut: false, ..RulesConfig::default() };
    workload(&mut s, "reclimb_b32", &v1, commits, seed, &out);
    workload(&mut s, "reclimb_b32", &d2, commits, seed + 1, &out);
    workload(&mut s, "closure_b32", &v1, commits, seed + 2, &out);
    workload(&mut s, "closure_b32", &d2, commits, seed + 3, &out);
}
