//! A hash of the logical content of a graph, independent of how it is stored.
//!
//! Two graphs have the same digest when they have the same positions with
//! the same records (store-managed `ext` pointers left out, blob references
//! replaced by the blobs' content), the same edges in the same order, the
//! same parents, the same FENs and the same key map.  The crash test and the
//! backup test compare the store against an independent in-memory model
//! (`mem::MemGraph`) with it.

use av2_model::{BlobRef, Edge, Fingerprint, GraphRead, PosId};
use xxhash_rust::xxh3::Xxh3;

fn blob_hash(g: &dyn GraphRead, r: BlobRef) -> u64 {
    match g.blob(r) {
        Some(b) => xxhash_rust::xxh3::xxh3_64(&b) | 1,
        None => 0,
    }
}

/// Digest of `g`, whose key map holds `pairs` (any order).
pub fn digest(g: &dyn GraphRead, mut pairs: Vec<(Fingerprint, PosId)>) -> u128 {
    let mut h = Xxh3::new();
    let n = g.len();
    h.update(&n.to_le_bytes());
    let mut es: Vec<Edge> = Vec::new();
    let mut ps: Vec<PosId> = Vec::new();
    for id in 0..n {
        let id = PosId(id);
        let mut c = g.core(id);
        let has_ext = c.ext != 0;
        c.ext = 0;
        h.update(&c.to_bytes());
        if has_ext && let Some(mut e) = g.ext(id) {
            let (a, p, w) = (blob_hash(g, e.analysis), blob_hash(g, e.passes), blob_hash(g, e.won_line));
            e.analysis = BlobRef(a);
            e.passes = BlobRef(p);
            e.won_line = BlobRef(w);
            h.update(&[1]);
            h.update(&e.to_bytes());
        } else {
            h.update(&[0]);
        }
        es.clear();
        g.children(id, &mut es);
        h.update(&(es.len() as u32).to_le_bytes());
        for e in &es {
            h.update(&e.child.0.to_le_bytes());
            h.update(&e.mv.0.to_le_bytes());
        }
        ps.clear();
        g.parents(id, &mut ps);
        h.update(&(ps.len() as u32).to_le_bytes());
        for p in &ps {
            h.update(&p.0.to_le_bytes());
        }
        match g.fen(id) {
            Some(f) => {
                h.update(&(f.len() as u32 + 1).to_le_bytes());
                h.update(f.as_bytes());
            }
            None => h.update(&0u32.to_le_bytes()),
        }
    }
    pairs.sort_by_key(|(_, id)| *id);
    h.update(&(pairs.len() as u64).to_le_bytes());
    for (fp, id) in &pairs {
        h.update(&fp.0);
        h.update(&id.0.to_le_bytes());
    }
    h.digest128()
}
