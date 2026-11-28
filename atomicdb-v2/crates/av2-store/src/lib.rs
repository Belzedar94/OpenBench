//! AtomicDB v2 storage core (plan sections 3.2 to 3.5).
//!
//! - `graph/`: fixed-stride column files and append-only arenas, mapped into
//!   memory (`mapped`, `layout`, `view`).
//! - `log/`: segments of CRC-protected frames, each frame the facts of one
//!   batch plus its redo records (`log`).
//! - One writer: a `Batch` overlay is filled by the rules, turned into redo
//!   records, logged, fsynced and only then applied, between two bumps of an
//!   epoch that lets readers run without locks (`batch`, `store`).
//! - Group commit (`group`): inputs that arrive while a commit is in flight
//!   form the next batch.
//! - Checkpoints run on their own thread; recovery replays the frames newer
//!   than the last checkpoint and cuts a torn tail (`checkpoint`, `store`).
//! - Page checksums at checkpoint and a scrubber; backups while running.
//!
//! See `docs/store.md` for the design decisions and the numbers.

pub mod batch;
pub mod checkpoint;
pub mod digest;
pub mod group;
pub mod layout;
pub mod loader;
pub mod log;
pub mod mapped;
pub mod mem;
pub mod store;
pub mod testkit;
pub mod view;

pub use batch::Batch;
pub use store::{Archive, CommitTimings, Config, InFlight, Notify, Reader, RecoveryReport, Stats, Store};

#[derive(Debug)]
pub enum StoreError {
    Io(std::io::Error),
    /// The files or the log are not what this code wrote.
    Corrupt(String),
    Graph(av2_model::graph::GraphError),
    /// A previous background checkpoint failed; the store refuses to go on.
    Checkpoint(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Io(e) => write!(f, "I/O: {e}"),
            StoreError::Corrupt(s) => write!(f, "corrupt store: {s}"),
            StoreError::Graph(e) => write!(f, "{e}"),
            StoreError::Checkpoint(s) => write!(f, "checkpoint failed earlier: {s}"),
        }
    }
}
impl std::error::Error for StoreError {}

impl From<std::io::Error> for StoreError {
    fn from(e: std::io::Error) -> Self {
        StoreError::Io(e)
    }
}
impl From<av2_model::graph::GraphError> for StoreError {
    fn from(e: av2_model::graph::GraphError) -> Self {
        StoreError::Graph(e)
    }
}

pub type Result<T> = std::result::Result<T, StoreError>;
