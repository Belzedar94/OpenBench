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
    /// A compaction may start in this batch (see `finalize`, step 8).
    pub(crate) can_compact: bool,
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
            can_compact: false,
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
        // 7. Key map: new fingerprints, by shard then id.  The current
        //    generation's arena is in the slot of its parity; while a
        //    compaction runs, the next generation fills the other slot.
        let km_gen = v.counter(C_KM_GEN);
        let cur_odd = km_gen % 2 == 1;
        let next_odd = !cur_odd;
        let mut cursor = v.counter(C_KM_CURSOR);
        let mut km_tail = v.counter(C_KM_TAIL);
        let mut km_live = v.counter(C_KM_LIVE);
        let mut next_tail = v.counter(C_KM_NEXT_TAIL);
        let mut next_live = v.counter(C_KM_NEXT_LIVE);
        let (tail0, next_tail0, cursor0) = (km_tail, next_tail, cursor);
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
            // fresh region at the tail of the current arena (of the next one
            // while a compaction runs).  The frame logs the rebuild as one
            // logical record naming the old region and the new one: replay
            // recomputes the new region from the old one, which nothing writes
            // after the rebuild, so the record can be replayed over a state
            // that is already ahead of it (as recovery from a checkpoint does).
            let mut region_mem: Option<Vec<u8>> = None;
            let cur = if sh.slots == 0 || total * MAX_LOAD_DEN > sh.slots * MAX_LOAD_NUM {
                let slots = ((total as f64 / REBUILD_LOAD).ceil() as u64).max(MIN_SLOTS);
                let into_next = cursor != 0;
                let at = Shard {
                    region: if into_next { next_tail } else { km_tail },
                    slots,
                    count: sh.count,
                    odd: if into_next { next_odd } else { cur_odd },
                };
                let (nsh, reg) = rebuild_region(v.maps, self.shard_bits, &sh, &at)?;
                op_rebuild(&mut f, s, &sh, &at);
                let n = reg.len() as u64;
                if into_next {
                    next_tail += n;
                    next_live += n;
                } else {
                    km_tail += n;
                    km_live += n;
                }
                if sh.slots > 0 {
                    if sh.odd == cur_odd {
                        km_live -= region_bytes(sh.slots);
                    } else {
                        next_live -= region_bytes(sh.slots);
                    }
                }
                region_mem = Some(reg);
                nsh
            } else {
                sh
            };
            let a = v.maps.f(cur.file());
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
                op_insert(&mut f, cur.odd, base + s_ * SLOT as u64, fp, n0 + k, cur.filter_block_offset(fp));
            }
            let nsh = Shard { count: total, ..cur };
            op(&mut f, F_KM_DIR, s * DIR_ENTRY as u64 + 12, &(total as u32).to_le_bytes());
            post.push((s, nsh));
        }
        // 8. Key-map compaction, incremental: once the old regions left
        //    behind by rebuilds reach half the live ones (with doubling,
        //    garbage stays just under the live size forever, so "garbage >
        //    live" would never fire), the live regions move into a new arena
        //    file (the next generation), at most `MOVE_BUDGET` bytes per
        //    commit, so the writer never stops for the whole copy.  Each move
        //    is a logical record naming the old region, which nothing writes
        //    afterwards: replay copies it again.  A compaction starts only
        //    once the last checkpoint is past the previous one (`can_compact`),
        //    so no recovery can need the file its slot reuses.
        let nshards = 1u64 << self.shard_bits;
        let garbage = km_tail - km_live;
        if cursor == 0 && self.can_compact && garbage * 2 >= km_live && garbage >= self.compact_min {
            record(&mut f, F_KM_MIGRATE_START, km_gen + 1, &[]);
            cursor = 1;
            next_tail = 0;
            next_live = 0;
        }
        let mut finish = false;
        if cursor != 0 {
            let mut moved = 0u64;
            while cursor - 1 < nshards && (moved == 0 || moved < MOVE_BUDGET) {
                let s = cursor - 1;
                let k = post.binary_search_by_key(&s, |p| p.0);
                let sh = match k {
                    Ok(k) => post[k].1,
                    Err(_) => v.shard(s),
                };
                if sh.slots > 0 && sh.odd != next_odd {
                    let to = Shard { region: next_tail, odd: next_odd, ..sh };
                    op_move(&mut f, s, &sh, &to);
                    let n = region_bytes(sh.slots);
                    next_tail += n;
                    next_live += n;
                    km_live -= n;
                    moved += n;
                    match k {
                        Ok(k) => post[k].1 = to,
                        Err(k) => post.insert(k, (s, to)),
                    }
                }
                cursor += 1;
            }
            finish = cursor - 1 == nshards;
        }
        // 9. Counters, one run (and the compaction's, if one runs).
        let mut ctl = [0u8; C_COUNTERS_LEN];
        for (k, val) in [n1, self.ext_tail, out_tail, inx_tail, self.blob_tail, km_tail, km_live].iter().enumerate() {
            ctl[k * 8..k * 8 + 8].copy_from_slice(&val.to_le_bytes());
        }
        op(&mut f, F_CTL, C_COUNTERS as u64, &ctl);
        if cursor != cursor0 || next_tail != next_tail0 || cursor != 0 {
            let mut m = [0u8; 24];
            m[0..8].copy_from_slice(&next_tail.to_le_bytes());
            m[8..16].copy_from_slice(&next_live.to_le_bytes());
            m[16..24].copy_from_slice(&cursor.to_le_bytes());
            op(&mut f, F_CTL, C_KM_NEXT_TAIL as u64, &m);
        }
        if finish {
            // Last: the next generation becomes the current one (its tail and
            // live bytes become the counters', the compaction state clears).
            record(&mut f, F_KM_MIGRATE_END, km_gen + 1, &[]);
        }
        let _ = tail0;
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
/// offset: `[fingerprint 12][id+1 u40][filter block offset u64]`, kind
/// `F_KM_INSERT` or `F_KM_INSERT_ODD` by the arena slot.  Applying it writes
/// the slot and sets the key's bits in the filter block, so the filter costs
/// no log bytes; setting bits is idempotent.
fn op_insert(f: &mut Finalized, odd: bool, slot_off: u64, fp: &Fingerprint, id: u64, block_off: u64) {
    let mut b = [0u8; INSERT_LEN];
    b[..12].copy_from_slice(&fp.0);
    put_u40(&mut b[12..17], id + 1);
    b[17..25].copy_from_slice(&block_off.to_le_bytes());
    let (kind, file) = if odd { (F_KM_INSERT_ODD, F_KM_ARENA_ODD) } else { (F_KM_INSERT, F_KM_ARENA) };
    record(f, kind, slot_off, &b);
    need(f, file, (slot_off + SLOT as u64).max(block_off + 64));
}

