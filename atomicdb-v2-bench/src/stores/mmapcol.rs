//! Custom store: fixed-stride column files mapped into memory, a redo log with
//! a CRC per frame, fsync before apply, idempotent replay (MoKy's design).
//!
//! Files: `core.bin` (32 B per position), `outoff.bin` (8 B per position:
//! offset+1 of its out-edge block), `pin.bin` (4 B per position: its parent, or
//! the head of a parent list), `out.bin` (append-only edge blocks), `inx.bin`
//! (append-only parent-list cells), `keymap.bin` (open-addressing hash table,
//! 96-bit fingerprint -> id), `ctl.bin` (counters, checkpoint, seqlock epoch)
//! and `wal.bin`.
//!
//! Commit protocol: build the redo frame (absolute byte writes), append it to
//! the log, fsync, then apply it to the maps between two bumps of the epoch.
//! A checkpoint flushes the maps, records the sequence number in `ctl.bin` and
//! truncates the log.  Recovery replays every valid frame newer than the
//! checkpoint; a torn tail frame fails its CRC and is discarded.  Readers in
//! other threads (or processes) take no lock: they retry when the epoch moved.

use crate::dataset::Dataset;
use crate::model::*;
use crate::store::{OpenOpts, Reader, Store};
use memmap2::{MmapOptions, MmapRaw};
use std::collections::{HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

const MAGIC: u64 = 0x41544F4D44423276; // "ATOMDB2v"
const F_CTL: u8 = 0;
const F_CORE: u8 = 1;
const F_OUTOFF: u8 = 2;
const F_PIN: u8 = 3;
const F_OUT: u8 = 4;
const F_INX: u8 = 5;
const F_KEYMAP: u8 = 6;
const F_FILTER: u8 = 7;
const NFILES: usize = 8;
const NAMES: [&str; NFILES] = ["ctl.bin", "core.bin", "outoff.bin", "pin.bin", "out.bin", "inx.bin", "keymap.bin", "kfilter.bin"];
/// Bits of key filter per position of capacity (a blocked Bloom filter, one cache line per key).
const FILTER_BITS: u64 = 10;
// ctl.bin layout (u64 each)
const C_MAGIC: usize = 0;
const C_NODES: usize = 8;
const C_OUT_TAIL: usize = 16;   // bytes used in out.bin
const C_INX_TAIL: usize = 24;   // cells used in inx.bin (cell 0 is never used)
const C_CKPT_SEQ: usize = 32;
const C_EPOCH: usize = 40;
const C_SLOTS: usize = 48;
const C_CAP_NODES: usize = 56;
const CTL_SIZE: u64 = 4096;
const PIN_LIST: u32 = 1 << 31;
const SLOT: usize = 16;
const WAL_CHECKPOINT_BYTES: u64 = 256 << 20;

pub struct Maps {
    m: [MmapRaw; NFILES],
    slots: u64,
    /// 64-byte blocks in the key filter; 0 = filter switched off (MMAP_KEY_FILTER=0).
    fblocks: u64,
}
// MmapRaw is Send + Sync; all access goes through raw pointers guarded by the epoch.
impl Maps {
    #[inline] fn ptr(&self, f: u8) -> *mut u8 { self.m[f as usize].as_mut_ptr() }
    #[inline] fn epoch(&self) -> &AtomicU64 { unsafe { &*(self.ptr(F_CTL).add(C_EPOCH) as *const AtomicU64) } }
    #[inline] fn ctl(&self, off: usize) -> u64 { unsafe { (self.ptr(F_CTL).add(off) as *const u64).read_unaligned() } }
    #[inline] fn set_ctl(&self, off: usize, v: u64) { unsafe { (self.ptr(F_CTL).add(off) as *mut u64).write_unaligned(v) } }
    #[inline] fn node(&self, id: u32) -> Node { unsafe { (self.ptr(F_CORE).add(id as usize * NODE_SIZE) as *const Node).read_unaligned() } }
    #[inline] fn outoff(&self, id: u32) -> u64 { unsafe { (self.ptr(F_OUTOFF).add(id as usize * 8) as *const u64).read_unaligned() } }
    #[inline] fn pin(&self, id: u32) -> u32 { unsafe { (self.ptr(F_PIN).add(id as usize * 4) as *const u32).read_unaligned() } }
    #[inline] fn inx(&self, cell: u32) -> (u32, u32) { unsafe { let p = self.ptr(F_INX).add(cell as usize * 8) as *const u32; (p.read_unaligned(), p.add(1).read_unaligned()) } }
    fn children(&self, id: u32, out: &mut Vec<Edge>) {
        let off = self.outoff(id);
        if off == 0 { return; }
        unsafe {
            let p = self.ptr(F_OUT).add(off as usize - 1);
            let cnt = (p as *const u16).read_unaligned() as usize;
            let b = std::slice::from_raw_parts(p.add(2), cnt * EDGE_SIZE);
            edges_from_bytes(b, out);
        }
    }
    fn parents(&self, id: u32, out: &mut Vec<u32>) {
        let p = self.pin(id);
        if p == 0 { return; }
        if p & PIN_LIST == 0 { out.push(p - 1); return; }
        let mut cell = p & !PIN_LIST;
        let mut guard = 0;
        while cell != 0 && guard < 1_000_000 {
            let (par, next) = self.inx(cell);
            out.push(par);
            cell = next;
            guard += 1;
        }
    }
    /// Monotone in the key (multiply-shift on its big-endian prefix), so a bulk
    /// load in key order fills the table front to back.
    #[inline] fn slot_of(&self, key: &[u8; 16]) -> u64 { ((u64::from_be_bytes(key[..8].try_into().unwrap()) as u128 * self.slots as u128) >> 64) as u64 }
    /// Key filter.  Almost every lookup an expansion makes is for a position
    /// that does not exist yet; the filter answers those from one cache line
    /// instead of one random page of the key map, which matters once the key
    /// map no longer fits in RAM.  Bits are only ever set, at apply time, and
    /// redo of a key-map slot sets them again, so the file needs no log of its own.
    #[inline] fn filter_pos(&self, fp: &[u8]) -> (usize, u64) {
        let h = xxhash_rust::xxh3::xxh3_64(&fp[..12]);
        ((((h >> 32) * self.fblocks) >> 32) as usize * 64, h)
    }
    #[inline] fn filter_set(&self, fp: &[u8]) -> usize {
        let (base, h) = self.filter_pos(fp);
        for i in 0..6 { let b = ((h >> (i * 9)) & 511) as usize; unsafe { *self.ptr(F_FILTER).add(base + b / 8) |= 1 << (b % 8); } }
        base
    }
    #[inline] fn filter_may_contain(&self, fp: &[u8]) -> bool {
        let (base, h) = self.filter_pos(fp);
        (0..6).all(|i| { let b = ((h >> (i * 9)) & 511) as usize; unsafe { *self.ptr(F_FILTER).add(base + b / 8) & (1 << (b % 8)) != 0 } })
    }
    fn lookup(&self, key: &[u8; 16]) -> Option<u32> {
        if self.fblocks != 0 && !self.filter_may_contain(&key[4..]) { return None; }
        let mut s = self.slot_of(key);
        loop {
            let b = unsafe { std::slice::from_raw_parts(self.ptr(F_KEYMAP).add(s as usize * SLOT), SLOT) };
            let id1 = u32::from_le_bytes(b[12..16].try_into().unwrap());
            if id1 == 0 { return None; }
            if b[..12] == key[4..16] { return Some(id1 - 1); }
            s += 1;
            if s == self.slots { s = 0; }
        }
    }
}

