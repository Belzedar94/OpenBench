//! The writer's overlay: the committed graph plus one batch's private changes.
//!
//! The rules read and write through `GraphRead`/`GraphWrite` on a `Batch`.
//! Nothing touches the mapped files until `Store::commit`, which turns the
//! batch into redo records (`finalize`), logs them, fsyncs and only then
//! applies them.  Iteration is always over ordered maps or vectors, so the
//! redo records of a batch are a pure function of the committed state and the
//! calls made on it.

use crate::layout::*;
use crate::mapped::Maps;
use crate::view::{Shard, View, filter_bits, shard_of};
use av2_model::fen::FEN_SIZE;
use av2_model::graph::GraphError;
use av2_model::id::{get_u40, put_u40};
use av2_model::record::{CORE_SIZE, EXT_SIZE};
use av2_model::{BlobRef, Core, Edge, Ext, Fingerprint, GraphRead, GraphWrite, MAX_ID, PackedFen, PosId};
use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;

/// Fingerprints are uniformly random (sha256 prefixes): their first 8 bytes
/// are already a good hash, so the overlay's maps skip SipHash.
#[derive(Default, Clone, Copy)]
pub(crate) struct FpHasher(u64);

impl std::hash::Hasher for FpHasher {
    #[inline]
    fn finish(&self) -> u64 {
        self.0
    }
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        let mut a = [0u8; 8];
        let n = bytes.len().min(8);
        a[..n].copy_from_slice(&bytes[..n]);
        self.0 = (self.0.rotate_left(5) ^ u64::from_le_bytes(a)).wrapping_mul(0x9E37_79B9_7F4A_7C15);
    }
}

pub(crate) type FpBuild = std::hash::BuildHasherDefault<FpHasher>;

pub struct Batch {
    pub(crate) maps: Arc<Maps>,
    shard_bits: u32,
    n0: u64,
    blob0: u64,
    compact_min: u64,
    new_cores: Vec<Core>,
    new_fens: Vec<PackedFen>,
    new_fps: Vec<Fingerprint>,
    keys: HashMap<Fingerprint, u64, FpBuild>,
    cores: BTreeMap<u64, Core>,
    exts: BTreeMap<u64, Ext>,
    ext_tail: u64,
    children: BTreeMap<u64, Vec<Edge>>,
    /// First parent (id + 1, 0 = none) of each new position.
    new_first_parent: Vec<u64>,
    /// Every other parent added in this batch, in the order added: all of
    /// them for existing positions, the second and later for new ones.
    more_parents: BTreeMap<u64, Vec<u64>>,
    blobs: BTreeMap<u64, Vec<u8>>,
    blob_tail: u64,
    /// A batch logged but not applied yet (its fsync is in flight): this one
    /// reads through it.  See `Store::commit_start`.
    prev: Option<Arc<Batch>>,
}

/// One redo record inside a frame body.
#[derive(Clone, Copy, Debug)]
pub(crate) struct OpRef {
    pub file: u8,
    pub off: u64,
    pub at: usize,
    pub len: usize,
}

/// A batch turned into bytes: the frame body (sequence number, facts, redo
/// records).  Everything, including a rebuilt or compacted key map, is in the
/// redo records, so the log alone can rebuild any change (backups rely on it).
pub(crate) struct Finalized {
    pub body: Vec<u8>,
    pub ops: Vec<OpRef>,
    /// Minimum length each file must have before the ops are applied.
    pub need: [u64; NFILES],
    pub edges: u64,
}

pub(crate) const BODY_HEADER: usize = 12; // seq u64 + facts length u32

impl Batch {
    pub(crate) fn new(maps: Arc<Maps>, compact_min: u64) -> Batch {
        let v = View::new(&maps);
        let (n0, ext0, blob0, shard_bits) = (v.counter(C_POSITIONS), v.counter(C_EXTS), v.counter(C_BLOB_TAIL), v.shard_bits);
        Batch {
            maps,
            shard_bits,
            n0,
            blob0,
            compact_min,
            new_cores: Vec::new(),
            new_fens: Vec::new(),
            new_fps: Vec::new(),
            keys: HashMap::default(),
            cores: BTreeMap::new(),
            exts: BTreeMap::new(),
            ext_tail: ext0,
            children: BTreeMap::new(),
            new_first_parent: Vec::new(),
            more_parents: BTreeMap::new(),
            blobs: BTreeMap::new(),
            blob_tail: blob0,
            prev: None,
        }
    }

