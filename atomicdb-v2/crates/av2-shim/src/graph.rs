//! `ShimGraph`: v1 rows loaded into the v2 in-memory graph, and the rows to
//! write back once a v2 function has run.
//!
//! The graph is `av2_store::mem::MemGraph` (the plain model the store is
//! tested against), plus what the bridge needs on top: the v1 key of every
//! id, the decoded row of every position as it was loaded (so that only the
//! columns a v2 function really changed are written back), and the edges
//! that were already there.
//!
//! Ids follow the loader's rule (plan section 8.2): creation order, taken as
//! the id of the first incoming edge; positions nobody points at come first,
//! in the order given.  Rules break ties by id (owner decision D3), so this
//! order is part of what a replay reproduces.

use crate::v1::{self, Encoded, MapError, Row};
use av2_model::graph::GraphError;
use av2_model::{BlobRef, Core, Edge, Ext, Fingerprint, GraphRead, GraphWrite, Key, Move, PosId};
use av2_store::mem::MemGraph;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};

/// A v1 `atomicdb_edge` row.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EdgeRow {
    /// v1 `Edge.id` (creation order); None when unknown.
    #[serde(default)]
    pub id: Option<i64>,
    pub parent: String,
    #[serde(rename = "move")]
    pub mv: String,
    pub child: String,
}

/// Something about the input the graph could not take as it was.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LoadIssue {
    pub key: String,
    pub kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<MapError>,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub detail: String,
}

/// What a v2 function changed, as v1 rows.
#[derive(Debug, Clone, Default, Serialize)]
pub struct Changes {
    /// key -> the modeled columns that differ from the loaded row.
    pub updated: Vec<(String, Row)>,
    /// Positions the function created, whole (with `key`).
    pub created: Vec<Row>,
    /// Edges the function added.
    pub edges: Vec<EdgeRow>,
}

impl Changes {
    pub fn is_empty(&self) -> bool {
        self.updated.is_empty() && self.created.is_empty() && self.edges.is_empty()
    }
}

pub struct ShimGraph {
    mem: MemGraph,
    keys: Vec<String>,
    by_key: HashMap<String, PosId>,
    /// The records at load (blob contents resolved); None for positions
    /// created afterwards.
    base: Vec<Option<Encoded>>,
    /// Positions only known by reference (an edge pointed at them).
    stub: Vec<bool>,
    /// Columns given at load, per position (all of them for a full row).
    given: Vec<Vec<String>>,
    base_edges: HashSet<(u64, Move)>,
    pub issues: Vec<LoadIssue>,
}

/// The fingerprint of a v1 key; keys that are not 64 hex characters (test
/// fixtures sometimes invent them) are hashed first.
pub fn fingerprint_of(key: &str) -> Fingerprint {
    match Key::from_hex(key) {
        Ok(k) => k.fingerprint(),
        Err(_) => hashed_fingerprint(key),
    }
}

fn hashed_fingerprint(key: &str) -> Fingerprint {
    let d: [u8; 32] = Sha256::digest(key.as_bytes()).into();
    let mut f = [0u8; 12];
    f.copy_from_slice(&d[..12]);
    Fingerprint(f)
}

impl Default for ShimGraph {
    fn default() -> ShimGraph {
        ShimGraph::new()
    }
}

impl ShimGraph {
    pub fn new() -> ShimGraph {
        ShimGraph {
            mem: MemGraph::new(),
            keys: Vec::new(),
            by_key: HashMap::new(),
            base: Vec::new(),
            stub: Vec::new(),
            given: Vec::new(),
            base_edges: HashSet::new(),
            issues: Vec::new(),
        }
    }

