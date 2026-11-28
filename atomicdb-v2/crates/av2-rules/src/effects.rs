//! What the rules hand to the rest of the system besides graph writes.
//!
//! v1 writes these as rows in the same transaction (`DBEvent`,
//! `ClosureProvenance`, `PendingClaim`, `AnalysisTask` absorption and
//! purchases, `Campaign` state).  The rules port does not own those models
//! (the task model and the projection are other agents' work), so every such
//! write is a call on a trait, made at the moment v1 makes it and in the same
//! order.  The writer implements the traits; [`Recorder`] records the calls
//! for tests.

use av2_facts::{Fact, Opaque, RawJson};
use av2_model::{Closure, PosId, Proof, Status};
use serde_json::{Value, json};

/// CAS-4.1: the queue a closure's result came from.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default, Hash)]
pub enum ClosureSource {
    User,
    Fill,
    Auto,
    Solve,
    #[default]
    None,
}

impl ClosureSource {
    /// `closure_attribution`: upper-cased, `NONE` when unknown.
    pub fn parse(s: &str) -> ClosureSource {
        match s.to_ascii_uppercase().as_str() {
            "USER" => ClosureSource::User,
            "FILL" => ClosureSource::Fill,
            "AUTO" => ClosureSource::Auto,
            "SOLVE" => ClosureSource::Solve,
            _ => ClosureSource::None,
        }
    }
    pub fn v1(self) -> &'static str {
        match self {
            ClosureSource::User => "USER",
            ClosureSource::Fill => "FILL",
            ClosureSource::Auto => "AUTO",
            ClosureSource::Solve => "SOLVE",
            ClosureSource::None => "NONE",
        }
    }
}

/// CAS-4.2: who produced a closure (`_clean_provenance`: account and sha
/// truncated to 64 characters).
#[derive(Clone, PartialEq, Eq, Debug, Default)]
pub struct Provenance {
    pub task: Option<u64>,
    pub account: String,
    pub engine_sha: String,
}

impl Provenance {
    pub fn clean(task: Option<u64>, account: &str, engine_sha: &str) -> Provenance {
        Provenance { task, account: account.chars().take(64).collect(), engine_sha: engine_sha.chars().take(64).collect() }
    }
}

/// The closure attribution of an apply (v1's two context variables).
#[derive(Clone, PartialEq, Eq, Debug, Default)]
pub struct Attribution {
    pub source: ClosureSource,
    pub provenance: Option<Provenance>,
}

/// A closure won by a door (CAS-7.1 `_emit_closure_events`).
#[derive(Clone, PartialEq, Debug)]
pub struct NodeClosed {
    pub pos: PosId,
    pub status: Status,
    pub closure: Closure,
    /// The proof as written with the closure (the provenance row records it).
    pub proof: Proof,
    pub source: ClosureSource,
    pub provenance: Option<Provenance>,
    /// Fields only some doors know (TB: `dtz`, `dtz_verified`,
    /// `clock_slack`; SOLVE: certificate data).
    pub extra: Vec<(String, Value)>,
}

/// The `DBEvent`s the value rules write (other than `NODE_CLOSED`).  Keys are
/// position ids; `to_fact` maps them to v1 keys for the log.
#[derive(Clone, PartialEq, Debug)]
pub enum Event {
    /// CAS-5.1.
    CascadeGuard { iterations: u64, remaining: u64, seed_count: u64 },
    /// CAS-5.2: first 20 keys sorted (by id in v2, D3), how many, the limit.
    CascadeCycle { keys: Vec<PosId>, count: u64, limit: u32, iterations: u64, seed_count: u64 },
    /// BKD-9.1: `ply-guard`, `node-budget`, `revisit-cap` (with `dropped`).
    BackedGuard {
        reason: GuardReason,
        processed: u64,
        plies: u64,
        seed_count: u64,
        generation: u32,
        left: u64,
        keys: Vec<PosId>,
        dropped: Option<u64>,
    },
    /// BKD-11.1.
    QualityConvergence { queued: u64, discrepancies: u64 },
    /// BKD-11.3.
    CycleDisambiguation { queued: u64, chosen_cycles: u64 },
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum GuardReason {
    PlyGuard,
    NodeBudget,
    RevisitCap,
}

impl GuardReason {
    pub fn v1(self) -> &'static str {
        match self {
            GuardReason::PlyGuard => "ply-guard",
            GuardReason::NodeBudget => "node-budget",
            GuardReason::RevisitCap => "revisit-cap",
        }
    }
}

