//! av2-perf: measure the storage core on a loaded store.
//!
//! ```text
//! av2-perf --src DIR --work DIR [--readers 8] [--secs 5] [--b1 2000] [--b32 200] [--recovery-frames 5000] [--json out.jsonl]
//!          [--landings-only [--repeat N] [--lean] [--pipeline]] [--until-compaction] [--fsync-delay-ms D]
//!
//! `--fsync-delay-ms` adds a sleep after every fsync to simulate a slower
//! disk (production's md RAID1: 16 ms median per fdatasync).
//! ```
//!
//! `--src` is copied to `--work` with an online backup (timed), and every
//! workload runs on the copy.  Workloads, all with one fsync per commit:
//!
//! - `landing_b1` / `landing_b32`: a search lands on an unexpanded position:
//!   ~29 children created (keys, packed FENs, edges, parent lists), the
//!   searched position's ext record and a 1.2 KB analysis blob, five values
//!   rewritten up the first-parent chain, a 1.3 KB search fact.  One landing
//!   per commit, then 32 per commit.
//! - `climb_b32`: 32 re-searches per commit: values rewritten up the chain only.
//! - `explore`: an explorer page (record, out-edges, every child's record)
//!   from 1 and N reader threads, then N threads while a writer lands
//!   searches at full speed.
//! - `restart` and `recovery`: reopen after a clean close; reopen after
//!   `--recovery-frames` commits with no checkpoint.
//! - `footprint`: live bytes per position, per file.
//!
//! These are not AtomicDB's rules (phase 2), only their read/write shape, as
//! in the benchmark harness.

use av2_facts::{Fact, RawJson, SearchLanded};
use av2_model::record::F_EXPANDED;
use av2_model::{Edge, GraphRead, GraphWrite, Move, PosId, Rng};
use av2_store::layout::{NAMES, NFILES};
use av2_store::testkit::{random_fen, random_fp};
use av2_store::{Batch, Config, Store};
use av2_tools::{Args, Lat, die, emit};
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

fn cfg() -> Config {
    let delay = std::env::args()
        .collect::<Vec<_>>()
        .windows(2)
        .find(|w| w[0] == "--fsync-delay-ms")
        .and_then(|w| w[1].parse::<f64>().ok())
        .unwrap_or(0.0);
    Config { ckpt_bytes: 256 << 20, fsync_delay: Duration::from_secs_f64(delay / 1e3), ..Config::default() }
}

/// Synthetic FENs, generated once: the store packs each one (that cost is
/// the store's); generating random boards is the harness's and stays out of
/// the measurement.
fn fen_pool() -> &'static [String] {
    static POOL: std::sync::OnceLock<Vec<String>> = std::sync::OnceLock::new();
    POOL.get_or_init(|| {
        let mut rng = Rng::new(0xFE11);
        (0..8192).map(|_| random_fen(&mut rng)).collect()
    })
}

/// `--lean`: landings shaped like the benchmark harness's (no FEN, no
/// analysis blob, no search fact), to compare the store with the benchmark
/// on the same bytes.
static LEAN: AtomicBool = AtomicBool::new(false);
/// `--pipeline`: compute batch n+1 while batch n's fsync is in flight
/// (`Store::commit_start` / `batch_after` / `commit_finish`).
static PIPELINE: AtomicBool = AtomicBool::new(false);

/// One landing: expand an unexpanded position and climb.
fn landing(b: &mut Batch, rng: &mut Rng, payload: &str, facts: &mut Vec<Fact>) {
    let n = b.len();
    let mut es = Vec::new();
    let p = loop {
        let id = PosId(rng.below(n));
        es.clear();
        b.children(id, &mut es);
        if es.is_empty() {
            break id;
        }
    };
    let k = 20 + rng.below(19) as usize;
    let mut block = Vec::with_capacity(k);
    for i in 0..k {
        let pool = fen_pool();
        let fen = &pool[rng.below(pool.len() as u64) as usize];
        let fen = if LEAN.load(Ordering::Relaxed) { None } else { Some(fen.as_str()) };
        let c = b.create(random_fp(rng), fen, av2_model::Core { eval_cp: (rng.below(400) as i16) - 200, ..Default::default() }).unwrap();
        block.push(Edge { child: c, mv: Move::new((i / 8) as u8, (i % 8 + 16) as u8, 0) });
    }
    b.set_children(p, &block).unwrap();
    let mut core = b.core(p);
    core.set_flag(F_EXPANDED, true);
    core.eval_cp = (rng.below(400) as i16) - 200;
    b.put_core(p, core).unwrap();
    let mut e = b.ext(p).unwrap_or_default();
    e.nodes_invested += 128_000_000;
    e.visits += 1;
    let lean = LEAN.load(Ordering::Relaxed);
    if !lean {
        let mut blob = vec![0u8; 1200];
        rng.fill(&mut blob);
        e.analysis = b.put_blob(&blob).unwrap();
    }
    b.put_ext(p, e).unwrap();
    climb(b, p, rng);
    if lean {
        return;
    }
    facts.push(Fact::SearchLanded(SearchLanded {
        task: rng.next_u64() >> 20,
        position: format!("{:x}", p.0),
        budget_nodes: 128_000_000,
        payload: RawJson(payload.to_string()),
        ..Default::default()
    }));
}

