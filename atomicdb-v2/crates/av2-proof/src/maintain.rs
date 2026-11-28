//! The writer-side owner of the proof tables (PN-4.2, PN-8, PN-4.6, PN-10.5).
//!
//! **How the writer uses it (PN-8 in v2).**  v1 refreshes proof numbers at
//! the end of every `backup_cascade` call with `seeds = cascade seeds +
//! changed keys`, either inline or replayed after the batch's commit in call
//! order (PN-8.1, PN-8.2), plus the landed position after `apply_job`
//! (PN-8.4).  Proof numbers read statuses and evals and never feed back into
//! them, so the v2 writer:
//!
//! 1. runs the batch's rules (cascade, ascent) on the overlay, collecting
//!    each cascade call's seed list in order (`calls`), plus one call per
//!    landing with its own position, exactly where v1 refreshes;
//! 2. commits the frame (proof numbers are not in it: D5);
//! 3. calls [`Proofs::after_batch`] with the committed view and `calls`.
//!
//! Because no other writer can change the graph between 2 and 3, replaying
//! the calls after commit gives the same numbers as refreshing inline (the
//! property v1 had to argue for in PR #240, here by construction).
//!
//! Readers share the state behind an `RwLock` (`SharedProofs`); a batch's
//! refresh holds the write lock for its duration (measured: see the bench).

use crate::campaign::{Campaign, active_campaigns};
use crate::gate::GateLog;
use crate::graph::ProofGraph;
use crate::numbers::{self, LeafInput, PROOF_INFINITY};
use crate::refresh::{RefreshConfig, RefreshCtx, RefreshReport, refresh_campaign};
use crate::table::{IdSet, ProofTable, Row};
use av2_model::{PosId, Status};
use std::sync::{Arc, RwLock};

pub type SharedProofs = Arc<RwLock<Proofs>>;

#[derive(Clone)]
pub struct Proofs {
    pub campaigns: Vec<Campaign>,
    tables: Vec<ProofTable>,
    pub cfg: RefreshConfig,
    pub gate_log: GateLog,
}

impl Proofs {
    pub fn new(campaigns: Vec<Campaign>, cfg: RefreshConfig) -> Proofs {
        let tables = campaigns.iter().map(|_| ProofTable::new()).collect();
        Proofs { campaigns, tables, cfg, gate_log: GateLog::new() }
    }

    fn index(&self, campaign_id: u32) -> Option<usize> {
        self.campaigns.iter().position(|c| c.id == campaign_id)
    }

    pub fn table(&self, campaign_id: u32) -> Option<&ProofTable> {
        self.index(campaign_id).map(|i| &self.tables[i])
    }
    pub fn table_mut(&mut self, campaign_id: u32) -> Option<&mut ProofTable> {
        self.index(campaign_id).map(move |i| &mut self.tables[i])
    }
    pub fn campaign(&self, campaign_id: u32) -> Option<&Campaign> {
        self.index(campaign_id).map(|i| &self.campaigns[i])
    }
    /// Replace a campaign's table (startup rebuild).
    pub fn set_table(&mut self, campaign_id: u32, t: ProofTable) {
        if let Some(i) = self.index(campaign_id) {
            self.tables[i] = t;
        }
    }

    /// PN-4.2 `refresh_proof_numbers` over every active campaign, in id order.
    pub fn refresh<G: ProofGraph + ?Sized>(&mut self, g: &G, seeds: &[PosId], seq: u64, now: f64) -> RefreshReport {
        let n = g.positions();
        let mut seen = IdSet::default();
        let seeds: Vec<PosId> = seeds.iter().copied().filter(|s| s.0 < n && seen.insert(s.0)).collect();
        let mut total = RefreshReport::default();
        if seeds.is_empty() {
            return total;
        }
        let order: Vec<usize> = {
            let act = active_campaigns(&self.campaigns);
            act.iter().map(|c| self.index(c.id).unwrap()).collect()
        };
        for i in order {
            let mut ctx = RefreshCtx { seq, now, gate_log: &mut self.gate_log };
            let r = refresh_campaign(g, &mut self.tables[i], &self.campaigns[i], &seeds, &self.cfg, &mut ctx);
            total.absorb(r);
        }
        total
    }

    /// PN-8.1/8.2/8.4: replay the batch's refresh calls in order.
    pub fn after_batch<G: ProofGraph + ?Sized>(&mut self, g: &G, calls: &[Vec<PosId>], seq: u64, now: f64) -> RefreshReport {
        let mut total = RefreshReport::default();
        for call in calls {
            total.absorb(self.refresh(g, call, seq, now));
        }
        total
    }

