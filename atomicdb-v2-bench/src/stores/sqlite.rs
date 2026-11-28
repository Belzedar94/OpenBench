//! SQLite in WAL mode with integer ids: `node(id INTEGER PRIMARY KEY, rec)`,
//! one edge block per parent, in-edges and the key map as WITHOUT ROWID tables.
//! `synchronous=FULL`, so a commit is an fsync of the WAL, like the others.

use crate::dataset::Dataset;
use crate::model::*;
use crate::store::{dir_bytes, OpenOpts, Reader, Store};
use rusqlite::{params, Connection, OpenFlags};

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS node(id INTEGER PRIMARY KEY, rec BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS outedge(parent INTEGER PRIMARY KEY, edges BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS inedge(child INTEGER NOT NULL, parent INTEGER NOT NULL, PRIMARY KEY(child, parent)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS keymap(key BLOB PRIMARY KEY, id INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta(k INTEGER PRIMARY KEY, v INTEGER NOT NULL);
";

fn db_file(o: &OpenOpts) -> std::path::PathBuf { o.path.join("graph.sqlite") }

fn tune(c: &Connection, cache_bytes: u64, durable: bool) {
    c.pragma_update(None, "journal_mode", "WAL").unwrap();
    c.pragma_update(None, "synchronous", if durable { "FULL" } else { "NORMAL" }).unwrap();
    c.pragma_update(None, "cache_size", -((cache_bytes / 1024) as i64)).unwrap();
    // Readers share the OS page cache through the map (needs a build with a raised SQLITE_MAX_MMAP_SIZE to go past 2 GiB).
    c.pragma_update(None, "mmap_size", 64i64 << 30).unwrap();
    c.pragma_update(None, "temp_store", "MEMORY").unwrap();
    c.pragma_update(None, "wal_autocheckpoint", 20_000i64).unwrap();
}

pub fn bulk_load(o: &OpenOpts, ds: &Dataset) {
    let _ = std::fs::remove_dir_all(&o.path);
    std::fs::create_dir_all(&o.path).unwrap();
    let mut c = Connection::open(db_file(o)).unwrap();
    c.pragma_update(None, "page_size", 4096i64).unwrap();
    c.pragma_update(None, "journal_mode", "OFF").unwrap();
    c.pragma_update(None, "synchronous", "OFF").unwrap();
    c.pragma_update(None, "locking_mode", "EXCLUSIVE").unwrap();
    c.pragma_update(None, "cache_size", -((o.cache_bytes / 1024) as i64)).unwrap();
    c.execute_batch(SCHEMA).unwrap();
    const CHUNK: usize = 1_000_000;
    let mut id = 0usize;
    let mut es = Vec::new();
    let mut buf = Vec::new();
    while id < ds.n {
        let end = (id + CHUNK).min(ds.n);
        let tx = c.transaction().unwrap();
        {
            let mut ins_n = tx.prepare_cached("INSERT INTO node(id, rec) VALUES(?1, ?2)").unwrap();
            let mut ins_o = tx.prepare_cached("INSERT INTO outedge(parent, edges) VALUES(?1, ?2)").unwrap();
            let mut ins_i = tx.prepare_cached("INSERT INTO inedge(child, parent) VALUES(?1, ?2)").unwrap();
            for i in id as u32..end as u32 {
                ins_n.execute(params![i as i64, ds.node_bytes(i)]).unwrap();
                es.clear();
                ds.children(i, &mut es);
                if !es.is_empty() { edges_to_bytes(&es, &mut buf); ins_o.execute(params![i as i64, &buf[..]]).unwrap(); }
                for &p in ds.parents(i) { ins_i.execute(params![i as i64, p as i64]).unwrap(); }
            }
        }
        tx.commit().unwrap();
        id = end;
    }
    {
        // The key map in key order: every insert lands on the rightmost leaf.
        c.execute_batch("BEGIN").unwrap();
        let mut pending = 0u32;
        let mut ins = c.prepare("INSERT INTO keymap(key, id) VALUES(?1, ?2)").unwrap();
        for_each_key_sorted(ds.n as u32, |k, i| {
            ins.execute(params![&k[..], i as i64]).unwrap();
            pending += 1;
            if pending == 2_000_000 { c.execute_batch("COMMIT; BEGIN").unwrap(); pending = 0; }
        });
        drop(ins);
        c.execute_batch("COMMIT").unwrap();
    }
    c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(1, 0)", []).unwrap();
    c.pragma_update(None, "locking_mode", "NORMAL").unwrap();
    c.pragma_update(None, "journal_mode", "WAL").unwrap();
    c.pragma_update(None, "synchronous", "FULL").unwrap();
    c.execute("UPDATE meta SET v = 0 WHERE k = 1", []).unwrap();
    drop(c);
}

fn q_node_count(c: &Connection) -> u32 { c.query_row("SELECT COALESCE(MAX(id), -1) + 1 FROM node", [], |r| r.get::<_, i64>(0)).unwrap() as u32 }
fn q_nodes(c: &Connection, ids: &[u32], out: &mut Vec<Node>) {
    out.clear();
    let mut st = c.prepare_cached("SELECT rec FROM node WHERE id = ?1").unwrap();
    for &id in ids {
        let n = st.query_row(params![id as i64], |r| Ok(Node::from_bytes(r.get_ref(0)?.as_blob()?))).unwrap();
        out.push(n);
    }
}
fn q_children(c: &Connection, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) {
    offs.clear(); out.clear(); offs.push(0);
    let mut st = c.prepare_cached("SELECT edges FROM outedge WHERE parent = ?1").unwrap();
    for &id in ids {
        let mut rows = st.query(params![id as i64]).unwrap();
        if let Some(r) = rows.next().unwrap() { edges_from_bytes(r.get_ref(0).unwrap().as_blob().unwrap(), out); }
        offs.push(out.len() as u32);
    }
}
fn q_parents(c: &Connection, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) {
    offs.clear(); out.clear(); offs.push(0);
    let mut st = c.prepare_cached("SELECT parent FROM inedge WHERE child = ?1").unwrap();
    for &id in ids {
        let mut rows = st.query(params![id as i64]).unwrap();
        while let Some(r) = rows.next().unwrap() { out.push(r.get::<_, i64>(0).unwrap() as u32); }
        offs.push(out.len() as u32);
    }
}
fn q_keys(c: &Connection, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) {
    out.clear();
    let mut st = c.prepare_cached("SELECT id FROM keymap WHERE key = ?1").unwrap();
    for k in keys {
        let mut rows = st.query(params![&k[..]]).unwrap();
        out.push(rows.next().unwrap().map(|r| r.get::<_, i64>(0).unwrap() as u32));
    }
}

pub struct SqliteStore { c: Connection, path: std::path::PathBuf, cache: u64, seq: u64 }
pub struct SqliteReader { c: Connection, in_txn: bool }

impl SqliteStore {
    pub fn open(o: &OpenOpts) -> SqliteStore {
        let c = Connection::open(db_file(o)).unwrap();
        tune(&c, o.cache_bytes, o.durable);
        c.set_prepared_statement_cache_capacity(64);
        let seq = c.query_row("SELECT v FROM meta WHERE k = 1", [], |r| r.get::<_, i64>(0)).unwrap_or(0) as u64;
        SqliteStore { c, path: o.path.clone(), cache: o.cache_bytes, seq }
    }
}

impl Reader for SqliteStore {
    fn node_count(&mut self) -> u32 { q_node_count(&self.c) }
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { q_nodes(&self.c, ids, out) }
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { q_children(&self.c, ids, offs, out) }
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { q_parents(&self.c, ids, offs, out) }
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { q_keys(&self.c, keys, out) }
}
impl Reader for SqliteReader {
    fn begin(&mut self) { if !self.in_txn { self.c.execute_batch("BEGIN").unwrap(); self.in_txn = true; } }
    fn end(&mut self) { if self.in_txn { self.c.execute_batch("COMMIT").unwrap(); self.in_txn = false; } }
    fn node_count(&mut self) -> u32 { q_node_count(&self.c) }
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { q_nodes(&self.c, ids, out) }
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { q_children(&self.c, ids, offs, out) }
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { q_parents(&self.c, ids, offs, out) }
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { q_keys(&self.c, keys, out) }
}

impl Store for SqliteStore {
    fn name(&self) -> String { "SQLite (WAL)".into() }
    fn apply_batch(&mut self, b: &Batch) {
        let mut buf = Vec::new();
        let tx = self.c.transaction().unwrap();
        {
            let mut ins_n = tx.prepare_cached("INSERT INTO node(id, rec) VALUES(?1, ?2)").unwrap();
            let mut ins_k = tx.prepare_cached("INSERT INTO keymap(key, id) VALUES(?1, ?2)").unwrap();
            for (k, n) in b.new_nodes.iter().enumerate() {
                let id = b.first_new + k as u32;
                ins_n.execute(params![id as i64, &n.to_bytes()[..]]).unwrap();
                ins_k.execute(params![&key_of(id)[..], id as i64]).unwrap();
            }
            let mut upd = tx.prepare_cached("UPDATE node SET rec = ?2 WHERE id = ?1").unwrap();
            for (id, n) in &b.puts { upd.execute(params![*id as i64, &n.to_bytes()[..]]).unwrap(); }
            let mut ins_o = tx.prepare_cached("INSERT OR REPLACE INTO outedge(parent, edges) VALUES(?1, ?2)").unwrap();
            let mut ins_i = tx.prepare_cached("INSERT OR IGNORE INTO inedge(child, parent) VALUES(?1, ?2)").unwrap();
            for (p, es) in &b.children {
                edges_to_bytes(es, &mut buf);
                ins_o.execute(params![*p as i64, &buf[..]]).unwrap();
                for e in es { ins_i.execute(params![e.child as i64, *p as i64]).unwrap(); }
            }
            tx.execute("UPDATE meta SET v = ?1 WHERE k = 1", params![b.seq as i64]).unwrap();
        }
        tx.commit().unwrap();
        self.seq = b.seq;
    }
    fn last_seq(&mut self) -> u64 { self.seq }
    fn new_reader(&self) -> Box<dyn Reader + Send> {
        let c = Connection::open_with_flags(self.path.join("graph.sqlite"), OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX).unwrap();
        c.pragma_update(None, "cache_size", -((self.cache / 8 / 1024) as i64)).unwrap();
        c.pragma_update(None, "mmap_size", 64i64 << 30).unwrap();
        c.busy_timeout(std::time::Duration::from_secs(30)).unwrap();
        Box::new(SqliteReader { c, in_txn: false })
    }
    fn disk_bytes(&mut self) -> u64 { dir_bytes(&self.path) }
    fn settle(&mut self) { self.c.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);").unwrap(); }
}