    /// An overlay on top of `prev`, a batch whose frame is written but not yet
    /// applied.  It must be finalized only after `prev` is applied.
    pub(crate) fn new_after(prev: Arc<Batch>, maps: Arc<Maps>, compact_min: u64) -> Batch {
        let mut b = Batch::new(maps, compact_min);
        b.n0 = prev.len();
        b.ext_tail = prev.ext_tail;
        b.blob0 = prev.blob_tail;
        b.blob_tail = prev.blob_tail;
        b.prev = Some(prev);
        b
    }

    /// The overlay this one reads through, dropped once it is applied.
    pub(crate) fn detach(&mut self) {
        self.prev = None;
    }

    fn base_core(&self, id: PosId) -> Core {
        match &self.prev {
            Some(p) => p.core(id),
            None => self.view().core(id),
        }
    }

    fn ext_at(&self, idx: u64) -> Option<Ext> {
        if let Some(e) = self.exts.get(&idx) {
            return Some(*e);
        }
        match &self.prev {
            Some(p) => p.ext_at(idx),
            None => self.view().ext_at(idx),
        }
    }

    #[inline]
    fn view(&self) -> View<'_> {
        View { maps: &self.maps, shard_bits: self.shard_bits }
    }

    /// True when the batch changes nothing (a frame is still logged if the
    /// caller has facts).
    pub fn is_empty(&self) -> bool {
        self.new_cores.is_empty() && self.cores.is_empty() && self.exts.is_empty() && self.children.is_empty() && self.blobs.is_empty()
    }

    pub fn new_positions(&self) -> u64 {
        self.new_cores.len() as u64
    }

    fn check(&self, id: PosId) -> Result<(), GraphError> {
        if id.0 < self.len() { Ok(()) } else { Err(GraphError::NoSuchPosition(id)) }
    }

    fn core_mut(&mut self, id: u64) -> &mut Core {
        if id >= self.n0 {
            return &mut self.new_cores[(id - self.n0) as usize];
        }
        if !self.cores.contains_key(&id) {
            let c = self.base_core(PosId(id));
            self.cores.insert(id, c);
        }
        self.cores.get_mut(&id).unwrap()
    }

    fn add_parent(&mut self, child: u64, parent: u64) {
        if child >= self.n0 {
            let slot = &mut self.new_first_parent[(child - self.n0) as usize];
            if *slot == 0 {
                *slot = parent + 1;
                return;
            }
        }
        self.more_parents.entry(child).or_default().push(parent);
    }

    // ------------------------------------------------------------ finalize

    /// Turn the batch into a frame body for sequence number `seq`.
    pub(crate) fn finalize(&self, seq: u64, facts: &[u8]) -> Result<Finalized, GraphError> {
        let v = View { maps: &self.maps, shard_bits: self.shard_bits };
        if self.prev.is_some() || v.counter(C_POSITIONS) != self.n0 {
            return Err(GraphError::Store("batch finalized before the batch it was built on was applied".into()));
        }
        let n_new = self.new_cores.len();
        let mut f = Finalized {
            body: Vec::with_capacity(
                BODY_HEADER
                    + facts.len()
                    + n_new * (CORE_SIZE + FEN_SIZE + PARENT_SIZE + INSERT_LEN + 16)
                    + self.blobs.values().map(|b| b.len() + 16).sum::<usize>()
                    + self.cores.len() * (CORE_SIZE + 8)
                    + self.children.values().map(|e| e.len() * EDGE_SIZE + 24).sum::<usize>()
                    + 1024,
            ),
            ops: Vec::with_capacity(16 + n_new * 2 + self.cores.len()),
            need: [0; NFILES],
            edges: 0,
        };
        f.body.extend_from_slice(&seq.to_le_bytes());
        f.body.extend_from_slice(&(facts.len() as u32).to_le_bytes());
        f.body.extend_from_slice(facts);
        let n0 = self.n0;
        let n1 = n0 + n_new as u64;
        if n1 > MAX_ID + 1 {
            return Err(GraphError::Store("40-bit id space exhausted".into()));
        }

        // 1. New positions: one run of core records and one of packed FENs.
        if n_new > 0 {
            let mut run = vec![0u8; n_new * CORE_SIZE];
            for (i, c) in self.new_cores.iter().enumerate() {
                let mut c = *c;
                c.version = seq;
                c.encode(&mut run[i * CORE_SIZE..]);
            }
            op(&mut f, F_CORE, n0 * CORE_SIZE as u64, &run);
            // FENs: the span from the first to the last new position that has
            // one (the column is zero, i.e. "none", everywhere else).
            let first = self.new_fens.iter().position(|p| !p.is_none());
            let last = self.new_fens.iter().rposition(|p| !p.is_none());
            if let (Some(a), Some(z)) = (first, last) {
                let mut fr = vec![0u8; (z + 1 - a) * FEN_SIZE];
                for (i, p) in self.new_fens[a..=z].iter().enumerate() {
                    fr[i * FEN_SIZE..(i + 1) * FEN_SIZE].copy_from_slice(&p.0);
                }
                op(&mut f, F_FEN, (n0 + a as u64) * FEN_SIZE as u64, &fr);
            }
            need(&mut f, F_FEN, n1 * FEN_SIZE as u64);
            // The out column of new positions starts at zero (the files are
            // zero-filled and only ever written by applied frames), but the
            // file must reach that far.
            need(&mut f, F_OUT, n1 * OUT_SIZE as u64);
        }
        // 2. Rewrites of existing positions, in id order.
        for (id, c) in &self.cores {
            let mut c = *c;
            c.version = seq;
            op(&mut f, F_CORE, id * CORE_SIZE as u64, &c.to_bytes());
        }
        // 3. Ext records, in index order.
        for (idx, e) in &self.exts {
            op(&mut f, F_EXT, idx * EXT_SIZE as u64, &e.to_bytes());
        }
        // 4. Blobs, in offset order.
        for (off, b) in &self.blobs {
            let mut rec = Vec::with_capacity(4 + b.len());
            rec.extend_from_slice(&(b.len() as u32).to_le_bytes());
            rec.extend_from_slice(b);
            op(&mut f, F_BLOB_ARENA, *off, &rec);
        }
        // 5. Out-edge blocks, appended in parent order.
        let mut out_tail = v.counter(C_OUT_TAIL);
        let mut block = Vec::new();
        for (p, es) in &self.children {
            block.clear();
            block.extend_from_slice(&(es.len() as u16).to_le_bytes());
            for e in es {
                let mut c = [0u8; EDGE_SIZE];
                put_u40(&mut c, e.child.0);
                c[5..7].copy_from_slice(&e.mv.0.to_le_bytes());
                block.extend_from_slice(&c);
            }
            op(&mut f, F_OUT_ARENA, out_tail, &block);
            let mut w = [0u8; OUT_SIZE];
            put_u40(&mut w, out_tail + 1);
            op(&mut f, F_OUT, p * OUT_SIZE as u64, &w);
            out_tail += block.len() as u64;
            f.edges += es.len() as u64;
        }
        // 6. Parent lists.  Existing positions one record each, in id order;
        //    new positions one run (they almost all have exactly one parent).
        let mut inx_tail = v.counter(C_INX_TAIL);
        let mut push_parent = |f: &mut Finalized, cur: u64, p: u64| -> u64 {
            if cur == 0 {
                p + 1
            } else if cur & PARENT_LIST == 0 {
                let mut cells = [0u8; 2 * INX_CELL];
                put_u40(&mut cells[0..5], cur - 1);
                put_u40(&mut cells[10..15], p);
                put_u40(&mut cells[15..20], inx_tail);
                op(f, F_INX_ARENA, inx_tail * INX_CELL as u64, &cells);
                inx_tail += 2;
                PARENT_LIST | (inx_tail - 1)
            } else {
                let mut cell = [0u8; INX_CELL];
                put_u40(&mut cell[0..5], p);
                put_u40(&mut cell[5..10], cur & !PARENT_LIST);
                op(f, F_INX_ARENA, inx_tail * INX_CELL as u64, &cell);
                inx_tail += 1;
                PARENT_LIST | (inx_tail - 1)
            }
        };
        for (child, ps) in self.more_parents.range(..n0) {
            let mut cur = v.parent_word(*child);
            for &p in ps {
                cur = push_parent(&mut f, cur, p);
            }
            let mut w = [0u8; PARENT_SIZE];
            put_u40(&mut w, cur);
            op(&mut f, F_PARENT, child * PARENT_SIZE as u64, &w);
        }
        if n_new > 0 {
            let mut run = vec![0u8; n_new * PARENT_SIZE];
            for (i, &first) in self.new_first_parent.iter().enumerate() {
                let child = n0 + i as u64;
                let mut cur = first;
                if let Some(ps) = self.more_parents.get(&child) {
                    for &p in ps {
                        cur = push_parent(&mut f, cur, p);
                    }
                }
                put_u40(&mut run[i * PARENT_SIZE..], cur);
            }
            op(&mut f, F_PARENT, n0 * PARENT_SIZE as u64, &run);
        }
        // 7. Key map: new fingerprints, by shard then id.
        let mut km_tail = v.counter(C_KM_TAIL);
        let mut km_live = v.counter(C_KM_LIVE);
        let mut keyed: Vec<(u64, u64)> = (0..n_new).map(|i| (shard_of(&self.new_fps[i], self.shard_bits), i as u64)).collect();
        keyed.sort_unstable();
        let mut post: Vec<(u64, Shard)> = Vec::new();
        let mut taken: Vec<u64> = Vec::new();
        let mut i = 0usize;
        while i < keyed.len() {
            let s = keyed[i].0;
            let mut j = i;
            while j < keyed.len() && keyed[j].0 == s {
                j += 1;
            }
            let keys = &keyed[i..j];
            i = j;
            let sh = v.shard(s);
            let total = sh.count + keys.len() as u64;
            // A shard past its maximal load is rebuilt, twice as large, into a
            // fresh region at the arena tail.  The frame logs the rebuild as one
            // logical record naming the old region and the new one: replay
            // recomputes the new region from the old one, which nothing writes
            // after the rebuild, so the record can be replayed over a state
            // that is already ahead of it (as recovery from a checkpoint does).
            let mut region_mem: Option<Vec<u8>> = None;
            let cur = if sh.slots == 0 || total * MAX_LOAD_DEN > sh.slots * MAX_LOAD_NUM {
                let slots = ((total as f64 / REBUILD_LOAD).ceil() as u64).max(MIN_SLOTS);
                let (nsh, reg) = rebuild_region(v.maps, self.shard_bits, &sh, km_tail, slots)?;
                op_rebuild(&mut f, s, &sh, km_tail, slots);
                km_tail += reg.len() as u64;
                km_live = km_live + reg.len() as u64 - if sh.slots > 0 { region_bytes(sh.slots) } else { 0 };
                region_mem = Some(reg);
                nsh
            } else {
                sh
            };
            let a = v.maps.f(F_KM_ARENA);
            let base = cur.slots_at();
            let slot_used = |k: u64| -> Result<bool, GraphError> {
                Ok(match &region_mem {
                    Some(r) => {
                        let at = (base - cur.region) as usize + k as usize * SLOT;
                        r[at + 12..at + 17] != [0u8; 5]
                    }
                    None => {
                        let b =
                            a.get(base + k * SLOT as u64, SLOT).ok_or_else(|| GraphError::Store("key map slot out of bounds".into()))?;
                        get_u40(&b[12..17]) != 0
                    }
                })
            };
            taken.clear();
            for &(_, k) in keys {
                let fp = &self.new_fps[k as usize];
                let mut s_ = cur.home(fp, self.shard_bits);
                while slot_used(s_)? || taken.contains(&s_) {
                    s_ += 1;
                    if s_ == cur.slots {
                        s_ = 0;
                    }
                }
                taken.push(s_);
                op_insert(&mut f, base + s_ * SLOT as u64, fp, n0 + k, cur.filter_block_offset(fp));
            }
            let nsh = Shard { count: total, ..cur };
            op(&mut f, F_KM_DIR, s * DIR_ENTRY as u64 + 12, &(total as u32).to_le_bytes());
            post.push((s, nsh));
        }
        // 8. Counters, one run.
        let mut ctl = [0u8; C_COUNTERS_LEN];
        for (k, val) in [n1, self.ext_tail, out_tail, inx_tail, self.blob_tail, km_tail, km_live].iter().enumerate() {
            ctl[k * 8..k * 8 + 8].copy_from_slice(&val.to_le_bytes());
        }
        op(&mut f, F_CTL, C_COUNTERS as u64, &ctl);
        // 9. Key-map compaction, last: once the old regions left behind by
        //    rebuilds reach half the live ones (with doubling, garbage stays
        //    just under the live size forever, so "garbage > live" would never
        //    fire; at one half the arena stays under 1.5x live), every live region is copied,
        //    packed in shard order, into a new arena file (the next
        //    generation).  Logged as one record naming every live region of
        //    the old generation, which is never written again: replay copies
        //    them again and gets the same file.
        let garbage = km_tail - km_live;
        if garbage * 2 >= km_live && garbage >= self.compact_min {
            let mut live: Vec<(u64, Shard)> = Vec::new();
            for s in 0..(1u64 << self.shard_bits) {
                let sh = match post.binary_search_by_key(&s, |p| p.0) {
                    Ok(k) => post[k].1,
                    Err(_) => v.shard(s),
                };
                if sh.slots > 0 {
                    live.push((s, sh));
                }
            }
            op_compact(&mut f, v.counter(C_KM_GEN), &live);
        }
        Ok(f)
    }
}

