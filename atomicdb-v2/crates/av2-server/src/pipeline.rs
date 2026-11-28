//! The writer's batch pipeline with the real rules: replaces `stub.rs`.
//!
//! For each batch the single writer calls [`PipelineRules`] (an
//! `av2_rules::Rules`), which, input by input and in order:
//!
//! 1. **lands a prepared search** ([`PreparedLanding`], what stage (a)'s
//!    preparers hand over: the position, its legal moves with the child keys,
//!    FENs and born-terminal statuses, the search's numbers): the expansion
//!    (EXP-1.1, children born closed when terminal, POS-2.1), visits and
//!    nodes (EXP-2.2), own eval and best move (EXP-2.6);
//! 2. **climbs** (spec section 2.3): the status cascade then the backed
//!    ascent (`av2_rules::climb`) with v1's loop rules (`V1Loops`), the
//!    purchases answered by the queue's quality arm (`QualityBuyer`), the
//!    proof-refresh calls collected (`ProofCalls`);
//! 3. **applies the queue's side** of the landing in the same batch: every
//!    closure absorbs the position's PENDING tasks (CAS-7.1,
//!    `Input::PositionClosed`), the landing absorbs the requests it covers
//!    (EXP-5.2, `Input::Landed`), and the purchases; each queue change is a
//!    `queue.v1` fact of the frame, so recovery rebuilds the queue from the
//!    log;
//! 4. or applies a **queue input** (lease, submit, request...) as it is.
//!
//! The rules' events (`NODE_CLOSED`, guards, purchases) go into the frame as
//! `Opaque` facts.  After the frame is durable and applied (the store's epoch
//! publishes it to readers), the writer calls [`Rules::after_commit`]: the
//! batch's proof calls are replayed on the committed view
//! (`av2_proof::Proofs::after_batch`, PN-8.2), under the write lock of the
//! [`SharedProofs`] that readers (the selector, the pages) share: the second
//! publication of the batch.
//!
//! What stage (a) adds and this does not (the stage-(a) agent's port): the
//! lines of a pass (showcase arbitration, claims, mate-line closures, trust,
//! tablebases).  A [`PreparedLanding`] carries the pass's verdict on its own
//! eval and best move instead.
//!
//! A failed batch: `apply` mutates the queue as it goes, like v1's
//! transaction does its rows, but there is no rollback of the in-memory
//! queue; a batch the rules reject leaves the queue ahead of the store, and
//! the writer should restart (the queue is rebuilt from its snapshot and the
//! log).  Rules errors are graph errors (capacity, I/O), not bad inputs:
//! bad inputs are rejected before anything is changed.

use av2_facts::Fact;
use av2_model::record::{F_BLACK_TO_MOVE, F_EXPANDED, F_REACHABLE, NO_SLACK};
use av2_model::{Closure, Core, Edge, Fingerprint, GraphRead, GraphWrite, Move, PosId, Proof, Rng, Status};
use av2_proof::{Campaign, Proofs, RefreshConfig, SharedProofs};
use av2_queue::{GraphEnv, NoRefill, QueueState};
use av2_rules::climb::{ClimbEnv, climb};
use av2_rules::config::RulesConfig;
use av2_rules::effects::{Attribution, Recorder};
use av2_rules::exact::BoardZeroing;
use av2_rules::loops::V1Loops;
use av2_rules::proofs::ProofCalls;
use av2_rules::{Rules, RulesError};
use std::collections::VecDeque;
use std::sync::{Arc, RwLock};

/// Milliseconds since the epoch (the queue's clock unit), carried by inputs.
pub type Ms = av2_queue::Ms;

/// One legal move of a landed position, prepared.
#[derive(Clone, Debug)]
pub struct PreparedChild {
    pub mv: Move,
    pub fp: Fingerprint,
    pub fen: Option<String>,
    /// `logic.terminal_status(child)` when the child is terminal.
    pub terminal: Option<Status>,
}

