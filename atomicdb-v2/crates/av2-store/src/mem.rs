//! An in-memory graph with the same semantics as the store, written as
//! plainly as possible.  It is the oracle of the property tests and of the
//! crash test: the store must end every sequence of batches with the same
//! digest as this model.

use av2_model::graph::GraphError;
use av2_model::{BlobRef, Core, Edge, Ext, Fingerprint, GraphRead, GraphWrite, PosId};
use std::collections::HashMap;

#[derive(Default, Clone)]
pub struct MemGraph {
    cores: Vec<Core>,
    exts: HashMap<u64, Ext>,
    children: HashMap<u64, Vec<Edge>>,
    parents: HashMap<u64, Vec<u64>>,
    fens: Vec<Option<String>>,
    keys: HashMap<Fingerprint, u64>,
    blobs: HashMap<u64, Vec<u8>>,
    next_blob: u64,
    /// Sequence number stamped on every record written (set by the caller per batch).
    pub seq: u64,
}

impl MemGraph {
    pub fn new() -> MemGraph {
        MemGraph { next_blob: 1, ..Default::default() }
    }

    pub fn key_pairs(&self) -> Vec<(Fingerprint, PosId)> {
        self.keys.iter().map(|(k, v)| (*k, PosId(*v))).collect()
    }

    fn check(&self, id: PosId) -> Result<(), GraphError> {
        if (id.0 as usize) < self.cores.len() { Ok(()) } else { Err(GraphError::NoSuchPosition(id)) }
    }
}

impl GraphRead for MemGraph {
    fn len(&self) -> u64 {
        self.cores.len() as u64
    }
    fn core(&self, id: PosId) -> Core {
        self.cores.get(id.index()).copied().unwrap_or_default()
    }
    fn ext(&self, id: PosId) -> Option<Ext> {
        self.exts.get(&id.0).copied()
    }
    fn children(&self, id: PosId, out: &mut Vec<Edge>) {
        if let Some(v) = self.children.get(&id.0) {
            out.extend_from_slice(v);
        }
    }
    fn parents(&self, id: PosId, out: &mut Vec<PosId>) {
        if let Some(v) = self.parents.get(&id.0) {
            out.extend(v.iter().map(|&p| PosId(p)));
        }
    }
    fn lookup(&self, fp: &Fingerprint) -> Option<PosId> {
        self.keys.get(fp).map(|&i| PosId(i))
    }
    fn fen(&self, id: PosId) -> Option<String> {
        self.fens.get(id.index()).cloned().flatten()
    }
    fn blob(&self, r: BlobRef) -> Option<Vec<u8>> {
        self.blobs.get(&r.0).cloned()
    }
}

impl GraphWrite for MemGraph {
    fn create(&mut self, fp: Fingerprint, fen: Option<&str>, core: Core) -> Result<PosId, GraphError> {
        if let Some(&id) = self.keys.get(&fp) {
            return Err(GraphError::Duplicate(PosId(id)));
        }
        let id = self.cores.len() as u64;
        self.cores.push(Core { ext: 0, version: self.seq, ..core });
        self.fens.push(fen.map(str::to_string));
        self.keys.insert(fp, id);
        Ok(PosId(id))
    }
    fn put_core(&mut self, id: PosId, core: Core) -> Result<(), GraphError> {
        self.check(id)?;
        let c = &mut self.cores[id.index()];
        *c = Core { ext: c.ext, version: self.seq, ..core };
        Ok(())
    }
    fn put_ext(&mut self, id: PosId, ext: Ext) -> Result<(), GraphError> {
        self.check(id)?;
        let c = &mut self.cores[id.index()];
        c.ext = 1; // "has an ext record"; the digest ignores the pointer itself
        c.version = self.seq;
        self.exts.insert(id.0, ext);
        Ok(())
    }
    fn set_children(&mut self, parent: PosId, edges: &[Edge]) -> Result<(), GraphError> {
        self.check(parent)?;
        if edges.len() > u16::MAX as usize {
            return Err(GraphError::TooManyEdges(edges.len()));
        }
        if let Some(e) = edges.iter().find(|e| e.child.0 >= self.len()) {
            return Err(GraphError::NoSuchPosition(e.child));
        }
        let old = self.children.get(&parent.0).cloned().unwrap_or_default();
        if old.iter().any(|o| !edges.contains(o)) {
            return Err(GraphError::Store("set_children drops an edge".into()));
        }
        for (k, e) in edges.iter().enumerate() {
            if old.iter().any(|x| x.child == e.child) || edges[..k].iter().any(|x| x.child == e.child) {
                continue;
            }
            self.parents.entry(e.child.0).or_default().push(parent.0);
        }
        self.children.insert(parent.0, edges.to_vec());
        Ok(())
    }
    fn put_blob(&mut self, bytes: &[u8]) -> Result<BlobRef, GraphError> {
        let r = self.next_blob;
        self.next_blob += 1;
        self.blobs.insert(r, bytes.to_vec());
        Ok(BlobRef(r))
    }
}
