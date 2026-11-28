//! What the proof numbers read of the graph.
//!
//! A small trait instead of `av2_rules` types, so this crate does not wait
//! for the statuses/cascade port: the proof refresh only needs, per
//! position, its status, eval, side to move and out-edges (in v1 edge-id
//! order), and its parents.  Every `av2_model::GraphRead` (the committed
//! store, the writer's batch overlay, `MemGraph`) is a `ProofGraph` through
//! the blanket impl.

use crate::gate::GateRow;
use av2_model::record::NO_CP;
use av2_model::{Edge, GraphRead, Move, PosId, Status};

/// Per-position facts the recurrences read (v1 `Position.only('key', 'fen',
/// 'status', 'eval_cp')`; side to move replaces the FEN).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Node {
    pub status: Status,
    /// White POV, `None` = NULL.
    pub eval_cp: Option<i32>,
    pub black_to_move: bool,
}

pub trait ProofGraph {
    /// Number of positions (ids `0..len`).
    fn positions(&self) -> u64;
    fn node(&self, id: PosId) -> Node;
    /// Out-edges in v1 edge-id order (the order the store keeps).
    fn out_edges(&self, id: PosId, out: &mut Vec<Edge>);
    /// Parents, each once.
    fn in_parents(&self, id: PosId, out: &mut Vec<PosId>);
    /// The order of rows inside one refresh level and the tie-break of the
    /// frontier listing (v1: `position_id`, the sha256 hex key).  Default:
    /// the id (owner decision D3, ties by id).  An adapter can return the
    /// key fingerprint to reproduce v1's database order (see `KeyOrdered`).
    fn order_key(&self, id: PosId) -> u128 {
        id.0 as u128
    }
    /// The canonical FEN (only read with the solve gate on).
    fn fen_of(&self, id: PosId) -> Option<String>;
    /// The full row the gate estimator reads (frontier leaves, root numbers).
    fn gate_row(&self, id: PosId) -> GateRow;
    /// The backed-value columns the value walker reads (PN-9.2).  The
    /// default (nothing backed, nothing invested) is for graphs that only
    /// serve the proof numbers.
    fn value(&self, _id: PosId) -> ValueInfo {
        ValueInfo::default()
    }
}

/// v1 `Position.backed_eval, backed_move, backed_plies, nodes_invested,
/// backed_nodes` (the columns `proof._VALUE_CHILD_COLUMNS` reads).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ValueInfo {
    /// White POV, `None` = NULL.
    pub backed_eval: Option<i32>,
    /// `Move::NONE` = NULL (or empty).
    pub backed_move: Move,
    pub backed_plies: u32,
    pub nodes_invested: u64,
    pub backed_nodes: u64,
}

impl<G: GraphRead + ?Sized> ProofGraph for G {
    fn positions(&self) -> u64 {
        self.len()
    }
    #[inline]
    fn node(&self, id: PosId) -> Node {
        let c = self.core(id);
        Node { status: c.status(), eval_cp: (c.eval_cp != NO_CP).then_some(c.eval_cp as i32), black_to_move: c.black_to_move() }
    }
    #[inline]
    fn out_edges(&self, id: PosId, out: &mut Vec<Edge>) {
        self.children(id, out)
    }
    #[inline]
    fn in_parents(&self, id: PosId, out: &mut Vec<PosId>) {
        self.parents(id, out)
    }
    fn fen_of(&self, id: PosId) -> Option<String> {
        self.fen(id)
    }
    fn gate_row(&self, id: PosId) -> GateRow {
        let c = self.core(id);
        let analysis = self
            .ext(id)
            .filter(|e| !e.analysis.is_none())
            .and_then(|e| self.blob(e.analysis))
            .and_then(|b| serde_json::from_slice(&b).ok());
        GateRow {
            fen: self.fen(id),
            eval_cp: (c.eval_cp != NO_CP).then_some(c.eval_cp as i32),
            last_analysis: analysis,
            mate_in: c.mate().map(u32::from),
        }
    }
    #[inline]
    fn value(&self, id: PosId) -> ValueInfo {
        let c = self.core(id);
        let (nodes_invested, backed_nodes) = self.ext(id).map(|e| (e.nodes_invested, e.backed_nodes)).unwrap_or((0, 0));
        ValueInfo {
            backed_eval: c.backed().map(i32::from),
            backed_move: c.backed_move,
            backed_plies: u32::from(c.backed_plies),
            nodes_invested,
            backed_nodes,
        }
    }
}

/// A graph whose level order and frontier tie-break follow v1's key order
/// (`order_key` = the 96-bit key fingerprint, which sorts like the hex key).
pub struct KeyOrdered<'a, G: ?Sized> {
    pub graph: &'a G,
    /// `keys[id]` = big-endian value of the fingerprint.
    pub keys: &'a [u128],
}

impl<G: ProofGraph + ?Sized> ProofGraph for KeyOrdered<'_, G> {
    fn positions(&self) -> u64 {
        self.graph.positions()
    }
    fn node(&self, id: PosId) -> Node {
        self.graph.node(id)
    }
    fn out_edges(&self, id: PosId, out: &mut Vec<Edge>) {
        self.graph.out_edges(id, out)
    }
    fn in_parents(&self, id: PosId, out: &mut Vec<PosId>) {
        self.graph.in_parents(id, out)
    }
    fn order_key(&self, id: PosId) -> u128 {
        self.keys.get(id.index()).copied().unwrap_or(id.0 as u128)
    }
    fn fen_of(&self, id: PosId) -> Option<String> {
        self.graph.fen_of(id)
    }
    fn gate_row(&self, id: PosId) -> GateRow {
        self.graph.gate_row(id)
    }
    fn value(&self, id: PosId) -> ValueInfo {
        self.graph.value(id)
    }
}

/// Fingerprint bytes as an order key.
pub fn fingerprint_order(fp: &av2_model::Fingerprint) -> u128 {
    let mut b = [0u8; 16];
    b[..12].copy_from_slice(&fp.0);
    u128::from_be_bytes(b)
}