fn need(f: &mut Finalized, file: u8, end: u64) {
    if (file as usize) < NFILES && end > f.need[file as usize] {
        f.need[file as usize] = end;
    }
}

/// Append a LEB128 varint.
#[inline]
pub(crate) fn put_varint(b: &mut Vec<u8>, mut v: u64) {
    while v >= 0x80 {
        b.push((v as u8) | 0x80);
        v >>= 7;
    }
    b.push(v as u8);
}

/// Read a LEB128 varint at `*p`, advancing it.
#[inline]
pub(crate) fn get_varint(b: &[u8], p: &mut usize) -> Option<u64> {
    let mut v = 0u64;
    let mut shift = 0;
    loop {
        let c = *b.get(*p)?;
        *p += 1;
        if shift >= 64 {
            return None;
        }
        v |= ((c & 0x7F) as u64) << shift;
        if c & 0x80 == 0 {
            return Some(v);
        }
        shift += 7;
    }
}

/// A redo record: `[file u8][offset varint][length varint][bytes]`.
fn record(f: &mut Finalized, file: u8, off: u64, bytes: &[u8]) {
    f.body.push(file);
    put_varint(&mut f.body, off);
    put_varint(&mut f.body, bytes.len() as u64);
    let at = f.body.len();
    f.body.extend_from_slice(bytes);
    f.ops.push(OpRef { file, off, at, len: bytes.len() });
}

