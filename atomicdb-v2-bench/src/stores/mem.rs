//! In-memory reference: no durability at all.  It is the upper bound ("what
//! the climb costs when storage is free") and the oracle of the crash tests.

use crate::dataset::Dataset;
use crate::model::*;
use crate::store::{Reader, Store};
use std::collections::HashMap;
use std::sync::{Arc, RwLock};

pub struct Inner {
    nodes: Vec<Node>,
    out_start: Vec<u32>,
    out_cnt: Vec<u16>,
    edges: Vec<Edge>,
    par0: Vec<u32>,                    // parent + 1, 0 = none
    par_more: HashMap<u32, Vec<u32>>,  // further parents
    keys: HashMap<u128, u32>,
    seq: u64,
}

pub struct MemStore { inner: Arc<RwLock<Inner>> }
pub struct MemReader { inner: Arc<RwLock<Inner>> }

impl MemStore {
    pub fn from_dataset(ds: &Dataset) -> MemStore {
        let n = ds.n;
        let mut inner = Inner { nodes: Vec::with_capacity(n), out_start: vec![0; n], out_cnt: vec![0; n], edges: Vec::with_capacity(ds.e),
                                par0: vec![0; n], par_more: HashMap::new(), keys: HashMap::with_capacity(n), seq: 0 };
        let mut es = Vec::new();
        for id in 0..n as u32 {
            inner.nodes.push(ds.node(id));
            inner.keys.insert(u128::from_le_bytes(key_of(id)), id);
            es.clear();
            ds.children(id, &mut es);
            if !es.is_empty() {
                inner.out_start[id as usize] = inner.edges.len() as u32;
                inner.out_cnt[id as usize] = es.len() as u16;
                inner.edges.extend_from_slice(&es);
            }
            let ps = ds.parents(id);
            if let Some(&p) = ps.first() { inner.par0[id as usize] = p + 1; }
            if ps.len() > 1 { inner.par_more.insert(id, ps[1..].to_vec()); }
        }
        MemStore { inner: Arc::new(RwLock::new(inner)) }
    }
}

fn r_nodes(i: &Inner, ids: &[u32], out: &mut Vec<Node>) { out.clear(); out.extend(ids.iter().map(|&id| i.nodes[id as usize])); }
fn r_children(i: &Inner, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) {
    offs.clear(); out.clear(); offs.push(0);
    for &id in ids {
        let (s, c) = (i.out_start[id as usize] as usize, i.out_cnt[id as usize] as usize);
        out.extend_from_slice(&i.edges[s..s + c]);
        offs.push(out.len() as u32);
    }
}
fn r_parents(i: &Inner, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) {
    offs.clear(); out.clear(); offs.push(0);
    for &id in ids {
        let p = i.par0[id as usize];
        if p != 0 { out.push(p - 1); if let Some(m) = i.par_more.get(&id) { out.extend_from_slice(m); } }
        offs.push(out.len() as u32);
    }
}
fn r_keys(i: &Inner, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { out.clear(); out.extend(keys.iter().map(|k| i.keys.get(&u128::from_le_bytes(*k)).copied())); }

macro_rules! impl_reader {
    ($t:ty) => {
        impl Reader for $t {
            fn node_count(&mut self) -> u32 { self.inner.read().unwrap().nodes.len() as u32 }
            fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { r_nodes(&self.inner.read().unwrap(), ids, out) }
            fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { r_children(&self.inner.read().unwrap(), ids, offs, out) }
            fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { r_parents(&self.inner.read().unwrap(), ids, offs, out) }
            fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { r_keys(&self.inner.read().unwrap(), keys, out) }
        }
    };
}
impl_reader!(MemStore);
impl_reader!(MemReader);

impl Store for MemStore {
    fn name(&self) -> String { "mem (no durability)".into() }
    fn apply_batch(&mut self, b: &Batch) {
        let mut i = self.inner.write().unwrap();
        assert_eq!(b.first_new as usize, i.nodes.len());
        for (k, n) in b.new_nodes.iter().enumerate() {
            let id = b.first_new + k as u32;
            i.nodes.push(*n);
            i.out_start.push(0); i.out_cnt.push(0); i.par0.push(0);
            i.keys.insert(u128::from_le_bytes(key_of(id)), id);
        }
        for (id, n) in &b.puts { i.nodes[*id as usize] = *n; }
        for (p, es) in &b.children {
            // A block that replaces an older one keeps its children: only the new ones gain a parent.
            let (os, oc) = (i.out_start[*p as usize] as usize, i.out_cnt[*p as usize] as usize);
            let old: Vec<u32> = i.edges[os..os + oc].iter().map(|e| e.child).collect();
            i.out_start[*p as usize] = i.edges.len() as u32;
            i.out_cnt[*p as usize] = es.len() as u16;
            i.edges.extend_from_slice(es);
            for (k, e) in es.iter().enumerate() {
                if es[..k].iter().any(|x| x.child == e.child) || old.contains(&e.child) { continue; }
                let c = e.child as usize;
                if i.par0[c] == 0 { i.par0[c] = p + 1; } else { i.par_more.entry(e.child).or_default().push(*p); }
            }
        }
        i.seq = b.seq;
    }
    fn last_seq(&mut self) -> u64 { self.inner.read().unwrap().seq }
    fn new_reader(&self) -> Box<dyn Reader + Send> { Box::new(MemReader { inner: self.inner.clone() }) }
    fn disk_bytes(&mut self) -> u64 { 0 }
}

// ---------------------------------------------------------------------------
// The same in-memory graph made durable the classic way: a logical write-ahead
// log (one fsynced frame per batch) and a full snapshot now and then.  It is
// the "keep it all in RAM" alternative to mapped columns: same speed while
// running, but a restart reloads everything and a snapshot rewrites everything.

use crate::store::OpenOpts;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};

