//! atomicdb-v2-bench: race the candidate stores of AtomicDB v2 on the same workloads.
//! See README.md for the commands and docs/atomicdb/v2-storage-benchmark.md for the results.

mod crash;
mod dataset;
mod model;
mod rules;
mod store;
mod stores;
mod util;
mod workload;

use dataset::Dataset;
use model::Rng;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use store::{OpenOpts, Store};
use util::{emit, proc_stats, Args, Lat};
use workload::{Counters, BatchTiming};

pub fn open_opts(a: &Args, ds_nodes: u64, ds_edges: u64) -> OpenOpts {
    OpenOpts {
        path: PathBuf::from(a.s("db", "db")),
        cache_bytes: a.n("cache-mb", 2048) << 20,
        cap_nodes: a.n("cap-nodes", ds_nodes + (ds_nodes / 5).max(6_000_000)),
        cap_edges: a.n("cap-edges", ds_edges + (ds_edges / 5).max(7_000_000)),
        pg: a.s("pg", "host=127.0.0.1 port=54329 user=postgres dbname=bench"),
        durable: !a.flag("no-fsync"),
    }
}

pub fn dataset_counts(dir: &str) -> (u64, u64) {
    let meta = std::fs::read_to_string(PathBuf::from(dir).join("meta.txt")).expect("meta.txt");
    let get = |k: &str| -> u64 { meta.lines().find_map(|l| l.strip_prefix(k).and_then(|v| v.strip_prefix('='))).unwrap().trim().parse().unwrap() };
    (get("nodes"), get("edges"))
}

/// One batch of the ingest or climb workload; everything depends only on (seed, seq) and the committed state.
pub fn step(store: &mut dyn Store, seq: u64, seed: u64, bsize: usize, q_pv: f64, research: bool) -> (Counters, BatchTiming, u64) {
    let t = Instant::now();
    let mut rng = Rng::new(seed.rotate_left(17) ^ seq.wrapping_mul(0xD6E8_FEB8_6659_FD93));
    let targets = workload::pick_targets(store, &mut rng, bsize, q_pv, research);
    let select_us = t.elapsed().as_micros() as u64;
    let (c, bt) = workload::run_batch(store, &targets, seq, seed);
    (c, bt, select_us)
}

struct Common { store: String, kind: String, data: String, out: Option<PathBuf>, tag: String }
impl Common {
    fn line(&self, stage: &str, body: String) {
        emit(&self.out, &format!("{{\"stage\":\"{}\",\"store\":\"{}\",\"kind\":\"{}\",\"dataset\":\"{}\",\"tag\":\"{}\",{}}}", stage, self.store, self.kind, self.data, self.tag, body));
    }
}

