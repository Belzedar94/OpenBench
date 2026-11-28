//! Datasets: the frozen graph every store is loaded from.
//!
//! On disk: `nodes.bin` (N x 32 B, id = index, creation order), `edges.bin`
//! (E x 8 B: child u32, move u16, pad; in expansion order, as the edge heap of
//! production is), `out_start.bin` (N x u32) and `out_cnt.bin` (N x u16).
//! Two sources: a synthetic generator calibrated on a production sample, and
//! an importer for a real slice exported read-only from production.

use crate::model::*;
use crate::rules;
use memmap2::{Mmap, MmapMut, MmapOptions};
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

pub struct Dataset {
    pub n: usize,
    pub e: usize,
    nodes: Mmap,
    edges: Mmap,
    out_start: Mmap,
    out_cnt: Mmap,
    in_off: Vec<u32>,
    in_par: Vec<u32>,
}

fn map_ro(p: &Path) -> Mmap { unsafe { Mmap::map(&File::open(p).unwrap_or_else(|e| panic!("open {p:?}: {e}"))).unwrap() } }
fn create_rw(p: &Path, len: u64) -> (File, MmapMut) {
    let f = OpenOptions::new().read(true).write(true).create(true).truncate(true).open(p).unwrap();
    f.set_len(len.max(1)).unwrap();
    let m = unsafe { MmapOptions::new().map_mut(&f).unwrap() };
    (f, m)
}
#[inline] fn rd_u32(m: &[u8], i: usize) -> u32 { u32::from_le_bytes(m[i * 4..i * 4 + 4].try_into().unwrap()) }
#[inline] fn wr_u32(m: &mut [u8], i: usize, v: u32) { m[i * 4..i * 4 + 4].copy_from_slice(&v.to_le_bytes()) }
#[inline] fn rd_u16(m: &[u8], i: usize) -> u16 { u16::from_le_bytes(m[i * 2..i * 2 + 2].try_into().unwrap()) }
#[inline] fn wr_u16(m: &mut [u8], i: usize, v: u16) { m[i * 2..i * 2 + 2].copy_from_slice(&v.to_le_bytes()) }
#[inline] fn rd_node(m: &[u8], i: usize) -> Node { Node::from_bytes(&m[i * NODE_SIZE..]) }
#[inline] fn wr_node(m: &mut [u8], i: usize, n: &Node) { m[i * NODE_SIZE..(i + 1) * NODE_SIZE].copy_from_slice(&n.to_bytes()) }
#[inline] fn rd_edge(m: &[u8], i: usize) -> Edge { Edge { child: rd_u32(m, i * 2), mv: rd_u16(m, i * 4 + 2) } }
#[inline] fn wr_edge(m: &mut [u8], i: usize, e: Edge) { wr_u32(m, i * 2, e.child); wr_u16(m, i * 4 + 2, e.mv); wr_u16(m, i * 4 + 3, 0); }