fn open_file(dir: &Path, name: &str, len: Option<u64>) -> File {
    let f = OpenOptions::new().read(true).write(true).create(true).truncate(false).open(dir.join(name)).unwrap();
    if let Some(l) = len { f.set_len(l).unwrap(); }
    f
}

fn file_sizes(cap_nodes: u64, cap_edges: u64) -> [u64; NFILES] {
    let slots = (cap_nodes as f64 / 0.65) as u64 + 16;
    [CTL_SIZE, cap_nodes * NODE_SIZE as u64, cap_nodes * 8, cap_nodes * 4,
     cap_edges * EDGE_SIZE as u64 + cap_nodes / 3 * 2 + 4096, (cap_edges + 4096) * 8, slots * SLOT as u64,
     (cap_nodes * FILTER_BITS / 512 + 1) * 64]
}

fn map_all(dir: &Path, create: Option<[u64; NFILES]>) -> Maps {
    let maps: Vec<MmapRaw> = (0..NFILES).map(|i| {
        let f = open_file(dir, NAMES[i], create.map(|c| c[i]));
        MmapOptions::new().map_raw(&f).unwrap()
    }).collect();
    let m: [MmapRaw; NFILES] = maps.try_into().ok().unwrap();
    let slots = (m[F_KEYMAP as usize].len() / SLOT) as u64;
    let on = std::env::var("MMAP_KEY_FILTER").map(|v| v != "0").unwrap_or(true);
    let fblocks = if on { (m[F_FILTER as usize].len() / 64) as u64 } else { 0 };
    Maps { m, slots, fblocks }
}

