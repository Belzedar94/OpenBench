//! Step 4 of the port: the status cascade (CAS-1.1, CAS-1.4) with its guards
//! (CAS-5) and the closure side effects (CAS-7.1, CAS-4); `closable_rows`
//! (CAS-2.1).
//!
//! **Pop order (owner decision D3): FIFO.**  v1 pops a Python `set`, whose
//! order of string keys changes with `PYTHONHASHSEED`.  Here the frontier is
//! a queue with set semantics: seeds in their order, then the parents of each
//! seed in parent-list order; a key already queued is not queued again; a
//! popped key may be queued again later.  The fixed point is the same as
//! v1's whenever neither guard bites (statuses are monotone and the distance
//! rules are functions of the children); where a guard cuts a cascade the
//! rows left behind can differ from any one v1 run, which differs from run to
//! run itself.

use crate::config::RulesConfig;
use crate::conv::{is_decisive, mate_of, stm_white, store_mate};
use crate::distance::{DtmOpts, mate_distance_refresh};
use crate::effects::{Attribution, Effects, Event, NodeClosed};
use crate::exact::{Kid, Zeroing, backup_status, minimax_proof, minimax_witness, node_minimax_slack, supporting_status};
use crate::read::kids_by_move;
use av2_model::record::NO_SLACK;
use av2_model::{Closure, Edge, GraphRead, GraphWrite, Move, PosId, Status};
use std::collections::{HashMap, HashSet, VecDeque};

/// What the rules need besides the graph: the switches, the effect sink, the
/// zeroing oracle (DTM-5.1) and the closure attribution of the apply.
pub struct Env<'a> {
    pub cfg: &'a RulesConfig,
    pub effects: &'a mut dyn Effects,
    pub zeroing: &'a mut dyn Zeroing,
    pub attribution: &'a Attribution,
}

/// A queue with set semantics.
#[derive(Default, Debug)]
pub struct FifoSet {
    q: VecDeque<PosId>,
    member: HashSet<PosId>,
}

impl FifoSet {
    pub fn push(&mut self, k: PosId) {
        if self.member.insert(k) {
            self.q.push_back(k);
        }
    }
    pub fn pop(&mut self) -> Option<PosId> {
        let k = self.q.pop_front()?;
        self.member.remove(&k);
        Some(k)
    }
    pub fn len(&self) -> usize {
        self.q.len()
    }
    pub fn is_empty(&self) -> bool {
        self.q.is_empty()
    }
}

/// Answers DTM-5.1 for one node, reading its FEN at most once.
pub(crate) struct NodeZeroing<'a, 'b> {
    g: &'a dyn GraphRead,
    id: PosId,
    fen: Option<Option<String>>,
    z: &'b mut dyn Zeroing,
}

impl<'a, 'b> NodeZeroing<'a, 'b> {
    pub(crate) fn new(g: &'a dyn GraphRead, id: PosId, z: &'b mut dyn Zeroing) -> Self {
        NodeZeroing { g, id, fen: None, z }
    }
    pub(crate) fn is_zeroing(&mut self, mv: Move) -> bool {
        if self.fen.is_none() {
            self.fen = Some(self.g.fen(self.id));
        }
        match self.fen.as_ref().unwrap() {
            Some(f) => self.z.is_zeroing(f, mv),
            // A position without a stored FEN (synthetic graphs): no move resets.
            None => false,
        }
    }
}

/// What a cascade did.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CascadeReport {
    /// The keys written, in write order (`report['changed']`).
    pub changed: Vec<PosId>,
    /// Pops made (`iterations`).
    pub pops: u64,
    /// Keys left in the frontier when the pop guard stopped the cascade.
    pub remaining: u64,
    /// Keys that hit the rewrite limit, sorted by id.
    pub cycling: Vec<PosId>,
}