impl Dataset {
    pub fn open(dir: &Path, with_in_edges: bool) -> Dataset {
        let meta = std::fs::read_to_string(dir.join("meta.txt")).expect("meta.txt");
        let get = |k: &str| -> usize { meta.lines().find_map(|l| l.strip_prefix(k).and_then(|v| v.strip_prefix('='))).unwrap().trim().parse().unwrap() };
        let (n, e) = (get("nodes"), get("edges"));
        let mut ds = Dataset { n, e, nodes: map_ro(&dir.join("nodes.bin")), edges: map_ro(&dir.join("edges.bin")),
                               out_start: map_ro(&dir.join("out_start.bin")), out_cnt: map_ro(&dir.join("out_cnt.bin")),
                               in_off: Vec::new(), in_par: Vec::new() };
        if with_in_edges { ds.build_in_edges(); }
        ds
    }
    fn build_in_edges(&mut self) {
        // The parents of a position are a SET: production has parents that
        // reach the same child by two different moves (atomic captures do that).
        let first_of = |s: usize, i: usize, ch: u32| -> bool { !(s..i).any(|j| rd_edge(&self.edges, j).child == ch) };
        let mut off = vec![0u32; self.n + 1];
        for p in 0..self.n {
            let (s, c) = (rd_u32(&self.out_start, p) as usize, rd_u16(&self.out_cnt, p) as usize);
            for i in s..s + c { let ch = rd_edge(&self.edges, i).child; if first_of(s, i, ch) { off[ch as usize + 1] += 1; } }
        }
        for i in 0..self.n { off[i + 1] += off[i]; }
        let mut fill = off.clone();
        let mut par = vec![0u32; off[self.n] as usize];
        for p in 0..self.n {
            let (s, c) = (rd_u32(&self.out_start, p) as usize, rd_u16(&self.out_cnt, p) as usize);
            for i in s..s + c {
                let ch = rd_edge(&self.edges, i).child;
                if !first_of(s, i, ch) { continue; }
                par[fill[ch as usize] as usize] = p as u32;
                fill[ch as usize] += 1;
            }
        }
        self.in_off = off;
        self.in_par = par;
    }
    #[inline] pub fn node(&self, id: u32) -> Node { rd_node(&self.nodes, id as usize) }
    #[inline] pub fn node_bytes(&self, id: u32) -> &[u8] { &self.nodes[id as usize * NODE_SIZE..(id as usize + 1) * NODE_SIZE] }
    #[inline] pub fn out_degree(&self, id: u32) -> usize { rd_u16(&self.out_cnt, id as usize) as usize }
    pub fn children(&self, id: u32, out: &mut Vec<Edge>) {
        let (s, c) = (rd_u32(&self.out_start, id as usize) as usize, rd_u16(&self.out_cnt, id as usize) as usize);
        for i in s..s + c { out.push(rd_edge(&self.edges, i)); }
    }
    #[inline] pub fn parents(&self, id: u32) -> &[u32] { &self.in_par[self.in_off[id as usize] as usize..self.in_off[id as usize + 1] as usize] }
    pub fn has_in_edges(&self) -> bool { !self.in_off.is_empty() }
}

/// Builder shared by the generator and the importer: arrays live in mapped
/// files so that a 200M-position dataset does not need 10 GB of commit.
struct Builder {
    dir: PathBuf,
    cap_n: usize,
    cap_e: usize,
    n: usize,
    e: usize,
    nodes: MmapMut,
    edges: MmapMut,
    out_start: MmapMut,
    out_cnt: MmapMut,
    files: Vec<(File, &'static str)>,
}

impl Builder {
    fn new(dir: &Path, cap_n: usize, cap_e: usize) -> Builder {
        std::fs::create_dir_all(dir).unwrap();
        let (f1, nodes) = create_rw(&dir.join("nodes.bin"), (cap_n * NODE_SIZE) as u64);
        let (f2, edges) = create_rw(&dir.join("edges.bin"), (cap_e * 8) as u64);
        let (f3, out_start) = create_rw(&dir.join("out_start.bin"), (cap_n * 4) as u64);
        let (f4, out_cnt) = create_rw(&dir.join("out_cnt.bin"), (cap_n * 2) as u64);
        Builder { dir: dir.to_path_buf(), cap_n, cap_e, n: 0, e: 0, nodes, edges, out_start, out_cnt,
                  files: vec![(f1, "nodes.bin"), (f2, "edges.bin"), (f3, "out_start.bin"), (f4, "out_cnt.bin")] }
    }
    #[inline] fn node(&self, id: usize) -> Node { rd_node(&self.nodes, id) }
    #[inline] fn set_node(&mut self, id: usize, n: &Node) { wr_node(&mut self.nodes, id, n) }
    fn push_node(&mut self, n: &Node) -> usize {
        assert!(self.n < self.cap_n, "node capacity");
        let id = self.n;
        wr_node(&mut self.nodes, id, n);
        self.n += 1;
        id
    }
    fn set_children(&mut self, parent: usize, es: &[Edge]) {
        assert!(self.e + es.len() <= self.cap_e, "edge capacity");
        wr_u32(&mut self.out_start, parent, self.e as u32);
        wr_u16(&mut self.out_cnt, parent, es.len() as u16);
        for (i, e) in es.iter().enumerate() { wr_edge(&mut self.edges, self.e + i, *e); }
        self.e += es.len();
    }
    #[inline] fn child_range(&self, p: usize) -> (usize, usize) { (rd_u32(&self.out_start, p) as usize, rd_u16(&self.out_cnt, p) as usize) }
    #[inline] fn edge(&self, i: usize) -> Edge { rd_edge(&self.edges, i) }

