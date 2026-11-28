//! LMDB through its C API (the `lmdb-master-sys` bindings that `heed` uses),
//! so that the candidate is LMDB itself and not a wrapper's overhead.
//!
//! Sub-databases: `node` (integer key id -> 32 B record), `out` (integer key
//! parent -> edge block), `inn` (8-byte big-endian (child, parent) -> empty),
//! `keymap` (16 B key -> id), `meta`.  One write transaction per batch; LMDB
//! fsyncs on commit and needs no recovery step (copy-on-write B+tree).

use crate::dataset::Dataset;
use crate::model::*;
use crate::store::{OpenOpts, Reader, Store};
use lmdb_master_sys as ffi;
use std::ffi::{c_uint, c_void, CString};
use std::ptr;
use std::sync::Arc;

struct EnvHandle(*mut ffi::MDB_env);
unsafe impl Send for EnvHandle {}
unsafe impl Sync for EnvHandle {}
impl Drop for EnvHandle { fn drop(&mut self) { unsafe { ffi::mdb_env_close(self.0) } } }

#[derive(Clone, Copy)]
struct Dbis { node: ffi::MDB_dbi, out: ffi::MDB_dbi, inn: ffi::MDB_dbi, keymap: ffi::MDB_dbi, meta: ffi::MDB_dbi }

fn chk(rc: i32, what: &str) { if rc != 0 { panic!("lmdb {what}: error {rc}"); } }
#[inline] fn val(b: &[u8]) -> ffi::MDB_val { ffi::MDB_val { mv_size: b.len(), mv_data: b.as_ptr() as *mut c_void } }
#[inline] unsafe fn bytes<'a>(v: &ffi::MDB_val) -> &'a [u8] { unsafe { std::slice::from_raw_parts(v.mv_data as *const u8, v.mv_size) } }

fn open_env(o: &OpenOpts, create: bool, nosync: bool) -> (Arc<EnvHandle>, Dbis) {
    std::fs::create_dir_all(&o.path).unwrap();
    unsafe {
        let mut env = ptr::null_mut();
        chk(ffi::mdb_env_create(&mut env), "env_create");
        let map = (((o.cap_nodes * 260 + o.cap_edges * 60) >> 30) + 4) << 30;
        chk(ffi::mdb_env_set_mapsize(env, map as usize), "set_mapsize");
        chk(ffi::mdb_env_set_maxdbs(env, 8), "set_maxdbs");
        chk(ffi::mdb_env_set_maxreaders(env, 126), "set_maxreaders");
        let mut flags = ffi::MDB_NOTLS;
        if nosync { flags |= ffi::MDB_NOSYNC; }
        let p = CString::new(o.path.to_str().unwrap()).unwrap();
        chk(ffi::mdb_env_open(env, p.as_ptr(), flags, 0o600), "env_open");
        let mut txn = ptr::null_mut();
        chk(ffi::mdb_txn_begin(env, ptr::null_mut(), 0, &mut txn), "txn_begin");
        let c = if create { ffi::MDB_CREATE } else { 0 };
        let open = |name: &str, f: c_uint| -> ffi::MDB_dbi {
            let n = CString::new(name).unwrap();
            let mut dbi = 0;
            chk(ffi::mdb_dbi_open(txn, n.as_ptr(), f | c, &mut dbi), "dbi_open");
            dbi
        };
        let d = Dbis { node: open("node", ffi::MDB_INTEGERKEY), out: open("out", ffi::MDB_INTEGERKEY), inn: open("inn", 0), keymap: open("keymap", 0), meta: open("meta", 0) };
        chk(ffi::mdb_txn_commit(txn), "commit");
        (Arc::new(EnvHandle(env)), d)
    }
}

unsafe fn put(txn: *mut ffi::MDB_txn, dbi: ffi::MDB_dbi, k: &[u8], v: &[u8], flags: c_uint) {
    let (mut kv, mut dv) = (val(k), val(v));
    let rc = unsafe { ffi::mdb_put(txn, dbi, &mut kv, &mut dv, flags) };
    chk(rc, "put");
}
#[inline] fn inn_key(child: u32, parent: u32) -> [u8; 8] { let mut k = [0u8; 8]; k[..4].copy_from_slice(&child.to_be_bytes()); k[4..].copy_from_slice(&parent.to_be_bytes()); k }