fn op(f: &mut Finalized, file: u8, off: u64, bytes: &[u8]) {
    record(f, file, off, bytes);
    need(f, file, off + bytes.len() as u64);
}

/// The logical record of a key-map insert, with the slot's byte offset as
/// offset: `[fingerprint 12][id+1 u40][filter block offset u64]`.  Applying it
/// writes the slot and sets the key's bits in the filter block, so the filter
/// costs no log bytes; setting bits is idempotent.
fn op_insert(f: &mut Finalized, slot_off: u64, fp: &Fingerprint, id: u64, block_off: u64) {
    let mut b = [0u8; INSERT_LEN];
    b[..12].copy_from_slice(&fp.0);
    put_u40(&mut b[12..17], id + 1);
    b[17..25].copy_from_slice(&block_off.to_le_bytes());
    record(f, F_KM_INSERT, slot_off, &b);
    need(f, F_KM_ARENA, (slot_off + SLOT as u64).max(block_off + 64));
}

pub(crate) const INSERT_LEN: usize = 25;

/// `(slot bytes, filter block offset)` of an insert record.
pub(crate) fn decode_insert(b: &[u8]) -> Option<(&[u8], u64)> {
    (b.len() == INSERT_LEN).then(|| (&b[..SLOT], u64::from_le_bytes(b[17..25].try_into().unwrap())))
}