/// A search result as the preparers hand it to the writer.
#[derive(Clone, Debug)]
pub struct PreparedLanding {
    pub pos: PosId,
    /// Every legal move (empty when the position is already expanded).
    pub children: Vec<PreparedChild>,
    pub eval_cp: Option<i16>,
    pub best_move: Move,
    /// Nodes the worker searched (EXP-2.2).
    pub nodes: u64,
    /// The task it answers, and the arbiter budget (EXP-5.1).
    pub task: Option<u64>,
    pub arbiter: u64,
    pub now: Ms,
    /// The showcase as v1 stores it (`last_analysis` JSON), when the pass
    /// takes it.
    pub analysis: Option<Vec<u8>>,
    /// The search fact to log (the system of record of the landing).
    pub fact: Option<Box<av2_facts::SearchLanded>>,
}

#[derive(Clone, Debug)]
pub enum PipelineInput {
    Landing(Box<PreparedLanding>),
    Queue(Box<av2_queue::Input>),
    /// Benchmarks: a landing prepared inside the batch, on an unexpanded open
    /// position chosen from the seed, with 20 to 38 children of random FENs
    /// and evals (the store benchmark's write shape) and a real climb.
    Synthetic {
        seed: u64,
        now: Ms,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub enum PipelineOutput {
    Landed(LandingSummary),
    /// EXP-2.7: the position was closed when the landing came.
    AlreadyClosed,
    Queue(av2_queue::Output),
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct LandingSummary {
    pub pos: PosId,
    pub created: usize,
    pub closed: usize,
    pub cascade_writes: usize,
    pub backed_writes: u64,
    pub bought: u64,
    pub absorbed: usize,
}

pub struct PipelineRules {
    pub cfg: RulesConfig,
    pub loops: V1Loops,
    pub queue: QueueState,
    pub proofs: SharedProofs,
    /// The proof calls of each batch computed and not yet committed, oldest
    /// first, with the batch's logical time.
    pending: VecDeque<(Vec<Vec<PosId>>, Ms)>,
}

impl PipelineRules {
    pub fn new(cfg: RulesConfig, queue: QueueState, proofs: SharedProofs) -> PipelineRules {
        PipelineRules { cfg, loops: V1Loops::default(), queue, proofs, pending: VecDeque::new() }
    }

    /// The default campaign on `root` (the start position), gate-off stored
    /// numbers, an empty queue.
    pub fn with_defaults(root: PosId) -> PipelineRules {
        let proofs = Arc::new(RwLock::new(Proofs::new(vec![Campaign::default_for(1, root)], RefreshConfig::default())));
        PipelineRules::new(RulesConfig::default(), QueueState::new(av2_queue::QueueConfig::default()), proofs)
    }
}

fn key_hex(g: &dyn GraphRead) -> impl Fn(PosId) -> String + '_ {
    move |id: PosId| g.fen(id).map(|f| av2_model::Key::of_canonical_fen(&f).to_hex()).unwrap_or_else(|| format!("id:{}", id.0))
}

/// EXP-1.1 (expansion) and EXP-2.2 / EXP-2.6 (the pass's numbers).
fn land(g: &mut dyn GraphWrite, l: &PreparedLanding) -> Result<usize, RulesError> {
    let mut created = 0;
    let mut core = g.core(l.pos);
    if !core.expanded() && !l.children.is_empty() {
        let reachable = core.flag(F_REACHABLE);
        let mut edges: Vec<Edge> = Vec::new();
        g.children(l.pos, &mut edges);
        for c in &l.children {
            let id = match g.lookup(&c.fp) {
                Some(id) => id,
                None => {
                    let mut cc = Core::default();
                    if c.fen.as_deref().and_then(|f| f.split(' ').nth(1)) == Some("b") {
                        cc.flags |= F_BLACK_TO_MOVE;
                    }
                    if let Some(s) = c.terminal {
                        // POS-2.1: born closed, no NODE_CLOSED event.
                        cc.set_status(s, Closure::Terminal, Proof::None);
                        cc.mate_in = 0;
                        cc.clock_slack = if s == Status::Draw { NO_SLACK } else { av2_rules::conv::CLOCK_SLACK_MAX };
                    }
                    created += 1;
                    g.create(c.fp, c.fen.as_deref(), cc)?
                }
            };
            if reachable {
                let mut cc = g.core(id);
                if !cc.flag(F_REACHABLE) {
                    cc.set_flag(F_REACHABLE, true);
                    g.put_core(id, cc)?;
                }
            }
            if !edges.iter().any(|e| e.mv == c.mv) {
                edges.push(Edge { child: id, mv: c.mv });
            }
        }
        g.set_children(l.pos, &edges)?;
        core = g.core(l.pos);
        core.set_flag(F_EXPANDED, true);
    }
    if !l.best_move.is_none() {
        core.best_move = l.best_move;
    }
    if let Some(e) = l.eval_cp {
        core.eval_cp = e;
    }
    g.put_core(l.pos, core)?;
    let mut ext = g.ext(l.pos).unwrap_or_default();
    ext.visits += 1;
    ext.nodes_invested += l.nodes;
    if let Some(a) = &l.analysis {
        ext.analysis = g.put_blob(a)?;
    }
    g.put_ext(l.pos, ext)?;
    Ok(created)
}

fn fen_pool() -> &'static [String] {
    static POOL: std::sync::OnceLock<Vec<String>> = std::sync::OnceLock::new();
    POOL.get_or_init(|| {
        let mut rng = Rng::new(0xFE11);
        (0..4096).map(|_| av2_store::testkit::random_fen(&mut rng)).collect()
    })
}

/// A synthetic prepared landing (benchmarks): `None` on an empty graph.
pub fn synthetic(g: &mut dyn GraphWrite, seed: u64, now: Ms) -> Result<PreparedLanding, RulesError> {
    let mut rng = Rng::new(seed);
    if g.is_empty() {
        let mut c = Core::default();
        c.set_flag(F_REACHABLE, true);
        g.create(av2_store::testkit::random_fp(&mut rng), Some(&fen_pool()[0]), c)?;
    }
    let n = g.len();
    let mut es = Vec::new();
    let mut p = PosId(rng.below(n));
    for _ in 0..64 {
        es.clear();
        g.children(p, &mut es);
        if es.is_empty() && g.core(p).is_open() {
            break;
        }
        p = PosId(rng.below(n));
    }
    let k = 20 + rng.below(19) as usize;
    let pool = fen_pool();
    let children = (0..k)
        .map(|i| PreparedChild {
            mv: Move::new((i / 8) as u8, (i % 8 + 16) as u8, 0),
            fp: av2_store::testkit::random_fp(&mut rng),
            fen: Some(pool[rng.below(pool.len() as u64) as usize].clone()),
            terminal: None,
        })
        .collect::<Vec<_>>();
    let best = children[rng.below(children.len() as u64) as usize].mv;
    // The store benchmark's sizes: a 1.2 kB showcase blob, a 1.3 kB search fact.
    let mut blob = vec![0u8; 1200];
    rng.fill(&mut blob);
    let fact = av2_facts::SearchLanded {
        task: seed,
        position: format!("{:x}", p.0),
        budget_nodes: 128_000_000,
        payload: av2_facts::RawJson(format!(
            "{{\"seed\":{seed},\"lines\":[{}]}}",
            "\"e2e4 e7e5 g1f3 b8c6\",".repeat(40).trim_end_matches(',')
        )),
        ..Default::default()
    };
    Ok(PreparedLanding {
        pos: p,
        children,
        eval_cp: Some(rng.below(600) as i16 - 300),
        best_move: best,
        nodes: 128_000_000,
        task: None,
        arbiter: 128_000_000,
        now,
        analysis: Some(blob),
        fact: Some(Box::new(fact)),
    })
}

impl PipelineRules {
    fn queue_apply(&mut self, g: &dyn GraphWrite, input: &av2_queue::Input, facts: &mut Vec<Fact>) -> av2_queue::Output {
        let env = GraphEnv::new(g);
        let applied = self.queue.apply(input, &env, &mut NoRefill);
        if !applied.deltas.is_empty() || !applied.events.is_empty() {
            facts.push(av2_queue::fact::record(input, &applied));
        }
        applied.output
    }

