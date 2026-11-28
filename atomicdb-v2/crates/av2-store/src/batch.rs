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
use crate::view::{Shard, View, shard_of};
use av2_model::fen::FEN_SIZE;
use av2_model::graph::GraphError;
use av2_model::id::{get_u40, put_u40};
use av2_model::record::{CORE_SIZE, EXT_SIZE};
use av2_model::{BlobRef, Core, Edge, Ext, Fingerprint, GraphRead, GraphWrite, MAX_ID, PackedFen, PosId};
use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;

pub struct Batch {
    pub(crate) maps: Arc<Maps>,
    shard_bits: u32,
    n0: u64,
    blob0: u64,
    new_cores: Vec<Core>,
    new_fens: Vec<PackedFen>,
    new_fps: Vec<Fingerprint>,
    keys: HashMap<Fingerprint, u64>,
    cores: BTreeMap<u64, Core>,
    exts: BTreeMap<u64, Ext>,
    ext_tail: u64,
    children: BTreeMap<u64, Vec<Edge>>,
    new_parents: BTreeMap<u64, Vec<u64>>,
    blobs: BTreeMap<u64, Vec<u8>>,
    blob_tail: u64,
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
/// records).  Everything, including a rebuilt key-map shard, is a redo
/// record, so the log alone can rebuild any change (backups rely on it).
pub(crate) struct Finalized {
    pub body: Vec<u8>,
    pub ops: Vec<OpRef>,
    /// Minimum length each file must have before the ops are applied.
    pub need: [u64; NFILES],
    pub edges: u64,
}

pub(crate) const BODY_HEADER: usize = 12; // seq u64 + facts length u32

impl Batch {
    pub(crate) fn new(maps: Arc<Maps>) -> Batch {
        let v = View::new(&maps);
        let (n0, ext0, blob0, shard_bits) = (v.counter(C_POSITIONS), v.counter(C_EXTS), v.counter(C_BLOB_TAIL), v.shard_bits);
        Batch {
            maps,
            shard_bits,
            n0,
            blob0,
            new_cores: Vec::new(),
            new_fens: Vec::new(),
            new_fps: Vec::new(),
            keys: HashMap::new(),
            cores: BTreeMap::new(),
            exts: BTreeMap::new(),
            ext_tail: ext0,
            children: BTreeMap::new(),
            new_parents: BTreeMap::new(),
            blobs: BTreeMap::new(),
            blob_tail: blob0,
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
            let c = self.view().core(PosId(id));
            self.cores.insert(id, c);
        }
        self.cores.get_mut(&id).unwrap()
    }

    // ------------------------------------------------------------ finalize