/// Rewrite the backed value of five ancestors (first parent each).
fn climb(b: &mut Batch, from: PosId, rng: &mut Rng) {
    let mut ps = Vec::new();
    let mut cur = from;
    for _ in 0..5 {
        ps.clear();
        b.parents(cur, &mut ps);
        let Some(&up) = ps.first() else { break };
        let mut c = b.core(up);
        c.backed_eval = (rng.below(400) as i16) - 200;
        c.backed_plies = c.backed_plies.wrapping_add(1);
        b.put_core(up, c).unwrap();
        cur = up;
    }
}

/// What one explorer page or one `/api/query` reads, as in the benchmark's
/// `explore_op`: 70% of the time move to the position's first parent (people
/// open interior positions), find it by key, read its record, its moves and
/// their records, and its parents.
fn explore_once(
    v: &av2_store::view::View<'_>,
    mut id: PosId,
    rng: &mut Rng,
    fps: &[av2_model::Fingerprint],
    es: &mut Vec<Edge>,
    ps: &mut Vec<PosId>,
) -> i64 {
    if rng.chance(0.7) {
        ps.clear();
        v.parents(id, ps);
        if let Some(&p) = ps.iter().min() {
            id = p;
        }
    }
    let found = fps.get(id.index()).and_then(|fp| v.lookup(fp));
    let c = v.core(id);
    es.clear();
    v.children(id, es);
    let mut acc = c.eval_cp as i64 + found.map_or(0, |f| f.0 as i64);
    for e in es.iter() {
        acc += v.core(e.child).eval_cp as i64;
    }
    ps.clear();
    v.parents(id, ps);
    acc + ps.len() as i64
}

/// What a run of landings measured.
struct Run {
    sps: f64,
    lat: Lat,
    build_ms: f64,
    commit_ms: f64,
    finalize_ms: f64,
    write_ms: f64,
    grow_ms: f64,
    grows: u64,
    fsync_ms: f64,
    apply_ms: f64,
    frame_bytes: u64,
    ops: u64,
    commits: u64,
}

impl Run {
    fn json(&mut self) -> String {
        let c = self.commits.max(1) as f64;
        format!(
            "\"searches_per_sec\":{:.0},\"commit_ms\":{},\"per_commit_ms\":{{\"build\":{:.3},\"commit\":{:.3},\"finalize\":{:.3},\"write\":{:.3},\"grow\":{:.3},\"fsync\":{:.3},\"apply\":{:.3}}},\"frame_kb\":{:.1},\"redo_records\":{:.0},\"grows\":{}",
            self.sps,
            self.lat.json(),
            self.build_ms / c,
            self.commit_ms / c,
            self.finalize_ms / c,
            self.write_ms / c,
            self.grow_ms / c,
            self.fsync_ms / c,
            self.apply_ms / c,
            self.frame_bytes as f64 / c / 1024.0,
            self.ops as f64 / c,
            self.grows
        )
    }
}