    /// Load v1 rows.  `positions` need a `key`; any subset of the modeled
    /// columns may be present (missing ones take the v1 default).  Edges
    /// whose ends are not among the positions create stub positions.
    pub fn load(positions: Vec<Row>, edges: Vec<EdgeRow>) -> ShimGraph {
        let mut g = ShimGraph::new();
        let mut first_in: HashMap<&str, i64> = HashMap::new();
        for (n, e) in edges.iter().enumerate() {
            let id = e.id.unwrap_or(n as i64);
            let slot = first_in.entry(e.child.as_str()).or_insert(id);
            *slot = (*slot).min(id);
        }
        let mut order: Vec<(i64, usize, &Row)> = Vec::with_capacity(positions.len());
        for (n, r) in positions.iter().enumerate() {
            let Some(key) = r.get(v1::KEY).and_then(|k| k.as_str()) else {
                g.issues.push(LoadIssue { key: String::new(), kind: "no_key", error: None, detail: format!("row {n}") });
                continue;
            };
            order.push((first_in.get(key).copied().unwrap_or(i64::MIN), n, r));
        }
        order.sort_by_key(|&(first, n, _)| (first, n));
        for (_, _, r) in order {
            let key = r[v1::KEY].as_str().unwrap().to_string();
            if g.by_key.contains_key(&key) {
                g.issues.push(LoadIssue { key, kind: "duplicate_key", error: None, detail: String::new() });
                continue;
            }
            let mut enc = Encoded::default();
            for e in v1::apply(&mut enc, r) {
                g.issues.push(LoadIssue { key: key.clone(), kind: "unrepresentable", error: Some(e), detail: String::new() });
            }
            let given = r.keys().filter(|c| v1::is_modeled(c)).cloned().collect();
            g.insert_loaded(key, &enc, false, given);
        }
        // Edges: per parent, in creation order.
        let mut by_parent: Vec<(i64, usize, &EdgeRow)> = edges.iter().enumerate().map(|(n, e)| (e.id.unwrap_or(n as i64), n, e)).collect();
        by_parent.sort_by_key(|&(id, n, _)| (id, n));
        let mut lists: HashMap<u64, Vec<Edge>> = HashMap::new();
        let mut parent_order: Vec<u64> = Vec::new();
        for (_, _, e) in by_parent {
            let mv = match Move::parse(&e.mv) {
                Ok(m) => m,
                Err(err) => {
                    g.issues.push(LoadIssue {
                        key: e.parent.clone(),
                        kind: "bad_edge_move",
                        error: None,
                        detail: format!("{} -> {}: {err}", e.mv, e.child),
                    });
                    continue;
                }
            };
            let parent = g.id_or_stub(&e.parent);
            let child = g.id_or_stub(&e.child);
            let list = lists.entry(parent.0).or_insert_with(|| {
                parent_order.push(parent.0);
                Vec::new()
            });
            if list.iter().any(|x| x.mv == mv) {
                g.issues.push(LoadIssue { key: e.parent.clone(), kind: "duplicate_edge", error: None, detail: e.mv.clone() });
                continue;
            }
            list.push(Edge { child, mv });
            g.base_edges.insert((parent.0, mv));
        }
        for p in parent_order {
            let list = &lists[&p];
            if let Err(e) = g.mem.set_children(PosId(p), list) {
                g.issues.push(LoadIssue { key: g.keys[p as usize].clone(), kind: "edges_refused", error: None, detail: e.to_string() });
            }
        }
        g
    }

    fn insert_loaded(&mut self, key: String, enc: &Encoded, stub: bool, given: Vec<String>) -> PosId {
        let mut fp = fingerprint_of(&key);
        if self.mem.lookup(&fp).is_some() {
            self.issues.push(LoadIssue { key: key.clone(), kind: "fingerprint_collision", error: None, detail: String::new() });
            fp = hashed_fingerprint(&format!("collision:{key}"));
        }
        let id = self.mem.create(fp, enc.fen.as_deref(), enc.core).expect("fresh fingerprint");
        self.keys.push(key.clone());
        self.by_key.insert(key, id);
        self.stub.push(stub);
        self.given.push(given);
        self.write_payload(id, enc).expect("position just created");
        let enc = self.encoded(id);
        self.base.push(Some(enc));
        id
    }

    fn id_or_stub(&mut self, key: &str) -> PosId {
        if let Some(&id) = self.by_key.get(key) {
            return id;
        }
        self.insert_loaded(key.to_string(), &Encoded::default(), true, Vec::new())
    }

    /// Write the ext record and the blobs of `enc` (the core is written by
    /// the caller or by `create`).
    fn write_payload(&mut self, id: PosId, enc: &Encoded) -> Result<(), GraphError> {
        let had_ext = self.mem.ext(id).is_some();
        if !enc.needs_ext() && !had_ext {
            return Ok(());
        }
        let mut ext = enc.ext;
        let old = self.mem.ext(id).unwrap_or_default();
        ext.analysis = self.blob_for(old.analysis, enc.analysis.as_deref())?;
        ext.passes = self.blob_for(old.passes, enc.passes.as_deref())?;
        ext.won_line = self.blob_for(old.won_line, enc.won_line.as_deref())?;
        if had_ext && old == ext {
            return Ok(());
        }
        self.mem.put_ext(id, ext)
    }

    fn blob_for(&mut self, old: BlobRef, bytes: Option<&[u8]>) -> Result<BlobRef, GraphError> {
        match bytes {
            None => Ok(BlobRef::NONE),
            Some(b) if !old.is_none() && self.mem.blob(old).as_deref() == Some(b) => Ok(old),
            Some(b) => self.mem.put_blob(b),
        }
    }

    /// The v2 records of a position with blob contents resolved.
    pub fn encoded(&self, id: PosId) -> Encoded {
        let core = self.mem.core(id);
        let ext = self.mem.ext(id).unwrap_or_default();
        let blob = |r: BlobRef| if r.is_none() { None } else { self.mem.blob(r) };
        Encoded { core, ext, fen: self.mem.fen(id), analysis: blob(ext.analysis), passes: blob(ext.passes), won_line: blob(ext.won_line) }
    }