/// CAS-1.1 `backup_cascade`: the fixed point of statuses from `seeds`.
pub fn backup_cascade(g: &mut dyn GraphWrite, seeds: &[PosId], env: &mut Env) -> Result<CascadeReport, av2_model::graph::GraphError> {
    let cfg = env.cfg;
    let mut frontier = FifoSet::default();
    for &s in seeds {
        frontier.push(s);
    }
    let mut pbuf: Vec<PosId> = Vec::new();
    for &s in seeds {
        pbuf.clear();
        g.parents(s, &mut pbuf);
        for &p in &pbuf {
            frontier.push(p);
        }
    }
    let mut report = CascadeReport::default();
    let mut rewrites: HashMap<PosId, u32> = HashMap::new();
    let mut cycling: Vec<PosId> = Vec::new();
    let mut ebuf: Vec<Edge> = Vec::new();
    while !frontier.is_empty() && report.pops < cfg.cascade_guard_limit {
        report.pops += 1;
        let key = frontier.pop().unwrap();
        let mut pos = g.core(key);
        let kids = kids_by_move(&*g, key, &mut ebuf);
        if kids.is_empty() {
            continue;
        }
        let stm = stm_white(&pos);
        let mut dirty = false;
        {
            let gr: &dyn GraphRead = &*g;
            let mut z = NodeZeroing::new(gr, key, &mut *env.zeroing);
            let new_status = if pos.status() != Status::Unknown {
                pos.status()
            } else {
                let statuses: Vec<Status> = kids.iter().map(|k| supporting_status(k, cfg.fresh_context, |m| z.is_zeroing(m))).collect();
                backup_status(stm, pos.expanded(), &statuses)
            };
            if new_status != pos.status() && pos.status() == Status::Unknown {
                let proof = minimax_proof(stm, &kids, new_status);
                pos.set_status(new_status, Closure::Minimax, proof);
                if let Some(w) = minimax_witness(&kids, new_status) {
                    pos.best_move = w;
                }
                dirty = true;
                env.effects.node_closed(NodeClosed {
                    pos: key,
                    status: new_status,
                    closure: Closure::Minimax,
                    proof,
                    source: env.attribution.source,
                    provenance: env.attribution.provenance.clone(),
                    extra: Vec::new(),
                });
            }
            if pos.closure() == Closure::Minimax && pos.status() != Status::Unknown {
                let inherited = minimax_proof(stm, &kids, pos.status());
                if inherited != pos.proof() {
                    pos.set_status(pos.status(), pos.closure(), inherited);
                    dirty = true;
                }
                let slack = node_minimax_slack(stm, pos.status(), &kids, |m| z.is_zeroing(m));
                if slack != pos.slack() {
                    pos.clock_slack = slack.unwrap_or(NO_SLACK);
                    dirty = true;
                }
            }
            if is_decisive(pos.status()) {
                let opts = DtmOpts { hops: cfg.mate_support_hops, ..DtmOpts::default() };
                let (m, b) = mate_distance_refresh(gr, key, &pos, &kids, opts);
                if (m, b) != (mate_of(&pos), pos.best_move) {
                    pos.mate_in = store_mate(m);
                    pos.best_move = b;
                    dirty = true;
                }
            }
        }
        if dirty && rewrites.get(&key).copied().unwrap_or(0) >= cfg.cascade_rewrite_limit {
            if !cycling.contains(&key) {
                cycling.push(key);
            }
            dirty = false;
        }
        if dirty {
            *rewrites.entry(key).or_insert(0) += 1;
            g.put_core(key, pos)?;
            report.changed.push(key);
            pbuf.clear();
            g.parents(key, &mut pbuf);
            for &p in &pbuf {
                frontier.push(p);
            }
        }
    }
    report.remaining = frontier.len() as u64;
    if !frontier.is_empty() {
        env.effects.event(Event::CascadeGuard { iterations: report.pops, remaining: report.remaining, seed_count: seeds.len() as u64 });
    }
    cycling.sort();
    if !cycling.is_empty() {
        env.effects.event(Event::CascadeCycle {
            keys: cycling.iter().take(20).copied().collect(),
            count: cycling.len() as u64,
            limit: cfg.cascade_rewrite_limit,
            iterations: report.pops,
            seed_count: seeds.len() as u64,
        });
    }
    report.cycling = cycling;
    let mut refresh: Vec<PosId> = seeds.to_vec();
    refresh.extend_from_slice(&report.changed);
    env.effects.proof_refresh(&refresh);
    Ok(report)
}

/// CAS-2.1 `closable_rows`: the open positions among `keys` that their
/// children decide now, with the status, in the order of `keys` (first
/// occurrence).  Read only.
pub fn closable_rows(g: &dyn GraphRead, keys: &[PosId], cfg: &RulesConfig, zeroing: &mut dyn Zeroing) -> Vec<(PosId, Status)> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    let mut ebuf = Vec::new();
    for &k in keys {
        if !seen.insert(k) {
            continue;
        }
        let row = g.core(k);
        if row.status() != Status::Unknown {
            continue;
        }
        let kids: Vec<Kid> = crate::read::kids_in_edge_order(g, k, &mut ebuf);
        if kids.is_empty() {
            continue;
        }
        let mut z = NodeZeroing::new(g, k, zeroing);
        let statuses: Vec<Status> = kids.iter().map(|kd| supporting_status(kd, cfg.fresh_context, |m| z.is_zeroing(m))).collect();
        let s = backup_status(stm_white(&row), row.expanded(), &statuses);
        if s != Status::Unknown {
            out.push((k, s));
        }
    }
    out
}
