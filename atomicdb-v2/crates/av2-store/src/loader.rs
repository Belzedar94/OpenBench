//! Bulk construction of a store (the migration loader, plan section 8.2).
//!
//! Writes the columns directly, without the log: the result is checkpointed
//! at sequence 0 and the import is recorded as frame 1 (a genesis fact), so
//! a crash during a load leaves nothing that looks like a valid store
//! (the checkpoint record is written last).

use crate::batch::build_region;
use crate::checkpoint::{self, Ckpt};
use crate::layout::*;
use crate::mapped::Maps;
use crate::store::{Config, Store};
use crate::view::{Shard, shard_of};
use crate::{Result, StoreError};
use av2_model::fen::FEN_SIZE;
use av2_model::id::{get_u40, put_u40};
use av2_model::record::{CORE_SIZE, EXT_SIZE};
use av2_model::{BlobRef, Core, Edge, Ext, Fingerprint, PackedFen};
use std::path::{Path, PathBuf};

pub struct BulkLoader {
    dir: PathBuf,
    cfg: Config,
    maps: Maps,
    n: u64,
    ext_tail: u64,
    out_tail: u64,
    inx_tail: u64,
    blob_tail: u64,
    fps: Vec<(Fingerprint, u64)>,
    pub blob_fens: u64,
}

impl BulkLoader {
    /// Start a store for exactly `positions` positions (ids `0..positions`).
    pub fn create(dir: &Path, cfg: Config, positions: u64, edges_hint: u64) -> Result<BulkLoader> {
        let g = dir.join("graph");
        if g.join("ctl").exists() {
            return Err(StoreError::Corrupt(format!("{} already holds a store", dir.display())));
        }
        std::fs::create_dir_all(&g)?;
        std::fs::create_dir_all(dir.join("log"))?;
        let n = positions.max(1);
        let mut l = [0u64; NFILES];
        l[F_CTL as usize] = CTL_SIZE;
        l[F_CORE as usize] = n * CORE_SIZE as u64;
        l[F_EXT as usize] = (n / 3).max(1) * EXT_SIZE as u64;
        l[F_OUT as usize] = n * OUT_SIZE as u64;
        l[F_PARENT as usize] = n * PARENT_SIZE as u64;
        l[F_FEN as usize] = n * FEN_SIZE as u64;
        l[F_OUT_ARENA as usize] = edges_hint * EDGE_SIZE as u64 + n / 4 * BLOCK_HEADER as u64 + 4096;
        l[F_INX_ARENA as usize] = n / 8 * INX_CELL as u64 + 4096;
        l[F_BLOB_ARENA as usize] = 1 << 20;
        l[F_KM_DIR as usize] = (1u64 << cfg.shard_bits) * DIR_ENTRY as u64;
        l[F_KM_ARENA as usize] = (n as f64 / BUILD_LOAD * SLOT as f64 * 1.1) as u64 + (1u64 << cfg.shard_bits) * region_bytes(MIN_SLOTS);
        let maps = Maps::open(&g, &l)?;
        crate::store::init_ctl_for_loader(&maps, cfg.shard_bits);
        Ok(BulkLoader {
            dir: dir.to_path_buf(),
            cfg,
            maps,
            n: positions,
            ext_tail: 0,
            out_tail: 0,
            inx_tail: 1,
            blob_tail: 8,
            fps: Vec::with_capacity(positions as usize),
            blob_fens: 0,
        })
    }

    fn reserve(&mut self, f: u8, end: u64) -> Result<()> {
        if end > self.maps.f(f).len() {
            self.maps = self.maps.grown(f, end)?;
        }
        Ok(())
    }

    pub fn put_blob(&mut self, bytes: &[u8]) -> Result<BlobRef> {
        let off = self.blob_tail;
        self.reserve(F_BLOB_ARENA, off + 4 + bytes.len() as u64)?;
        let a = self.maps.f(F_BLOB_ARENA);
        a.put(off, &(bytes.len() as u32).to_le_bytes());
        a.put(off + 4, bytes);
        self.blob_tail += 4 + bytes.len() as u64;
        Ok(BlobRef(off))
    }

    /// Write position `id` (any order, each id once).
    pub fn set_position(&mut self, id: u64, fp: Fingerprint, core: Core, ext: Option<Ext>, fen: Option<&str>) -> Result<()> {
        if id >= self.n {
            return Err(StoreError::Corrupt(format!("position id {id} beyond the declared {}", self.n)));
        }
        let mut c = Core { version: 0, ext: 0, ..core };
        if let Some(e) = ext {
            let idx = self.ext_tail;
            self.ext_tail += 1;
            self.reserve(F_EXT, self.ext_tail * EXT_SIZE as u64)?;
            self.maps.f(F_EXT).put(idx * EXT_SIZE as u64, &e.to_bytes());
            c.ext = idx + 1;
        }
        self.maps.f(F_CORE).put(id * CORE_SIZE as u64, &c.to_bytes());
        if let Some(s) = fen {
            let p = match PackedFen::pack(s) {
                Some(p) => p,
                None => {
                    self.blob_fens += 1;
                    PackedFen::blob(self.put_blob(s.as_bytes())?)
                }
            };
            self.maps.f(F_FEN).put(id * FEN_SIZE as u64, &p.0);
        }
        self.fps.push((fp, id));
        Ok(())
    }

