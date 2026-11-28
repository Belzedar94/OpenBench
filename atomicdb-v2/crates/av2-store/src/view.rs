//! Reads of the committed graph straight from the mapped files.
//!
//! Every read is bounds-checked against the mapping it uses, so a reader that
//! races with the writer can read stale or torn bytes but never fault; the
//! epoch check in `Reader` makes it retry in that case.

use crate::layout::*;
use crate::mapped::Maps;
use av2_model::fen::FenSlot;
use av2_model::id::get_u40;
use av2_model::record::{CORE_SIZE, EXT_SIZE};
use av2_model::{BlobRef, Core, Edge, Ext, Fingerprint, GraphRead, Move, PackedFen, PosId};

/// A key-map shard as described by its directory entry.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Shard {
    pub region: u64,
    pub slots: u64,
    pub count: u64,
}

impl Shard {
    pub fn decode(b: &[u8]) -> Shard {
        Shard {
            region: u64::from_le_bytes(b[0..8].try_into().unwrap()),
            slots: u32::from_le_bytes(b[8..12].try_into().unwrap()) as u64,
            count: u32::from_le_bytes(b[12..16].try_into().unwrap()) as u64,
        }
    }
    pub fn encode(&self) -> [u8; DIR_ENTRY] {
        let mut b = [0u8; DIR_ENTRY];
        b[0..8].copy_from_slice(&self.region.to_le_bytes());
        b[8..12].copy_from_slice(&(self.slots as u32).to_le_bytes());
        b[12..16].copy_from_slice(&(self.count as u32).to_le_bytes());
        b
    }
    #[inline]
    pub fn fblocks(&self) -> u64 {
        filter_blocks(self.slots)
    }
    #[inline]
    pub fn slots_at(&self) -> u64 {
        self.region + self.fblocks() * 64
    }
    #[inline]
    pub fn home(&self, fp: &Fingerprint, shard_bits: u32) -> u64 {
        (((fp.hi64() << shard_bits) as u128 * self.slots as u128) >> 64) as u64
    }
    /// Byte offset (in the arena) of `fp`'s filter block.
    #[inline]
    pub fn filter_block_offset(&self, fp: &Fingerprint) -> u64 {
        self.region + ((fp.lo32() as u64 * self.fblocks()) >> 32) * 64
    }
    /// `fp`'s filter block offset and the bits to test in it.
    #[inline]
    pub fn filter_block(&self, fp: &Fingerprint) -> (u64, [u16; FILTER_PROBES as usize]) {
        (self.filter_block_offset(fp), filter_bits(fp))
    }
}

/// The bits of `fp` in its filter block (independent of the shard's size).
#[inline]
pub fn filter_bits(fp: &Fingerprint) -> [u16; FILTER_PROBES as usize] {
    let h = xxhash_rust::xxh3::xxh3_64(&fp.0);
    let mut bits = [0u16; FILTER_PROBES as usize];
    for (i, b) in bits.iter_mut().enumerate() {
        *b = ((h >> (i * 9)) & 511) as u16;
    }
    bits
}

#[inline]
pub fn shard_of(fp: &Fingerprint, shard_bits: u32) -> u64 {
    if shard_bits == 0 { 0 } else { fp.hi64() >> (64 - shard_bits) }
}

/// Read-only view of the committed graph.
pub struct View<'a> {
    pub maps: &'a Maps,
    pub shard_bits: u32,
}

impl<'a> View<'a> {
    pub fn new(maps: &'a Maps) -> View<'a> {
        View { maps, shard_bits: maps.ctl(C_SHARD_BITS) as u32 }
    }

    #[inline]
    pub fn counter(&self, off: usize) -> u64 {
        self.maps.ctl(off)
    }

    #[inline]
    pub fn core_bytes(&self, id: u64) -> Option<&'a [u8]> {
        self.maps.f(F_CORE).get(id * CORE_SIZE as u64, CORE_SIZE)
    }

    pub fn ext_at(&self, index: u64) -> Option<Ext> {
        self.maps.f(F_EXT).get(index * EXT_SIZE as u64, EXT_SIZE).map(Ext::decode)
    }

    #[inline]
    pub fn out_of(&self, id: u64) -> u64 {
        self.maps.f(F_OUT).get(id * OUT_SIZE as u64, OUT_SIZE).map(get_u40).unwrap_or(0)
    }

    #[inline]
    pub fn parent_word(&self, id: u64) -> u64 {
        self.maps.f(F_PARENT).get(id * PARENT_SIZE as u64, PARENT_SIZE).map(get_u40).unwrap_or(0)
    }

    pub fn block_at(&self, off: u64, out: &mut Vec<Edge>) {
        let a = self.maps.f(F_OUT_ARENA);
        let Some(h) = a.get(off, BLOCK_HEADER) else { return };
        let n = u16::from_le_bytes([h[0], h[1]]) as usize;
        let Some(b) = a.get(off + BLOCK_HEADER as u64, n * EDGE_SIZE) else { return };
        for c in b.as_chunks::<EDGE_SIZE>().0 {
            out.push(Edge { child: PosId(get_u40(&c[..5])), mv: Move(u16::from_le_bytes([c[5], c[6]])) });
        }
    }

