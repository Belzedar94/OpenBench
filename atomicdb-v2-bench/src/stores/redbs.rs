//! redb: a pure-Rust copy-on-write B-tree (the same family as LMDB).

use crate::dataset::Dataset;
use crate::model::*;
use crate::store::{OpenOpts, Reader, Store};
use redb::{Database, Durability, ReadOnlyTable, ReadableDatabase, ReadableTable, TableDefinition};
use std::sync::Arc;

const NODE: TableDefinition<u32, &[u8]> = TableDefinition::new("node");
const OUT: TableDefinition<u32, &[u8]> = TableDefinition::new("out");
const INN: TableDefinition<u64, ()> = TableDefinition::new("inn"); // (child << 32) | parent
const KEYMAP: TableDefinition<&[u8; 16], u32> = TableDefinition::new("keymap");
const META: TableDefinition<u8, u64> = TableDefinition::new("meta");

fn file(o: &OpenOpts) -> std::path::PathBuf { o.path.join("graph.redb") }
#[inline] fn inn_key(child: u32, parent: u32) -> u64 { ((child as u64) << 32) | parent as u64 }

pub fn bulk_load(o: &OpenOpts, ds: &Dataset) {
    let _ = std::fs::remove_dir_all(&o.path);
    std::fs::create_dir_all(&o.path).unwrap();
    let db = Database::builder().set_cache_size(o.cache_bytes as usize).create(file(o)).unwrap();
    const CHUNK: usize = 1_000_000;
    let mut id = 0usize;
    let (mut es, mut buf) = (Vec::new(), Vec::new());
    while id < ds.n {
        let end = (id + CHUNK).min(ds.n);
        let mut w = db.begin_write().unwrap();
        w.set_durability(Durability::None).unwrap();
        {
            let mut tn = w.open_table(NODE).unwrap();
            let mut to = w.open_table(OUT).unwrap();
            let mut ti = w.open_table(INN).unwrap();
            let mut tk = w.open_table(KEYMAP).unwrap();
            for i in id as u32..end as u32 {
                tn.insert(i, ds.node_bytes(i)).unwrap();
                es.clear();
                ds.children(i, &mut es);
                if !es.is_empty() { edges_to_bytes(&es, &mut buf); to.insert(i, &buf[..]).unwrap(); }
                for &p in ds.parents(i) { ti.insert(inn_key(i, p), ()).unwrap(); }
            }
            let _ = &mut tk;
        }
        w.commit().unwrap();
        id = end;
    }
    {
        let mut w = db.begin_write().unwrap();
        w.set_durability(Durability::None).unwrap();
        {
            let mut tk = w.open_table(KEYMAP).unwrap();
            for_each_key_sorted(ds.n as u32, |k, i| { tk.insert(k, i).unwrap(); });
        }
        w.commit().unwrap();
    }
    let w = db.begin_write().unwrap();
    { let mut tm = w.open_table(META).unwrap(); tm.insert(1u8, 0u64).unwrap(); }
    w.commit().unwrap();
}

struct Snap { node: ReadOnlyTable<u32, &'static [u8]>, out: ReadOnlyTable<u32, &'static [u8]>, inn: ReadOnlyTable<u64, ()>, keymap: ReadOnlyTable<&'static [u8; 16], u32> }
fn snap(db: &Database) -> Snap {
    let r = db.begin_read().unwrap();
    Snap { node: r.open_table(NODE).unwrap(), out: r.open_table(OUT).unwrap(), inn: r.open_table(INN).unwrap(), keymap: r.open_table(KEYMAP).unwrap() }
}

struct View { db: Arc<Database>, s: Option<Snap> }
impl View {
    #[inline] fn s(&mut self) -> &Snap { if self.s.is_none() { self.s = Some(snap(&self.db)); } self.s.as_ref().unwrap() }
    fn node_count(&mut self) -> u32 { self.s().node.last().unwrap().map(|(k, _)| k.value() + 1).unwrap_or(0) }
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) {
        out.clear();
        let s = self.s();
        for &id in ids { out.push(Node::from_bytes(s.node.get(id).unwrap().expect("node").value())); }
    }
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) {
        offs.clear(); out.clear(); offs.push(0);
        let s = self.s();
        for &id in ids {
            if let Some(g) = s.out.get(id).unwrap() { edges_from_bytes(g.value(), out); }
            offs.push(out.len() as u32);
        }
    }
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) {
        offs.clear(); out.clear(); offs.push(0);
        let s = self.s();
        for &id in ids {
            for e in s.inn.range(inn_key(id, 0)..=inn_key(id, u32::MAX)).unwrap() { out.push(e.unwrap().0.value() as u32); }
            offs.push(out.len() as u32);
        }
    }
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) {
        out.clear();
        let s = self.s();
        for k in keys { out.push(s.keymap.get(k).unwrap().map(|g| g.value())); }
    }
}

