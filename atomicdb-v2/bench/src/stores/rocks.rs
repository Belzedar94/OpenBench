//! RocksDB: one column family per table, bloom filters and an LRU block cache
//! for point lookups, no compression, a synced WAL write per batch.

use crate::dataset::Dataset;
use crate::model::*;
use crate::store::{dir_bytes, OpenOpts, Reader, Store};
use rocksdb::{BlockBasedOptions, Cache, ColumnFamilyDescriptor, DBCompressionType, Direction, IteratorMode, Options, ReadOptions, WriteBatch, WriteOptions, DB};
use std::sync::Arc;

const CFS: [&str; 5] = ["node", "out", "inn", "keymap", "meta"];

fn open_db(o: &OpenOpts, bulk: bool) -> DB {
    std::fs::create_dir_all(&o.path).unwrap();
    let cache = Cache::new_lru_cache(o.cache_bytes as usize);
    let mut bb = BlockBasedOptions::default();
    bb.set_block_cache(&cache);
    bb.set_bloom_filter(10.0, false);
    bb.set_cache_index_and_filter_blocks(true);
    bb.set_block_size(4096);
    let mut cfo = Options::default();
    cfo.set_block_based_table_factory(&bb);
    cfo.set_compression_type(DBCompressionType::None);
    cfo.set_write_buffer_size(64 << 20);
    cfo.set_level_compaction_dynamic_level_bytes(true);
    if bulk { cfo.prepare_for_bulk_load(); }
    let mut db = Options::default();
    db.create_if_missing(true);
    db.create_missing_column_families(true);
    db.increase_parallelism(8);
    db.set_max_background_jobs(6);
    db.set_bytes_per_sync(1 << 20);
    let cfs: Vec<ColumnFamilyDescriptor> = CFS.iter().map(|n| ColumnFamilyDescriptor::new(*n, cfo.clone())).collect();
    DB::open_cf_descriptors(&db, &o.path, cfs).unwrap()
}

#[inline] fn inn_key(child: u32, parent: u32) -> [u8; 8] { let mut k = [0u8; 8]; k[..4].copy_from_slice(&child.to_be_bytes()); k[4..].copy_from_slice(&parent.to_be_bytes()); k }

pub fn bulk_load(o: &OpenOpts, ds: &Dataset) {
    let _ = std::fs::remove_dir_all(&o.path);
    let db = open_db(o, true);
    let (node, out, inn, keymap, meta) = (db.cf_handle("node").unwrap(), db.cf_handle("out").unwrap(), db.cf_handle("inn").unwrap(), db.cf_handle("keymap").unwrap(), db.cf_handle("meta").unwrap());
    let mut wo = WriteOptions::default();
    wo.disable_wal(true);
    let mut wb = WriteBatch::default();
    let (mut es, mut buf) = (Vec::new(), Vec::new());
    for id in 0..ds.n as u32 {
        wb.put_cf(node, id.to_be_bytes(), ds.node_bytes(id));
        wb.put_cf(keymap, key_of(id), id.to_le_bytes());
        es.clear();
        ds.children(id, &mut es);
        if !es.is_empty() { edges_to_bytes(&es, &mut buf); wb.put_cf(out, id.to_be_bytes(), &buf); }
        for &p in ds.parents(id) { wb.put_cf(inn, inn_key(id, p), b""); }
        if wb.len() >= 100_000 { db.write_opt(std::mem::take(&mut wb), &wo).unwrap(); }
    }
    wb.put_cf(meta, b"seq", 0u64.to_le_bytes());
    db.write_opt(wb, &wo).unwrap();
    for cf in CFS { let h = db.cf_handle(cf).unwrap(); db.flush_cf(h).unwrap(); db.compact_range_cf(h, None::<&[u8]>, None::<&[u8]>); }
    db.flush_wal(true).unwrap();
}

