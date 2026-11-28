//! The islands: the only components compiled to WASM.
//!
//! An island renders on the server like any component and is hydrated in the
//! browser from the props serialized next to it.  Keep them few and small:
//! every byte here is downloaded by every visitor of a page that has one.

pub mod board;
pub mod request;
