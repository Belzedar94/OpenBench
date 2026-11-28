//! av2-check: verify a store against a v1 export and against itself.
//!
//! ```text
//! av2-check --db DIR [--pos position.csv] [--edges edge.csv] [--every K] [--structure] [--scrub]
//! ```
//!
//! - `--pos`: every K-th row (default every row) is looked up by key and
//!   compared field by field; counts by status, closure and proof are
//!   compared over the rows read.
//! - `--edges`: every K-th edge must be in its parent's block (same move,
//!   same child) and the parent in the child's parent list.
//! - `--structure`: every edge points inside the graph, every parent list
//!   matches the out blocks both ways, every key resolves to its id.
//! - `--scrub`: checkpoint, then verify the page checksums.
//! - `--footprint`: live bytes per file and per position.

use av2_model::{Edge, GraphRead, PosId};
use av2_store::{Config, Store};
use av2_tools::export::{self, Cols};
use av2_tools::{Args, die, emit};
use std::collections::BTreeMap;
use std::time::Instant;

fn main() {
    let a = Args::parse();
    let db = a.path("db");
    let out = a.get("json").map(std::path::PathBuf::from);
    let every = a.n("every", 1).max(1);
    let cfg = Config { background_checkpoint: false, ..Config::default() };
    let store = Store::open(&db, cfg).unwrap_or_else(|e| die(&e.to_string()));
    let r = store.reader();
    let mut failed = false;

    if let Some(p) = a.get("pos") {
        let t0 = Instant::now();
        let mut rdr = export::reader(std::path::Path::new(p)).unwrap_or_else(|e| die(&e));
        let cols = Cols::new(&rdr.headers().unwrap().clone());
        let mut rec = csv::StringRecord::new();
        let (mut rows, mut checked, mut missing, mut differ) = (0u64, 0u64, 0u64, 0u64);
        let mut by_field: BTreeMap<&'static str, u64> = BTreeMap::new();
        let mut want_counts: BTreeMap<String, u64> = BTreeMap::new();
        let mut got_counts: BTreeMap<String, u64> = BTreeMap::new();
        let mut examples: Vec<String> = Vec::new();
        while rdr.read_record(&mut rec).unwrap_or_else(|e| die(&e.to_string())) {
            rows += 1;
            if (rows - 1) % every != 0 {
                continue;
            }
            let row = match export::parse_position(&cols, &rec) {
                Ok(x) => x,
                Err(e) => die(&format!("row {rows}: {e}")),
            };
            checked += 1;
            let res = r.read(|v| {
                let id = v.lookup(&row.fp)?;
                let core = v.core(id);
                let ext = v.ext(id);
                let fen = v.fen(id);
                Some((id, export::diff(&row, &core, ext.as_ref(), fen.as_deref(), |b| v.blob(b)), core))
            });
            let key = |c: &av2_model::Core| format!("{}/{}/{}", c.status().v1(), c.closure().v1(), c.proof().v1());
            *want_counts.entry(key(&row.core)).or_default() += 1;
            match res {
                None => {
                    missing += 1;
                    if examples.len() < 5 {
                        examples.push(format!("missing {}", row.fp.to_hex()));
                    }
                }
                Some((id, d, core)) => {
                    *got_counts.entry(key(&core)).or_default() += 1;
                    if !d.is_empty() {
                        differ += 1;
                        for f in &d {
                            *by_field.entry(f).or_default() += 1;
                        }
                        if examples.len() < 5 {
                            examples.push(format!("{} {}: {:?}", row.fp.to_hex(), id, d));
                        }
                    }
                }
            }
        }
        let counts_equal = want_counts == got_counts;
        failed |= missing > 0 || differ > 0 || !counts_equal;
        emit(
            &out,
            &format!(
                "{{\"stage\":\"check_positions\",\"rows\":{rows},\"checked\":{checked},\"missing\":{missing},\"differ\":{differ},\"by_field\":{},\"counts_equal\":{counts_equal},\"counts\":{},\"examples\":{},\"secs\":{:.2}}}",
                serde_json::to_string(&by_field).unwrap(),
                serde_json::to_string(&want_counts).unwrap(),
                serde_json::to_string(&examples).unwrap(),
                t0.elapsed().as_secs_f64()
            ),
        );
    }

    if let Some(p) = a.get("edges") {
        let t0 = Instant::now();
        let mut rdr = export::reader(std::path::Path::new(p)).unwrap_or_else(|e| die(&e));
        let cols = Cols::new(&rdr.headers().unwrap().clone());
        let mut rec = csv::StringRecord::new();
        let (mut rows, mut checked, mut bad) = (0u64, 0u64, 0u64);
        let mut examples: Vec<String> = Vec::new();
        let mut es: Vec<Edge> = Vec::new();
        let mut ps: Vec<PosId> = Vec::new();
        while rdr.read_record(&mut rec).unwrap_or_else(|e| die(&e.to_string())) {
            rows += 1;
            if (rows - 1) % every != 0 {
                continue;
            }
            let (id, pf, mv, cf) = export::parse_edge(&cols, &rec).unwrap_or_else(|e| die(&e));
            checked += 1;
            let ok = r.read(|v| {
                let (Some(p), Some(c)) = (v.lookup(&pf), v.lookup(&cf)) else { return false };
                es.clear();
                v.children(p, &mut es);
                ps.clear();
                v.parents(c, &mut ps);
                es.iter().any(|e| e.child == c && e.mv == mv) && ps.contains(&p)
            });
            if !ok {
                bad += 1;
                if examples.len() < 5 {
                    examples.push(format!("edge {id}"));
                }
            }
        }
        failed |= bad > 0;
        emit(
            &out,
            &format!(
                "{{\"stage\":\"check_edges\",\"rows\":{rows},\"checked\":{checked},\"bad\":{bad},\"examples\":{},\"secs\":{:.2}}}",
                serde_json::to_string(&examples).unwrap(),
                t0.elapsed().as_secs_f64()
            ),
        );
    }

    if a.flag("structure") {
        let t0 = Instant::now();
        let (n, problems, edges, pairs) = r.read(|v| {
            let n = v.len();
            let mut problems: Vec<String> = Vec::new();
            let mut es = Vec::new();
            let mut ps = Vec::new();
            let mut edges = 0u64;
            for id in 0..n {
                let id = PosId(id);
                es.clear();
                v.children(id, &mut es);
                edges += es.len() as u64;
                for e in &es {
                    if e.child.0 >= n {
                        problems.push(format!("{id} -> {} beyond {n}", e.child));
                        continue;
                    }
                    ps.clear();
                    v.parents(e.child, &mut ps);
                    if !ps.contains(&id) {
                        problems.push(format!("{} lacks parent {id}", e.child));
                    }
                }
                ps.clear();
                v.parents(id, &mut ps);
                for p in ps.clone() {
                    es.clear();
                    v.children(p, &mut es);
                    if !es.iter().any(|e| e.child == id) {
                        problems.push(format!("{id} lists parent {p} that has no edge to it"));
                    }
                }
                if problems.len() > 20 {
                    break;
                }
            }
            let mut pairs = Vec::new();
            v.key_pairs(&mut pairs);
            for (fp, id) in &pairs {
                if v.lookup(fp) != Some(*id) {
                    problems.push(format!("key {fp:?} does not resolve to {id}"));
                }
            }
            (n, problems, edges, pairs.len() as u64)
        });
        if pairs != n {
            failed = true;
        }
        failed |= !problems.is_empty();
        emit(
            &out,
            &format!(
                "{{\"stage\":\"check_structure\",\"positions\":{n},\"edges\":{edges},\"keys\":{pairs},\"problems\":{},\"secs\":{:.2}}}",
                serde_json::to_string(&problems).unwrap(),
                t0.elapsed().as_secs_f64()
            ),
        );
    }

    if a.flag("footprint") {
        let (per_file, live) = store.live_bytes();
        let n = r.read(|v| v.len()).max(1);
        let files: Vec<String> =
            (0..av2_store::layout::NFILES).map(|f| format!("\"{}\":{:.2}", av2_store::layout::NAMES[f], per_file[f] as f64 / n as f64)).collect();
        emit(
            &out,
            &format!(
                "{{\"stage\":\"footprint\",\"positions\":{n},\"live_bytes\":{live},\"bytes_per_position\":{:.1},\"per_file\":{{{}}}}}",
                live as f64 / n as f64,
                files.join(",")
            ),
        );
    }

    if a.flag("scrub") {
        let t0 = Instant::now();
        store.checkpoint().unwrap_or_else(|e| die(&e.to_string()));
        let t1 = Instant::now();
        let rep = store.scrub().unwrap_or_else(|e| die(&e.to_string()));
        failed |= !rep.mismatches.is_empty();
        emit(
            &out,
            &format!(
                "{{\"stage\":\"scrub\",\"manifest_seq\":{},\"checked_chunks\":{},\"skipped\":{},\"mismatches\":{},\"checkpoint_secs\":{:.3},\"scrub_secs\":{:.3}}}",
                rep.manifest_seq,
                rep.checked,
                rep.skipped,
                serde_json::to_string(&rep.mismatches).unwrap(),
                (t1 - t0).as_secs_f64(),
                t1.elapsed().as_secs_f64()
            ),
        );
    }
    drop(store);
    if failed {
        std::process::exit(1);
    }
}