/// The logical redo record of a shard rebuild, with the shard index as
/// offset: `[old region u64][old slots u64][old count u64][new region u64][new slots u64]`.
fn op_rebuild(f: &mut Finalized, shard: u64, old: &Shard, region: u64, slots: u64) {
    record(f, F_KM_REBUILD, shard, &encode_rebuild(old, region, slots));
    need(f, F_KM_ARENA, region + region_bytes(slots));
}

/// The logical record of a compaction, with the new generation as offset:
/// `[old generation u64][entries u32]` then per live shard
/// `[shard u32][region u64][slots u32][count u32]`, in shard order.
fn op_compact(f: &mut Finalized, old_gen: u64, live: &[(u64, Shard)]) {
    let mut b = Vec::with_capacity(12 + live.len() * 20);
    b.extend_from_slice(&old_gen.to_le_bytes());
    b.extend_from_slice(&(live.len() as u32).to_le_bytes());
    for (s, sh) in live {
        b.extend_from_slice(&(*s as u32).to_le_bytes());
        b.extend_from_slice(&sh.region.to_le_bytes());
        b.extend_from_slice(&(sh.slots as u32).to_le_bytes());
        b.extend_from_slice(&(sh.count as u32).to_le_bytes());
    }
    record(f, F_KM_COMPACT, old_gen + 1, &b);
}

