//! Stage (b) of one batch (spec section 2.3 steps 2 and 3): the status
//! cascade from the cascade seeds, then the backed ascent from the backed
//! seeds.  Stage (a) (landing a search: expansion, lines, claims, mate-line
//! closures) is ported separately and ends here with the seed lists of its
//! `Landing` (EXP-6.1); a batch of landings merges them (EXP-6.2).
//!
//! The arms of section 2.3 step 4 (`_uncertainty_expand`, the value cascade,
//! idle repair, autosolve) and the rechecks after commit (CAS-6) are not
//! here: the arms are the queue model's, and in v2's single writer there is
//! nothing to recheck.

use crate::ascent::{AscentEnv, AscentOpts, AscentReport, backup_backed_evals};
use crate::cascade::{CascadeReport, Env, backup_cascade};
use crate::config::RulesConfig;
use crate::effects::{Attribution, Effects};
use crate::exact::Zeroing;
use crate::loops::LoopRules;
use crate::purchase::TaskSink;
use av2_model::{GraphWrite, PosId};

/// Everything a climb needs besides the graph.
pub struct ClimbEnv<'a> {
    pub cfg: &'a RulesConfig,
    pub effects: &'a mut dyn Effects,
    pub zeroing: &'a mut dyn Zeroing,
    pub tasks: &'a mut dyn TaskSink,
    pub loops: &'a mut dyn LoopRules,
    pub attribution: &'a Attribution,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ClimbReport {
    pub cascade: CascadeReport,
    pub ascent: AscentReport,
}

/// `backup_cascade(cascade_seeds)` then `backup_backed_evals(backed_seeds)`.
pub fn climb(
    g: &mut dyn GraphWrite,
    cascade_seeds: &[PosId],
    backed_seeds: &[PosId],
    env: &mut ClimbEnv,
) -> Result<ClimbReport, av2_model::graph::GraphError> {
    let cascade = {
        let mut e = Env { cfg: env.cfg, effects: &mut *env.effects, zeroing: &mut *env.zeroing, attribution: env.attribution };
        backup_cascade(g, cascade_seeds, &mut e)?
    };
    let ascent = {
        let mut e = AscentEnv { cfg: env.cfg, effects: &mut *env.effects, tasks: &mut *env.tasks, loops: &mut *env.loops };
        backup_backed_evals(g, backed_seeds, AscentOpts::default(), &mut e)?
    };
    Ok(ClimbReport { cascade, ascent })
}

/// EXP-6.2: the seeds of a batch of landings, merged without duplicates in
/// key order.  v1 sorts by the hex key; here by the position id (D3).  One
/// landing keeps its lists in order (call `climb` with them directly).
pub fn merge_seeds(lists: &[&[PosId]]) -> Vec<PosId> {
    let mut v: Vec<PosId> = lists.iter().flat_map(|l| l.iter().copied()).collect();
    v.sort();
    v.dedup();
    v
}
