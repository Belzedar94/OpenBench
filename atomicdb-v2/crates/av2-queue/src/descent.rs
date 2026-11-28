//! The production [`Descent`]: av2-proof's df-pn descent (PN-6.5), value
//! walker (PN-9.11), campaign roots (PN-6.3) and OR clamp (PN-7.1) over the
//! in-memory graph and proof tables.
//!
//! The writer builds one per minting pass from what it holds: the graph view
//! (the committed store, or the batch overlay), the proof tables
//! (`av2_proof::Proofs`, kept current by the writer's refresh hook), and the
//! community campaign rows (relational side).  Owner decision (28-Nov): the
//! stored/served proof numbers are gate-off; the children ranking inside the
//! descents runs with the gate on (`DescentConfig::gate`, default on).

use crate::budget::{Clamp, ValueArm};
use crate::consts::VALUE_SPINE_NODES;
use crate::selector::{Descent, ValueTarget};
use av2_model::PosId;
use av2_proof::descent::{self as pd, CommunityCampaign, DescentConfig, Walker};
use av2_proof::{ProofGraph, Proofs};
use std::collections::BTreeSet;

pub struct ProofDescent<'a, G: ProofGraph + ?Sized> {
    pub graph: &'a G,
    pub proofs: &'a Proofs,
    /// Community campaigns (`Campaign` rows); `campaign_roots` filters them.
    pub community: &'a [CommunityCampaign],
    pub cfg: DescentConfig,
}

impl<'a, G: ProofGraph + ?Sized> ProofDescent<'a, G> {
    /// Production settings: `DESCENT_MAX_PLIES`, gate on for the ranking,
    /// the saturation threshold read from the queue's ladder.
    pub fn new(graph: &'a G, proofs: &'a Proofs, community: &'a [CommunityCampaign]) -> ProofDescent<'a, G> {
        ProofDescent { graph, proofs, community, cfg: DescentConfig { saturated_nodes: VALUE_SPINE_NODES, ..DescentConfig::default() } }
    }

    fn walker(&self, campaign: u64) -> Option<Walker<'_, G>> {
        let id = u32::try_from(campaign).ok()?;
        let c = self.proofs.campaign(id)?;
        let t = self.proofs.table(id)?;
        Some(Walker::new(self.graph, t, c, self.cfg))
    }
}

fn arm(a: pd::ValueArm) -> ValueArm {
    match a {
        pd::ValueArm::Spine => ValueArm::Spine,
        pd::ValueArm::Bottleneck => ValueArm::Bottleneck,
        pd::ValueArm::Rung => ValueArm::Rung,
        pd::ValueArm::Cycle => ValueArm::Cycle,
        pd::ValueArm::Backup => ValueArm::Backup,
        pd::ValueArm::Explore => ValueArm::Explore,
    }
}

impl<G: ProofGraph + ?Sized> Descent for ProofDescent<'_, G> {
    fn active_campaigns(&self) -> Vec<u64> {
        av2_proof::campaign::active_campaigns(&self.proofs.campaigns).into_iter().map(|c| u64::from(c.id)).collect()
    }

    fn campaign_roots(&self) -> Vec<(u64, f64)> {
        pd::campaign_roots(self.graph, self.community).into_iter().map(|(p, w)| (p.0, w)).collect()
    }

    fn descend(&self, campaign: u64, counter: u64, avoid: &BTreeSet<u64>, start: Option<u64>) -> Option<u64> {
        let w = self.walker(campaign)?;
        w.descend(counter, &|p: PosId| avoid.contains(&p.0), start.map(PosId)).0.map(|p| p.0)
    }

    fn descend_value(
        &self,
        campaign: u64,
        counter: u64,
        avoid: &BTreeSet<u64>,
        start: Option<u64>,
        live: &dyn Fn(u64) -> bool,
    ) -> Option<ValueTarget> {
        let w = self.walker(campaign)?;
        w.descend_value(counter, &|p: PosId| avoid.contains(&p.0), &|p: PosId| live(p.0), start.map(PosId))
            .map(|t| ValueTarget { pos: t.pos.0, arm: arm(t.arm), floor_nodes: t.floor_nodes })
    }

    fn proved_or_clamp(&self, pos: u64, campaigns: &[u64]) -> Option<Clamp> {
        let cs: Vec<&av2_proof::Campaign> =
            campaigns.iter().filter_map(|&c| u32::try_from(c).ok().and_then(|c| self.proofs.campaign(c))).collect();
        av2_proof::clamp::proved_or_clamp(self.graph, PosId(pos), &cs, true)
    }
}