pub fn bulk_load(o: &OpenOpts, ds: &Dataset) {
    std::fs::create_dir_all(&o.path).unwrap();
    for n in NAMES.iter().chain(["wal.bin"].iter()) { let _ = std::fs::remove_file(o.path.join(n)); }
    let cap_nodes = o.cap_nodes.max(ds.n as u64 + 1024);
    let cap_edges = o.cap_edges.max(ds.e as u64 + 1024);
    let maps = map_all(&o.path, Some(file_sizes(cap_nodes, cap_edges)));
    let mut out_tail = 0usize;
    let mut inx_tail = 1u32;
    let mut es = Vec::new();
    let mut buf = Vec::new();
    unsafe {
        for id in 0..ds.n as u32 {
            std::ptr::copy_nonoverlapping(ds.node_bytes(id).as_ptr(), maps.ptr(F_CORE).add(id as usize * NODE_SIZE), NODE_SIZE);
            es.clear();
            ds.children(id, &mut es);
            if !es.is_empty() {
                edges_to_bytes(&es, &mut buf);
                let p = maps.ptr(F_OUT).add(out_tail);
                (p as *mut u16).write_unaligned(es.len() as u16);
                std::ptr::copy_nonoverlapping(buf.as_ptr(), p.add(2), buf.len());
                (maps.ptr(F_OUTOFF).add(id as usize * 8) as *mut u64).write_unaligned(out_tail as u64 + 1);
                out_tail += 2 + buf.len();
            }
            let ps = ds.parents(id);
            let pin = match ps.len() {
                0 => 0,
                1 => ps[0] + 1,
                _ => {
                    let mut next = 0u32;
                    for &p in ps {
                        let c = maps.ptr(F_INX).add(inx_tail as usize * 8) as *mut u32;
                        c.write_unaligned(p); c.add(1).write_unaligned(next);
                        next = inx_tail; inx_tail += 1;
                    }
                    PIN_LIST | next
                }
            };
            (maps.ptr(F_PIN).add(id as usize * 4) as *mut u32).write_unaligned(pin);
            // Hand the pages written so far to the OS now and then, so a 200M load does not pile up dirty pages.
            if id % 16_000_000 == 15_999_999 { for m in maps.m.iter() { let _ = m.flush_async(); } }
        }
        // Key map, in key order: the table fills sequentially.
        for_each_key_sorted(ds.n as u32, |key, id| {
            let mut s = maps.slot_of(key);
            loop {
                let sp = maps.ptr(F_KEYMAP).add(s as usize * SLOT);
                if (sp.add(12) as *const u32).read_unaligned() == 0 {
                    std::ptr::copy_nonoverlapping(key[4..].as_ptr(), sp, 12);
                    (sp.add(12) as *mut u32).write_unaligned(id + 1);
                    break;
                }
                s += 1; if s == maps.slots { s = 0; }
            }
            if maps.fblocks != 0 { maps.filter_set(&key[4..]); }
        });
    }
    maps.set_ctl(C_MAGIC, MAGIC);
    maps.set_ctl(C_NODES, ds.n as u64);
    maps.set_ctl(C_OUT_TAIL, out_tail as u64);
    maps.set_ctl(C_INX_TAIL, inx_tail as u64);
    maps.set_ctl(C_CKPT_SEQ, 0);
    maps.set_ctl(C_EPOCH, 0);
    maps.set_ctl(C_SLOTS, maps.slots);
    maps.set_ctl(C_CAP_NODES, cap_nodes);
    for m in maps.m.iter().rev() { m.flush().unwrap(); }
    File::create(o.path.join("wal.bin")).unwrap().sync_all().unwrap();
}

