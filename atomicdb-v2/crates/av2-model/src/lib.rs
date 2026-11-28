//! AtomicDB v2 data model.
//!
//! What every other crate agrees on: position ids, position keys, the 16-bit
//! move encoding, the on-disk records of the graph columns (`Core`, `Ext`),
//! the packed FEN, and the two traits the rules use to read and change the
//! graph (`GraphRead`, `GraphWrite`).  The storage crate implements the
//! traits; the rules crate is written against them only.
//!
//! Every record field documents the v1 column it maps to
//! (`atomicdb/models.py`, class `Position`).  Encodings are little-endian at
//! fixed byte offsets, never `repr(C)` transmutes, so the files read the same
//! on every machine.

pub mod fen;
pub mod graph;
pub mod id;
pub mod key;
pub mod mv;
pub mod record;
pub mod rng;

pub use fen::PackedFen;
pub use graph::{BlobRef, Edge, GraphRead, GraphWrite};
pub use id::{ID_BITS, MAX_ID, PosId};
pub use key::{Fingerprint, Key};
pub use mv::Move;
pub use record::{Closure, Core, Ext, Proof, Status};
pub use rng::Rng;