    /// Replace the modeled columns of a position (core, ext, blobs).
    pub fn write_encoded(&mut self, id: PosId, enc: &Encoded) -> Result<(), GraphError> {
        self.mem.put_core(id, enc.core)?;
        self.write_payload(id, enc)
    }

    /// Set some v1 columns of a position through the graph (what a write
    /// from v1 code looks like to v2: used by the echo mode and the tests).
    pub fn put_row(&mut self, id: PosId, row: &Row) -> Result<Vec<MapError>, GraphError> {
        let mut enc = self.encoded(id);
        let errors = v1::apply(&mut enc, row);
        self.write_encoded(id, &enc)?;
        Ok(errors)
    }

    /// Create a position from a v1 row (it must carry `key`; `fen` is
    /// stored as given).
    pub fn create_row(&mut self, row: &Row) -> Result<(PosId, Vec<MapError>), GraphError> {
        let key = row.get(v1::KEY).and_then(Value::as_str).unwrap_or_default().to_string();
        let mut enc = Encoded::default();
        let errors = v1::apply(&mut enc, row);
        let id = self.mem.create(fingerprint_of(&key), enc.fen.as_deref(), enc.core)?;
        self.keys.push(key.clone());
        self.by_key.insert(key, id);
        self.stub.push(false);
        self.given.push(Vec::new());
        self.base.push(None);
        self.write_payload(id, &enc)?;
        Ok((id, errors))
    }

    /// Add one edge (if the parent has no edge with this move yet).
    pub fn add_edge(&mut self, parent: PosId, mv: Move, child: PosId) -> Result<bool, GraphError> {
        let mut list = Vec::new();
        self.mem.children(parent, &mut list);
        if list.iter().any(|e| e.mv == mv) {
            return Ok(false);
        }
        list.push(Edge { child, mv });
        self.mem.set_children(parent, &list)?;
        Ok(true)
    }

    /// The child key of `parent`'s edge with move `mv`, if any.
    pub fn edge_child(&self, parent: &str, mv: &str) -> Option<String> {
        let (id, mv) = (self.id(parent)?, Move::parse(mv).ok()?);
        let mut list = Vec::new();
        self.mem.children(id, &mut list);
        list.iter().find(|e| e.mv == mv).map(|e| self.key(e.child).to_string())
    }

    pub fn id(&self, key: &str) -> Option<PosId> {
        self.by_key.get(key).copied()
    }

    pub fn key(&self, id: PosId) -> &str {
        &self.keys[id.index()]
    }

    pub fn is_stub(&self, id: PosId) -> bool {
        self.stub.get(id.index()).copied().unwrap_or(false)
    }

    /// The modeled columns the loaded row carried.
    pub fn given_columns(&self, id: PosId) -> &[String] {
        self.given.get(id.index()).map(Vec::as_slice).unwrap_or(&[])
    }

    /// Every modeled column of a position, with `key`.
    pub fn row(&self, id: PosId) -> Row {
        let mut r = Row::new();
        r.insert(v1::KEY.into(), Value::String(self.key(id).to_string()));
        r.extend(v1::decode(&self.encoded(id)));
        r
    }

    /// The changes since `load`.
    pub fn changes(&self) -> Changes {
        let mut out = Changes::default();
        for i in 0..self.mem.len() {
            let id = PosId(i);
            let now = self.encoded(id);
            match &self.base[i as usize] {
                Some(before) if same_values(before, &now) => {}
                Some(before) => {
                    let diff = v1::changed(&v1::decode(before), &v1::decode(&now));
                    if !diff.is_empty() {
                        out.updated.push((self.key(id).to_string(), diff));
                    }
                }
                None => out.created.push(self.row(id)),
            }
            let mut list = Vec::new();
            self.mem.children(id, &mut list);
            for e in list {
                if !self.base_edges.contains(&(i, e.mv)) {
                    out.edges.push(EdgeRow {
                        id: None,
                        parent: self.key(id).to_string(),
                        mv: e.mv.to_uci(),
                        child: self.key(e.child).to_string(),
                    });
                }
            }
        }
        out
    }

    pub fn mem(&self) -> &MemGraph {
        &self.mem
    }
}

use serde_json::Value;

/// The same v1 values: the records equal but for what the store manages
/// (version, ext pointer, blob references), the blob contents equal.
fn same_values(a: &Encoded, b: &Encoded) -> bool {
    let strip_core = |c: &Core| Core { version: 0, ext: 0, ..*c };
    let strip_ext = |x: &Ext| Ext { analysis: BlobRef::NONE, passes: BlobRef::NONE, won_line: BlobRef::NONE, ..*x };
    strip_core(&a.core) == strip_core(&b.core)
        && strip_ext(&a.ext).to_bytes() == strip_ext(&b.ext).to_bytes()
        && a.fen == b.fen
        && a.analysis == b.analysis
        && a.passes == b.passes
        && a.won_line == b.won_line
}