struct Op { file: u8, off: u64, data_at: usize, len: u32 }

pub struct MmapStore {
    maps: Arc<Maps>,
    dir: std::path::PathBuf,
    wal: File,
    wal_len: u64,
    seq: u64,
    durable: bool,
    frame: Vec<u8>,
    ops: Vec<Op>,
    dirty: HashSet<(u8, u32)>,
    phys_written: u64,
    pub recovery_frames: u64,
    ckpt_bytes: u64,
    wal_total: u64,
    ckpts: u64,
    ckpt_pages: u64,
    ckpt_us: u64,
}

pub struct MmapReader { maps: Arc<Maps>, e0: u64 }

impl MmapStore {
    pub fn open(o: &OpenOpts) -> MmapStore {
        let maps = map_all(&o.path, None);
        assert_eq!(maps.ctl(C_MAGIC), MAGIC, "not an mmap column store");
        let mut wal = OpenOptions::new().read(true).write(true).create(true).truncate(false).open(o.path.join("wal.bin")).unwrap();
        // Recovery: replay valid frames newer than the checkpoint.
        let ckpt = maps.ctl(C_CKPT_SEQ);
        let mut log = Vec::new();
        wal.read_to_end(&mut log).unwrap();
        let mut pos = 0usize;
        let mut seq = ckpt;
        let mut frames = 0u64;
        let e = maps.epoch().load(Ordering::Relaxed);
        maps.epoch().store(e | 1, Ordering::SeqCst);
        while pos + 16 <= log.len() {
            let len = u32::from_le_bytes(log[pos..pos + 4].try_into().unwrap()) as usize;
            let crc = u32::from_le_bytes(log[pos + 4..pos + 8].try_into().unwrap());
            if len < 8 || pos + 8 + len > log.len() { break; }
            let body = &log[pos + 8..pos + 8 + len];
            if crc32fast::hash(body) != crc { break; }
            let fseq = u64::from_le_bytes(body[..8].try_into().unwrap());
            if fseq > ckpt {
                let mut p = 8usize;
                while p < body.len() {
                    let file = body[p];
                    let off = u64::from_le_bytes(body[p + 1..p + 9].try_into().unwrap()) as usize;
                    let l = u32::from_le_bytes(body[p + 9..p + 13].try_into().unwrap()) as usize;
                    unsafe { std::ptr::copy_nonoverlapping(body[p + 13..].as_ptr(), maps.ptr(file).add(off), l); }
                    if file == F_KEYMAP && maps.fblocks != 0 { maps.filter_set(&body[p + 13..p + 25]); }
                    p += 13 + l;
                }
                seq = fseq;
                frames += 1;
            }
            pos += 8 + len;
        }
        maps.epoch().store((e | 1) + 1, Ordering::SeqCst);
        if (pos as u64) < log.len() as u64 { wal.set_len(pos as u64).unwrap(); }
        wal.seek(SeekFrom::Start(pos as u64)).unwrap();
        MmapStore { maps: Arc::new(maps), dir: o.path.clone(), wal, wal_len: pos as u64, seq, durable: o.durable, frame: Vec::new(), ops: Vec::new(),
                    dirty: HashSet::new(), phys_written: 0, recovery_frames: frames,
                    ckpt_bytes: std::env::var("MMAP_WAL_CKPT_MB").ok().and_then(|v| v.parse::<u64>().ok()).map(|m| m << 20).unwrap_or(WAL_CHECKPOINT_BYTES),
                    wal_total: 0, ckpts: 0, ckpt_pages: 0, ckpt_us: 0 }
    }

