//! AtomicDB v2 work queue (phase 4): the task model, lanes and fairness, the
//! lease protocol, the engine gate, requests, the selector and the
//! background arms, as **state applied by the single writer**.
//!
//! Spec: `atomicdb-v2/docs/rules-spec-queue.md` (families Q, PRI, SOL-4,
//! SOL-5); decisions and coverage: `atomicdb-v2/docs/queue.md`.
//!
//! - [`QueueState::apply`] takes one [`Input`] (a lease, a heartbeat, a
//!   submit, a release, a request, a timer tick...) with its explicit `now`
//!   and returns the [`Output`] plus the deltas of every row it changed.
//!   The rules never read a clock, a random source or a file.
//! - The writer logs `fact::record(input, applied)` in the batch frame.
//!   Recovery loads the last queue snapshot and replays the deltas after it
//!   (`fact::replay`): leases survive a restart with their tokens.
//! - The graph is read through [`env::QueueEnv`]; the queue never writes it.
//!   The selector proposes through [`selector::Refill`]; the proof crate
//!   plugs its descent in through [`selector::Descent`].

pub mod apply;
pub mod arms;
pub mod budget;
pub mod config;
pub mod consts;
pub mod env;
pub mod fact;
pub mod gate;
pub mod input;
pub mod lease;
pub mod model;
pub mod protocol;
pub mod quality;
pub mod request;
pub mod selector;
pub mod solve;
pub mod state;

pub use apply::Applied;
pub use config::{QueueConfig, SelectorMode};
pub use consts::Ms;
pub use env::{GraphEnv, MemEnv, PosInfo, QueueEnv, StoredPriority, V1Priority};
pub use input::*;
pub use model::*;
pub use selector::{Descent, MintPlan, NoRefill, PnSelector, Refill, RegretSelector};
pub use state::{Delta, Event, Ladder, QueueState};
