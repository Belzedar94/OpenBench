//! AtomicDB v2 proof numbers and selector priority, in memory (owner
//! decision D5).
//!
//! Port of `atomicdb/proof.py` (PN-0 to PN-7, PN-8, PN-10 of
//! `docs/rules-spec-proofs.md`), `atomicdb/solve_estimate.py` (the gate,
//! PN-2) and the priority formula of `atomicdb/ingest.py` (PRI-1, PRI-2.1).
//!
//! | Module | Rules |
//! |---|---|
//! | [`numbers`] | PN-1 arithmetic, PN-2.8 gated pn, PN-5.1 sticky primary, PN-4.6 format |
//! | [`gate`] | PN-2.1 to PN-2.7, PN-2.9, PN-2.10 rate limit |
//! | [`table`] | PN-0.1 rows (`ProofNode` in memory) |
//! | [`campaign`] | PN-0.2, PN-6.1 policy |
//! | [`refresh`] | PN-3 cycle rule, PN-4.1 `compute_numbers`, PN-4.3 level loop, PN-4.4 caps |
//! | [`maintain`] | PN-4.2 entry point, PN-8 writer hook, PN-4.6 served numbers, PN-10.5 recascade |
//! | [`frontier`] | PN-10.1 to PN-10.4 |
//! | [`clamp`] | PN-7.1 |
//! | [`descent`] | PN-6 df-pn descent, PN-9 value walker |
//! | [`rebuild`] | startup rebuild and the lazy (per-epoch) alternative |
//! | [`priority`] | PRI-1.2 to PRI-1.6, PRI-2.1, PRI-2.3 |
//!
//! The crate reads the graph through [`graph::ProofGraph`], implemented for
//! every `av2_model::GraphRead`; it does not depend on `av2-rules`.

pub mod campaign;
pub mod clamp;
pub mod descent;
pub mod frontier;
pub mod gate;
pub mod graph;
pub mod maintain;
pub mod numbers;
pub mod priority;
pub mod rebuild;
pub mod refresh;
pub mod table;

pub use campaign::Campaign;
pub use graph::{Node, ProofGraph};
pub use maintain::{Proofs, SharedProofs};
pub use numbers::{Goal, PROOF_INFINITY, PROOF_MAX_LEAF};
pub use refresh::{ProofEvent, RefreshConfig, RefreshReport};
pub use table::{ProofTable, Row};

#[cfg(test)]
mod descent_tests;
#[cfg(test)]
mod tests;
#[cfg(test)]
mod v1diff;