impl Event {
    pub fn kind(&self) -> &'static str {
        match self {
            Event::CascadeGuard { .. } => "CASCADE_GUARD",
            Event::CascadeCycle { .. } => "CASCADE_CYCLE",
            Event::BackedGuard { .. } => "BACKED_GUARD",
            Event::QualityConvergence { .. } => "QUALITY_CONVERGENCE",
            Event::CycleDisambiguation { .. } => "CYCLE_DISAMBIGUATION",
        }
    }

    /// v1's payload, with `key` mapping ids to v1 keys.
    pub fn payload(&self, key: &dyn Fn(PosId) -> String) -> Value {
        let keys = |v: &[PosId]| Value::from(v.iter().map(|&k| key(k)).collect::<Vec<_>>());
        match self {
            Event::CascadeGuard { iterations, remaining, seed_count } => {
                json!({"iterations": iterations, "remaining": remaining, "seed_count": seed_count})
            }
            Event::CascadeCycle { keys: k, count, limit, iterations, seed_count } => {
                json!({"keys": keys(k), "count": count, "limit": limit, "iterations": iterations, "seed_count": seed_count})
            }
            Event::BackedGuard { reason, processed, plies, seed_count, generation, left, keys: k, dropped } => {
                let mut v = json!({"reason": reason.v1(), "processed": processed, "plies": plies, "seed_count": seed_count,
                                   "generation": generation, "left": left, "keys": keys(k)});
                if let Some(d) = dropped {
                    v["dropped"] = json!(d);
                }
                v
            }
            Event::QualityConvergence { queued, discrepancies } => json!({"queued": queued, "discrepancies": discrepancies}),
            Event::CycleDisambiguation { queued, chosen_cycles } => json!({"queued": queued, "chosen_cycles": chosen_cycles}),
        }
    }

    /// The event as a log fact (`Fact::Opaque`, kind = the `DBEvent` kind).
    pub fn to_fact(&self, key: &dyn Fn(PosId) -> String) -> Fact {
        Fact::Opaque(Opaque { kind: self.kind().to_string(), data: RawJson(self.payload(key).to_string()) })
    }
}

impl NodeClosed {
    /// The `NODE_CLOSED` payload `{key, status, closure, source, **extra}`.
    pub fn payload(&self, key: &dyn Fn(PosId) -> String) -> Value {
        let mut v = json!({"key": key(self.pos), "status": self.status.v1(), "closure": self.closure.v1(), "source": self.source.v1()});
        for (k, x) in &self.extra {
            v[k.as_str()] = x.clone();
        }
        v
    }
}

/// Everything the value rules do besides writing the graph.
pub trait Effects {
    /// CAS-7.1, once per closure a door wins, at the moment of the closure:
    /// the `NODE_CLOSED` event, the provenance row, the resolution of the
    /// open pending claims, the absorption of every PENDING task on the
    /// position and the end of every ACTIVE campaign rooted there.
    fn node_closed(&mut self, closed: NodeClosed);
    /// Any other event of the rules.
    fn event(&mut self, event: Event);
    /// PN-8.1: the proof-number refresh of `seeds + changed` at the end of a
    /// cascade.  Proof numbers are another agent's port (step 10); the rules
    /// only say when and with which seeds.
    fn proof_refresh(&mut self, _seeds: &[PosId]) {}
}

/// Records every call, in order (tests, benchmarks, the writer's buffer).
#[derive(Default, Debug, Clone)]
pub struct Recorder {
    pub closed: Vec<NodeClosed>,
    pub events: Vec<Event>,
    pub proof_refreshes: Vec<Vec<PosId>>,
}

impl Effects for Recorder {
    fn node_closed(&mut self, closed: NodeClosed) {
        self.closed.push(closed);
    }
    fn event(&mut self, event: Event) {
        self.events.push(event);
    }
    fn proof_refresh(&mut self, seeds: &[PosId]) {
        self.proof_refreshes.push(seeds.to_vec());
    }
}

/// Ignores everything.
#[derive(Default, Debug, Clone, Copy)]
pub struct NoEffects;

impl Effects for NoEffects {
    fn node_closed(&mut self, _: NodeClosed) {}
    fn event(&mut self, _: Event) {}
}