/// The rules see a `ShimGraph` as any other graph.  `create` derives the v1
/// key from the FEN (`sha256` of the canonical FEN, as v1 does).
impl GraphRead for ShimGraph {
    fn len(&self) -> u64 {
        self.mem.len()
    }
    fn core(&self, id: PosId) -> Core {
        self.mem.core(id)
    }
    fn ext(&self, id: PosId) -> Option<Ext> {
        self.mem.ext(id)
    }
    fn children(&self, id: PosId, out: &mut Vec<Edge>) {
        self.mem.children(id, out)
    }
    fn parents(&self, id: PosId, out: &mut Vec<PosId>) {
        self.mem.parents(id, out)
    }
    fn lookup(&self, fp: &Fingerprint) -> Option<PosId> {
        self.mem.lookup(fp)
    }
    fn fen(&self, id: PosId) -> Option<String> {
        self.mem.fen(id)
    }
    fn blob(&self, r: BlobRef) -> Option<Vec<u8>> {
        self.mem.blob(r)
    }
}

impl GraphWrite for ShimGraph {
    fn create(&mut self, fp: Fingerprint, fen: Option<&str>, core: Core) -> Result<PosId, GraphError> {
        let id = self.mem.create(fp, fen, core)?;
        let key = match fen {
            Some(f) => Key::of_canonical_fen(f).to_hex(),
            None => hex::encode(fp.0),
        };
        self.keys.push(key.clone());
        self.by_key.insert(key, id);
        self.stub.push(false);
        self.given.push(Vec::new());
        self.base.push(None);
        Ok(id)
    }
    fn put_core(&mut self, id: PosId, core: Core) -> Result<(), GraphError> {
        self.mem.put_core(id, core)
    }
    fn put_ext(&mut self, id: PosId, ext: Ext) -> Result<(), GraphError> {
        self.mem.put_ext(id, ext)
    }
    fn set_children(&mut self, parent: PosId, edges: &[Edge]) -> Result<(), GraphError> {
        self.mem.set_children(parent, edges)
    }
    fn put_blob(&mut self, bytes: &[u8]) -> Result<BlobRef, GraphError> {
        self.mem.put_blob(bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const A: &str = "a81b39f292eca4c6959bf6d1a81b39f292eca4c6959bf6d1a81b39f292eca4c6";
    const B: &str = "b81b39f292eca4c6959bf6d1a81b39f292eca4c6959bf6d1a81b39f292eca4c6";

    fn row(v: Value) -> Row {
        v.as_object().unwrap().clone()
    }

    #[test]
    fn load_change_and_collect() {
        let mut g = ShimGraph::load(
            vec![
                row(json!({"key": B, "eval_cp": 5})),
                row(json!({"key": A, "eval_cp": 10, "status": "UNKNOWN", "last_analysis": "{\"x\":1}"})),
            ],
            vec![EdgeRow { id: Some(7), parent: A.into(), mv: "e2e4".into(), child: B.into() }],
        );
        assert!(g.issues.is_empty(), "{:?}", g.issues);
        // A has no incoming edge: it comes first.
        assert_eq!(g.id(A), Some(PosId(0)));
        assert!(g.changes().is_empty());
        let b = g.id(B).unwrap();
        g.put_row(b, &row(json!({"backed_eval": -3, "backed_move": "d7d5"}))).unwrap();
        let c = g.changes();
        assert_eq!(c.updated.len(), 1);
        assert_eq!(c.updated[0].0, B);
        assert_eq!(Value::Object(c.updated[0].1.clone()), json!({"backed_eval": -3, "backed_move": "d7d5"}));
        let (n, _) = g.create_row(&row(json!({"key": "fake-key", "fen": "8/8/8/8/8/8/8/K6k w - - 0 1"}))).unwrap();
        assert!(g.add_edge(b, Move::parse("a1a2").unwrap(), n).unwrap());
        let c = g.changes();
        assert_eq!(c.created.len(), 1);
        assert_eq!(c.edges, vec![EdgeRow { id: None, parent: B.into(), mv: "a1a2".into(), child: "fake-key".into() }]);
    }

    #[test]
    fn stubs_and_issues() {
        let g = ShimGraph::load(
            vec![row(json!({"key": A, "eval_cp": 99999}))],
            vec![EdgeRow { id: None, parent: A.into(), mv: "e2e4".into(), child: B.into() }],
        );
        assert!(g.is_stub(g.id(B).unwrap()));
        assert_eq!(g.issues.len(), 1);
        assert_eq!(g.issues[0].kind, "unrepresentable");
    }
}