fn write_stage(cm: &Common, store: &mut dyn Store, stage: &str, batches: u64, bsize: usize, research: bool, seed: u64, q: f64, seq: &mut u64) {
    let (mut total, mut compute, mut commit, mut select) = (Lat::new(), Lat::new(), Lat::new(), Lat::new());
    let mut sum = Counters::default();
    let p0 = proc_stats();
    let w0 = store.physical_bytes_written();
    let t0 = Instant::now();
    let mut busy_us = 0u64;
    for _ in 0..batches {
        *seq += 1;
        let (c, bt, sel) = step(store, *seq, seed, bsize, q, research);
        total.add_us(bt.compute_us + bt.commit_us);
        compute.add_us(bt.compute_us);
        commit.add_us(bt.commit_us);
        select.add_us(sel);
        busy_us += bt.compute_us + bt.commit_us;
        sum.node_reads += c.node_reads; sum.block_reads += c.block_reads; sum.parent_reads += c.parent_reads; sum.key_lookups += c.key_lookups;
        sum.landings += c.landings; sum.expansions += c.expansions; sum.closures += c.closures; sum.backed_writes += c.backed_writes; sum.levels += c.levels;
        sum.new_nodes += c.new_nodes; sum.new_edges += c.new_edges; sum.puts += c.puts; sum.logical_bytes += c.logical_bytes;
    }
    let wall = t0.elapsed().as_secs_f64();
    store.settle();
    let p1 = proc_stats();
    let phys = match (w0, store.physical_bytes_written()) { (Some(a), Some(b)) => b - a, _ => p1.write_bytes - p0.write_bytes };
    let l = sum.landings.max(1) as f64;
    cm.line(stage, format!(
        "\"batches\":{},\"batch_size\":{},\"landings\":{},\"landings_per_s\":{:.1},\"landings_per_s_with_select\":{:.1},\"batch\":{},\"compute\":{},\"commit\":{},\"select\":{},\
         \"per_landing\":{{\"node_reads\":{:.1},\"block_reads\":{:.1},\"parent_reads\":{:.1},\"key_lookups\":{:.1},\"new_nodes\":{:.1},\"new_edges\":{:.1},\"rewrites\":{:.1},\"closures\":{:.2},\"backed_writes\":{:.2},\"logical_bytes\":{:.0}}},\
         \"levels_per_batch\":{:.1},\"logical_bytes\":{},\"physical_bytes\":{},\"write_amplification\":{:.2},\"wall_s\":{:.2},\"store_stats\":{}",
        batches, bsize, sum.landings, sum.landings as f64 / (busy_us as f64 / 1e6), sum.landings as f64 / wall, total.json(), compute.json(), commit.json(), select.json(),
        sum.node_reads as f64 / l, sum.block_reads as f64 / l, sum.parent_reads as f64 / l, sum.key_lookups as f64 / l, sum.new_nodes as f64 / l, sum.new_edges as f64 / l,
        sum.puts as f64 / l, sum.closures as f64 / l, sum.backed_writes as f64 / l, sum.logical_bytes as f64 / l,
        sum.levels as f64 / batches.max(1) as f64, sum.logical_bytes, phys, phys as f64 / sum.logical_bytes.max(1) as f64, wall, store.extra_stats()));
}

fn explore_stage(cm: &Common, store: &mut dyn Store, stage: &str, readers: usize, secs: f64, seed: u64, writer: Option<(usize, f64, &mut u64)>) {
    let stop = Arc::new(AtomicBool::new(false));
    let mut handles = Vec::new();
    for t in 0..readers {
        let mut r = store.new_reader();
        let stop = stop.clone();
        handles.push(std::thread::spawn(move || {
            let mut rng = Rng::new(seed ^ (t as u64 + 1) * 7919);
            let mut lat = Lat::new();
            let mut rows = 0u64;
            while !stop.load(Ordering::Relaxed) {
                let t0 = Instant::now();
                rows += workload::explore_op(&mut *r, &mut rng) as u64;
                lat.add_us(t0.elapsed().as_micros() as u64);
            }
            (lat, rows)
        }));
    }
    let t0 = Instant::now();
    let mut wl = Lat::new();
    let mut landings = 0u64;
    let mut busy = 0u64;
    if let Some((bsize, q, seq)) = writer {
        // A fixed number of batches (here `secs` is that number), so that every store ends in the same state.
        for _ in 0..secs as u64 {
            *seq += 1;
            let (c, bt, _) = step(store, *seq, seed, bsize, q, false);
            wl.add_us(bt.compute_us + bt.commit_us);
            busy += bt.compute_us + bt.commit_us;
            landings += c.landings;
        }
    } else {
        std::thread::sleep(Duration::from_secs_f64(secs));
    }
    stop.store(true, Ordering::Relaxed);
    let wall = t0.elapsed().as_secs_f64();
    let mut all = Lat::new();
    let mut rows = 0u64;
    for h in handles { let (l, r) = h.join().unwrap(); all.merge(&l); rows += r; }
    let ops = all.len();
    cm.line(stage, format!("\"readers\":{},\"seconds\":{:.1},\"reads\":{},\"reads_per_s\":{:.0},\"rows_per_read\":{:.1},\"read\":{},\"writer_landings_per_s\":{:.1},\"writer_batch\":{}",
        readers, wall, ops, ops as f64 / wall, rows as f64 / ops.max(1) as f64, all.json(), if busy > 0 { landings as f64 / (busy as f64 / 1e6) } else { 0.0 }, wl.json()));
}

