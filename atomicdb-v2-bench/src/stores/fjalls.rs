//! fjall: a pure-Rust LSM tree (the same family as RocksDB).

use crate::dataset::Dataset;
use crate::model::*;
use crate::store::{dir_bytes, OpenOpts, Reader, Store};
use fjall::{Database, Keyspace, KeyspaceCreateOptions, PersistMode};

#[derive(Clone)]
struct Ks { db: Database, node: Keyspace, out: Keyspace, inn: Keyspace, keymap: Keyspace, meta: Keyspace }

fn open_ks(o: &OpenOpts) -> Ks {
    std::fs::create_dir_all(&o.path).unwrap();
    let db = Database::builder(&o.path).cache_size(o.cache_bytes).open().unwrap();
    let ks = |name: &str| db.keyspace(name, || KeyspaceCreateOptions::default()).unwrap();
    Ks { node: ks("node"), out: ks("out"), inn: ks("inn"), keymap: ks("keymap"), meta: ks("meta"), db }
}
#[inline] fn inn_key(child: u32, parent: u32) -> [u8; 8] { let mut k = [0u8; 8]; k[..4].copy_from_slice(&child.to_be_bytes()); k[4..].copy_from_slice(&parent.to_be_bytes()); k }

pub fn bulk_load(o: &OpenOpts, ds: &Dataset) {
    let _ = std::fs::remove_dir_all(&o.path);
    let k = open_ks(o);
    let (mut es, mut buf) = (Vec::new(), Vec::new());
    let mut b = k.db.batch();
    let mut pending = 0usize;
    for id in 0..ds.n as u32 {
        b.insert(&k.node, id.to_be_bytes(), ds.node_bytes(id));
        b.insert(&k.keymap, key_of(id), id.to_le_bytes());
        es.clear();
        ds.children(id, &mut es);
        if !es.is_empty() { edges_to_bytes(&es, &mut buf); b.insert(&k.out, id.to_be_bytes(), &buf[..]); }
        for &p in ds.parents(id) { b.insert(&k.inn, inn_key(id, p), b""); }
        pending += 3;
        if pending >= 50_000 { b.commit().unwrap(); b = k.db.batch(); pending = 0; }
    }
    b.insert(&k.meta, b"seq", 0u64.to_le_bytes());
    b.commit().unwrap();
    k.db.persist(PersistMode::SyncAll).unwrap();
}

fn r_nodes(k: &Ks, ids: &[u32], out: &mut Vec<Node>) {
    out.clear();
    for &id in ids { out.push(Node::from_bytes(&k.node.get(id.to_be_bytes()).unwrap().expect("node"))); }
}
fn r_children(k: &Ks, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) {
    offs.clear(); out.clear(); offs.push(0);
    for &id in ids {
        if let Some(b) = k.out.get(id.to_be_bytes()).unwrap() { edges_from_bytes(&b, out); }
        offs.push(out.len() as u32);
    }
}
fn r_parents(k: &Ks, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) {
    offs.clear(); out.clear(); offs.push(0);
    for &id in ids {
        for g in k.inn.prefix(id.to_be_bytes()) {
            let key = g.key().unwrap();
            out.push(u32::from_be_bytes(key[4..8].try_into().unwrap()));
        }
        offs.push(out.len() as u32);
    }
}
fn r_keys(k: &Ks, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) {
    out.clear();
    for key in keys { out.push(k.keymap.get(key).unwrap().map(|b| u32::from_le_bytes(b[..4].try_into().unwrap()))); }
}
fn r_count(k: &Ks) -> u32 {
    match k.node.last_key_value() { Some(g) => u32::from_be_bytes(g.key().unwrap()[..4].try_into().unwrap()) + 1, None => 0 }
}

pub struct FjallStore { k: Ks, path: std::path::PathBuf, seq: u64, durable: bool }
pub struct FjallReader { k: Ks }

impl FjallStore {
    pub fn open(o: &OpenOpts) -> FjallStore {
        let k = open_ks(o);
        let seq = k.meta.get(b"seq").unwrap().map(|b| u64::from_le_bytes(b[..8].try_into().unwrap())).unwrap_or(0);
        FjallStore { k, path: o.path.clone(), seq, durable: o.durable }
    }
}

macro_rules! impl_reader {
    ($t:ty) => {
        impl Reader for $t {
            fn node_count(&mut self) -> u32 { r_count(&self.k) }
            fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { r_nodes(&self.k, ids, out) }
            fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { r_children(&self.k, ids, offs, out) }
            fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { r_parents(&self.k, ids, offs, out) }
            fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { r_keys(&self.k, keys, out) }
        }
    };
}
impl_reader!(FjallStore);
impl_reader!(FjallReader);

impl Store for FjallStore {
    fn name(&self) -> String { "fjall".into() }
    fn apply_batch(&mut self, b: &Batch) {
        let k = &self.k;
        let mut wb = k.db.batch();
        let mut buf = Vec::new();
        for (i, n) in b.new_nodes.iter().enumerate() {
            let id = b.first_new + i as u32;
            wb.insert(&k.node, id.to_be_bytes(), n.to_bytes());
            wb.insert(&k.keymap, key_of(id), id.to_le_bytes());
        }
        for (id, n) in &b.puts { wb.insert(&k.node, id.to_be_bytes(), n.to_bytes()); }
        for (p, es) in &b.children {
            edges_to_bytes(es, &mut buf);
            wb.insert(&k.out, p.to_be_bytes(), &buf[..]);
            for e in es { wb.insert(&k.inn, inn_key(e.child, *p), b""); }
        }
        wb.insert(&k.meta, b"seq", b.seq.to_le_bytes());
        wb.commit().unwrap();
        if self.durable { k.db.persist(PersistMode::SyncAll).unwrap(); }
        self.seq = b.seq;
    }
    fn last_seq(&mut self) -> u64 { self.seq }
    fn new_reader(&self) -> Box<dyn Reader + Send> { Box::new(FjallReader { k: self.k.clone() }) }
    fn disk_bytes(&mut self) -> u64 { dir_bytes(&self.path) }
    fn settle(&mut self) { self.k.db.persist(PersistMode::SyncAll).unwrap(); }
}
