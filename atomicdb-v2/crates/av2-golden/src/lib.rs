//! AtomicDB v2 golden-case replay (plan section 7.2).
//!
//! v1 records, for a sample of its drainer batches and stage (a) landings,
//! everything the batch read, its inputs and everything it wrote
//! (`atomicdb/golden_cases.py`).  This crate loads one case into an isolated
//! v2 graph (the rows the batch read, nothing else), runs the v2 entry point
//! of the batch, and compares the rows v2 wrote with the rows v1 wrote,
//! field by field, classifying every difference.

pub mod case;
pub mod read;
pub mod replay;