fn suite(a: &Args) {
    let kind = a.s("store", "mmap");
    let data = a.s("data", "data");
    let (dn, de) = dataset_counts(&data);
    let o = open_opts(a, dn, de);
    let seed = a.n("seed", 20261127);
    let q = a.f("q-pv", 0.95);
    let ds = if kind == "mem" { Some(Dataset::open(&PathBuf::from(&data), true)) } else { None };
    let t = Instant::now();
    let mut store = store::open(&kind, &o, ds.as_ref());
    let open_ms = t.elapsed().as_secs_f64() * 1e3;
    let cm = Common { store: store.name(), kind: kind.clone(), data: PathBuf::from(&data).file_name().unwrap().to_string_lossy().to_string(), out: a.get("out").map(PathBuf::from), tag: a.s("tag", "") };
    let mut seq = store.last_seq();
    let n0 = store.node_count();
    cm.line("open", format!("\"open_ms\":{:.1},\"last_seq\":{},\"positions\":{},\"disk_bytes\":{},\"durable\":{}", open_ms, seq, n0, store.disk_bytes(), o.durable));

    // 1. Selector: descents from the root to a leaf.
    let descents = a.n("descents", 2000);
    if descents > 0 {
        let mut lat = Lat::new();
        let mut rng = Rng::new(seed ^ 0x5E1E);
        let mut found = 0u64;
        store.begin();
        let t0 = Instant::now();
        for _ in 0..descents {
            let t = Instant::now();
            if workload::descend(&mut *store, &mut rng, q, false).is_some() { found += 1; }
            lat.add_us(t.elapsed().as_micros() as u64);
        }
        let wall = t0.elapsed().as_secs_f64();
        store.end();
        cm.line("select", format!("\"descents\":{},\"reached_leaf\":{},\"descents_per_s\":{:.0},\"descent\":{}", descents, found, descents as f64 / wall, lat.json()));
    }
    // 2. Explorer reads, nobody writing.
    let secs = a.f("read-seconds", 5.0);
    let readers = a.n("readers", 8) as usize;
    if secs > 0.0 {
        explore_stage(&cm, &mut *store, "explore_1_reader", 1, secs, seed, None);
        explore_stage(&cm, &mut *store, "explore_n_readers", readers, secs, seed, None);
    }
    // 3-5. The writer.
    let (k1, k32, kc) = (a.n("ingest1", 300), a.n("ingest32", 100), a.n("climb32", 100));
    if k1 > 0 { write_stage(&cm, &mut *store, "ingest_b1", k1, 1, false, seed, q, &mut seq); }
    if k32 > 0 { write_stage(&cm, &mut *store, "ingest_b32", k32, 32, false, seed, q, &mut seq); }
    if kc > 0 { write_stage(&cm, &mut *store, "climb_b32", kc, 32, true, seed, q, &mut seq); }
    // 6. Explorer reads while the writer runs.
    let mixed = a.n("mixed-batches", 100);
    if mixed > 0 { explore_stage(&cm, &mut *store, "explore_under_writes", readers, mixed as f64, seed, Some((32, q, &mut seq))); }
    // 7. Footprint.
    store.settle();
    let p = proc_stats();
    let n1 = store.node_count();
    let disk = store.disk_bytes();
    cm.line("footprint", format!("\"positions\":{},\"disk_bytes\":{},\"bytes_per_position\":{:.1},\"rss_bytes\":{},\"peak_rss_bytes\":{},\"private_bytes\":{},\"last_seq\":{}",
        n1, disk, disk as f64 / n1.max(1) as f64, p.rss, p.peak_rss, p.private, seq));
    // 8. State hash: every store that ran the same suite must print the same value.
    if !a.flag("no-digest") {
        let t = Instant::now();
        let d = workload::digest(&mut *store);
        cm.line("digest", format!("\"digest\":\"{:032x}\",\"seconds\":{:.1}", d, t.elapsed().as_secs_f64()));
    }
}