    #[inline] fn op(&mut self, file: u8, off: u64, bytes: &[u8]) {
        self.frame.push(file);
        self.frame.extend_from_slice(&off.to_le_bytes());
        self.frame.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
        let at = self.frame.len();
        self.frame.extend_from_slice(bytes);
        self.ops.push(Op { file, off, data_at: at, len: bytes.len() as u32 });
    }

    pub fn checkpoint(&mut self) {
        let t0 = std::time::Instant::now();
        self.ckpts += 1;
        self.ckpt_pages += self.dirty.len() as u64;
        for i in (1..NFILES).rev() { self.maps.m[i].flush().unwrap(); }
        self.maps.set_ctl(C_CKPT_SEQ, self.seq);
        self.maps.m[F_CTL as usize].flush().unwrap();
        self.wal.set_len(0).unwrap();
        self.wal.seek(SeekFrom::Start(0)).unwrap();
        self.wal.sync_data().unwrap();
        self.wal_len = 0;
        self.phys_written += self.dirty.len() as u64 * 4096 + 4096;
        self.dirty.clear();
        self.ckpt_us += t0.elapsed().as_micros() as u64;
    }
}

fn m_nodes(m: &Maps, ids: &[u32], out: &mut Vec<Node>) { out.clear(); out.extend(ids.iter().map(|&id| m.node(id))); }
fn m_children(m: &Maps, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) {
    offs.clear(); out.clear(); offs.push(0);
    for &id in ids { m.children(id, out); offs.push(out.len() as u32); }
}
fn m_parents(m: &Maps, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) {
    offs.clear(); out.clear(); offs.push(0);
    for &id in ids { m.parents(id, out); offs.push(out.len() as u32); }
}
fn m_keys(m: &Maps, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { out.clear(); out.extend(keys.iter().map(|k| m.lookup(k))); }

// The writer is the only thread that changes the maps, so its own reads need no retry.
impl Reader for MmapStore {
    fn node_count(&mut self) -> u32 { self.maps.ctl(C_NODES) as u32 }
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) { m_nodes(&self.maps, ids, out) }
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) { m_children(&self.maps, ids, offs, out) }
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) { m_parents(&self.maps, ids, offs, out) }
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) { m_keys(&self.maps, keys, out) }
}

// Readers elsewhere: optimistic, validated by the epoch (a seqlock over the whole batch apply).
impl MmapReader {
    #[inline] fn stable(&self) -> u64 {
        loop { let e = self.maps.epoch().load(Ordering::Acquire); if e & 1 == 0 { return e; } std::hint::spin_loop(); }
    }
}
impl Reader for MmapReader {
    fn begin(&mut self) { self.e0 = self.stable(); }
    fn validate(&mut self) -> bool { self.maps.epoch().load(Ordering::Acquire) == self.e0 }
    fn node_count(&mut self) -> u32 { self.maps.ctl(C_NODES) as u32 }
    fn get_nodes(&mut self, ids: &[u32], out: &mut Vec<Node>) {
        loop { let e = self.stable(); m_nodes(&self.maps, ids, out); if self.maps.epoch().load(Ordering::Acquire) == e { return; } }
    }
    fn get_children(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<Edge>) {
        loop { let e = self.stable(); m_children(&self.maps, ids, offs, out); if self.maps.epoch().load(Ordering::Acquire) == e { return; } }
    }
    fn get_parents(&mut self, ids: &[u32], offs: &mut Vec<u32>, out: &mut Vec<u32>) {
        loop { let e = self.stable(); m_parents(&self.maps, ids, offs, out); if self.maps.epoch().load(Ordering::Acquire) == e { return; } }
    }
    fn lookup_keys(&mut self, keys: &[[u8; 16]], out: &mut Vec<Option<u32>>) {
        loop { let e = self.stable(); m_keys(&self.maps, keys, out); if self.maps.epoch().load(Ordering::Acquire) == e { return; } }
    }
}

