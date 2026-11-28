//! Step 10 wiring: the proof numbers (`av2-proof`, merged in #346) hang off
//! the cascade through `Effects::proof_refresh` (PN-8.1).
//!
//! v1 refreshes pn/dn at the end of every `backup_cascade` with `seeds +
//! changed`, inline or, in a drained batch, replayed after the commit in call
//! order (PN-8.2); plus the landed position after `apply_job` (PN-8.4).
//! Proof numbers read `status`, `eval_cp`, the side to move and the edges,
//! and never feed back into values or closures.  So the writer:
//!
//! 1. runs the batch's rules with a [`ProofCalls`] around its effect sink:
//!    every cascade's seed list is collected in call order (stage (a) adds
//!    one call per landing with [`ProofCalls::landing`], PN-8.4);
//! 2. commits the frame (proof numbers are not in it: owner decision D5);
//! 3. replays `ProofCalls::calls` through `av2_proof::Proofs::after_batch`
//!    on the committed view (`proofs.after_batch(&view, &calls.calls, seq, now)`).
//!
//! This crate does not link `av2-proof` (its optional bench feature depends
//! on `av2-tools`, which depends on this crate): the writer makes the call;
//! `tests/proofs.rs` makes it the same way.
//!
//! Replaying after the commit gives the same numbers as refreshing inline:
//! between a cascade's end and the commit the batch writes only backed
//! columns and tasks, which the proof numbers do not read (tested below and
//! in `tests/proofs.rs`).  Stored pn/dn are the gate-off numbers
//! (`RefreshConfig::default()`); the gate-on ranking is the selector's.

use crate::effects::{Effects, Event, NodeClosed};
use av2_model::PosId;

/// An [`Effects`] that forwards everything to `inner` and collects the
/// proof-refresh calls of the batch.
pub struct ProofCalls<'a> {
    pub inner: &'a mut dyn Effects,
    pub calls: Vec<Vec<PosId>>,
}

impl<'a> ProofCalls<'a> {
    pub fn new(inner: &'a mut dyn Effects) -> Self {
        ProofCalls { inner, calls: Vec::new() }
    }

    /// PN-8.4: the landed position after `apply_job` (outside the cascade).
    pub fn landing(&mut self, pos: PosId) {
        self.calls.push(vec![pos]);
    }
}

impl Effects for ProofCalls<'_> {
    fn node_closed(&mut self, closed: NodeClosed) {
        self.inner.node_closed(closed);
    }
    fn event(&mut self, event: Event) {
        self.inner.event(event);
    }
    fn proof_refresh(&mut self, seeds: &[PosId]) {
        self.calls.push(seeds.to_vec());
        self.inner.proof_refresh(seeds);
    }
}
