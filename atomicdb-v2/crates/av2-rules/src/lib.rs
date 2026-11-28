//! AtomicDB v2 rules: **placeholder**.
//!
//! The rules port (phase 2 of the plan: cascade, mate distance, backed values,
//! loops, proof numbers) lives here and is written by another agent.  This
//! crate only fixes the interface the writer calls, so that the store, the
//! writer loop and the tools can be built and tested before the rules exist.
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