    /// Parent list cells starting at `cell`, newest first.
    pub fn cells(&self, mut cell: u64, out: &mut Vec<PosId>) {
        let a = self.maps.f(F_INX_ARENA);
        let mut guard = 0u64;
        while cell != 0 && guard < 1 << 24 {
            let Some(c) = a.get(cell * INX_CELL as u64, INX_CELL) else { return };
            out.push(PosId(get_u40(&c[..5])));
            cell = get_u40(&c[5..10]);
            guard += 1;
        }
    }

    pub fn shard(&self, s: u64) -> Shard {
        self.maps.f(F_KM_DIR).get(s * DIR_ENTRY as u64, DIR_ENTRY).map(Shard::decode).unwrap_or_default()
    }

    pub fn filter_may_contain(&self, sh: &Shard, fp: &Fingerprint) -> bool {
        let (base, bits) = sh.filter_block(fp);
        let Some(block) = self.maps.f(F_KM_ARENA).get(base, 64) else { return true };
        bits.iter().all(|&b| block[b as usize / 8] & (1 << (b % 8)) != 0)
    }

    /// Committed key-map lookup.
    pub fn lookup_committed(&self, fp: &Fingerprint) -> Option<PosId> {
        let sh = self.shard(shard_of(fp, self.shard_bits));
        if sh.slots == 0 || !self.filter_may_contain(&sh, fp) {
            return None;
        }
        let a = self.maps.f(F_KM_ARENA);
        let base = sh.slots_at();
        let mut s = sh.home(fp, self.shard_bits);
        for _ in 0..sh.slots {
            let b = a.get(base + s * SLOT as u64, SLOT)?;
            let id1 = get_u40(&b[12..17]);
            if id1 == 0 {
                return None;
            }
            if b[..12] == fp.0 {
                return Some(PosId(id1 - 1));
            }
            s += 1;
            if s == sh.slots {
                s = 0;
            }
        }
        None
    }

    pub fn blob_at(&self, r: BlobRef) -> Option<Vec<u8>> {
        if r.is_none() {
            return None;
        }
        let a = self.maps.f(F_BLOB_ARENA);
        let h = a.get(r.0, 4)?;
        let n = u32::from_le_bytes(h.try_into().unwrap()) as usize;
        a.get(r.0 + 4, n).map(|b| b.to_vec())
    }

    pub fn packed_fen(&self, id: u64) -> PackedFen {
        let mut p = PackedFen::default();
        if let Some(b) = self.maps.f(F_FEN).get(id * av2_model::fen::FEN_SIZE as u64, av2_model::fen::FEN_SIZE) {
            p.0.copy_from_slice(b);
        }
        p
    }

    pub fn fen_of(&self, p: &PackedFen) -> Option<String> {
        match p.slot() {
            FenSlot::None => None,
            FenSlot::Text(s) => Some(s),
            FenSlot::Blob(r) => self.blob_at(r).and_then(|b| String::from_utf8(b).ok()),
        }
    }

    /// Every `(fingerprint, id)` pair of the key map (unordered).
    pub fn key_pairs(&self, out: &mut Vec<(Fingerprint, PosId)>) {
        let a = self.maps.f(F_KM_ARENA);
        for s in 0..(1u64 << self.shard_bits) {
            let sh = self.shard(s);
            let base = sh.slots_at();
            for i in 0..sh.slots {
                if let Some(b) = a.get(base + i * SLOT as u64, SLOT) {
                    let id1 = get_u40(&b[12..17]);
                    if id1 != 0 {
                        out.push((Fingerprint(b[..12].try_into().unwrap()), PosId(id1 - 1)));
                    }
                }
            }
        }
    }
}

impl GraphRead for View<'_> {
    fn len(&self) -> u64 {
        self.counter(C_POSITIONS)
    }
    fn core(&self, id: PosId) -> Core {
        self.core_bytes(id.0).map(Core::decode).unwrap_or_default()
    }
    fn ext(&self, id: PosId) -> Option<Ext> {
        self.core(id).ext_index().and_then(|i| self.ext_at(i))
    }
    fn children(&self, id: PosId, out: &mut Vec<Edge>) {
        let o = self.out_of(id.0);
        if o != 0 {
            self.block_at(o - 1, out);
        }
    }
    fn parents(&self, id: PosId, out: &mut Vec<PosId>) {
        let w = self.parent_word(id.0);
        if w == 0 {
            return;
        }
        if w & PARENT_LIST == 0 {
            out.push(PosId(w - 1));
            return;
        }
        let start = out.len();
        self.cells(w & !PARENT_LIST, out);
        out[start..].reverse();
    }
    fn lookup(&self, fp: &Fingerprint) -> Option<PosId> {
        self.lookup_committed(fp)
    }
    fn fen(&self, id: PosId) -> Option<String> {
        self.fen_of(&self.packed_fen(id.0))
    }
    fn blob(&self, r: BlobRef) -> Option<Vec<u8>> {
        self.blob_at(r)
    }
}
