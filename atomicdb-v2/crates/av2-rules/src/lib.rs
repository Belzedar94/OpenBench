//! AtomicDB v2 rules: the port of v1's value rules (phase 2 of the plan).
//!
//! Port order and rule ids: `atomicdb-v2/docs/rules-spec.md` section 16;
//! status per rule: `atomicdb-v2/docs/rules-port-status.md`.
//!
//! - step 1 `conv`, `bands`: conventions, score bands, headline value
//! - step 2 `exact`: backup_status, clock slack, fresh context, proof grade, witness
//! - steps 3 and 5 `distance`: mate distance, the repetition check
//! - step 4 `cascade`: the status cascade, its guards, the closure side effects
//! - steps 6 to 8 `backed`: child contributions, `_backed_for`, the ascent
//! - step 9 `purchase`: what the ascent buys, as intents through a trait
//! - `climb`: the cascade then the ascent (stage (b) of one batch)
//! - `loops`: the hooks steps 11 to 14 fill in
//!
//! The writer calls [`Rules::apply`] once per batch; stage (a) (landing a
//! search) is another agent's port and ends by calling [`climb::climb`].
//!
//! # The contract
//!
//! The writer thread owns the graph.  For each batch it:
//! 1. takes the prepared inputs that arrived while the previous commit was in
//!    flight (group commit), in sequence order;
//! 2. calls [`Rules::apply`] once with an overlay (`GraphWrite`: the committed
//!    graph plus this batch's private changes) and those inputs;
//! 3. writes the facts `apply` returned and the overlay's redo records into
//!    one log frame, fsyncs, applies, acknowledges.
//!
//! `apply` must be a **pure function** of the committed state and the inputs
//! (plan section 2.2): no wall clock, no hash-set iteration order, no thread
//! timing; budgets count steps; ties break by id.  It must run to rest (the
//! cascade and the ascent finish inside the batch).  If it returns an error,
//! the overlay is discarded and nothing of the batch is written.
//!
//! "Prepared" means everything expensive and pure was done before the writer
//! saw the input (legal moves and child keys, mate-PV verification, AND/OR
//! proofs, tablebase probes): see plan section 3.1, preparers.

pub mod bands;
pub mod conv;
pub mod exact;
pub mod testkit;

use av2_facts::Fact;
use av2_model::GraphWrite;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RulesError {
    /// The input contradicts the graph (unknown position, stale lease...): the
    /// batch is rejected and the writer reports it to the input's sender.
    Rejected(String),
    /// The graph refused a change (capacity, I/O).
    Graph(av2_model::graph::GraphError),
    /// Not ported yet.
    NotImplemented(&'static str),
}

impl std::fmt::Display for RulesError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RulesError::Rejected(s) => write!(f, "rejected: {s}"),
            RulesError::Graph(e) => write!(f, "graph: {e}"),
            RulesError::NotImplemented(s) => write!(f, "not implemented: {s}"),
        }
    }
}
impl std::error::Error for RulesError {}

impl From<av2_model::graph::GraphError> for RulesError {
    fn from(e: av2_model::graph::GraphError) -> Self {
        RulesError::Graph(e)
    }
}

/// What the writer calls once per batch.
pub trait Rules {
    /// A prepared input (a landed search with its child keys and verified
    /// proofs, a request, a lease event...).  Defined by the rules port.
    type Input: Send;
    /// What each input's sender gets back once the batch is durable.
    type Output: Send;

    /// Apply `inputs`, in order, to `graph`; append the facts to log to
    /// `facts`; return one output per input, in order.
    fn apply(&mut self, graph: &mut dyn GraphWrite, inputs: &[Self::Input], facts: &mut Vec<Fact>)
    -> Result<Vec<Self::Output>, RulesError>;
}

/// The input type of phase 2/3, sketched so that other crates can name it.
/// TODO(rules): replace by the real prepared inputs.
#[derive(Debug, Clone)]
pub enum PreparedInput {
    /// A landed search, with the facts that describe it.
    Search(Box<av2_facts::SearchLanded>),
    /// A proof the preparers verified.
    Proof(Box<av2_facts::ProofAccepted>),
    /// Anything else, as a fact.
    Fact(Box<Fact>),
}

/// Rules that do nothing: they log the facts of their inputs and change no
/// value.  Used by the tools and tests until the port lands.
#[derive(Debug, Default)]
pub struct LogOnly;

impl Rules for LogOnly {
    type Input = Fact;
    type Output = ();

    fn apply(&mut self, _graph: &mut dyn GraphWrite, inputs: &[Fact], facts: &mut Vec<Fact>) -> Result<Vec<()>, RulesError> {
        facts.extend(inputs.iter().cloned());
        Ok(vec![(); inputs.len()])
    }
}
