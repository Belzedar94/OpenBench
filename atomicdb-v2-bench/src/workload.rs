//! The workloads, written once against the `Store` interface.
//!
//! A batch is what AtomicDB's writer does: land B search results (expand the
//! position, seed its replies, sometimes prove a mate and materialise the
//! line), then one merged climb through the ancestors (status cascade, then the
//! backed-value ascent level by level).  All of it is computed against the
//! committed state plus a private overlay and handed to the store as one
//! atomic, durable batch.

use crate::model::*;
use crate::rules;
use crate::store::{Reader, Store};
use std::collections::HashMap;
use std::time::Instant;

#[derive(Default, Clone, Copy, Debug)]
pub struct Counters {
    pub node_reads: u64,
    pub block_reads: u64,
    pub parent_reads: u64,
    pub key_lookups: u64,
    pub landings: u64,
    pub expansions: u64,
    pub closures: u64,
    pub backed_writes: u64,
    pub levels: u64,
    pub new_nodes: u64,
    pub new_edges: u64,
    pub puts: u64,
    pub logical_bytes: u64,
}

pub struct Ctx<'a> {
    pub store: &'a mut dyn Store,
    base_n: u32,
    nodes: HashMap<u32, Node>,
    new_nodes: Vec<Node>,
    children: HashMap<u32, Vec<Edge>>,
    extra_parents: HashMap<u32, Vec<u32>>,
    pub c: Counters,
}