    /// PN-4.6 `root_numbers`: the root's row, else its leaf numbers (never logs).
    pub fn root_numbers<G: ProofGraph + ?Sized>(&self, g: &G, campaign_id: u32) -> Option<(u64, u64)> {
        let i = self.index(campaign_id)?;
        let c = &self.campaigns[i];
        if let Some(r) = self.tables[i].get(c.root) {
            return Some(r.numbers());
        }
        let node = g.node(c.root);
        let annoyance = self.cfg.gate.full(|| g.gate_row(c.root));
        let leaf = LeafInput { black_to_move: node.black_to_move, status: node.status, eval_cp: node.eval_cp };
        Some(numbers::leaf_numbers(leaf, c.goal, None, annoyance, false).0)
    }

    /// PN-4.6 `headline_numbers`: root numbers of the first active campaign.
    pub fn headline_numbers<G: ProofGraph + ?Sized>(&self, g: &G) -> (Option<u64>, Option<u64>) {
        match crate::campaign::headline_campaign(&self.campaigns) {
            None => (None, None),
            Some(c) => match self.root_numbers(g, c.id) {
                Some((p, d)) => (Some(p), Some(d)),
                None => (None, None),
            },
        }
    }

    /// PN-4.6 `views._proof_numbers_for`: numbers of the first active
    /// campaign; `None` (absent, not `(1, 1)`) where there is no row.
    pub fn numbers_for(&self, ids: &[PosId]) -> Vec<Option<(u64, u64)>> {
        let t = crate::campaign::headline_campaign(&self.campaigns).and_then(|c| self.table(c.id));
        ids.iter().map(|&id| t.and_then(|t| t.get(id)).map(|r| r.numbers())).collect()
    }

    /// PN-10.5 `recascade_proof`: refresh saturated open rows until a pass
    /// writes nothing.  Returns the census before, after, and the passes.
    pub fn recascade<G: ProofGraph + ?Sized>(
        &mut self,
        g: &G,
        keys: Option<&[PosId]>,
        chunk: usize,
        max_passes: u32,
        seq: u64,
    ) -> RecascadeReport {
        let before = self.census(g);
        let mut rep = RecascadeReport { before, ..Default::default() };
        for pass in 1..=max_passes {
            let seeds: Vec<PosId> = match keys {
                Some(k) => k.to_vec(),
                None => self.saturated_open_ids(g),
            };
            if seeds.is_empty() {
                rep.end = RecascadeEnd::NothingSaturated(pass);
                break;
            }
            let mut written = 0;
            for part in seeds.chunks(chunk.max(1)) {
                written += self.refresh(g, part, seq, 0.0).written;
            }
            rep.passes.push((seeds.len(), written, self.census(g)));
            if written == 0 {
                rep.end = RecascadeEnd::Fixpoint(pass);
                break;
            }
            if pass == max_passes {
                rep.end = RecascadeEnd::PassCap(max_passes);
            }
        }
        rep.after = self.census(g);
        rep
    }

    /// `(impossible, dn, pn)` over every campaign (PN-10.3 with campaign=None).
    pub fn census<G: ProofGraph + ?Sized>(&self, g: &G) -> (u64, u64, u64) {
        let mut out = (0, 0, 0);
        for t in &self.tables {
            out.0 += crate::frontier::saturated_open_count(g, t, crate::frontier::Saturation::Impossible);
            out.1 += crate::frontier::saturated_open_count(g, t, crate::frontier::Saturation::Dn);
            out.2 += crate::frontier::saturated_open_count(g, t, crate::frontier::Saturation::Pn);
        }
        out
    }

    /// Distinct ids of open rows with `pn >= INF or dn >= INF`, any campaign,
    /// in id order (v1's query has no order; id order is D3's choice).
    pub fn saturated_open_ids<G: ProofGraph + ?Sized>(&self, g: &G) -> Vec<PosId> {
        let mut v: Vec<u64> = Vec::new();
        for t in &self.tables {
            for (id, r) in t.explicit() {
                if (r.pn >= PROOF_INFINITY || r.dn >= PROOF_INFINITY) && g.node(id).status == Status::Unknown {
                    v.push(id.0);
                }
            }
            // Implicit rows are (0, INF) or (INF, 0): saturated when open.
            for id in t.ids() {
                if let Some(r) = t.get(id)
                    && (r == Row::PROVED || r == Row::REFUTED)
                    && g.node(id).status == Status::Unknown
                {
                    v.push(id.0);
                }
            }
        }
        v.sort_unstable();
        v.dedup();
        v.into_iter().map(PosId).collect()
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum RecascadeEnd {
    #[default]
    None,
    NothingSaturated(u32),
    Fixpoint(u32),
    PassCap(u32),
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RecascadeReport {
    pub before: (u64, u64, u64),
    pub after: (u64, u64, u64),
    /// `(seeds, rows written, census after)` per pass.
    pub passes: Vec<(usize, u64, (u64, u64, u64))>,
    pub end: RecascadeEnd,
}