fn r_nodes(db: &DB, ids: &[u32], out: &mut Vec<Node>) {
    out.clear();
    let cf = db.cf_handle("node").unwrap();
    for &id in ids { out.push(Node::from_bytes(&db.get_pinned_cf(cf, id.to_be_bytes()).unwrap().expect("node"))); }
}
fn r_children(db: &DB, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) {
    offs.clear(); out.clear(); offs.push(0);
    let cf = db.cf_handle("out").unwrap();
    for &id in ids {
        if let Some(b) = db.get_pinned_cf(cf, id.to_be_bytes()).unwrap() { edges_from_bytes(&b, out); }
        offs.push(out.len() as u32);
    }
}
fn r_parents(db: &DB, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) {
    offs.clear(); out.clear(); offs.push(0);
    let cf = db.cf_handle("inn").unwrap();
    for &id in ids {
        let lo = inn_key(id, 0);
        let mut ro = ReadOptions::default();
        if id < u32::MAX { ro.set_iterate_upper_bound(inn_key(id + 1, 0).to_vec()); }
        for kv in db.iterator_cf_opt(cf, ro, IteratorMode::From(&lo, Direction::Forward)) {
            let (k, _) = kv.unwrap();
            if k[..4] != lo[..4] { break; }
            out.push(u32::from_be_bytes(k[4..8].try_into().unwrap()));
        }
        offs.push(out.len() as u32);
    }
}
fn r_keys(db: &DB, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) {
    out.clear();
    let cf = db.cf_handle("keymap").unwrap();
    for k in keys { out.push(db.get_pinned_cf(cf, k).unwrap().map(|b| u32::from_le_bytes(b[..4].try_into().unwrap()))); }
}
fn r_count(db: &DB) -> u32 {
    let cf = db.cf_handle("node").unwrap();
    match db.iterator_cf(cf, IteratorMode::End).next() { Some(kv) => u32::from_be_bytes(kv.unwrap().0[..4].try_into().unwrap()) + 1, None => 0 }
}

pub struct RocksStore { db: Arc<DB>, path: std::path::PathBuf, seq: u64, durable: bool }
pub struct RocksReader { db: Arc<DB> }

impl RocksStore {
    pub fn open(o: &OpenOpts) -> RocksStore {
        let db = open_db(o, false);
        let seq = db.get_cf(db.cf_handle("meta").unwrap(), b"seq").unwrap().map(|b| u64::from_le_bytes(b[..8].try_into().unwrap())).unwrap_or(0);
        RocksStore { db: Arc::new(db), path: o.path.clone(), seq, durable: o.durable }
    }
}

macro_rules! impl_reader {
    ($t:ty) => {
        impl Reader for $t {
            fn node_count(&mut self) -> u32 { r_count(&self.db) }
            fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { r_nodes(&self.db, ids, out) }
            fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { r_children(&self.db, ids, offs, out) }
            fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { r_parents(&self.db, ids, offs, out) }
            fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { r_keys(&self.db, keys, out) }
        }
    };
}
impl_reader!(RocksStore);
impl_reader!(RocksReader);

impl Store for RocksStore {
    fn name(&self) -> String { "RocksDB".into() }
    fn apply_batch(&mut self, b: &Batch) {
        let db = &self.db;
        let (node, out, inn, keymap, meta) = (db.cf_handle("node").unwrap(), db.cf_handle("out").unwrap(), db.cf_handle("inn").unwrap(), db.cf_handle("keymap").unwrap(), db.cf_handle("meta").unwrap());
        let mut wb = WriteBatch::default();
        let mut buf = Vec::new();
        for (k, n) in b.new_nodes.iter().enumerate() {
            let id = b.first_new + k as u32;
            wb.put_cf(node, id.to_be_bytes(), n.to_bytes());
            wb.put_cf(keymap, key_of(id), id.to_le_bytes());
        }
        for (id, n) in &b.puts { wb.put_cf(node, id.to_be_bytes(), n.to_bytes()); }
        for (p, es) in &b.children {
            edges_to_bytes(es, &mut buf);
            wb.put_cf(out, p.to_be_bytes(), &buf);
            for e in es { wb.put_cf(inn, inn_key(e.child, *p), b""); }
        }
        wb.put_cf(meta, b"seq", b.seq.to_le_bytes());
        let mut wo = WriteOptions::default();
        wo.set_sync(self.durable);
        db.write_opt(wb, &wo).unwrap();
        self.seq = b.seq;
    }
    fn last_seq(&mut self) -> u64 { self.seq }
    fn new_reader(&self) -> Box<dyn Reader + Send> { Box::new(RocksReader { db: self.db.clone() }) }
    fn disk_bytes(&mut self) -> u64 { dir_bytes(&self.path) }
    fn settle(&mut self) {
        for cf in CFS { let h = self.db.cf_handle(cf).unwrap(); self.db.flush_cf(h).unwrap(); }
        self.db.flush_wal(true).unwrap();
    }
}