    /// Write the out-edge block of `parent` (once per parent).  Parent lists
    /// are written separately with `add_parent`.
    pub fn set_children(&mut self, parent: u64, edges: &[Edge]) -> Result<()> {
        if edges.is_empty() {
            return Ok(());
        }
        if edges.len() > u16::MAX as usize {
            return Err(StoreError::Corrupt(format!("{} edges out of position {parent}", edges.len())));
        }
        let len = BLOCK_HEADER + edges.len() * EDGE_SIZE;
        self.reserve(F_OUT_ARENA, self.out_tail + len as u64)?;
        let mut b = Vec::with_capacity(len);
        b.extend_from_slice(&(edges.len() as u16).to_le_bytes());
        for e in edges {
            let mut c = [0u8; EDGE_SIZE];
            put_u40(&mut c, e.child.0);
            c[5..7].copy_from_slice(&e.mv.0.to_le_bytes());
            b.extend_from_slice(&c);
        }
        self.maps.f(F_OUT_ARENA).put(self.out_tail, &b);
        let mut w = [0u8; OUT_SIZE];
        put_u40(&mut w, self.out_tail + 1);
        self.maps.f(F_OUT).put(parent * OUT_SIZE as u64, &w);
        self.out_tail += len as u64;
        Ok(())
    }

    /// Append `parent` to the parent list of `child` (call in v1 edge order;
    /// once per distinct parent).
    pub fn add_parent(&mut self, child: u64, parent: u64) -> Result<()> {
        let pc = self.maps.f(F_PARENT);
        let cur = get_u40(pc.get(child * PARENT_SIZE as u64, PARENT_SIZE).unwrap());
        let new = if cur == 0 {
            parent + 1
        } else if cur & PARENT_LIST == 0 {
            self.reserve(F_INX_ARENA, (self.inx_tail + 2) * INX_CELL as u64)?;
            let mut cells = [0u8; 2 * INX_CELL];
            put_u40(&mut cells[0..5], cur - 1);
            put_u40(&mut cells[10..15], parent);
            put_u40(&mut cells[15..20], self.inx_tail);
            self.maps.f(F_INX_ARENA).put(self.inx_tail * INX_CELL as u64, &cells);
            self.inx_tail += 2;
            PARENT_LIST | (self.inx_tail - 1)
        } else {
            self.reserve(F_INX_ARENA, (self.inx_tail + 1) * INX_CELL as u64)?;
            let mut cell = [0u8; INX_CELL];
            put_u40(&mut cell[0..5], parent);
            put_u40(&mut cell[5..10], cur & !PARENT_LIST);
            self.maps.f(F_INX_ARENA).put(self.inx_tail * INX_CELL as u64, &cell);
            self.inx_tail += 1;
            PARENT_LIST | (self.inx_tail - 1)
        };
        let mut w = [0u8; PARENT_SIZE];
        put_u40(&mut w, new);
        self.maps.f(F_PARENT).put(child * PARENT_SIZE as u64, &w);
        Ok(())
    }

    /// Build the key map, write the counters, flush, checkpoint at 0, open the
    /// store and commit `facts` (the genesis record) as frame 1.
    pub fn finish(mut self, facts: &[u8]) -> Result<Store> {
        if self.fps.len() as u64 != self.n {
            return Err(StoreError::Corrupt(format!("{} positions written, {} declared", self.fps.len(), self.n)));
        }
        let bits = self.cfg.shard_bits;
        // Duplicate fingerprints would make two ids share a key.
        self.fps.sort_unstable_by_key(|(fp, _)| *fp);
        if let Some(w) = self.fps.windows(2).find(|w| w[0].0 == w[1].0) {
            return Err(StoreError::Corrupt(format!("duplicate fingerprint {:?} (ids {} and {})", w[0].0, w[0].1, w[1].1)));
        }
        // Group by shard (fingerprint order is shard order), insert by id.
        let mut km_tail = 0u64;
        let mut i = 0usize;
        let mut group: Vec<(Fingerprint, u64)> = Vec::new();
        for s in 0..(1u64 << bits) {
            group.clear();
            while i < self.fps.len() && shard_of(&self.fps[i].0, bits) == s {
                group.push(self.fps[i]);
                i += 1;
            }
            if group.is_empty() {
                continue;
            }
            group.sort_unstable_by_key(|(_, id)| *id);
            let slots = ((group.len() as f64 / BUILD_LOAD).ceil() as u64).max(MIN_SLOTS);
            let sh = Shard { region: km_tail, slots, count: group.len() as u64 };
            let region = build_region(&sh, &group, bits);
            self.reserve(F_KM_ARENA, km_tail + region.len() as u64)?;
            self.maps.f(F_KM_ARENA).put(km_tail, &region);
            self.maps.f(F_KM_DIR).put(s * DIR_ENTRY as u64, &sh.encode());
            km_tail += region.len() as u64;
        }
        self.fps = Vec::new();
        let mut ctl = [0u8; C_COUNTERS_LEN];
        for (k, v) in [self.n, self.ext_tail, self.out_tail, self.inx_tail, self.blob_tail, km_tail].iter().enumerate() {
            ctl[k * 8..k * 8 + 8].copy_from_slice(&v.to_le_bytes());
        }
        self.maps.f(F_CTL).put(C_COUNTERS as u64, &ctl);
        for m in self.maps.m.iter() {
            m.flush()?;
        }
        checkpoint::write(&self.dir.join("graph").join("ckpt"), Ckpt { generation: 1, seq: 0 })?;
        let dir = self.dir.clone();
        let cfg = self.cfg.clone();
        drop(self);
        let mut store = Store::open(&dir, cfg)?;
        let b = store.batch();
        store.commit(b, facts)?;
        Ok(store)
    }
}