fn run_landings(s: &mut Store, commits: u64, per: usize, seed: u64, payload: &str) -> Run {
    let mut rng = Rng::new(seed);
    let mut r = Run {
        sps: 0.0,
        lat: Lat::new(),
        build_ms: 0.0,
        commit_ms: 0.0,
        finalize_ms: 0.0,
        write_ms: 0.0,
        grow_ms: 0.0,
        grows: 0,
        fsync_ms: 0.0,
        apply_ms: 0.0,
        frame_bytes: 0,
        ops: 0,
        commits,
    };
    let ms = |d: Duration| d.as_secs_f64() * 1e3;
    let grows0 = s.stats().grows;
    let t0 = Instant::now();
    let pipeline = PIPELINE.load(Ordering::Relaxed);
    let mut flight: Option<av2_store::InFlight> = None;
    let account = |r: &mut Run, s: &Store, t: Instant| {
        let ct = s.last_commit();
        r.finalize_ms += ms(ct.finalize);
        r.write_ms += ms(ct.write);
        r.grow_ms += ms(ct.grow);
        r.fsync_ms += ms(if pipeline { ct.fsync_wait } else { ct.fsync });
        r.apply_ms += ms(ct.apply);
        r.frame_bytes += ct.frame_bytes;
        r.ops += ct.ops;
        r.lat.add(t.elapsed().as_nanos() as f64 / 1e6);
    };
    let mut started: Option<Instant> = None;
    for _ in 0..commits {
        let t = Instant::now();
        let mut b = match &flight {
            Some(f) => s.batch_after(f),
            None => s.batch(),
        };
        let mut facts = Vec::new();
        for _ in 0..per {
            landing(&mut b, &mut rng, payload, &mut facts);
        }
        let bytes = av2_facts::encode_all(&facts);
        let t1 = Instant::now();
        r.build_ms += ms(t1 - t);
        if pipeline {
            // Batch n+1 was built while n's fsync ran; now finish n, start n+1.
            if let Some(f) = flight.take() {
                s.commit_finish(f).unwrap();
                account(&mut r, s, started.unwrap());
            }
            flight = Some(s.commit_start(b, &bytes).unwrap());
            started = Some(t);
        } else {
            s.commit(b, &bytes).unwrap();
            account(&mut r, s, t);
        }
        r.commit_ms += ms(t1.elapsed());
    }
    if let Some(f) = flight.take() {
        s.commit_finish(f).unwrap();
        account(&mut r, s, started.unwrap());
    }
    r.sps = (commits * per as u64) as f64 / t0.elapsed().as_secs_f64();
    r.grows = s.stats().grows - grows0;
    r
}