    fn landing(
        &mut self,
        g: &mut dyn GraphWrite,
        l: &PreparedLanding,
        facts: &mut Vec<Fact>,
        calls: &mut Vec<Vec<PosId>>,
    ) -> Result<PipelineOutput, RulesError> {
        if l.pos.0 >= g.len() {
            return Err(RulesError::Rejected(format!("no position {}", l.pos)));
        }
        if !g.core(l.pos).is_open() {
            // EXP-2.7: nothing is written; the requests it covers are still absorbed.
            self.queue_apply(g, &av2_queue::Input::Landed { now: l.now, pos: l.pos.0, task: l.task, arbiter: l.arbiter }, facts);
            return Ok(PipelineOutput::AlreadyClosed);
        }
        let created = land(g, l)?;
        if let Some(f) = &l.fact {
            facts.push(Fact::SearchLanded((**f).clone()));
        }
        // EXP-6.1 (no line closed anything here): cascade from the position,
        // ascent from it and its parents.
        let mut backed = vec![l.pos];
        g.parents(l.pos, &mut backed);
        let mut rec = Recorder::default();
        let (report, purchases) = {
            let mut pc = ProofCalls::new(&mut rec);
            let mut buyer = self.queue.quality_buyer(l.now);
            let mut z = BoardZeroing;
            let attribution = Attribution::default();
            let r = {
                let mut env = ClimbEnv {
                    cfg: &self.cfg,
                    effects: &mut pc,
                    zeroing: &mut z,
                    tasks: &mut buyer,
                    loops: &mut self.loops,
                    attribution: &attribution,
                };
                climb(g, &[l.pos], &backed, &mut env)?
            };
            pc.landing(l.pos);
            calls.extend(pc.calls);
            (r, buyer.finish_fact())
        };
        facts.extend(purchases);
        {
            let key = key_hex(g);
            for c in &rec.closed {
                let payload = c.payload(&key);
                facts.push(Fact::Opaque(av2_facts::Opaque { kind: "NODE_CLOSED".into(), data: av2_facts::RawJson(payload.to_string()) }));
            }
            facts.extend(rec.events.iter().map(|e| e.to_fact(&key)));
        }
        // CAS-7.1: every closure absorbs the position's PENDING tasks.
        let mut absorbed = 0;
        for c in &rec.closed {
            if let av2_queue::Output::Absorbed { count } =
                self.queue_apply(g, &av2_queue::Input::PositionClosed { now: l.now, pos: c.pos.0 }, facts)
            {
                absorbed += count;
            }
        }
        // EXP-5.2: the landing absorbs the requests it covers.
        if let av2_queue::Output::Absorbed { count } =
            self.queue_apply(g, &av2_queue::Input::Landed { now: l.now, pos: l.pos.0, task: l.task, arbiter: l.arbiter }, facts)
        {
            absorbed += count;
        }
        Ok(PipelineOutput::Landed(LandingSummary {
            pos: l.pos,
            created,
            closed: rec.closed.len(),
            cascade_writes: report.cascade.changed.len(),
            backed_writes: report.ascent.changed,
            bought: report.ascent.bought,
            absorbed,
        }))
    }
}

impl Rules for PipelineRules {
    type Input = PipelineInput;
    type Output = PipelineOutput;