pub struct RedbStore { v: View, path: std::path::PathBuf, seq: u64, durable: bool }
pub struct RedbReader { v: View }
// The tables of a read transaction are only touched by the thread that owns the reader.
unsafe impl Send for RedbReader {}

impl RedbStore {
    pub fn open(o: &OpenOpts) -> RedbStore {
        let db = Arc::new(Database::builder().set_cache_size(o.cache_bytes as usize).create(file(o)).unwrap());
        let seq = { let r = db.begin_read().unwrap(); let t = r.open_table(META).unwrap(); t.get(1u8).unwrap().map(|g| g.value()).unwrap_or(0) };
        RedbStore { v: View { db, s: None }, path: o.path.clone(), seq, durable: o.durable }
    }
}

macro_rules! impl_reader {
    ($t:ty) => {
        impl Reader for $t {
            fn begin(&mut self) { self.v.s = None; self.v.s(); }
            fn end(&mut self) { self.v.s = None; }
            fn node_count(&mut self) -> u32 { self.v.node_count() }
            fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { self.v.get_nodes(ids, out) }
            fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { self.v.get_children(ids, offs, out) }
            fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { self.v.get_parents(ids, offs, out) }
            fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { self.v.lookup_keys(keys, out) }
        }
    };
}
impl_reader!(RedbStore);
impl_reader!(RedbReader);

impl Store for RedbStore {
    fn name(&self) -> String { "redb".into() }
    fn apply_batch(&mut self, b: &Batch) {
        self.v.s = None;
        let mut buf = Vec::new();
        let mut w = self.v.db.begin_write().unwrap();
        w.set_durability(if self.durable { Durability::Immediate } else { Durability::None }).unwrap();
        {
            let mut tn = w.open_table(NODE).unwrap();
            let mut tk = w.open_table(KEYMAP).unwrap();
            for (k, n) in b.new_nodes.iter().enumerate() {
                let id = b.first_new + k as u32;
                tn.insert(id, &n.to_bytes()[..]).unwrap();
                tk.insert(&key_of(id), id).unwrap();
            }
            for (id, n) in &b.puts { tn.insert(*id, &n.to_bytes()[..]).unwrap(); }
            let mut to = w.open_table(OUT).unwrap();
            let mut ti = w.open_table(INN).unwrap();
            for (p, es) in &b.children {
                edges_to_bytes(es, &mut buf);
                to.insert(*p, &buf[..]).unwrap();
                for e in es { ti.insert(inn_key(e.child, *p), ()).unwrap(); }
            }
            let mut tm = w.open_table(META).unwrap();
            tm.insert(1u8, b.seq).unwrap();
        }
        w.commit().unwrap();
        self.seq = b.seq;
    }
    fn last_seq(&mut self) -> u64 { self.seq }
    fn new_reader(&self) -> Box<dyn Reader + Send> { Box::new(RedbReader { v: View { db: self.v.db.clone(), s: None } }) }
    fn disk_bytes(&mut self) -> u64 { std::fs::metadata(self.path.join("graph.redb")).map(|m| m.len()).unwrap_or(0) }
}