fn pod_bytes<T: Copy>(v: &[T]) -> &[u8] { unsafe { std::slice::from_raw_parts(v.as_ptr() as *const u8, std::mem::size_of_val(v)) } }
fn read_pod<T: Copy + Default>(f: &mut impl Read, n: usize) -> Vec<T> {
    let mut v = vec![T::default(); n];
    let b = unsafe { std::slice::from_raw_parts_mut(v.as_mut_ptr() as *mut u8, n * std::mem::size_of::<T>()) };
    f.read_exact(b).unwrap();
    v
}

fn write_snapshot(dir: &std::path::Path, i: &Inner) {
    let tmp = dir.join("snapshot.tmp");
    let mut f = std::io::BufWriter::with_capacity(8 << 20, File::create(&tmp).unwrap());
    let more: usize = i.par_more.values().map(|v| v.len()).sum();
    for v in [i.nodes.len() as u64, i.edges.len() as u64, i.seq, i.par_more.len() as u64, more as u64] { f.write_all(&v.to_le_bytes()).unwrap(); }
    let node_bytes = unsafe { std::slice::from_raw_parts(i.nodes.as_ptr() as *const u8, i.nodes.len() * NODE_SIZE) };
    f.write_all(node_bytes).unwrap();
    f.write_all(pod_bytes(&i.out_start)).unwrap();
    f.write_all(pod_bytes(&i.out_cnt)).unwrap();
    f.write_all(pod_bytes(&i.par0)).unwrap();
    let mut buf = Vec::new();
    for chunk in i.edges.chunks(1 << 16) { edges_to_bytes(chunk, &mut buf); f.write_all(&buf).unwrap(); }
    let mut ks: Vec<&u32> = i.par_more.keys().collect();
    ks.sort_unstable();
    for k in ks {
        let v = &i.par_more[k];
        f.write_all(&k.to_le_bytes()).unwrap();
        f.write_all(&(v.len() as u32).to_le_bytes()).unwrap();
        f.write_all(pod_bytes(v)).unwrap();
    }
    let f = f.into_inner().unwrap();
    f.sync_all().unwrap();
    drop(f);
    std::fs::rename(&tmp, dir.join("snapshot.bin")).unwrap();
}