    fn apply(
        &mut self,
        g: &mut dyn GraphWrite,
        inputs: &[PipelineInput],
        facts: &mut Vec<Fact>,
    ) -> Result<Vec<PipelineOutput>, RulesError> {
        let mut calls: Vec<Vec<PosId>> = Vec::new();
        let mut now: Ms = 0;
        let mut out = Vec::with_capacity(inputs.len());
        for input in inputs {
            let o = match input {
                PipelineInput::Landing(l) => {
                    now = now.max(l.now);
                    self.landing(g, l, facts, &mut calls)?
                }
                PipelineInput::Synthetic { seed, now: at } => {
                    now = now.max(*at);
                    let l = synthetic(g, *seed, *at)?;
                    self.landing(g, &l, facts, &mut calls)?
                }
                PipelineInput::Queue(q) => {
                    now = now.max(q.now());
                    PipelineOutput::Queue(self.queue_apply(g, q, facts))
                }
            };
            out.push(o);
        }
        self.pending.push_back((calls, now));
        Ok(out)
    }

    fn after_commit(&mut self, committed: &dyn GraphRead, seq: u64) {
        let Some((calls, now)) = self.pending.pop_front() else { return };
        if calls.is_empty() {
            return;
        }
        let mut p = self.proofs.write().unwrap();
        p.after_batch(committed, &calls, seq, now as f64 / 1000.0);
    }