impl Store for MmapStore {
    fn name(&self) -> String { "mmap columns + WAL".into() }

    fn apply_batch(&mut self, b: &Batch) {
        let maps = self.maps.clone();
        let n0 = maps.ctl(C_NODES);
        assert_eq!(b.first_new as u64, n0, "ids must be dense");
        assert!(n0 + b.new_nodes.len() as u64 <= maps.ctl(C_CAP_NODES), "node capacity exhausted");
        self.frame.clear();
        self.ops.clear();
        self.frame.extend_from_slice(&[0u8; 8]); // len + crc, patched below
        self.frame.extend_from_slice(&b.seq.to_le_bytes());
        // 1. new positions: one contiguous run of core records, plus their keys.
        if !b.new_nodes.is_empty() {
            let mut run = Vec::with_capacity(b.new_nodes.len() * NODE_SIZE);
            for n in &b.new_nodes { run.extend_from_slice(&n.to_bytes()); }
            self.op(F_CORE, n0 * NODE_SIZE as u64, &run);
            let mut taken: HashSet<u64> = HashSet::with_capacity(b.new_nodes.len());
            for k in 0..b.new_nodes.len() as u32 {
                let id = b.first_new + k;
                let key = key_of(id);
                let mut s = maps.slot_of(&key);
                loop {
                    let occupied = unsafe { (maps.ptr(F_KEYMAP).add(s as usize * SLOT + 12) as *const u32).read_unaligned() } != 0;
                    if !occupied && taken.insert(s) { break; }
                    s += 1; if s == maps.slots { s = 0; }
                }
                let mut slot = [0u8; SLOT];
                slot[..12].copy_from_slice(&key[4..]);
                slot[12..].copy_from_slice(&(id + 1).to_le_bytes());
                self.op(F_KEYMAP, s * SLOT as u64, &slot);
            }
        }
        // 2. rewrites of existing records.
        for (id, n) in &b.puts { self.op(F_CORE, *id as u64 * NODE_SIZE as u64, &n.to_bytes()); }
        // 3. new out-edge blocks and the in-edges they imply.
        let mut out_tail = maps.ctl(C_OUT_TAIL);
        let mut inx_tail = maps.ctl(C_INX_TAIL) as u32;
        let mut pending_pin: HashMap<u32, u32> = HashMap::new();
        let mut block = Vec::new();
        let mut ebytes = Vec::new();
        let mut old: Vec<Edge> = Vec::new();
        for (p, es) in &b.children {
            // A block that replaces an older one (appended, the old one is left behind) keeps its
            // children: only the new ones gain a parent.
            old.clear();
            if (*p as u64) < n0 { maps.children(*p, &mut old); }
            edges_to_bytes(es, &mut ebytes);
            block.clear();
            block.extend_from_slice(&(es.len() as u16).to_le_bytes());
            block.extend_from_slice(&ebytes);
            self.op(F_OUT, out_tail, &block);
            self.op(F_OUTOFF, *p as u64 * 8, &(out_tail + 1).to_le_bytes());
            out_tail += block.len() as u64;
            for (k, e) in es.iter().enumerate() {
                if es[..k].iter().any(|x| x.child == e.child) || old.iter().any(|x| x.child == e.child) { continue; }
                let cur = match pending_pin.get(&e.child) { Some(&v) => v, None => if (e.child as u64) < n0 { maps.pin(e.child) } else { 0 } };
                let new = if cur == 0 {
                    p + 1
                } else if cur & PIN_LIST == 0 {
                    let mut cells = [0u8; 16];
                    cells[0..4].copy_from_slice(&(cur - 1).to_le_bytes());
                    cells[8..12].copy_from_slice(&p.to_le_bytes());
                    cells[12..16].copy_from_slice(&inx_tail.to_le_bytes());
                    self.op(F_INX, inx_tail as u64 * 8, &cells);
                    inx_tail += 2;
                    PIN_LIST | (inx_tail - 1)
                } else {
                    let mut cell = [0u8; 8];
                    cell[0..4].copy_from_slice(&p.to_le_bytes());
                    cell[4..8].copy_from_slice(&(cur & !PIN_LIST).to_le_bytes());
                    self.op(F_INX, inx_tail as u64 * 8, &cell);
                    inx_tail += 1;
                    PIN_LIST | (inx_tail - 1)
                };
                pending_pin.insert(e.child, new);
            }
        }
        let mut pins: Vec<(u32, u32)> = pending_pin.into_iter().collect();
        pins.sort_unstable();
        for (c, v) in pins { self.op(F_PIN, c as u64 * 4, &v.to_le_bytes()); }
        // 4. counters.
        let mut ctl = [0u8; 24];
        ctl[0..8].copy_from_slice(&(n0 + b.new_nodes.len() as u64).to_le_bytes());
        ctl[8..16].copy_from_slice(&out_tail.to_le_bytes());
        ctl[16..24].copy_from_slice(&(inx_tail as u64).to_le_bytes());
        self.op(F_CTL, C_NODES as u64, &ctl);
        // Log, fsync, apply.
        let len = (self.frame.len() - 8) as u32;
        let crc = crc32fast::hash(&self.frame[8..]);
        self.frame[0..4].copy_from_slice(&len.to_le_bytes());
        self.frame[4..8].copy_from_slice(&crc.to_le_bytes());
        self.wal.write_all(&self.frame).unwrap();
        if self.durable { self.wal.sync_data().unwrap(); }
        self.wal_len += self.frame.len() as u64;
        self.wal_total += self.frame.len() as u64;
        self.phys_written += self.frame.len() as u64;
        let e = maps.epoch().load(Ordering::Relaxed);
        maps.epoch().store(e + 1, Ordering::SeqCst);
        for o in &self.ops {
            unsafe { std::ptr::copy_nonoverlapping(self.frame[o.data_at..].as_ptr(), maps.ptr(o.file).add(o.off as usize), o.len as usize); }
            let (first, last) = ((o.off >> 12) as u32, ((o.off + o.len as u64 - 1) >> 12) as u32);
            for pg in first..=last { self.dirty.insert((o.file, pg)); }
            if o.file == F_KEYMAP && maps.fblocks != 0 {
                let base = maps.filter_set(&self.frame[o.data_at..o.data_at + 12]);
                self.dirty.insert((F_FILTER, (base >> 12) as u32));
            }
        }
        maps.epoch().store(e + 2, Ordering::SeqCst);
        self.seq = b.seq;
        if self.wal_len > self.ckpt_bytes { self.checkpoint(); }
    }