fn main() {
    let a = Args::parse();
    let src = a.path("src");
    let work = a.path("work");
    let out: Option<PathBuf> = a.get("json").map(PathBuf::from);
    let threads = a.n("readers", 8) as usize;
    let secs = a.f("secs", 5.0);
    let payload = format!(
        "{{\"lines\":[{}]}}",
        (0..5).map(|i| format!("{{\"move\":\"e2e{i}\",\"cp\":{i},\"pv\":\"{}\"}}", "e2e4 ".repeat(40))).collect::<Vec<_>>().join(",")
    );
    if work.exists() {
        std::fs::remove_dir_all(&work).unwrap_or_else(|e| die(&e.to_string()));
    }
    // Copy with an online backup (the source is opened, not modified beyond its log).
    let s0 = Store::open(&src, cfg()).unwrap_or_else(|e| die(&e.to_string()));
    let rep = s0.backup(&work).unwrap_or_else(|e| die(&e.to_string()));
    let n0 = s0.reader().read(|v| v.len());
    drop(s0);
    emit(
        &out,
        &format!(
            "{{\"stage\":\"backup\",\"positions\":{n0},\"graph_bytes\":{},\"log_bytes\":{},\"secs\":{:.2}}}",
            rep.graph_bytes,
            rep.log_bytes,
            rep.micros as f64 / 1e6
        ),
    );
    let t = Instant::now();
    let mut s = Store::open(&work, cfg()).unwrap_or_else(|e| die(&e.to_string()));
    emit(
        &out,
        &format!(
            "{{\"stage\":\"open_backup\",\"ms\":{:.2},\"frames_replayed\":{}}}",
            t.elapsed().as_secs_f64() * 1e3,
            s.recovery.frames_replayed
        ),
    );

    // id -> fingerprint, so that readers can look positions up by key.
    let fps: Arc<Vec<av2_model::Fingerprint>> = Arc::new(s.reader().read(|v| {
        let mut pairs = Vec::new();
        v.key_pairs(&mut pairs);
        let mut f = vec![av2_model::Fingerprint::default(); v.len() as usize];
        for (fp, id) in pairs {
            f[id.index()] = fp;
        }
        f
    }));
    LEAN.store(a.flag("lean"), Ordering::Relaxed);
    PIPELINE.store(a.flag("pipeline"), Ordering::Relaxed);
    if a.flag("until-compaction") {
        // Land searches (32 per commit) until the key map compacts; report
        // the arena before and after and what that commit cost.
        drop(s);
        let mut s = Store::open(&work, Config { km_compact_min: 1 << 20, ..cfg() }).unwrap();
        let mut rng = Rng::new(77);
        let (mut commits, mut worst) = (0u64, 0f64);
        let km = |s: &Store| s.live_bytes().0[av2_store::layout::F_KM_ARENA as usize];
        loop {
            let before = km(&s);
            let mut b = s.batch();
            let mut facts = Vec::new();
            for _ in 0..32 {
                landing(&mut b, &mut rng, &payload, &mut facts);
            }
            let t = Instant::now();
            s.commit(b, &av2_facts::encode_all(&facts)).unwrap();
            let ms = t.elapsed().as_secs_f64() * 1e3;
            commits += 1;
            if s.stats().compactions > 0 {
                let ct = s.last_commit();
                let n = s.reader().read(|v| v.len());
                emit(
                    &out,
                    &format!(
                        "{{\"stage\":\"compaction\",\"commits_before\":{commits},\"positions\":{n},\"arena_before\":{before},\"arena_after\":{},\"commit_ms\":{ms:.2},\"apply_ms\":{:.2},\"other_commits_worst_ms\":{worst:.2}}}",
                        km(&s),
                        ct.apply.as_secs_f64() * 1e3
                    ),
                );
                break;
            }
            worst = worst.max(ms);
            if commits > 20_000 {
                emit(&out, "{\"stage\":\"compaction\",\"note\":\"none after 20000 commits\"}");
                break;
            }
        }
        drop(s);
        let _ = std::fs::remove_dir_all(&work);
        return;
    }
    let only = a.flag("landings-only");
    if only {
        let b32 = a.n("b32", 200);
        for r in 0..a.n("repeat", 3) {
            let mut run = run_landings(&mut s, b32, 32, 100 + r, &payload);
            emit(
                &out,
                &format!("{{\"stage\":\"landing_b32\",\"lean\":{},\"repeat\":{r},\"commits\":{b32},{}}}", a.flag("lean"), run.json()),
            );
        }
        drop(s);
        let _ = std::fs::remove_dir_all(&work);
        return;
    }
    // Readers on a quiet store.
    for th in [1usize, threads] {
        let (rps, lat) = readers_from(s.reader(), th, secs, fps.clone());
        emit(&out, &format!("{{\"stage\":\"explore\",\"readers\":{th},\"writer\":false,\"reads_per_sec\":{rps:.0},\"latency_us\":{lat}}}"));
    }

    // Landings.
    let b1 = a.n("b1", 2000);
    let mut run = run_landings(&mut s, b1, 1, 11, &payload);
    emit(&out, &format!("{{\"stage\":\"landing_b1\",\"commits\":{b1},{}}}", run.json()));
    let b32 = a.n("b32", 200);
    let mut run = run_landings(&mut s, b32, 32, 12, &payload);
    emit(&out, &format!("{{\"stage\":\"landing_b32\",\"commits\":{b32},{}}}", run.json()));
    {
        let mut rng = Rng::new(13);
        let mut lat = Lat::new();
        let t0 = Instant::now();
        for _ in 0..b32 {
            let t = Instant::now();
            let mut b = s.batch();
            for _ in 0..32 {
                let id = PosId(rng.below(b.len()));
                climb(&mut b, id, &mut rng);
            }
            s.commit(b, &[]).unwrap();
            lat.add(t.elapsed().as_nanos() as f64 / 1e6);
        }
        let rps = (b32 * 32) as f64 / t0.elapsed().as_secs_f64();
        emit(&out, &format!("{{\"stage\":\"climb_b32\",\"commits\":{b32},\"climbs_per_sec\":{rps:.0},\"commit_ms\":{}}}", lat.json()));
    }

    // Readers while a writer lands searches at full speed (on its own thread).
    {
        let stop = Arc::new(AtomicBool::new(false));
        let stop2 = stop.clone();
        let payload2 = payload.clone();
        let (tx, rx) = std::sync::mpsc::channel::<Store>();
        let r_store = s.reader();
        let wh = std::thread::spawn(move || {
            let mut rng = Rng::new(14);
            let mut commits = 0u64;
            let t0 = Instant::now();
            while !stop2.load(Ordering::Relaxed) {
                let mut b = s.batch();
                let mut facts = Vec::new();
                landing(&mut b, &mut rng, &payload2, &mut facts);
                s.commit(b, &av2_facts::encode_all(&facts)).unwrap();
                commits += 1;
            }
            let rate = commits as f64 / t0.elapsed().as_secs_f64();
            tx.send(s).unwrap();
            rate
        });
        // Readers need a Store reference only for `reader()`: use the clone we took.
        let (rps, lat) = readers_from(r_store, threads, secs, fps.clone());
        stop.store(true, Ordering::Relaxed);
        let wrate = wh.join().unwrap();
        s = rx.recv().unwrap();
        emit(
            &out,
            &format!(
                "{{\"stage\":\"explore\",\"readers\":{threads},\"writer\":true,\"reads_per_sec\":{rps:.0},\"latency_us\":{lat},\"writer_searches_per_sec\":{wrate:.0}}}"
            ),
        );
    }

    // Checkpoint, footprint.
    let t = Instant::now();
    s.checkpoint().unwrap();
    let ckpt_ms = t.elapsed().as_secs_f64() * 1e3;
    let (per_file, live) = s.live_bytes();
    let n = s.reader().read(|v| v.len());
    let st = s.stats();
    let files: Vec<String> = (0..NFILES).map(|f| format!("\"{}\":{:.2}", NAMES[f], per_file[f] as f64 / n as f64)).collect();
    emit(
        &out,
        &format!(
            "{{\"stage\":\"footprint\",\"positions\":{n},\"live_bytes\":{live},\"bytes_per_position\":{:.1},\"per_file\":{{{}}},\"checkpoint_ms\":{ckpt_ms:.1},\"checkpoints\":{},\"checkpoint_ms_max\":{:.1}}}",
            live as f64 / n as f64,
            files.join(","),
            st.checkpoints,
            st.checkpoint_micros_max as f64 / 1e3
        ),
    );

    // Restart after a clean close.
    s.close().unwrap();
    let t = Instant::now();
    let s2 = Store::open(&work, cfg()).unwrap();
    emit(
        &out,
        &format!(
            "{{\"stage\":\"restart\",\"ms\":{:.2},\"frames_replayed\":{}}}",
            t.elapsed().as_secs_f64() * 1e3,
            s2.recovery.frames_replayed
        ),
    );
    drop(s2);

    // Recovery: commits with no checkpoint, then drop without closing.
    let frames = a.n("recovery-frames", 5000);
    let mut s3 = Store::open(&work, Config { ckpt_bytes: u64::MAX, background_checkpoint: false, ..cfg() }).unwrap();
    run_landings(&mut s3, frames, 1, 15, &payload);
    let log_bytes = s3.log_bytes();
    drop(s3);
    let t = Instant::now();
    let s4 = Store::open(&work, cfg()).unwrap();
    emit(
        &out,
        &format!(
            "{{\"stage\":\"recovery\",\"frames\":{},\"bytes_replayed\":{},\"log_bytes_on_disk\":{log_bytes},\"ms\":{:.1}}}",
            s4.recovery.frames_replayed,
            s4.recovery.bytes_replayed,
            t.elapsed().as_secs_f64() * 1e3
        ),
    );
    drop(s4);
    if !a.flag("keep") {
        let _ = std::fs::remove_dir_all(&work);
    }
}