    /// Bring statuses and backed values to the rest point of the benchmark
    /// rules, so that the measured climbs only carry real changes.
    fn settle(&mut self, passes: usize) -> usize {
        let mut total = 0;
        let (mut es, mut kids) = (Vec::new(), Vec::new());
        for _ in 0..passes {
            let mut changed = 0;
            for id in (0..self.n).rev() {
                let (s, c) = self.child_range(id);
                if c == 0 { continue; }
                let mut n = self.node(id);
                es.clear(); kids.clear();
                for i in s..s + c { let e = self.edge(i); es.push(e); kids.push(self.node(e.child as usize)); }
                let before = n;
                if let Some((st, m, mv)) = rules::close_rule(&n, &es, &kids) { n.set_status(st, CL_MINIMAX); n.mate_in = m; n.best_move = mv; }
                let (b, bm, bp, bn) = rules::decide(&n, &es, &kids);
                n.backed = b; n.backed_move = bm; n.backed_plies = bp; n.backed_nodes = bn;
                if n != before { self.set_node(id, &n); changed += 1; }
            }
            total += changed;
            if changed == 0 { break; }
        }
        total
    }

    fn finish(mut self, extra_meta: &str) {
        self.nodes.flush().unwrap(); self.edges.flush().unwrap(); self.out_start.flush().unwrap(); self.out_cnt.flush().unwrap();
        let (n, e) = (self.n, self.e);
        let dir = self.dir.clone();
        let files = std::mem::take(&mut self.files);
        drop(self); // unmap before truncating (Windows refuses to shrink a mapped file)
        for (f, name) in files {
            let len = match name { "nodes.bin" => n * NODE_SIZE, "edges.bin" => e * 8, "out_start.bin" => n * 4, _ => n * 2 } as u64;
            f.set_len(len.max(1)).unwrap();
            f.sync_all().unwrap();
        }
        let mut m = File::create(dir.join("meta.txt")).unwrap();
        writeln!(m, "nodes={n}\nedges={e}\n{extra_meta}").unwrap();
    }
}

/// Synthetic graph grown the way production grows: pick a leaf by a descent
/// from the root that mostly follows the best line, expand it, seed a few
/// replies, sometimes prove a mate and materialise its line.
pub fn generate(dir: &Path, target_nodes: usize, seed: u64, q_pv: f64) {
    let t0 = std::time::Instant::now();
    let cap_n = target_nodes + 256;
    let cap_e = target_nodes + target_nodes / 4 + 4096;
    let mut b = Builder::new(dir, cap_n, cap_e);
    let mut rng = Rng::new(seed);
    // Generator-only side arrays, also file-backed.
    let (_pf, mut parent0) = create_rw(&dir.join("gen_parent0.tmp"), (cap_n * 4) as u64);
    let mut depth = vec![0u8; cap_n];
    let mut root = Node::default();
    root.flags |= F_REACHABLE;
    root.eval = 60;
    b.push_node(&root);
    let mut expansions = 0u64;
    let mut depth_hist = vec![0u64; 256];
    let mut transposed = 0u64;
    let mut es: Vec<Edge> = Vec::new();
    while b.n < target_nodes {
        // Descent.
        let mut x = 0usize;
        let mut steps = 0;
        loop {
            let nx = b.node(x);
            if !nx.expanded() { break; }
            let (s, c) = b.child_range(x);
            // Mostly the best line still standing (the first open reply), sometimes a side line.
            let mut pick = None;
            if rng.chance(q_pv) {
                for i in 0..c { let ch = b.edge(s + i).child as usize; if b.node(ch).is_open() { pick = Some(ch); break; } }
            } else {
                for _ in 0..8 {
                    let ch = b.edge(s + rng.below(c as u64) as usize).child as usize;
                    if b.node(ch).is_open() { pick = Some(ch); break; }
                }
            }
            let Some(ch) = pick else { x = usize::MAX; break };
            x = ch;
            steps += 1;
            if steps > 240 { x = usize::MAX; break; }
        }
        if x == usize::MAX { continue; }
        let mut nx = b.node(x);
        if !nx.is_open() { continue; }
        let d = depth[x] as usize;
        depth_hist[d.min(255)] += 1;
        expansions += 1;
        // Transposition candidates: children of an expanded sibling.
        let mut cousin: Vec<u32> = Vec::new();
        if x != 0 {
            let p = rd_u32(&parent0, x) as usize;
            let (ps, pc) = b.child_range(p);
            for _ in 0..3 {
                let sib = b.edge(ps + rng.below(pc as u64) as usize).child as usize;
                if sib != x && b.node(sib).expanded() {
                    let (ss, sc) = b.child_range(sib);
                    for i in ss..ss + sc { cousin.push(b.edge(i).child); }
                    break;
                }
            }
        }
        let k = sample_out_degree(&mut rng);
        let sign = nx.sign();
        nx.eval = (nx.eval as f64 + rng.normal(0.0, 40.0)).clamp(-3000.0, 3000.0) as i16;
        if nx.eval == NO_EVAL { nx.eval = 0; }
        nx.nodes = sample_budget(&mut rng);
        nx.visits = 1;
        nx.flags |= F_EXPANDED;
        es.clear();
        for i in 0..k {
            let mv = (i as u16) * 64 + rng.below(64) as u16;
            if rng.chance(P_TRANSPOSE) {
                let c = if !cousin.is_empty() { cousin[rng.below(cousin.len() as u64) as usize] }
                        else { (x as u64 + 1 + rng.below(TRANSPOSE_WINDOW.min((b.n - x) as u64))) as u32 };
                if (c as usize) > x && (c as usize) < b.n && !es.iter().any(|e| e.child == c) { es.push(Edge { child: c, mv }); transposed += 1; continue; }
            }
            let mut ch = Node::default();
            ch.flags = F_REACHABLE | if sign > 0 { F_BLACK_TO_MOVE } else { 0 };
            if i < SEEDED_LINES {
                ch.eval = (nx.eval as i32 - sign * (i as i32 * 12 + rng.below(20) as i32)).clamp(-9000, 9000) as i16;
            }
            let id = b.push_node(&ch);
            wr_u32(&mut parent0, id, x as u32);
            depth[id] = (d + 1).min(255) as u8;
            es.push(Edge { child: id as u32, mv });
        }
        nx.best_move = es[0].mv;
        // Mates: a won line under the first move (the position closes), or a lost reply elsewhere.
        let r = rng.unit();
        let mate_child = if d >= 3 && r < P_WIN { Some((0usize, win_status_for(sign))) }
                         else if d >= 3 && r < P_WIN + P_LOSS_CHILD && k > 1 { Some((1 + rng.below(k as u64 - 1) as usize, win_status_for(-sign))) }
                         else { None };
        b.set_children(x, &es);
        if let Some((ci, winner)) = mate_child {
            let head = es[ci].child as usize;
            let mut hn = b.node(head);
            if hn.is_open() && !hn.expanded() && b.child_range(head).1 == 0 {
                let len = sample_chain(&mut rng);
                hn.set_status(winner, CL_MATE_PV);
                hn.mate_in = len as u16;
                hn.slack = 100;
                b.set_node(head, &hn);
                let mut prev = head;
                for j in 1..len {
                    let mut cn = Node::default();
                    let pb = b.node(prev).black_to_move();
                    cn.flags = F_REACHABLE | if pb { 0 } else { F_BLACK_TO_MOVE };
                    cn.set_status(winner, if j + 1 == len { CL_TERMINAL } else { CL_MATE_PV });
                    cn.mate_in = (len - j) as u16;
                    cn.slack = 100;
                    let id = b.push_node(&cn);
                    wr_u32(&mut parent0, id, prev as u32);
                    depth[id] = (depth[prev] as usize + 1).min(255) as u8;
                    let mv = rng.below(4096) as u16;
                    b.set_children(prev, &[Edge { child: id as u32, mv }]);
                    let mut pn = b.node(prev);
                    pn.best_move = mv;
                    b.set_node(prev, &pn);
                    prev = id;
                }
                if ci == 0 { nx.set_status(winner, CL_MINIMAX); nx.mate_in = len as u16 + 1; }
            }
        }
        b.set_node(x, &nx);
    }
    let gen_s = t0.elapsed().as_secs_f64();
    let changed = b.settle(1);
    // Depth of the positions that were searched, to compare with production (p50 52 plies).
    let tot: u64 = depth_hist.iter().sum();
    let pct = |p: f64| { let mut acc = 0; for (d, c) in depth_hist.iter().enumerate() { acc += c; if acc as f64 >= p * tot as f64 { return d; } } 255 };
    let meta = format!("kind=synthetic\nseed={seed}\nq_pv={q_pv}\nexpansions={expansions}\ntransposed_edges={transposed}\nlanding_depth_p10={}\nlanding_depth_p50={}\nlanding_depth_p90={}\nsettled={changed}",
                       pct(0.10), pct(0.50), pct(0.90));
    println!("generated {} nodes, {} edges, {} expansions in {:.1}s (+settle {:.1}s); landing depth p10/p50/p90 = {}/{}/{}",
             b.n, b.e, expansions, gen_s, t0.elapsed().as_secs_f64() - gen_s, pct(0.10), pct(0.50), pct(0.90));
    drop(parent0);
    drop(_pf);
    b.finish(&meta);
    let _ = std::fs::remove_file(dir.join("gen_parent0.tmp"));
}

fn uci_to_code(s: &str) -> u16 {
    let b = s.as_bytes();
    if b.len() < 4 { return 0; }
    let sq = |f: u8, r: u8| ((r.wrapping_sub(b'1') & 7) as u16) * 8 + ((f.wrapping_sub(b'a') & 7) as u16);
    let promo = match b.get(4) { Some(b'n') => 1, Some(b'b') => 2, Some(b'r') => 3, Some(b'q') => 4, Some(b'k') => 5, _ => 0u16 };
    sq(b[0], b[1]) | (sq(b[2], b[3]) << 6) | (promo << 12)
}

/// Import the real slice exported from production: `ball_pos.csv` and
/// `ball_edges.csv` (see the benchmark report for the export queries).
/// Ids follow production creation order (the smallest edge id that reaches a
/// position), so sibling locality is the real one.
pub fn import_ball(dir: &Path, pos_csv: &Path, edges_csv: &Path) {
    use std::collections::HashMap;
    use std::io::{BufRead, BufReader};
    let t0 = std::time::Instant::now();
    // Pass 1: positions.
    struct Row { node: Node, first_edge: u64 }
    let mut rows: Vec<Row> = Vec::new();
    let mut index: HashMap<[u8; 12], u32> = HashMap::new();
    let hex12 = |s: &str| -> [u8; 12] { let mut k = [0u8; 12]; for i in 0..12 { k[i] = u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).unwrap(); } k };
    let rd = BufReader::new(File::open(pos_csv).unwrap());
    let mut lines = rd.lines();
    let header: Vec<String> = lines.next().unwrap().unwrap().split(',').map(|s| s.to_string()).collect();
    let col = |name: &str| header.iter().position(|h| h == name).unwrap_or_else(|| panic!("missing column {name}"));
    let (c_key, c_depth, c_status, c_closure, c_eval, c_backed, c_bmove, c_bplies, c_bnodes, c_best, c_mate, c_slack, c_reach, c_nodes, c_visits, c_stm) =
        (col("key"), col("depth"), col("status"), col("closure"), col("eval_cp"), col("backed_eval"), col("backed_move"), col("backed_plies"), col("backed_nodes"),
         col("best_move"), col("mate_in"), col("clock_slack"), col("reachable"), col("nodes_invested"), col("visits"), col("stm"));
    for line in lines {
        let line = line.unwrap();
        let f: Vec<&str> = line.split(',').collect();
        let mut n = Node::default();
        let st = match f[c_status] { "WHITE_WIN" => ST_WHITE, "BLACK_WIN" => ST_BLACK, "DRAW" => ST_DRAW, _ => ST_UNKNOWN };
        let cl = match f[c_closure] { "MATE_PV" => CL_MATE_PV, "TERMINAL" => CL_TERMINAL, "MINIMAX" => CL_MINIMAX, "TB" => CL_TB, "" => CL_NONE, _ => CL_MATE_PV };
        n.set_status(st, cl);
        if f[c_stm] == "b" { n.flags |= F_BLACK_TO_MOVE; }
        if f[c_reach] == "t" { n.flags |= F_REACHABLE; }
        let cp = |s: &str| -> i16 { if s.is_empty() { NO_EVAL } else { s.parse::<i32>().unwrap().clamp(-10000, 10000) as i16 } };
        n.eval = cp(f[c_eval]);
        n.backed = cp(f[c_backed]);
        n.backed_move = uci_to_code(f[c_bmove]);
        n.backed_plies = f[c_bplies].parse::<i64>().unwrap_or(0).clamp(0, 65535) as u16;
        n.backed_nodes = f[c_bnodes].parse().unwrap_or(0);
        n.best_move = uci_to_code(f[c_best]);
        n.mate_in = f[c_mate].parse::<i64>().unwrap_or(0).clamp(0, 65535) as u16;
        n.slack = f[c_slack].parse::<i64>().unwrap_or(0).clamp(0, 255) as u8;
        n.nodes = f[c_nodes].parse().unwrap_or(0);
        n.visits = f[c_visits].parse::<i64>().unwrap_or(0).clamp(0, 255) as u8;
        let first_edge = if f[c_depth] == "0" { 0 } else { u64::MAX };
        index.insert(hex12(f[c_key]), rows.len() as u32);
        rows.push(Row { node: n, first_edge });
    }
    // Pass 2: edges.
    struct E { id: u64, parent: u32, child: u32, mv: u16 }
    let mut edges: Vec<E> = Vec::new();
    let rd = BufReader::new(File::open(edges_csv).unwrap());
    let mut lines = rd.lines();
    let _ = lines.next();
    let mut dropped = 0u64;
    for line in lines {
        let line = line.unwrap();
        let f: Vec<&str> = line.split(',').collect();
        let (Some(&p), Some(&c)) = (index.get(&hex12(f[1])), index.get(&hex12(f[3]))) else { dropped += 1; continue };
        let id: u64 = f[0].parse().unwrap();
        if id < rows[c as usize].first_edge { rows[c as usize].first_edge = id; }
        edges.push(E { id, parent: p, child: c, mv: uci_to_code(f[2]) });
    }
    // Creation order.
    let mut order: Vec<u32> = (0..rows.len() as u32).collect();
    order.sort_by_key(|&i| rows[i as usize].first_edge);
    let mut new_id = vec![0u32; rows.len()];
    for (new, &old) in order.iter().enumerate() { new_id[old as usize] = new as u32; }
    edges.sort_by_key(|e| (new_id[e.parent as usize], e.id));
    let mut b = Builder::new(dir, rows.len() + 16, edges.len() + 16);
    for &old in &order { b.push_node(&rows[old as usize].node); }
    let mut i = 0;
    let mut es: Vec<Edge> = Vec::new();
    let mut real_expanded = 0u64;
    while i < edges.len() {
        let p = new_id[edges[i].parent as usize];
        es.clear();
        while i < edges.len() && new_id[edges[i].parent as usize] == p {
            es.push(Edge { child: new_id[edges[i].child as usize], mv: edges[i].mv });
            i += 1;
        }
        b.set_children(p as usize, &es);
        // A node counts as expanded when its real move list came with it (a
        // materialised mate line has a single edge and is not an expansion).
        let mut n = b.node(p as usize);
        if es.len() >= 2 || n.nodes > 0 { n.flags |= F_EXPANDED; real_expanded += 1; b.set_node(p as usize, &n); }
    }
    // Positions at the rim were searched in production but their children are
    // outside the slice: make them plain leaves again (their eval stays, as a seed).
    for id in 0..b.n {
        if b.child_range(id).1 == 0 {
            let mut n = b.node(id);
            if n.nodes > 0 || n.backed != NO_EVAL {
                n.nodes = 0; n.visits = 0; n.backed = NO_EVAL; n.backed_move = 0; n.backed_plies = 0; n.backed_nodes = 0;
                b.set_node(id, &n);
            }
        }
    }
    let changed = b.settle(6);
    println!("imported {} positions, {} edges ({} dropped), {} expanded, settle rewrote {} records, {:.1}s",
             b.n, b.e, dropped, real_expanded, changed, t0.elapsed().as_secs_f64());
    let meta = format!("kind=production-ball\nexpanded={real_expanded}\nsettled={changed}");
    b.finish(&meta);
}