/// `(old generation, live shards)` of a compaction record.
pub(crate) fn decode_compact(b: &[u8]) -> Option<(u64, Vec<(u64, Shard)>)> {
    if b.len() < 12 {
        return None;
    }
    let old = u64::from_le_bytes(b[..8].try_into().unwrap());
    let n = u32::from_le_bytes(b[8..12].try_into().unwrap()) as usize;
    if b.len() != 12 + n * 20 {
        return None;
    }
    let mut v = Vec::with_capacity(n);
    for c in b[12..].as_chunks::<20>().0 {
        let s = u32::from_le_bytes(c[0..4].try_into().unwrap()) as u64;
        let region = u64::from_le_bytes(c[4..12].try_into().unwrap());
        let slots = u32::from_le_bytes(c[12..16].try_into().unwrap()) as u64;
        let count = u32::from_le_bytes(c[16..20].try_into().unwrap()) as u64;
        v.push((s, Shard { region, slots, count }));
    }
    Some((old, v))
}

pub(crate) const REBUILD_LEN: usize = 40;

pub(crate) fn encode_rebuild(old: &Shard, region: u64, slots: u64) -> [u8; REBUILD_LEN] {
    let mut b = [0u8; REBUILD_LEN];
    for (i, v) in [old.region, old.slots, old.count, region, slots].iter().enumerate() {
        b[i * 8..i * 8 + 8].copy_from_slice(&v.to_le_bytes());
    }
    b
}

/// `(old shard, new region, new slots)` of a rebuild record.
pub(crate) fn decode_rebuild(b: &[u8]) -> Option<(Shard, u64, u64)> {
    if b.len() != REBUILD_LEN {
        return None;
    }
    let v = |i: usize| u64::from_le_bytes(b[i * 8..i * 8 + 8].try_into().unwrap());
    Some((Shard { region: v(0), slots: v(1), count: v(2) }, v(3), v(4)))
}