pub fn bulk_load(o: &OpenOpts, ds: &Dataset) {
    let _ = std::fs::remove_dir_all(&o.path);
    let (env, d) = open_env(o, true, true);
    const CHUNK: u32 = 500_000;
    unsafe {
        let mut id = 0u32;
        let mut es = Vec::new();
        let mut buf = Vec::new();
        while (id as usize) < ds.n {
            let end = ((id as usize + CHUNK as usize).min(ds.n)) as u32;
            let mut txn = ptr::null_mut();
            chk(ffi::mdb_txn_begin(env.0, ptr::null_mut(), 0, &mut txn), "txn_begin");
            for i in id..end {
                put(txn, d.node, &i.to_ne_bytes(), ds.node_bytes(i), ffi::MDB_APPEND);
                es.clear();
                ds.children(i, &mut es);
                if !es.is_empty() { edges_to_bytes(&es, &mut buf); put(txn, d.out, &i.to_ne_bytes(), &buf, ffi::MDB_APPEND); }
                for &p in ds.parents(i) { put(txn, d.inn, &inn_key(i, p), &[], ffi::MDB_APPEND); }
            }
            chk(ffi::mdb_txn_commit(txn), "commit");
            id = end;
        }
        // The key map in key order, appended.
        let mut txn = ptr::null_mut();
        chk(ffi::mdb_txn_begin(env.0, ptr::null_mut(), 0, &mut txn), "txn_begin");
        let mut pending = 0u32;
        for_each_key_sorted(ds.n as u32, |k, i| {
            put(txn, d.keymap, k, &i.to_le_bytes(), ffi::MDB_APPEND);
            pending += 1;
            if pending == 2_000_000 {
                chk(ffi::mdb_txn_commit(txn), "commit");
                chk(ffi::mdb_txn_begin(env.0, ptr::null_mut(), 0, &mut txn), "txn_begin");
                pending = 0;
            }
        });
        chk(ffi::mdb_txn_commit(txn), "commit");
        let mut txn = ptr::null_mut();
        chk(ffi::mdb_txn_begin(env.0, ptr::null_mut(), 0, &mut txn), "txn_begin");
        put(txn, d.meta, b"seq", &0u64.to_le_bytes(), 0);
        chk(ffi::mdb_txn_commit(txn), "commit");
        chk(ffi::mdb_env_sync(env.0, 1), "env_sync");
    }
}

struct Rtxn { env: Arc<EnvHandle>, d: Dbis, txn: *mut ffi::MDB_txn, active: bool }
unsafe impl Send for Rtxn {}
impl Rtxn {
    fn new(env: Arc<EnvHandle>, d: Dbis) -> Rtxn { Rtxn { env, d, txn: ptr::null_mut(), active: false } }
    #[inline] fn ensure(&mut self) {
        if self.active { return; }
        unsafe {
            if self.txn.is_null() { chk(ffi::mdb_txn_begin(self.env.0, ptr::null_mut(), ffi::MDB_RDONLY, &mut self.txn), "ro begin"); }
            else { chk(ffi::mdb_txn_renew(self.txn), "renew"); }
        }
        self.active = true;
    }
    #[inline] fn release(&mut self) { if self.active { unsafe { ffi::mdb_txn_reset(self.txn) }; self.active = false; } }
    #[inline] unsafe fn get<'a>(&self, dbi: ffi::MDB_dbi, k: &[u8]) -> Option<&'a [u8]> {
        let (mut kv, mut dv) = (val(k), ffi::MDB_val { mv_size: 0, mv_data: ptr::null_mut() });
        let rc = unsafe { ffi::mdb_get(self.txn, dbi, &mut kv, &mut dv) };
        if rc == ffi::MDB_NOTFOUND { return None; }
        chk(rc, "get");
        Some(unsafe { bytes(&dv) })
    }
    fn node_count(&mut self) -> u32 {
        self.ensure();
        unsafe {
            let mut st: ffi::MDB_stat = std::mem::zeroed();
            chk(ffi::mdb_stat(self.txn, self.d.node, &mut st), "stat");
            st.ms_entries as u32
        }
    }
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) {
        self.ensure();
        out.clear();
        for &id in ids { out.push(Node::from_bytes(unsafe { self.get(self.d.node, &id.to_ne_bytes()) }.expect("node"))); }
    }
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) {
        self.ensure();
        offs.clear(); out.clear(); offs.push(0);
        for &id in ids {
            if let Some(b) = unsafe { self.get(self.d.out, &id.to_ne_bytes()) } { edges_from_bytes(b, out); }
            offs.push(out.len() as u32);
        }
    }
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) {
        self.ensure();
        offs.clear(); out.clear(); offs.push(0);
        unsafe {
            let mut cur = ptr::null_mut();
            chk(ffi::mdb_cursor_open(self.txn, self.d.inn, &mut cur), "cursor_open");
            for &id in ids {
                let start = inn_key(id, 0);
                let (mut kv, mut dv) = (val(&start), ffi::MDB_val { mv_size: 0, mv_data: ptr::null_mut() });
                let mut rc = ffi::mdb_cursor_get(cur, &mut kv, &mut dv, ffi::MDB_SET_RANGE);
                while rc == 0 {
                    let k = bytes(&kv);
                    if k[..4] != start[..4] { break; }
                    out.push(u32::from_be_bytes(k[4..8].try_into().unwrap()));
                    rc = ffi::mdb_cursor_get(cur, &mut kv, &mut dv, ffi::MDB_NEXT);
                }
                offs.push(out.len() as u32);
            }
            ffi::mdb_cursor_close(cur);
        }
    }
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) {
        self.ensure();
        out.clear();
        for k in keys { out.push(unsafe { self.get(self.d.keymap, k) }.map(|b| u32::from_le_bytes(b[..4].try_into().unwrap()))); }
    }
}
impl Drop for Rtxn { fn drop(&mut self) { if !self.txn.is_null() { unsafe { ffi::mdb_txn_abort(self.txn) } } } }