/// N readers for `secs`: (reads per second, latency summary in microseconds).
fn readers_from(r: av2_store::Reader, threads: usize, secs: f64, fps: Arc<Vec<av2_model::Fingerprint>>) -> (f64, String) {
    let stop = Arc::new(AtomicBool::new(false));
    let total = Arc::new(AtomicU64::new(0));
    let mut hs = Vec::new();
    for t in 0..threads {
        let r = r.clone();
        let (stop, total, fps) = (stop.clone(), total.clone(), fps.clone());
        hs.push(std::thread::spawn(move || {
            let mut rng = Rng::new(0xF0 + t as u64);
            let mut es = Vec::with_capacity(64);
            let mut ps = Vec::with_capacity(8);
            let mut lat = Lat::new();
            let mut n = 0u64;
            let mut sink = 0i64;
            while !stop.load(Ordering::Relaxed) {
                for _ in 0..256 {
                    let t0 = if n.is_multiple_of(16) { Some(Instant::now()) } else { None };
                    let pick = rng.next_u64();
                    sink += r.read(|v| {
                        let id = PosId(((pick as u128 * v.len() as u128) >> 64) as u64);
                        let mut local = Rng::new(pick);
                        explore_once(v, id, &mut local, &fps, &mut es, &mut ps)
                    });
                    if let Some(t0) = t0 {
                        lat.add(t0.elapsed().as_nanos() as f64 / 1e3);
                    }
                    n += 1;
                }
            }
            total.fetch_add(n, Ordering::Relaxed);
            std::hint::black_box(sink);
            lat
        }));
    }
    std::thread::sleep(Duration::from_secs_f64(secs));
    stop.store(true, Ordering::Relaxed);
    let mut all = Lat::new();
    for h in hs {
        all.extend(h.join().unwrap());
    }
    (total.load(Ordering::Relaxed) as f64 / secs, all.json())
}