impl<'a> Ctx<'a> {
    pub fn new(store: &'a mut dyn Store) -> Ctx<'a> {
        let base_n = store.node_count();
        Ctx { store, base_n, nodes: HashMap::new(), new_nodes: Vec::new(), children: HashMap::new(), extra_parents: HashMap::new(), c: Counters::default() }
    }
    pub fn nodes(&mut self, ids: &[u32]) -> Vec<Node> {
        let mut out = vec![Node::default(); ids.len()];
        let (mut miss_i, mut miss) = (Vec::new(), Vec::new());
        for (i, &id) in ids.iter().enumerate() {
            if id >= self.base_n { out[i] = self.new_nodes[(id - self.base_n) as usize]; }
            else if let Some(n) = self.nodes.get(&id) { out[i] = *n; }
            else { miss_i.push(i); miss.push(id); }
        }
        if !miss.is_empty() {
            let mut got = Vec::with_capacity(miss.len());
            self.store.get_nodes(&miss, &mut got);
            self.c.node_reads += miss.len() as u64;
            for (k, i) in miss_i.into_iter().enumerate() { out[i] = got[k]; }
        }
        out
    }
    pub fn node1(&mut self, id: u32) -> Node { self.nodes(&[id])[0] }
    /// CSR of the children of `ids`.
    pub fn children(&mut self, ids: &[u32]) -> (Vec<u32>, Vec<Edge>) {
        let mut miss = Vec::new();
        for &id in ids { if id < self.base_n && !self.children.contains_key(&id) { miss.push(id); } }
        let (mut moffs, mut medges) = (Vec::new(), Vec::new());
        if !miss.is_empty() { self.store.get_children(&miss, &mut moffs, &mut medges); self.c.block_reads += miss.len() as u64; }
        let (mut offs, mut out) = (Vec::with_capacity(ids.len() + 1), Vec::new());
        offs.push(0u32);
        let mut k = 0usize;
        for &id in ids {
            if let Some(es) = self.children.get(&id) { out.extend_from_slice(es); }
            else if id < self.base_n { out.extend_from_slice(&medges[moffs[k] as usize..moffs[k + 1] as usize]); k += 1; }
            offs.push(out.len() as u32);
        }
        (offs, out)
    }
    pub fn children1(&mut self, id: u32) -> Vec<Edge> { self.children(&[id]).1 }
    /// CSR of the parents of `ids`, each group sorted (stores return them in different orders).
    pub fn parents(&mut self, ids: &[u32]) -> (Vec<u32>, Vec<u32>) {
        let miss: Vec<u32> = ids.iter().copied().filter(|&id| id < self.base_n).collect();
        let (mut moffs, mut mpar) = (Vec::new(), Vec::new());
        if !miss.is_empty() { self.store.get_parents(&miss, &mut moffs, &mut mpar); self.c.parent_reads += miss.len() as u64; }
        let (mut offs, mut out) = (Vec::with_capacity(ids.len() + 1), Vec::new());
        offs.push(0u32);
        let mut k = 0usize;
        for &id in ids {
            let mut g: Vec<u32> = Vec::new();
            if id < self.base_n { g.extend_from_slice(&mpar[moffs[k] as usize..moffs[k + 1] as usize]); k += 1; }
            if let Some(ps) = self.extra_parents.get(&id) { g.extend_from_slice(ps); }
            g.sort_unstable();
            g.dedup();
            out.extend_from_slice(&g);
            offs.push(out.len() as u32);
        }
        (offs, out)
    }
    pub fn lookup(&mut self, keys: &[[u8; 16]]) -> Vec<Option<u32>> {
        let mut out = Vec::new();
        self.store.lookup_keys(keys, &mut out);
        self.c.key_lookups += keys.len() as u64;
        out
    }
    pub fn put(&mut self, id: u32, n: Node) {
        if id >= self.base_n { self.new_nodes[(id - self.base_n) as usize] = n; } else { self.nodes.insert(id, n); }
    }
    pub fn new_node(&mut self, n: Node) -> u32 { self.new_nodes.push(n); self.base_n + self.new_nodes.len() as u32 - 1 }
    pub fn next_id(&self) -> u32 { self.base_n + self.new_nodes.len() as u32 }
    pub fn set_children(&mut self, parent: u32, es: Vec<Edge>) {
        for (i, e) in es.iter().enumerate() { if !es[..i].iter().any(|x| x.child == e.child) { self.extra_parents.entry(e.child).or_default().push(parent); } }
        self.children.insert(parent, es);
    }
    pub fn finish(self, seq: u64) -> (Batch, Counters) {
        let mut puts: Vec<(u32, Node)> = self.nodes.into_iter().collect();
        puts.sort_unstable_by_key(|p| p.0);
        let mut children: Vec<(u32, Vec<Edge>)> = self.children.into_iter().collect();
        children.sort_unstable_by_key(|p| p.0);
        let b = Batch { seq, first_new: self.base_n, new_nodes: self.new_nodes, puts, children };
        let mut c = self.c;
        c.new_nodes = b.new_nodes.len() as u64;
        c.new_edges = b.edge_count() as u64;
        c.puts = b.puts.len() as u64;
        c.logical_bytes = b.logical_bytes();
        (b, c)
    }
}

fn clamp_cp(v: f64) -> i16 { v.clamp(-9000.0, 9000.0) as i16 }

/// Stage (a) of one landing: expansion, the search result, the seeded replies, the mate line.
fn land(ctx: &mut Ctx, x: u32, rng: &mut Rng, seeds: &mut Vec<u32>) {
    let mut n = ctx.node1(x);
    if !n.is_open() { return; }
    ctx.c.landings += 1;
    let sign = n.sign();
    let edges: Vec<Edge>;
    if !n.expanded() {
        ctx.c.expansions += 1;
        // Transposition candidates: the children of an expanded sibling.
        let mut cousin: Vec<u32> = Vec::new();
        let (_, ps) = ctx.parents(&[x]);
        if let Some(&p0) = ps.first() {
            let sib = ctx.children1(p0);
            if sib.len() > 1 {
                let picks: Vec<u32> = (0..3).map(|_| sib[rng.below(sib.len() as u64) as usize].child).filter(|&c| c != x).collect();
                let sn = ctx.nodes(&picks);
                if let Some(i) = sn.iter().position(|s| s.expanded()) { cousin = ctx.children1(picks[i]).iter().map(|e| e.child).collect(); }
            }
        }
        // Moves the position already had (a materialised line, a click) stay; the expansion adds the rest.
        let had = ctx.children1(x);
        let k = sample_out_degree(rng).max(had.len() + 1);
        let mut plan: Vec<(u16, Option<u32>)> = had.iter().map(|e| (e.mv, Some(e.child))).collect();
        let mut next = ctx.next_id();
        let mut keys: Vec<[u8; 16]> = had.iter().map(|e| key_of(e.child)).collect();
        for i in had.len()..k {
            let mut mv = (i as u16) * 64 + rng.below(64) as u16;
            while plan.iter().any(|p| p.0 == mv) { mv = mv.wrapping_add(1); }
            let mut existing = None;
            if rng.chance(P_TRANSPOSE) {
                let c = if !cousin.is_empty() { cousin[rng.below(cousin.len() as u64) as usize] }
                        else { (x as u64 + 1 + rng.below(TRANSPOSE_WINDOW.min((ctx.base_n - x) as u64))) as u32 };
                if c > x && c < ctx.base_n && !plan.iter().any(|p| p.1 == Some(c)) { existing = Some(c); }
            }
            keys.push(key_of(existing.unwrap_or(next)));
            if existing.is_none() { next += 1; }
            plan.push((mv, existing));
        }
        // The dedupe every expansion pays: one key lookup per legal move.
        let found = ctx.lookup(&keys);
        let mut es = Vec::with_capacity(k);
        for (i, (mv, existing)) in plan.iter().enumerate() {
            match existing {
                Some(c) => { debug_assert!(*c >= ctx.base_n || found[i] == Some(*c)); es.push(Edge { child: *c, mv: *mv }); }
                None => {
                    debug_assert!(found[i].is_none());
                    let mut ch = Node::default();
                    ch.flags = F_REACHABLE | if sign > 0 { F_BLACK_TO_MOVE } else { 0 };
                    es.push(Edge { child: ctx.new_node(ch), mv: *mv });
                }
            }
        }
        ctx.set_children(x, es.clone());
        n.flags |= F_EXPANDED;
        edges = es;
    } else {
        edges = ctx.children1(x);
    }
    if edges.is_empty() { return; }
    // The search result.
    let base = if n.backed != NO_EVAL { n.backed } else if n.eval != NO_EVAL { n.eval } else { 0 };
    n.eval = clamp_cp(base as f64 + rng.normal(0.0, EVAL_NOISE_CP));
    n.nodes += sample_budget(rng);
    n.visits = n.visits.saturating_add(1);
    n.best_move = edges[0].mv;
    // Seed the heads of the MultiPV lines (a claim never overwrites own data).
    let heads: Vec<u32> = edges.iter().take(SEEDED_LINES).map(|e| e.child).collect();
    let hn = ctx.nodes(&heads);
    for (i, (&id, c)) in heads.iter().zip(hn.iter()).enumerate() {
        let mut c = *c;
        if c.is_open() && (c.eval == NO_EVAL || (c.nodes == 0 && c.backed_nodes == 0)) {
            c.eval = clamp_cp(n.eval as f64 - (sign * (i as i32 * 12 + rng.below(20) as i32)) as f64);
            ctx.put(id, c);
        }
    }
    // A proven mate: won for the mover under the first move, or one reply proven lost.
    let r = rng.unit();
    let mate = if r < P_WIN { Some((0usize, win_status_for(sign))) }
               else if r < P_WIN + P_LOSS_CHILD && edges.len() > 1 { Some((1 + rng.below(edges.len() as u64 - 1) as usize, win_status_for(-sign))) }
               else { None };
    if let Some((ci, winner)) = mate {
        let head = edges[ci].child;
        let mut h = ctx.node1(head);
        if h.is_open() && !h.expanded() && ctx.children1(head).is_empty() {
            let len = sample_chain(rng);
            h.set_status(winner, CL_MATE_PV);
            h.mate_in = len as u16;
            h.slack = 100;
            let first = ctx.next_id();
            let keys: Vec<[u8; 16]> = (0..len as u32 - 1).map(|j| key_of(first + j)).collect();
            if !keys.is_empty() { ctx.lookup(&keys); }
            let (mut prev, mut pn) = (head, h);
            for j in 1..len {
                let mut cn = Node::default();
                cn.flags = F_REACHABLE | if pn.black_to_move() { 0 } else { F_BLACK_TO_MOVE };
                cn.set_status(winner, if j + 1 == len { CL_TERMINAL } else { CL_MATE_PV });
                cn.mate_in = (len - j) as u16;
                cn.slack = 100;
                let id = ctx.new_node(cn);
                let mv = rng.below(4096) as u16;
                ctx.set_children(prev, vec![Edge { child: id, mv }]);
                pn.best_move = mv;
                ctx.put(prev, pn);
                prev = id;
                pn = cn;
            }
            ctx.put(prev, pn);
            seeds.push(head);
        }
    }
    ctx.put(x, n);
    seeds.push(x);
}

fn sorted(mut v: Vec<u32>) -> Vec<u32> { v.sort_unstable(); v.dedup(); v }

/// Status cascade: a fixed point upwards from the seeds.  Returns what it closed.
fn cascade(ctx: &mut Ctx, seeds: &[u32]) -> Vec<u32> {
    let (_, ps) = ctx.parents(seeds);
    let mut frontier = sorted(seeds.iter().copied().chain(ps).collect());
    let mut closed = Vec::new();
    let mut visited = 0usize;
    while !frontier.is_empty() && visited < CASCADE_GUARD {
        visited += frontier.len();
        let ns = ctx.nodes(&frontier);
        let (cand, cn): (Vec<u32>, Vec<Node>) = frontier.iter().zip(ns.iter()).filter(|(_, n)| n.is_open()).map(|(i, n)| (*i, *n)).unzip();
        if cand.is_empty() { break; }
        let (offs, edges) = ctx.children(&cand);
        let kid_ids: Vec<u32> = edges.iter().map(|e| e.child).collect();
        let kids = ctx.nodes(&kid_ids);
        let mut newly = Vec::new();
        for (i, &id) in cand.iter().enumerate() {
            let (s, e) = (offs[i] as usize, offs[i + 1] as usize);
            if let Some((st, m, mv)) = rules::close_rule(&cn[i], &edges[s..e], &kids[s..e]) {
                let mut n = cn[i];
                n.set_status(st, CL_MINIMAX);
                n.mate_in = m;
                n.best_move = mv;
                ctx.put(id, n);
                newly.push(id);
            }
        }
        if newly.is_empty() { break; }
        ctx.c.closures += newly.len() as u64;
        let (_, ps) = ctx.parents(&newly);
        closed.extend_from_slice(&newly);
        frontier = sorted(ps);
    }
    closed
}

/// Backed-value ascent, level by level.  `forced` are positions whose own
/// record changed (a new search, a closure): their parents are always woken.
fn ascend(ctx: &mut Ctx, forced: &[u32]) {
    let mut level = sorted(forced.to_vec());
    let mut first = true;
    for _ in 0..MAX_LEVELS {
        if level.is_empty() { break; }
        ctx.c.levels += 1;
        let ns = ctx.nodes(&level);
        let (offs, edges) = ctx.children(&level);
        let kid_ids: Vec<u32> = edges.iter().map(|e| e.child).collect();
        let kids = ctx.nodes(&kid_ids);
        let (mut wake_all, mut wake_standing) = (Vec::new(), Vec::new());
        for (i, &id) in level.iter().enumerate() {
            let mut n = ns[i];
            if !n.is_open() { continue; }
            let (s, e) = (offs[i] as usize, offs[i + 1] as usize);
            let old = n.backed_tuple();
            let new = rules::decide(&n, &edges[s..e], &kids[s..e]);
            if new != old {
                n.backed = new.0; n.backed_move = new.1; n.backed_plies = new.2; n.backed_nodes = new.3;
                ctx.put(id, n);
                ctx.c.backed_writes += 1;
                if rules::is_small_drift(old, new) { wake_standing.push(id); } else { wake_all.push(id); }
            }
        }
        if first { wake_all.extend_from_slice(forced); wake_all = sorted(wake_all); wake_standing.retain(|id| wake_all.binary_search(id).is_err()); first = false; }
        let mut next = Vec::new();
        if !wake_all.is_empty() { next.extend(ctx.parents(&wake_all).1); }
        if !wake_standing.is_empty() {
            // The noise cut: only parents that stand on the edge that drifted.
            let (po, ps) = ctx.parents(&wake_standing);
            let uniq = sorted(ps.clone());
            let pn = ctx.nodes(&uniq);
            let (co, ce) = ctx.children(&uniq);
            for (i, &c) in wake_standing.iter().enumerate() {
                for &p in &ps[po[i] as usize..po[i + 1] as usize] {
                    let k = uniq.binary_search(&p).unwrap();
                    if pn[k].backed == NO_EVAL { continue; }
                    if ce[co[k] as usize..co[k + 1] as usize].iter().any(|e| e.child == c && e.mv == pn[k].backed_move) { next.push(p); }
                }
            }
        }
        level = sorted(next);
    }
}

#[derive(Default, Clone, Copy)]
pub struct BatchTiming { pub compute_us: u64, pub commit_us: u64 }

/// One writer batch: land the targets, climb once, commit durably.
pub fn run_batch(store: &mut dyn Store, targets: &[u32], seq: u64, seed: u64) -> (Counters, BatchTiming) {
    let t0 = Instant::now();
    store.begin();
    let mut ctx = Ctx::new(store);
    let mut rng = Rng::new(seed ^ seq.wrapping_mul(0x9E37_79B9_7F4A_7C15));
    let mut seeds = Vec::new();
    for &x in targets { land(&mut ctx, x, &mut rng, &mut seeds); }
    let seeds = sorted(seeds);
    let closed = cascade(&mut ctx, &seeds);
    let forced = sorted(seeds.iter().chain(closed.iter()).copied().collect());
    ascend(&mut ctx, &forced);
    let (batch, counters) = ctx.finish(seq);
    store.end();
    let t1 = Instant::now();
    store.apply_batch(&batch);
    let t2 = Instant::now();
    (counters, BatchTiming { compute_us: (t1 - t0).as_micros() as u64, commit_us: (t2 - t1).as_micros() as u64 })
}

/// The selector's descent: from the root, mostly along the line the position
/// stands on, to a leaf worth searching (or, for re-searches, an interior node).
pub fn descend(r: &mut dyn Reader, rng: &mut Rng, q_pv: f64, want_expanded: bool) -> Option<u32> {
    let mut x = 0u32;
    let (mut nodes, mut offs, mut edges, mut kids) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    r.get_nodes(&[x], &mut nodes);
    let mut n = nodes[0];
    for step in 0..200 {
        if !n.is_open() { return None; }
        if !n.expanded() { return if want_expanded { None } else { Some(x) }; }
        if want_expanded && step >= 2 && rng.chance(0.08) { return Some(x); }
        r.get_children(&[x], &mut offs, &mut edges);
        if edges.is_empty() { return None; }
        let ids: Vec<u32> = edges.iter().map(|e| e.child).collect();
        r.get_nodes(&ids, &mut kids);
        // Mostly the line the position stands on (else the first open reply), sometimes a side line.
        let mut pick = None;
        if rng.chance(q_pv) {
            if n.backed != NO_EVAL { pick = edges.iter().position(|e| e.mv == n.backed_move).filter(|&i| kids[i].is_open()); }
            if pick.is_none() { pick = kids.iter().position(|k| k.is_open()); }
        } else {
            for _ in 0..8 {
                let i = rng.below(edges.len() as u64) as usize;
                if kids[i].is_open() { pick = Some(i); break; }
            }
        }
        let i = pick?;
        x = ids[i];
        n = kids[i];
    }
    None
}

/// Pick the targets of one batch (distinct), the way the selector feeds the workers.
pub fn pick_targets(r: &mut dyn Reader, rng: &mut Rng, count: usize, q_pv: f64, want_expanded: bool) -> Vec<u32> {
    let mut out: Vec<u32> = Vec::with_capacity(count);
    let mut tries = 0;
    r.begin();
    while out.len() < count && tries < count * 20 + 50 {
        tries += 1;
        if let Some(x) = descend(r, rng, q_pv, want_expanded) { if !out.contains(&x) { out.push(x); } }
    }
    r.end();
    out
}

/// What one explorer page or one `/api/query` reads: the position by key, its
/// record, its moves with their records, and its parents.
pub fn explore_op(r: &mut dyn Reader, rng: &mut Rng) -> usize {
    let (mut nodes, mut offs, mut edges, mut kids, mut poffs, mut pars, mut found) = (Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new());
    loop {
        r.begin();
        let n = r.node_count();
        let mut id = rng.below(n as u64) as u32;
        // Bias towards interior positions, which is what people open.
        if rng.chance(0.7) {
            r.get_parents(&[id], &mut poffs, &mut pars);
            if let Some(&p) = pars.iter().min() { id = p; }
        }
        r.lookup_keys(&[key_of(id)], &mut found);
        r.get_nodes(&[id], &mut nodes);
        r.get_children(&[id], &mut offs, &mut edges);
        let ids: Vec<u32> = edges.iter().map(|e| e.child).collect();
        r.get_nodes(&ids, &mut kids);
        r.get_parents(&[id], &mut poffs, &mut pars);
        let ok = r.validate();
        r.end();
        if ok { assert!(found[0] == Some(id) || found[0].is_none()); return 2 + ids.len(); }
    }
}

/// Hash of the whole logical state, identical for every store that applied the same batches.
pub fn digest(r: &mut dyn Reader) -> u128 {
    let mut h = xxhash_rust::xxh3::Xxh3::new();
    r.begin();
    let n = r.node_count();
    h.update(&n.to_le_bytes());
    let (mut nodes, mut offs, mut edges, mut poffs, mut pars) = (Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new());
    let mut id = 0u32;
    while id < n {
        let end = (id + 2048).min(n);
        let ids: Vec<u32> = (id..end).collect();
        r.get_nodes(&ids, &mut nodes);
        r.get_children(&ids, &mut offs, &mut edges);
        r.get_parents(&ids, &mut poffs, &mut pars);
        for (i, nd) in nodes.iter().enumerate() {
            h.update(&nd.to_bytes());
            let es = &edges[offs[i] as usize..offs[i + 1] as usize];
            h.update(&(es.len() as u32).to_le_bytes());
            for e in es { h.update(&e.child.to_le_bytes()); h.update(&e.mv.to_le_bytes()); }
            let mut ps: Vec<u32> = pars[poffs[i] as usize..poffs[i + 1] as usize].to_vec();
            ps.sort_unstable();
            h.update(&(ps.len() as u32).to_le_bytes());
            for p in ps { h.update(&p.to_le_bytes()); }
        }
        id = end;
    }
    // A sample of key lookups: the map must agree with the ids.
    let keys: Vec<[u8; 16]> = (0..n.min(4096)).map(|i| key_of((i as u64 * (n as u64 / n.min(4096) as u64).max(1)) as u32 % n)).collect();
    let mut found = Vec::new();
    r.lookup_keys(&keys, &mut found);
    for f in found { h.update(&f.map_or(u32::MAX, |v| v).to_le_bytes()); }
    r.end();
    h.digest128()
}
