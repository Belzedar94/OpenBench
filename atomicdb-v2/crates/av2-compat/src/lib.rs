//! AtomicDB v2 compatibility harness: v2's outside must answer as v1 does.
//!
//! The contract is `atomicdb-v2/docs/api-contract.md`.  This crate checks it:
//!
//! * [`replay`] sends the requests of a golden recording of v1
//!   (`tests/golden/v1-*/responses.jsonl.gz`) to a base URL and diffs every
//!   answer field by field under the rules of `tests/golden/normalize.json`
//!   ([`rules`], [`diff`]).
//! * [`scenarios`] drives the worker protocol (lease, heartbeat, submit,
//!   release and their refusals) against a test instance.
//!
//! [`json`] is the parser both use: it keeps key order and number text, which
//! is what byte compatibility is made of.

pub mod diff;
pub mod golden;
pub mod http;
pub mod json;
pub mod replay;
pub mod rules;
pub mod scenarios;

/// The rules file and the golden recording that ship with the workspace.
pub mod defaults {
    pub const RULES: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/golden/normalize.json");
    pub const GOLDEN: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/golden/v1-20261128/responses.jsonl.gz");
}