fn main() {
    let cmd = std::env::args().nth(1).unwrap_or_default();
    let a = Args::new();
    match cmd.as_str() {
        "gen" => dataset::generate(&PathBuf::from(a.s("out", "data")), a.n("nodes", 2_000_000) as usize, a.n("seed", 1), a.f("q-pv", 0.985)),
        "import" => dataset::import_ball(&PathBuf::from(a.s("out", "data")), &PathBuf::from(a.s("pos", "ball_pos.csv")), &PathBuf::from(a.s("edges", "ball_edges.csv"))),
        "describe" => dataset::describe(&Dataset::open(&PathBuf::from(a.s("data", "data")), true)),
        "load" => {
            let kind = a.s("store", "mmap");
            let ds = Dataset::open(&PathBuf::from(a.s("data", "data")), true);
            let o = open_opts(&a, ds.n as u64, ds.e as u64);
            let p0 = proc_stats();
            let t = Instant::now();
            store::bulk_load(&kind, &o, &ds);
            let secs = t.elapsed().as_secs_f64();
            let p1 = proc_stats();
            let mut s = store::open(&kind, &o, Some(&ds));
            let disk = s.disk_bytes();
            emit(&a.get("out").map(PathBuf::from), &format!(
                "{{\"stage\":\"load\",\"store\":\"{}\",\"kind\":\"{}\",\"dataset\":\"{}\",\"tag\":\"{}\",\"positions\":{},\"edges\":{},\"seconds\":{:.1},\"positions_per_s\":{:.0},\"disk_bytes\":{},\"bytes_per_position\":{:.1},\"peak_rss_bytes\":{},\"write_bytes\":{}}}",
                s.name(), kind, PathBuf::from(a.s("data", "data")).file_name().unwrap().to_string_lossy(), a.s("tag", ""), ds.n, ds.e, secs, ds.n as f64 / secs, disk, disk as f64 / ds.n as f64, p1.peak_rss, p1.write_bytes - p0.write_bytes));
        }
        "suite" => suite(&a),
        "digest" => {
            let kind = a.s("store", "mmap");
            let data = a.s("data", "data");
            let (dn, de) = dataset_counts(&data);
            let ds = if kind == "mem" { Some(Dataset::open(&PathBuf::from(&data), true)) } else { None };
            let mut s = store::open(&kind, &open_opts(&a, dn, de), ds.as_ref());
            println!("{:032x} seq={} positions={}", workload::digest(&mut *s), s.last_seq(), s.node_count());
        }
        "dump" => {
            // Debugging aid: print records, edge blocks and parent sets of a range of ids.
            let kind = a.s("store", "mmap");
            let data = a.s("data", "data");
            let (dn, de) = dataset_counts(&data);
            let ds = if kind == "mem" { Some(Dataset::open(&PathBuf::from(&data), true)) } else { None };
            let mut s = store::open(&kind, &open_opts(&a, dn, de), ds.as_ref());
            let ids: Vec<u32> = (a.n("from", 0) as u32..a.n("to", 100) as u32).collect();
            let (mut nodes, mut offs, mut edges, mut poffs, mut pars) = (Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new());
            s.get_nodes(&ids, &mut nodes);
            s.get_children(&ids, &mut offs, &mut edges);
            s.get_parents(&ids, &mut poffs, &mut pars);
            for (i, id) in ids.iter().enumerate() {
                let mut ps = pars[poffs[i] as usize..poffs[i + 1] as usize].to_vec();
                ps.sort_unstable();
                let es: Vec<String> = edges[offs[i] as usize..offs[i + 1] as usize].iter().map(|e| format!("{}:{}", e.mv, e.child)).collect();
                println!("{} {:?} [{}] {:?}", id, nodes[i], es.join(" "), ps);
            }
        }
        "crash-child" => crash::child(&a),
        "crash" => crash::run(&a),
        _ => {
            eprintln!("usage: atomicdb-v2-bench <gen|import|describe|load|suite|digest|crash> [--key value ...]\nstores: {}", store::KINDS.join(", "));
            std::process::exit(2);
        }
    }
}
