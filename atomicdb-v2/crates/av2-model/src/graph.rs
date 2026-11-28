//! The graph as the rules see it.
//!
//! `GraphRead` is implemented by the committed store (lock-free readers) and
//! by the writer's overlay (committed state plus the batch being computed).
//! `GraphWrite` is implemented by the overlay only: every change the rules
//! make goes through it and ends up in one log frame.
//!
//! Contract (the store enforces it):
//! - ids are dense and assigned by `create` in call order;
//! - `put_core` overwrites everything but `ext` and `version`, which the store
//!   manages (`version` becomes the batch's sequence number);
//! - `set_children` replaces the out-edge list of a position; children that
//!   were not in the old list gain `parent` in their parent list (a parent is
//!   listed once per child even when two moves lead to the same child);
//! - edges are never deleted (v1 never deletes them either).

use crate::id::PosId;
use crate::key::Fingerprint;
use crate::mv::Move;
use crate::record::{Core, Ext};
use serde::{Deserialize, Serialize};

/// An out-edge: the move and the child it leads to (v1 `Edge`: `move_uci`,
/// `child_id`; the parent is the block's owner).
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug, Default, Serialize, Deserialize)]
pub struct Edge {
    pub child: PosId,
    pub mv: Move,
}

/// Reference to an immutable blob (analysis JSON, witness line, long FEN).
/// 0 = none.  Opaque outside the store.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct BlobRef(pub u64);

impl BlobRef {
    pub const NONE: BlobRef = BlobRef(0);
    pub fn is_none(self) -> bool {
        self.0 == 0
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GraphError {
    /// `create` was asked for a fingerprint that already has this id.
    Duplicate(PosId),
    /// An id at or beyond the number of positions.
    NoSuchPosition(PosId),
    /// More out-edges than one block holds (65,535).
    TooManyEdges(usize),
    /// A store-level failure (I/O, capacity); the batch is abandoned.
    Store(String),
}

impl std::fmt::Display for GraphError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            GraphError::Duplicate(id) => write!(f, "position already exists as {id}"),
            GraphError::NoSuchPosition(id) => write!(f, "no position {id}"),
            GraphError::TooManyEdges(n) => write!(f, "{n} out-edges do not fit in one block"),
            GraphError::Store(s) => write!(f, "store: {s}"),
        }
    }
}
impl std::error::Error for GraphError {}

pub trait GraphRead {
    /// Number of positions (ids are `0..len`).
    fn len(&self) -> u64;
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
    fn core(&self, id: PosId) -> Core;
    fn ext(&self, id: PosId) -> Option<Ext>;
    /// Appends the out-edges of `id` to `out`, in the order they were set.
    fn children(&self, id: PosId, out: &mut Vec<Edge>);
    /// Appends the parents of `id` to `out`, in the order they were added.
    fn parents(&self, id: PosId, out: &mut Vec<PosId>);
    fn lookup(&self, fp: &Fingerprint) -> Option<PosId>;
    /// Canonical FEN, if the position has one stored.
    fn fen(&self, id: PosId) -> Option<String>;
    fn blob(&self, r: BlobRef) -> Option<Vec<u8>>;
}

pub trait GraphWrite: GraphRead {
    fn create(&mut self, fp: Fingerprint, fen: Option<&str>, core: Core) -> Result<PosId, GraphError>;
    fn put_core(&mut self, id: PosId, core: Core) -> Result<(), GraphError>;
    fn put_ext(&mut self, id: PosId, ext: Ext) -> Result<(), GraphError>;
    fn set_children(&mut self, parent: PosId, edges: &[Edge]) -> Result<(), GraphError>;
    fn put_blob(&mut self, bytes: &[u8]) -> Result<BlobRef, GraphError>;
}