/// Print the shape of a dataset next to the production figures it is meant to match.
pub fn describe(ds: &Dataset) {
    let n = ds.n as f64;
    let (mut closed, mut expanded, mut searched, mut has_eval, mut backed_open, mut chain, mut leaves) = (0u64, 0u64, 0u64, 0u64, 0u64, 0u64, 0u64);
    let mut out_sum = 0u64;
    let mut multi_parent = 0u64;
    for id in 0..ds.n as u32 {
        let nd = ds.node(id);
        let od = ds.out_degree(id);
        closed += !nd.is_open() as u64;
        expanded += nd.expanded() as u64;
        searched += (nd.nodes > 0) as u64;
        has_eval += (nd.eval != NO_EVAL) as u64;
        backed_open += (nd.is_open() && nd.backed != NO_EVAL) as u64;
        chain += (od == 1) as u64;
        leaves += (od == 0) as u64;
        if nd.expanded() { out_sum += od as u64; }
        if ds.has_in_edges() && ds.parents(id).len() > 1 { multi_parent += 1; }
    }
    println!("positions {}  edges {}  edges/position {:.3} (production 1.087)", ds.n, ds.e, ds.e as f64 / n);
    println!("closed {:.3} (prod 0.248)  expanded {:.4} (prod 0.029)  searched {:.4} (prod 0.029)  has eval {:.3} (prod 0.145)  open with backed {:.4} (prod 0.021)",
             closed as f64 / n, expanded as f64 / n, searched as f64 / n, has_eval as f64 / n, backed_open as f64 / n);
    println!("out-degree 0: {:.3} (prod 0.741)  out-degree 1: {:.3} (prod 0.222)  mean out-degree of expanded {:.1} (prod 28.7)  positions with >1 parent {:.4} (prod 0.054)",
             leaves as f64 / n, chain as f64 / n, out_sum as f64 / expanded.max(1) as f64, multi_parent as f64 / n);
}