    fn abandoned(&mut self) {
        self.pending.pop_front();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_landing_expands_climbs_and_absorbs() {
        let mut g = av2_rules::testkit::TestGraph::new();
        let mut rules = PipelineRules::with_defaults(PosId(0));
        let mut facts = Vec::new();
        // First synthetic landing creates the root and expands it.
        let out = rules.apply(&mut g, &[PipelineInput::Synthetic { seed: 1, now: 1_000 }], &mut facts).unwrap();
        let PipelineOutput::Landed(s) = &out[0] else { panic!("{out:?}") };
        assert!(s.created >= 20);
        assert!(g.core(PosId(0)).expanded());
        // A second landing on a child climbs to the root.
        let child = PosId(1);
        let l = PreparedLanding {
            pos: child,
            children: Vec::new(),
            eval_cp: Some(42),
            best_move: Move::NONE,
            nodes: 8_000_000,
            task: None,
            arbiter: 8_000_000,
            now: 2_000,
            analysis: None,
            fact: None,
        };
        rules.apply(&mut g, &[PipelineInput::Landing(Box::new(l))], &mut facts).unwrap();
        assert_eq!(g.ext(child).unwrap().visits, 1);
        // Proof calls are kept per batch until the commit.
        assert_eq!(rules.pending.len(), 2);
        rules.after_commit(&g, 1);
        rules.after_commit(&g, 2);
        assert!(rules.pending.is_empty());
        assert!(!rules.proofs.read().unwrap().table(1).unwrap().is_empty());
    }

    #[test]
    fn a_landing_on_a_closed_position_changes_nothing() {
        let mut g = av2_rules::testkit::TestGraph::new();
        let mut rules = PipelineRules::with_defaults(PosId(0));
        let mut facts = Vec::new();
        rules.apply(&mut g, &[PipelineInput::Synthetic { seed: 1, now: 1_000 }], &mut facts).unwrap();
        let mut c = g.core(PosId(1));
        c.set_status(Status::WhiteWin, Closure::Terminal, Proof::None);
        g.put_core(PosId(1), c).unwrap();
        let before = g.core(PosId(1));
        let l = PreparedLanding {
            pos: PosId(1),
            children: Vec::new(),
            eval_cp: Some(1),
            best_move: Move::NONE,
            nodes: 1,
            task: None,
            arbiter: 1,
            now: 3_000,
            analysis: None,
            fact: None,
        };
        let out = rules.apply(&mut g, &[PipelineInput::Landing(Box::new(l))], &mut facts).unwrap();
        assert_eq!(out[0], PipelineOutput::AlreadyClosed);
        assert_eq!(g.core(PosId(1)), before);
        assert!(g.ext(PosId(1)).is_none());
    }
}