fn read_snapshot(dir: &std::path::Path) -> Inner {
    let mut f = std::io::BufReader::with_capacity(8 << 20, File::open(dir.join("snapshot.bin")).expect("snapshot.bin"));
    let mut h = [0u8; 40];
    f.read_exact(&mut h).unwrap();
    let g = |k: usize| u64::from_le_bytes(h[k * 8..k * 8 + 8].try_into().unwrap()) as usize;
    let (n, e, seq, groups) = (g(0), g(1), g(2) as u64, g(3));
    let mut nodes = vec![Node::default(); n];
    f.read_exact(unsafe { std::slice::from_raw_parts_mut(nodes.as_mut_ptr() as *mut u8, n * NODE_SIZE) }).unwrap();
    let out_start: Vec<u32> = read_pod(&mut f, n);
    let out_cnt: Vec<u16> = read_pod(&mut f, n);
    let par0: Vec<u32> = read_pod(&mut f, n);
    let mut eb = vec![0u8; e * EDGE_SIZE];
    f.read_exact(&mut eb).unwrap();
    let mut edges = Vec::with_capacity(e);
    edges_from_bytes(&eb, &mut edges);
    drop(eb);
    let mut par_more = HashMap::with_capacity(groups);
    for _ in 0..groups {
        let mut kb = [0u8; 8];
        f.read_exact(&mut kb).unwrap();
        let (k, c) = (u32::from_le_bytes(kb[..4].try_into().unwrap()), u32::from_le_bytes(kb[4..].try_into().unwrap()) as usize);
        par_more.insert(k, read_pod::<u32>(&mut f, c));
    }
    // The key map is an index: rebuilt, not stored.
    let mut keys = HashMap::with_capacity(n);
    for id in 0..n as u32 { keys.insert(u128::from_le_bytes(key_of(id)), id); }
    Inner { nodes, out_start, out_cnt, edges, par0, par_more, keys, seq }
}

fn encode_batch(b: &Batch, out: &mut Vec<u8>) {
    out.clear();
    out.extend_from_slice(&[0u8; 8]);
    out.extend_from_slice(&b.seq.to_le_bytes());
    out.extend_from_slice(&b.first_new.to_le_bytes());
    out.extend_from_slice(&(b.new_nodes.len() as u32).to_le_bytes());
    for n in &b.new_nodes { out.extend_from_slice(&n.to_bytes()); }
    out.extend_from_slice(&(b.puts.len() as u32).to_le_bytes());
    for (id, n) in &b.puts { out.extend_from_slice(&id.to_le_bytes()); out.extend_from_slice(&n.to_bytes()); }
    out.extend_from_slice(&(b.children.len() as u32).to_le_bytes());
    for (p, es) in &b.children {
        out.extend_from_slice(&p.to_le_bytes());
        out.extend_from_slice(&(es.len() as u32).to_le_bytes());
        for e in es { out.extend_from_slice(&e.child.to_le_bytes()); out.extend_from_slice(&e.mv.to_le_bytes()); }
    }
    let len = (out.len() - 8) as u32;
    let crc = crc32fast::hash(&out[8..]);
    out[0..4].copy_from_slice(&len.to_le_bytes());
    out[4..8].copy_from_slice(&crc.to_le_bytes());
}

fn decode_batch(b: &[u8]) -> Batch {
    let mut p = 0usize;
    let u32_ = |p: &mut usize| { let v = u32::from_le_bytes(b[*p..*p + 4].try_into().unwrap()); *p += 4; v };
    let seq = u64::from_le_bytes(b[0..8].try_into().unwrap());
    p += 8;
    let first_new = u32_(&mut p);
    let n_new = u32_(&mut p) as usize;
    let mut new_nodes = Vec::with_capacity(n_new);
    for _ in 0..n_new { new_nodes.push(Node::from_bytes(&b[p..])); p += NODE_SIZE; }
    let n_puts = u32_(&mut p) as usize;
    let mut puts = Vec::with_capacity(n_puts);
    for _ in 0..n_puts { let id = u32_(&mut p); puts.push((id, Node::from_bytes(&b[p..]))); p += NODE_SIZE; }
    let n_ch = u32_(&mut p) as usize;
    let mut children = Vec::with_capacity(n_ch);
    for _ in 0..n_ch {
        let parent = u32_(&mut p);
        let c = u32_(&mut p) as usize;
        let mut es = Vec::with_capacity(c);
        edges_from_bytes(&b[p..p + c * EDGE_SIZE], &mut es);
        p += c * EDGE_SIZE;
        children.push((parent, es));
    }
    Batch { seq, first_new, new_nodes, puts, children }
}