pub(crate) const INSERT_LEN: usize = 25;

/// `(slot bytes, filter block offset)` of an insert record.
pub(crate) fn decode_insert(b: &[u8]) -> Option<(&[u8], u64)> {
    (b.len() == INSERT_LEN).then(|| (&b[..SLOT], u64::from_le_bytes(b[17..25].try_into().unwrap())))
}

/// The logical redo record of a shard rebuild, with the shard index as
/// offset: `[old region u64][old slots u64][old count u64][new region u64][new slots u64]`
/// (regions with `REGION_ODD` for the odd arena slot).
fn op_rebuild(f: &mut Finalized, shard: u64, old: &Shard, to: &Shard) {
    record(f, F_KM_REBUILD, shard, &encode_two(old, to));
    need(f, to.file(), to.region + region_bytes(to.slots));
}

/// A shard moved into the next generation: same layout as a rebuild record
/// (the slot count does not change).
fn op_move(f: &mut Finalized, shard: u64, old: &Shard, to: &Shard) {
    record(f, F_KM_MOVE, shard, &encode_two(old, to));
    need(f, to.file(), to.region + region_bytes(to.slots));
}

pub(crate) const REBUILD_LEN: usize = 40;

fn tagged(sh: &Shard) -> u64 {
    sh.region | if sh.odd { REGION_ODD } else { 0 }
}

fn untagged(r: u64, slots: u64, count: u64) -> Shard {
    Shard { region: r & !REGION_ODD, slots, count, odd: r & REGION_ODD != 0 }
}

pub(crate) fn encode_two(old: &Shard, to: &Shard) -> [u8; REBUILD_LEN] {
    let mut b = [0u8; REBUILD_LEN];
    for (i, v) in [tagged(old), old.slots, old.count, tagged(to), to.slots].iter().enumerate() {
        b[i * 8..i * 8 + 8].copy_from_slice(&v.to_le_bytes());
    }
    b
}

/// `(old shard, new shard)` of a rebuild or move record (the new shard's
/// count is the old one's).
pub(crate) fn decode_two(b: &[u8]) -> Option<(Shard, Shard)> {
    if b.len() != REBUILD_LEN {
        return None;
    }
    let v = |i: usize| u64::from_le_bytes(b[i * 8..i * 8 + 8].try_into().unwrap());
    Some((untagged(v(0), v(1), v(2)), untagged(v(3), v(4), v(2))))
}

/// Rebuild `old` (a shard as committed in `maps`) into the region `to` (its
/// slot count, offset and arena slot): its entries, in slot order.  Used by `finalize` and by
/// the apply/replay of the logical record, so both produce the same bytes.
pub(crate) fn rebuild_region(maps: &Maps, shard_bits: u32, old: &Shard, to: &Shard) -> Result<(Shard, Vec<u8>), GraphError> {
    let mut entries = Vec::with_capacity(old.count as usize);
    if old.slots > 0 {
        let a = maps.f(old.file());
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
    let nsh = Shard { count: old.count, ..*to };
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