/// Rebuild `old` (a shard as committed in `maps`) into a region of `slots`
/// slots at `region`: its entries, in slot order.  Used by `finalize` and by
/// the apply/replay of the logical record, so both produce the same bytes.
pub(crate) fn rebuild_region(maps: &Maps, shard_bits: u32, old: &Shard, region: u64, slots: u64) -> Result<(Shard, Vec<u8>), GraphError> {
    let mut entries = Vec::with_capacity(old.count as usize);
    if old.slots > 0 {
        let a = maps.f(F_KM_ARENA);
        let base = old.slots_at();
        let b = a.get(base, old.slots as usize * SLOT).ok_or_else(|| GraphError::Store("key map shard out of bounds".into()))?;
        for c in b.as_chunks::<SLOT>().0 {
            let id1 = get_u40(&c[12..17]);
            if id1 != 0 {
                entries.push((Fingerprint(c[..12].try_into().unwrap()), id1 - 1));
            }
        }
    }
    if entries.len() as u64 != old.count {
        return Err(GraphError::Store(format!("key map shard holds {} entries, its directory says {}", entries.len(), old.count)));
    }
    let nsh = Shard { region, slots, count: old.count };
    let r = build_region(&nsh, &entries, shard_bits);
    Ok((nsh, r))
}

/// Bytes of a shard region holding `entries`, inserted in the given order.
pub(crate) fn build_region(sh: &Shard, entries: &[(Fingerprint, u64)], shard_bits: u32) -> Vec<u8> {
    let fbytes = (sh.fblocks() * 64) as usize;
    let mut r = vec![0u8; fbytes + sh.slots as usize * SLOT];
    for (fp, id) in entries {
        let b0 = (sh.filter_block_offset(fp) - sh.region) as usize;
        for b in filter_bits(fp) {
            r[b0 + b as usize / 8] |= 1 << (b % 8);
        }
        let mut s = sh.home(fp, shard_bits);
        loop {
            let at = fbytes + s as usize * SLOT;
            if r[at + 12..at + 17] == [0u8; 5] {
                r[at..at + 12].copy_from_slice(&fp.0);
                put_u40(&mut r[at + 12..at + 17], id + 1);
                break;
            }
            s += 1;
            if s == sh.slots {
                s = 0;
            }
        }
    }
    r
}

impl GraphRead for Batch {
    fn len(&self) -> u64 {
        self.n0 + self.new_cores.len() as u64
    }
    fn core(&self, id: PosId) -> Core {
        if id.0 >= self.n0 {
            return self.new_cores.get((id.0 - self.n0) as usize).copied().unwrap_or_default();
        }
        self.cores.get(&id.0).copied().unwrap_or_else(|| self.base_core(id))
    }
    fn ext(&self, id: PosId) -> Option<Ext> {
        let i = self.core(id).ext_index()?;
        self.ext_at(i)
    }
    fn children(&self, id: PosId, out: &mut Vec<Edge>) {
        match self.children.get(&id.0) {
            Some(es) => out.extend_from_slice(es),
            None if id.0 < self.n0 => match &self.prev {
                Some(p) => p.children(id, out),
                None => self.view().children(id, out),
            },
            None => {}
        }
    }
    fn parents(&self, id: PosId, out: &mut Vec<PosId>) {
        if id.0 < self.n0 {
            match &self.prev {
                Some(p) => p.parents(id, out),
                None => self.view().parents(id, out),
            }
        } else if let Some(&first) = self.new_first_parent.get((id.0 - self.n0) as usize)
            && first != 0
        {
            out.push(PosId(first - 1));
        }
        if let Some(ps) = self.more_parents.get(&id.0) {
            out.extend(ps.iter().map(|&p| PosId(p)));
        }
    }
    fn lookup(&self, fp: &Fingerprint) -> Option<PosId> {
        self.keys.get(fp).map(|&id| PosId(id)).or_else(|| match &self.prev {
            Some(p) => p.lookup(fp),
            None => self.view().lookup_committed(fp),
        })
    }
    fn fen(&self, id: PosId) -> Option<String> {
        if id.0 >= self.n0 {
            let p = self.new_fens.get((id.0 - self.n0) as usize)?;
            return match p.slot() {
                av2_model::fen::FenSlot::Blob(r) => self.blob(r).and_then(|b| String::from_utf8(b).ok()),
                _ => self.view().fen_of(p),
            };
        }
        match &self.prev {
            Some(p) => p.fen(id),
            None => self.view().fen(id),
        }
    }
    fn blob(&self, r: BlobRef) -> Option<Vec<u8>> {
        if r.0 >= self.blob0 {
            return self.blobs.get(&r.0).cloned();
        }
        match &self.prev {
            Some(p) => p.blob(r),
            None => self.view().blob_at(r),
        }
    }
}

