//! av2-load: build a v2 store from a v1 export (plan section 8.2).
//!
//! ```text
//! av2-load --pos position.csv --edges edge.csv --out DIR [--shard-bits 12] [--root KEY] [--lenient]
//! ```
//!
//! Ids follow creation order: positions without an incoming edge first (the
//! root, `--root`, default the start position, at id 0; the others in file
//! order), then every other position in the order of its first incoming
//! edge id.  Out-edge blocks keep v1's edge order (by edge id); parent lists
//! hold each distinct parent once, in the order of its first edge.

use av2_facts::{Fact, Genesis};
use av2_model::{Edge, Fingerprint, Move, PosId};
use av2_store::Config;
use av2_store::loader::BulkLoader;
use av2_tools::export::{self, Cols};
use av2_tools::{Args, die, emit};
use std::collections::HashMap;
use std::time::Instant;

const START_KEY: &str = "b1791d7fc9ae3d38966568c257ffb3a02cbf8394cdb4805bc70f64fc3c0b6879";

fn main() {
    let a = Args::parse();
    let pos_path = a.path("pos");
    let edge_path = a.path("edges");
    let out_dir = a.path("out");
    let out = a.get("json").map(std::path::PathBuf::from);
    let lenient = a.flag("lenient");
    let root = Fingerprint::from_hex(&a.s("root", START_KEY)).unwrap_or_else(|e| die(&e.to_string()));
    let cfg = Config { shard_bits: a.n("shard-bits", 12) as u32, durable: true, ..Config::default() };
    let t0 = Instant::now();

    // Pass 1: the keys of every position, in file order.
    let mut rdr = export::reader(&pos_path).unwrap_or_else(|e| die(&e));
    let cols = Cols::new(&rdr.headers().unwrap().clone());
    let mut fps: Vec<Fingerprint> = Vec::new();
    let mut rec = csv::StringRecord::new();
    while rdr.read_record(&mut rec).unwrap_or_else(|e| die(&e.to_string())) {
        let k = cols.req(&rec, "key").unwrap_or_else(|e| die(&e));
        fps.push(Fingerprint::from_hex(k).unwrap_or_else(|e| die(&e.to_string())));
    }
    let n = fps.len();
    let mut row_of: HashMap<Fingerprint, u32> = HashMap::with_capacity(n);
    for (i, fp) in fps.iter().enumerate() {
        if row_of.insert(*fp, i as u32).is_some() {
            die(&format!("duplicate key {fp:?} in the position export"));
        }
    }
    let t_keys = t0.elapsed();

    // Pass 2: edges, as (edge id, parent row, child row, move); sorted by edge id.
    let mut rdr = export::reader(&edge_path).unwrap_or_else(|e| die(&e));
    let ecols = Cols::new(&rdr.headers().unwrap().clone());
    let mut edges: Vec<(u64, u32, u32, Move)> = Vec::new();
    let (mut dangling, mut bad_edges) = (0u64, 0u64);
    while rdr.read_record(&mut rec).unwrap_or_else(|e| die(&e.to_string())) {
        match export::parse_edge(&ecols, &rec) {
            Ok((id, p, mv, c)) => match (row_of.get(&p), row_of.get(&c)) {
                (Some(&pr), Some(&cr)) => edges.push((id, pr, cr, mv)),
                _ => dangling += 1,
            },
            Err(e) => {
                bad_edges += 1;
                if !lenient {
                    die(&format!("edge row {}: {e}", rec.position().map(|p| p.line()).unwrap_or(0)));
                }
            }
        }
    }
    edges.sort_unstable_by_key(|e| e.0);
    if edges.windows(2).any(|w| w[0].0 == w[1].0) {
        die("duplicate edge id in the edge export");
    }
    let t_edges = t0.elapsed();

    // Ids in creation order.
    let mut first_in = vec![u64::MAX; n];
    for &(id, _, c, _) in &edges {
        let f = &mut first_in[c as usize];
        if *f == u64::MAX {
            *f = id;
        }
    }
    let root_row = row_of.get(&root).copied();
    let mut order: Vec<u32> = (0..n as u32).collect();
    // Orphans (no incoming edge) first, root before the others, then by first incoming edge.
    order.sort_by_key(|&r| {
        let orphan = first_in[r as usize] == u64::MAX;
        (!orphan, if orphan { if Some(r) == root_row { 0 } else { 1 + r as u64 } } else { first_in[r as usize] })
    });
    let mut id_of = vec![0u64; n];
    for (id, &r) in order.iter().enumerate() {
        id_of[r as usize] = id as u64;
    }
    let orphans = first_in.iter().filter(|&&f| f == u64::MAX).count();
    drop(order);
    drop(row_of);

    // v1's stored priority (export column `priority`), for the queue's exact
    // AUTO/FILL order: `<out>/v1prio.col`, 8 bytes per id (av2_model::record::V1PRIO_FILE).
    let mut prio_map = if cols.has("priority") {
        std::fs::create_dir_all(&out_dir).unwrap_or_else(|e| die(&e.to_string()));
        let f = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(out_dir.join(av2_model::record::V1PRIO_FILE))
            .unwrap_or_else(|e| die(&e.to_string()));
        f.set_len((n as u64).max(1) * 8).unwrap_or_else(|e| die(&e.to_string()));
        Some(unsafe { memmap2::MmapMut::map_mut(&f) }.unwrap_or_else(|e| die(&e.to_string())))
    } else {
        None
    };
    let mut prio_nonzero = 0u64;

    // Pass 3: positions.
    let mut bl = BulkLoader::create(&out_dir, cfg, n as u64, edges.len() as u64).unwrap_or_else(|e| die(&e.to_string()));
    let mut rdr = export::reader(&pos_path).unwrap_or_else(|e| die(&e));
    let (mut row, mut bad_rows, mut exts, mut blobs) = (0usize, 0u64, 0u64, 0u64);
    while rdr.read_record(&mut rec).unwrap_or_else(|e| die(&e.to_string())) {
        let p = match export::parse_position(&cols, &rec) {
            Ok(p) => p,
            Err(e) => {
                bad_rows += 1;
                if !lenient {
                    die(&format!("position row {}: {e}", row + 2));
                }
                // Keep the position (its key and side to move) with default values.
                export::PosRow {
                    fp: fps[row],
                    core: Default::default(),
                    ext: Default::default(),
                    fen: None,
                    analysis: None,
                    passes: None,
                    won_line: None,
                }
            }
        };
        let ext = if p.needs_ext() {
            exts += 1;
            let mut e = p.ext;
            for (src, dst) in [(&p.analysis, &mut e.analysis), (&p.passes, &mut e.passes), (&p.won_line, &mut e.won_line)] {
                if let Some(t) = src {
                    *dst = bl.put_blob(t.as_bytes()).unwrap_or_else(|e| die(&e.to_string()));
                    blobs += 1;
                }
            }
            Some(e)
        } else {
            None
        };
        bl.set_position(id_of[row], p.fp, p.core, ext, p.fen.as_deref()).unwrap_or_else(|e| die(&e.to_string()));
        if let Some(m) = prio_map.as_mut() {
            let v = export::v1_priority(&cols, &rec).unwrap_or(0.0);
            if v != 0.0 {
                prio_nonzero += 1;
                let at = id_of[row] as usize * 8;
                m[at..at + 8].copy_from_slice(&v.to_le_bytes());
            }
        }
        row += 1;
    }
    drop(fps);
    if let Some(m) = prio_map.take() {
        m.flush().unwrap_or_else(|e| die(&e.to_string()));
    }
    let t_pos = t0.elapsed();

    // Out-edge blocks: by parent id, then edge id.
    let mut by_parent: Vec<(u64, u64, u64, Move)> =
        edges.iter().map(|&(id, p, c, mv)| (id_of[p as usize], id, id_of[c as usize], mv)).collect();
    by_parent.sort_unstable_by_key(|e| (e.0, e.1));
    let mut block: Vec<Edge> = Vec::new();
    let mut i = 0usize;
    let mut dup_moves = 0u64;
    while i < by_parent.len() {
        let p = by_parent[i].0;
        block.clear();
        while i < by_parent.len() && by_parent[i].0 == p {
            let e = Edge { child: PosId(by_parent[i].2), mv: by_parent[i].3 };
            if block.iter().any(|b| b.mv == e.mv) {
                dup_moves += 1;
            }
            block.push(e);
            i += 1;
        }
        bl.set_children(p, &block).unwrap_or_else(|e| die(&e.to_string()));
    }
    drop(by_parent);
    // Parent lists: by child id, then edge id; each parent once.
    let mut by_child: Vec<(u64, u64, u64)> = edges.iter().map(|&(id, p, c, _)| (id_of[c as usize], id, id_of[p as usize])).collect();
    drop(edges);
    by_child.sort_unstable_by_key(|e| (e.0, e.1));
    let mut seen: Vec<u64> = Vec::new();
    let mut multi_parent = 0u64;
    let mut i = 0usize;
    while i < by_child.len() {
        let c = by_child[i].0;
        seen.clear();
        while i < by_child.len() && by_child[i].0 == c {
            let p = by_child[i].2;
            if !seen.contains(&p) {
                seen.push(p);
                bl.add_parent(c, p).unwrap_or_else(|e| die(&e.to_string()));
            }
            i += 1;
        }
        if seen.len() > 1 {
            multi_parent += 1;
        }
    }
    let n_edges = by_child.len() as u64;
    drop(by_child);
    let blob_fens = bl.blob_fens;
    let t_edges_written = t0.elapsed();

    let meta = |p: &std::path::Path| std::fs::metadata(p).map(|m| m.len()).unwrap_or(0);
    let counts = [
        ("positions", n as u64),
        ("edges", n_edges),
        ("orphans", orphans as u64),
        ("dangling_edges", dangling),
        ("bad_edges", bad_edges),
        ("bad_rows", bad_rows),
        ("ext_records", exts),
        ("blobs", blobs),
        ("v1_priorities", prio_nonzero),
        ("blob_fens", blob_fens),
        ("duplicate_moves", dup_moves),
        ("multi_parent_children", multi_parent),
        ("position_csv_bytes", meta(&pos_path)),
        ("edge_csv_bytes", meta(&edge_path)),
    ];
    let genesis = Fact::Genesis(Genesis {
        source: "pg-export".into(),
        created_unix: std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0),
        note: format!("av2-load {} {}", pos_path.display(), edge_path.display()),
        counts: counts.iter().map(|(k, v)| (k.to_string(), *v)).collect(),
    });
    let store = bl.finish(&av2_facts::encode_all(&[genesis])).unwrap_or_else(|e| die(&e.to_string()));
    let t_done = t0.elapsed();
    let (_, live) = store.live_bytes();
    let log = store.log_bytes();
    drop(store);
    let mut j = String::from("{\"stage\":\"load\"");
    for (k, v) in counts {
        j.push_str(&format!(",\"{k}\":{v}"));
    }
    j.push_str(&format!(
        ",\"secs_keys\":{:.2},\"secs_edges_read\":{:.2},\"secs_positions\":{:.2},\"secs_edges_written\":{:.2},\"secs_total\":{:.2},\"graph_bytes\":{},\"bytes_per_position\":{:.1},\"log_bytes\":{}}}",
        t_keys.as_secs_f64(),
        t_edges.as_secs_f64(),
        t_pos.as_secs_f64(),
        t_edges_written.as_secs_f64(),
        t_done.as_secs_f64(),
        live,
        live as f64 / n.max(1) as f64,
        log
    ));
    emit(&out, &j);
}