pub struct MemWalStore { mem: MemStore, dir: std::path::PathBuf, wal: File, wal_len: u64, durable: bool, frame: Vec<u8>, written: u64 }

pub fn bulk_load_wal(o: &OpenOpts, ds: &Dataset) {
    let _ = std::fs::remove_dir_all(&o.path);
    std::fs::create_dir_all(&o.path).unwrap();
    let m = MemStore::from_dataset(ds);
    write_snapshot(&o.path, &m.inner.read().unwrap());
    File::create(o.path.join("wal.bin")).unwrap().sync_all().unwrap();
}

impl MemWalStore {
    pub fn open(o: &OpenOpts) -> MemWalStore {
        let mem = MemStore { inner: Arc::new(RwLock::new(read_snapshot(&o.path))) };
        let mut wal = OpenOptions::new().read(true).write(true).create(true).truncate(false).open(o.path.join("wal.bin")).unwrap();
        let mut log = Vec::new();
        wal.read_to_end(&mut log).unwrap();
        let mut pos = 0usize;
        let mut s = MemWalStore { mem, dir: o.path.clone(), wal, wal_len: 0, durable: o.durable, frame: Vec::new(), written: 0 };
        while pos + 8 <= log.len() {
            let len = u32::from_le_bytes(log[pos..pos + 4].try_into().unwrap()) as usize;
            let crc = u32::from_le_bytes(log[pos + 4..pos + 8].try_into().unwrap());
            if len < 8 || pos + 8 + len > log.len() || crc32fast::hash(&log[pos + 8..pos + 8 + len]) != crc { break; }
            let b = decode_batch(&log[pos + 8..pos + 8 + len]);
            if b.seq > s.mem.last_seq() { s.mem.apply_batch(&b); }
            pos += 8 + len;
        }
        if pos < log.len() { s.wal.set_len(pos as u64).unwrap(); }
        s.wal.seek(SeekFrom::Start(pos as u64)).unwrap();
        s.wal_len = pos as u64;
        s
    }
}
impl Reader for MemWalStore {
    fn node_count(&mut self) -> u32 { self.mem.node_count() }
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { self.mem.get_nodes(ids, out) }
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { self.mem.get_children(ids, offs, out) }
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { self.mem.get_parents(ids, offs, out) }
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { self.mem.lookup_keys(keys, out) }
}
impl Store for MemWalStore {
    fn name(&self) -> String { "in memory + WAL + snapshot".into() }
    fn apply_batch(&mut self, b: &Batch) {
        let mut frame = std::mem::take(&mut self.frame);
        encode_batch(b, &mut frame);
        self.wal.write_all(&frame).unwrap();
        if self.durable { self.wal.sync_data().unwrap(); }
        self.wal_len += frame.len() as u64;
        self.written += frame.len() as u64;
        self.frame = frame;
        self.mem.apply_batch(b);
    }
    fn last_seq(&mut self) -> u64 { self.mem.last_seq() }
    fn new_reader(&self) -> Box<dyn Reader + Send> { self.mem.new_reader() }
    fn disk_bytes(&mut self) -> u64 { crate::store::dir_bytes(&self.dir) }
    fn physical_bytes_written(&mut self) -> Option<u64> { Some(self.written) }
    /// A snapshot: the whole graph is written again.
    fn settle(&mut self) {
        let t0 = self.written;
        write_snapshot(&self.dir, &self.mem.inner.read().unwrap());
        self.written = t0 + std::fs::metadata(self.dir.join("snapshot.bin")).map(|m| m.len()).unwrap_or(0);
        self.wal.set_len(0).unwrap();
        self.wal.seek(SeekFrom::Start(0)).unwrap();
        self.wal.sync_data().unwrap();
        self.wal_len = 0;
    }
}