impl GraphWrite for Batch {
    fn create(&mut self, fp: Fingerprint, fen: Option<&str>, core: Core) -> Result<PosId, GraphError> {
        if let Some(id) = self.lookup(&fp) {
            return Err(GraphError::Duplicate(id));
        }
        let id = self.len();
        let packed = match fen {
            None => PackedFen::default(),
            Some(s) => match PackedFen::pack(s) {
                Some(p) => p,
                None => PackedFen::blob(self.put_blob(s.as_bytes())?),
            },
        };
        self.new_cores.push(Core { ext: 0, version: 0, ..core });
        self.new_fens.push(packed);
        self.new_fps.push(fp);
        self.new_first_parent.push(0);
        self.keys.insert(fp, id);
        Ok(PosId(id))
    }

    fn put_core(&mut self, id: PosId, core: Core) -> Result<(), GraphError> {
        self.check(id)?;
        let c = self.core_mut(id.0);
        *c = Core { ext: c.ext, version: c.version, ..core };
        Ok(())
    }

    fn put_ext(&mut self, id: PosId, ext: Ext) -> Result<(), GraphError> {
        self.check(id)?;
        let idx = match self.core(id).ext_index() {
            Some(i) => i,
            None => {
                let i = self.ext_tail;
                self.ext_tail += 1;
                if self.ext_tail > MAX_ID {
                    return Err(GraphError::Store("ext index space exhausted".into()));
                }
                self.core_mut(id.0).ext = i + 1;
                i
            }
        };
        // Touch the core so that its version records the change.
        let _ = self.core_mut(id.0);
        self.exts.insert(idx, ext);
        Ok(())
    }

    fn set_children(&mut self, parent: PosId, edges: &[Edge]) -> Result<(), GraphError> {
        self.check(parent)?;
        if edges.len() > u16::MAX as usize {
            return Err(GraphError::TooManyEdges(edges.len()));
        }
        let n = self.len();
        if let Some(e) = edges.iter().find(|e| e.child.0 >= n) {
            return Err(GraphError::NoSuchPosition(e.child));
        }
        let mut old = Vec::new();
        self.children(parent, &mut old);
        // Edges are never deleted (v1 never deletes them either).
        if let Some(e) = old.iter().find(|o| !edges.iter().any(|x| x.child == o.child && x.mv == o.mv)) {
            return Err(GraphError::Store(format!("set_children of {parent} drops the edge {:?} -> {}", e.mv, e.child)));
        }
        for (k, e) in edges.iter().enumerate() {
            if old.iter().any(|x| x.child == e.child) || edges[..k].iter().any(|x| x.child == e.child) {
                continue;
            }
            self.add_parent(e.child.0, parent.0);
        }
        self.children.insert(parent.0, edges.to_vec());
        Ok(())
    }

    fn put_blob(&mut self, bytes: &[u8]) -> Result<BlobRef, GraphError> {
        if bytes.len() > u32::MAX as usize {
            return Err(GraphError::Store("blob larger than 4 GiB".into()));
        }
        let off = self.blob_tail;
        self.blob_tail += 4 + bytes.len() as u64;
        self.blobs.insert(off, bytes.to_vec());
        Ok(BlobRef(off))
    }
}