    fn last_seq(&mut self) -> u64 { self.seq }
    fn new_reader(&self) -> Box<dyn Reader + Send> { Box::new(MmapReader { maps: self.maps.clone(), e0: 0 }) }
    fn disk_bytes(&mut self) -> u64 {
        // Files are created at their reserved capacity; what counts is the part in use.
        let m = &self.maps;
        let n = m.ctl(C_NODES);
        CTL_SIZE + n * (NODE_SIZE as u64 + 8 + 4) + m.ctl(C_OUT_TAIL) + m.ctl(C_INX_TAIL) * 8 + (n as f64 / 0.65) as u64 * SLOT as u64
            + if m.fblocks != 0 { n * FILTER_BITS / 8 } else { 0 } + self.wal_len
    }
    fn physical_bytes_written(&mut self) -> Option<u64> { Some(self.phys_written + self.dirty.len() as u64 * 4096) }
    fn settle(&mut self) { self.checkpoint(); let _ = &self.dir; }
    fn extra_stats(&mut self) -> String {
        format!("{{\"wal_bytes\":{},\"checkpoints\":{},\"checkpoint_pages\":{},\"checkpoint_ms\":{:.1},\"frames_replayed_at_open\":{}}}",
                self.wal_total, self.ckpts, self.ckpt_pages, self.ckpt_us as f64 / 1e3, self.recovery_frames)
    }
}