pub struct LmdbStore { env: Arc<EnvHandle>, d: Dbis, r: Rtxn, seq: u64 }
pub struct LmdbReader { r: Rtxn }

impl LmdbStore {
    pub fn open(o: &OpenOpts) -> LmdbStore {
        let (env, d) = open_env(o, false, !o.durable);
        let mut r = Rtxn::new(env.clone(), d);
        r.ensure();
        let seq = unsafe { r.get(d.meta, b"seq") }.map(|b| u64::from_le_bytes(b[..8].try_into().unwrap())).unwrap_or(0);
        r.release();
        LmdbStore { env, d, r, seq }
    }
}

macro_rules! impl_reader {
    ($t:ty) => {
        impl Reader for $t {
            fn begin(&mut self) { self.r.release(); self.r.ensure(); }
            fn end(&mut self) { self.r.release(); }
            fn node_count(&mut self) -> u32 { self.r.node_count() }
            fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { self.r.get_nodes(ids, out) }
            fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { self.r.get_children(ids, offs, out) }
            fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { self.r.get_parents(ids, offs, out) }
            fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { self.r.lookup_keys(keys, out) }
        }
    };
}
impl_reader!(LmdbStore);
impl_reader!(LmdbReader);

impl Store for LmdbStore {
    fn name(&self) -> String { "LMDB".into() }
    fn apply_batch(&mut self, b: &Batch) {
        self.r.release();
        let d = self.d;
        let mut buf = Vec::new();
        unsafe {
            let mut txn = ptr::null_mut();
            chk(ffi::mdb_txn_begin(self.env.0, ptr::null_mut(), 0, &mut txn), "txn_begin");
            for (k, n) in b.new_nodes.iter().enumerate() {
                let id = b.first_new + k as u32;
                put(txn, d.node, &id.to_ne_bytes(), &n.to_bytes(), ffi::MDB_APPEND);
                put(txn, d.keymap, &key_of(id), &id.to_le_bytes(), 0);
            }
            for (id, n) in &b.puts { put(txn, d.node, &id.to_ne_bytes(), &n.to_bytes(), 0); }
            for (p, es) in &b.children {
                edges_to_bytes(es, &mut buf);
                put(txn, d.out, &p.to_ne_bytes(), &buf, 0);
                for e in es { put(txn, d.inn, &inn_key(e.child, *p), &[], 0); }
            }
            put(txn, d.meta, b"seq", &b.seq.to_le_bytes(), 0);
            chk(ffi::mdb_txn_commit(txn), "commit");
        }
        self.seq = b.seq;
    }
    fn last_seq(&mut self) -> u64 { self.seq }
    fn new_reader(&self) -> Box<dyn Reader + Send> { Box::new(LmdbReader { r: Rtxn::new(self.env.clone(), self.d) }) }
    fn disk_bytes(&mut self) -> u64 {
        unsafe {
            let mut info: ffi::MDB_envinfo = std::mem::zeroed();
            chk(ffi::mdb_env_info(self.env.0, &mut info), "env_info");
            (info.me_last_pgno as u64 + 1) * 4096
        }
    }
    fn settle(&mut self) { unsafe { chk(ffi::mdb_env_sync(self.env.0, 1), "env_sync") } }
}