    /// Turn the batch into a frame body for sequence number `seq`.
    pub(crate) fn finalize(self, seq: u64, facts: &[u8]) -> Result<Finalized, GraphError> {
        let v = View { maps: &self.maps, shard_bits: self.shard_bits };
        let mut f = Finalized { body: Vec::with_capacity(BODY_HEADER + facts.len() + 256), ops: Vec::new(), need: [0; NFILES], edges: 0 };
        f.body.extend_from_slice(&seq.to_le_bytes());
        f.body.extend_from_slice(&(facts.len() as u32).to_le_bytes());
        f.body.extend_from_slice(facts);
        let n0 = self.n0;
        let n1 = n0 + self.new_cores.len() as u64;
        if n1 > MAX_ID + 1 {
            return Err(GraphError::Store("40-bit id space exhausted".into()));
        }

        // 1. New positions: one run of core records and one of packed FENs.
        if !self.new_cores.is_empty() {
            let mut run = vec![0u8; self.new_cores.len() * CORE_SIZE];
            for (i, c) in self.new_cores.iter().enumerate() {
                let mut c = *c;
                c.version = seq;
                c.encode(&mut run[i * CORE_SIZE..]);
            }
            op(&mut f, F_CORE, n0 * CORE_SIZE as u64, &run);
            let mut fr = vec![0u8; self.new_fens.len() * FEN_SIZE];
            for (i, p) in self.new_fens.iter().enumerate() {
                fr[i * FEN_SIZE..(i + 1) * FEN_SIZE].copy_from_slice(&p.0);
            }
            op(&mut f, F_FEN, n0 * FEN_SIZE as u64, &fr);
            // The out and parent columns of new positions start at zero (the
            // files are zero-filled and only ever written by applied frames),
            // but the file must reach that far.
            need(&mut f, F_OUT, n1 * OUT_SIZE as u64);
            need(&mut f, F_PARENT, n1 * PARENT_SIZE as u64);
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
        // 6. Parent lists, in child order; new parents in the order they came.
        let mut inx_tail = v.counter(C_INX_TAIL);
        for (child, ps) in &self.new_parents {
            let mut cur = if *child < n0 { v.parent_word(*child) } else { 0 };
            for &p in ps {
                cur = if cur == 0 {
                    p + 1
                } else if cur & PARENT_LIST == 0 {
                    let mut cells = [0u8; 2 * INX_CELL];
                    put_u40(&mut cells[0..5], cur - 1);
                    put_u40(&mut cells[10..15], p);
                    put_u40(&mut cells[15..20], inx_tail);
                    op(&mut f, F_INX_ARENA, inx_tail * INX_CELL as u64, &cells);
                    inx_tail += 2;
                    PARENT_LIST | (inx_tail - 1)
                } else {
                    let mut cell = [0u8; INX_CELL];
                    put_u40(&mut cell[0..5], p);
                    put_u40(&mut cell[5..10], cur & !PARENT_LIST);
                    op(&mut f, F_INX_ARENA, inx_tail * INX_CELL as u64, &cell);
                    inx_tail += 1;
                    PARENT_LIST | (inx_tail - 1)
                };
            }
            let mut w = [0u8; PARENT_SIZE];
            put_u40(&mut w, cur);
            op(&mut f, F_PARENT, child * PARENT_SIZE as u64, &w);
        }
        // 7. Key map: new fingerprints, grouped by shard.
        let mut km_tail = v.counter(C_KM_TAIL);
        let mut by_shard: BTreeMap<u64, Vec<(Fingerprint, u64)>> = BTreeMap::new();
        for (i, fp) in self.new_fps.iter().enumerate() {
            by_shard.entry(shard_of(fp, self.shard_bits)).or_default().push((*fp, n0 + i as u64));
        }
        for (s, keys) in &by_shard {
            let sh = v.shard(*s);
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
                op_rebuild(&mut f, *s, &sh, km_tail, slots);
                km_tail += reg.len() as u64;
                region_mem = Some(reg);
                nsh
            } else {
                sh
            };
            let a = v.maps.f(F_KM_ARENA);
            let base = cur.slots_at();
            let slot_used = |i: u64| -> Result<bool, GraphError> {
                Ok(match &region_mem {
                    Some(r) => {
                        let at = (base - cur.region) as usize + i as usize * SLOT;
                        r[at + 12..at + 17] != [0u8; 5]
                    }
                    None => {
                        let b =
                            a.get(base + i * SLOT as u64, SLOT).ok_or_else(|| GraphError::Store("key map slot out of bounds".into()))?;
                        get_u40(&b[12..17]) != 0
                    }
                })
            };
            let mut taken: HashMap<u64, ()> = HashMap::new();
            let mut blocks: BTreeMap<u64, [u8; 64]> = BTreeMap::new();
            for (fp, id) in keys {
                let mut s_ = cur.home(fp, self.shard_bits);
                while slot_used(s_)? || taken.contains_key(&s_) {
                    s_ += 1;
                    if s_ == cur.slots {
                        s_ = 0;
                    }
                }
                taken.insert(s_, ());
                let mut slot = [0u8; SLOT];
                slot[..12].copy_from_slice(&fp.0);
                put_u40(&mut slot[12..17], id + 1);
                op(&mut f, F_KM_ARENA, base + s_ * SLOT as u64, &slot);
                let (boff, bits) = cur.filter_block(fp);
                let blk = blocks.entry(boff).or_insert_with(|| match &region_mem {
                    Some(r) => r[(boff - cur.region) as usize..(boff - cur.region) as usize + 64].try_into().unwrap(),
                    None => a.get(boff, 64).map(|b| b.try_into().unwrap()).unwrap_or([0u8; 64]),
                });
                for b in bits {
                    blk[b as usize / 8] |= 1 << (b % 8);
                }
            }
            for (boff, blk) in &blocks {
                op(&mut f, F_KM_ARENA, *boff, blk);
            }
            let nsh = Shard { count: total, ..cur };
            op(&mut f, F_KM_DIR, s * DIR_ENTRY as u64, &nsh.encode());
        }
        // 8. Counters, one run.
        let mut ctl = [0u8; C_COUNTERS_LEN];
        for (i, val) in [n1, self.ext_tail, out_tail, inx_tail, self.blob_tail, km_tail].iter().enumerate() {
            ctl[i * 8..i * 8 + 8].copy_from_slice(&val.to_le_bytes());
        }
        op(&mut f, F_CTL, C_COUNTERS as u64, &ctl);
        Ok(f)
    }
}

fn need(f: &mut Finalized, file: u8, end: u64) {
    if end > f.need[file as usize] {
        f.need[file as usize] = end;
    }
}

fn op(f: &mut Finalized, file: u8, off: u64, bytes: &[u8]) {
    f.body.push(file);
    f.body.extend_from_slice(&off.to_le_bytes());
    f.body.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
    let at = f.body.len();
    f.body.extend_from_slice(bytes);
    f.ops.push(OpRef { file, off, at, len: bytes.len() });
    need(f, file, off + bytes.len() as u64);
}

/// The logical redo record of a shard rebuild, with the shard index as
/// offset: `[old region u64][old slots u64][old count u64][new region u64][new slots u64]`.
fn op_rebuild(f: &mut Finalized, shard: u64, old: &Shard, region: u64, slots: u64) {
    let b = encode_rebuild(old, region, slots);
    f.body.push(F_KM_REBUILD);
    f.body.extend_from_slice(&shard.to_le_bytes());
    f.body.extend_from_slice(&(b.len() as u32).to_le_bytes());
    let at = f.body.len();
    f.body.extend_from_slice(&b);
    f.ops.push(OpRef { file: F_KM_REBUILD, off: shard, at, len: b.len() });
    need(f, F_KM_ARENA, region + region_bytes(slots));
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
        let (boff, bits) = sh.filter_block(fp);
        let b0 = (boff - sh.region) as usize;
        for b in bits {
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
        self.cores.get(&id.0).copied().unwrap_or_else(|| self.view().core(id))
    }
    fn ext(&self, id: PosId) -> Option<Ext> {
        let i = self.core(id).ext_index()?;
        self.exts.get(&i).copied().or_else(|| self.view().ext_at(i))
    }
    fn children(&self, id: PosId, out: &mut Vec<Edge>) {
        match self.children.get(&id.0) {
            Some(es) => out.extend_from_slice(es),
            None if id.0 < self.n0 => self.view().children(id, out),
            None => {}
        }
    }
    fn parents(&self, id: PosId, out: &mut Vec<PosId>) {
        if id.0 < self.n0 {
            self.view().parents(id, out);
        }
        if let Some(ps) = self.new_parents.get(&id.0) {
            out.extend(ps.iter().map(|&p| PosId(p)));
        }
    }
    fn lookup(&self, fp: &Fingerprint) -> Option<PosId> {
        self.keys.get(fp).map(|&id| PosId(id)).or_else(|| self.view().lookup_committed(fp))
    }
    fn fen(&self, id: PosId) -> Option<String> {
        if id.0 >= self.n0 {
            let p = self.new_fens.get((id.0 - self.n0) as usize)?;
            return match p.slot() {
                av2_model::fen::FenSlot::Blob(r) => self.blob(r).and_then(|b| String::from_utf8(b).ok()),
                _ => self.view().fen_of(p),
            };
        }
        self.view().fen(id)
    }
    fn blob(&self, r: BlobRef) -> Option<Vec<u8>> {
        if r.0 >= self.blob0 {
            return self.blobs.get(&r.0).cloned();
        }
        self.view().blob_at(r)
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
            self.new_parents.entry(e.child.0).or_default().push(parent.0);
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
