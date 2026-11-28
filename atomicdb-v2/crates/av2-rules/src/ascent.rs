//! Step 8 of the port: the backed ascent.
//!
//! - BKD-7.1 `backup_backed_evals`, the level loop
//! - BKD-7.2 `_decide_level` (steps 2, 4 and 7 are the loop rules' hooks,
//!   `loops::LoopRules`; with [`NoLoops`](crate::loops::NoLoops) they do
//!   nothing, as section 16 says for step 8)
//! - BKD-7.3 `_rows_that_wait`
//! - BKD-7.4 the revisit cap, BKD-7.5 the node budget, BKD-7.6 the settling
//!   look (its trigger, `returning`, only fills once the loop rules mark
//!   returning children)
//! - BKD-8.1 the noise cut (switch `RulesConfig::noise_cut`, D2) and BKD-8.2
//!   the parents to wake
//! - BKD-9.1 guard events; the ply-guard frontier is returned for BKD-9.2
//! - BKD-12.1 `stale_backed_rows`
//! - BKD-11 the purchases at the end of each level (`purchase.rs`)
//!
//! **Orders (owner decision D3).**  v1 reads a level's rows with no ORDER BY
//! and wakes parents from a Python set.  Neither changes a value (a level is
//! decided as a whole); they order the purchases of a level (which fit under
//! the arm's room) and the `rested`/`written` lists.  Here rows are read in
//! id order and woken parents are listed in id order.

use crate::backed::{Backed, ChildValue, Discrepancy, Row, Showcase, backed_for, children_of, showcase_from_graph};
use crate::config::{BACKED_EPSILON_CP, BACKED_GUARD_KEYS, RulesConfig};
use crate::conv::status_eval;
use crate::effects::{Effects, Event, GuardReason};
use crate::loops::{LevelChildren, LoopRules};
use crate::purchase::{TaskSink, queue_cycle_disambiguation, queue_quality_convergence};
use av2_model::record::NO_CP;
use av2_model::{Edge, GraphRead, GraphWrite, Move, PosId, Status};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

/// The arguments of `backup_backed_evals` besides the seeds.
#[derive(Clone, Copy, Debug)]
pub struct AscentOpts {
    /// `max_plies` (default `RulesConfig::backed_max_plies`).
    pub max_plies: Option<u64>,
    /// `open_only`: proven rows are left out of every level.
    pub open_only: bool,
    /// `generation`: how many times a cut frontier has been carried (BKD-9).
    pub generation: u32,
}

impl Default for AscentOpts {
    fn default() -> Self {
        AscentOpts { max_plies: None, open_only: false, generation: 0 }
    }
}

/// How an ascent ended (v1's `report` dict).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct AscentReport {
    pub levels: u64,
    pub processed: u64,
    pub changed: u64,
    pub guard: Option<GuardReason>,
    /// `len(set(left) | set(dropped))`.
    pub left: u64,
    /// Rows decided and found already stored, in level order.
    pub rested_keys: Vec<PosId>,
    /// Rows rewritten, in level order.
    pub written_keys: Vec<PosId>,
    /// The frontier a ply-guard cut leaves (BKD-9.2 `carried_cuts`).
    pub carry: Vec<PosId>,
    /// Purchases that counted (BKD-11).
    pub bought: u64,
}

/// Everything an ascent needs besides the graph.
pub struct AscentEnv<'a> {
    pub cfg: &'a RulesConfig,
    pub effects: &'a mut dyn Effects,
    pub tasks: &'a mut dyn TaskSink,
    pub loops: &'a mut dyn LoopRules,
}

/// One level, decided and not yet written (`_LevelDecision`).
pub struct LevelDecision {
    pub children: LevelChildren,
    pub decided: BTreeMap<PosId, Backed>,
    pub waiting: BTreeSet<PosId>,
    pub wake: Vec<PosId>,
    pub returning: Vec<PosId>,
    pub discrepancies: Vec<Discrepancy>,
    pub cycle_picks: Vec<Discrepancy>,
}

fn row_of(g: &dyn GraphRead, id: PosId) -> Row {
    Row { id, core: g.core(id), nodes_invested: g.ext(id).map(|e| e.nodes_invested).unwrap_or(0) }
}

fn stored_of(g: &dyn GraphRead, id: PosId) -> Backed {
    Backed::stored(&g.core(id), &g.ext(id).unwrap_or_default())
}

/// BKD-7.2 `_decide_level`: what one level writes for `rows`, without
/// writing or buying anything.
pub fn decide_level(g: &dyn GraphRead, rows: &[Row], loops: &mut dyn LoopRules) -> LevelDecision {
    let mut buf: Vec<Edge> = Vec::new();
    let mut children: LevelChildren = BTreeMap::new();
    for r in rows {
        let kids = children_of(g, r.id, &mut buf);
        if !kids.is_empty() {
            children.insert(r.id, kids);
        }
    }
    let open_rows: Vec<PosId> = rows.iter().filter(|r| status_eval(r.core.status()).is_none()).map(|r| r.id).collect();
    // Step 2: mark returning children (LOOP-1.1).
    let (marked, elsewhere) = loops.mark_returning(g, &mut children);
    let returning: Vec<PosId> = {
        let mut v: Vec<PosId> = marked.into_iter().filter(|p| open_rows.contains(p)).collect();
        v.sort();
        v.dedup();
        v
    };
    // Step 3: returns / circular for the ties of this level (BKD-6.4).
    let elsewhere: HashSet<(PosId, Move)> = elsewhere.into_iter().collect();
    for (parent, kids) in children.iter_mut() {
        for k in kids.iter_mut() {
            k.returns = k.cycles;
            k.circular = elsewhere.contains(&(*parent, k.mv));
        }
    }
    // Step 4: value returning children on the line that returns (LOOP-2.1).
    loops.value_returning(g, &mut children, &open_rows);
    // Step 5: the showcases of the rows with a gap.
    let mut showcases: BTreeMap<PosId, Showcase> = BTreeMap::new();
    for r in rows {
        let gap = !r.core.expanded() || children.get(&r.id).is_some_and(|ks| ks.iter().any(|c| c.value.is_none() || c.floor));
        if gap {
            showcases.insert(r.id, showcase_from_graph(g, r.id));
        }
    }
    // Step 6: decide each row.
    let mut discrepancies = Vec::new();
    let mut cycle_picks = Vec::new();
    let mut decided: BTreeMap<PosId, Backed> = BTreeMap::new();
    let empty = Showcase::default();
    for r in rows {
        loops.wrote(r.id, r.core.backed_move);
        let show = showcases.get(&r.id).unwrap_or(&empty);
        let mut none: Vec<ChildValue> = Vec::new();
        let kids = children.get_mut(&r.id).unwrap_or(&mut none);
        let d = backed_for(r, kids, Some(&mut discrepancies), show, Some(&mut cycle_picks));
        loops.wrote(r.id, d.mv);
        decided.insert(r.id, d);
    }
    // Step 7: pointer loops (LOOP-3.1, LOOP-4.1).
    let verdict = loops.open_pointer_loops(g, rows, &mut children, &mut decided, &showcases, &mut cycle_picks);
    let ruled: HashSet<PosId> = verdict.ruled.into_iter().collect();
    // Step 8: rows that wait (BKD-7.3).
    let stored: HashMap<PosId, Backed> = rows.iter().map(|r| (r.id, stored_of(g, r.id))).collect();
    let waiting = rows_that_wait(rows, &children, &decided, &stored, &ruled);
    LevelDecision { children, decided, waiting, wake: verdict.wake, returning, discrepancies, cycle_picks }
}

/// BKD-7.3 `_rows_that_wait`.
pub fn rows_that_wait(
    rows: &[Row],
    children: &LevelChildren,
    decided: &BTreeMap<PosId, Backed>,
    stored: &HashMap<PosId, Backed>,
    ruled: &HashSet<PosId>,
) -> BTreeSet<PosId> {
    let changed: HashSet<PosId> = decided.iter().filter(|(k, v)| stored[*k] != **v).map(|(k, _)| *k).collect();
    let moved: HashSet<PosId> = changed
        .iter()
        .copied()
        .filter(|k| {
            let (d, s) = (decided[k], stored[k]);
            (d.value, d.plies, d.quality) != (s.value, s.plies, s.quality)
        })
        .collect();
    let mut stands_on: BTreeMap<PosId, PosId> = BTreeMap::new();
    for r in rows {
        let mv = decided[&r.id].mv;
        if !changed.contains(&r.id) || mv.is_none() || ruled.contains(&r.id) {
            continue;
        }
        if let Some(c) = children.get(&r.id).and_then(|ks| ks.iter().find(|c| c.mv == mv)) {
            if moved.contains(&c.id) && !c.cycles && !c.returns {
                stands_on.insert(r.id, c.id);
            }
        }
    }
    let mut writes: HashMap<PosId, bool> = HashMap::new();
    for &start in stands_on.keys() {
        let mut chain: Vec<PosId> = Vec::new();
        let mut node = start;
        while stands_on.contains_key(&node) && !writes.contains_key(&node) && !chain.contains(&node) {
            chain.push(node);
            node = stands_on[&node];
        }
        let mut below;
        if let Some(at) = chain.iter().position(|&k| k == node) {
            for &k in &chain[at..] {
                writes.insert(k, true);
            }
            chain.truncate(at);
            below = true;
        } else {
            below = writes.get(&node).copied().unwrap_or(true);
        }
        for &k in chain.iter().rev() {
            below = !below;
            writes.insert(k, below);
        }
    }
    writes.into_iter().filter(|(_, w)| !w).map(|(k, _)| k).collect()
}

/// BKD-8.1 `_backed_worth_propagating`: does this change wake the parents
/// that do not stand on this row?
pub fn worth_propagating(stored: &Backed, value: Option<i32>, mv: Move) -> bool {
    if stored.value.is_none() != value.is_none() {
        return true;
    }
    let Some(v) = value else { return false };
    if stored.mv != mv {
        return true;
    }
    (v - stored.value.unwrap()).abs() >= BACKED_EPSILON_CP
}

/// BKD-8.2 `_parents_to_wake`, in id order: every parent of a `propagate`
/// row, and the parents of a `standing` row whose `backed_move` is the edge
/// to it.
pub fn parents_to_wake(g: &dyn GraphRead, propagate: &[PosId], standing: &[PosId]) -> Vec<PosId> {
    let wide: HashSet<PosId> = propagate.iter().copied().collect();
    let mut keys: Vec<PosId> = propagate.iter().chain(standing).copied().collect();
    keys.sort();
    keys.dedup();
    let mut out: BTreeSet<PosId> = BTreeSet::new();
    let mut pbuf = Vec::new();
    let mut ebuf = Vec::new();
    for child in keys {
        pbuf.clear();
        g.parents(child, &mut pbuf);
        for &p in &pbuf {
            if wide.contains(&child) {
                out.insert(p);
                continue;
            }
            let pm = g.core(p).backed_move;
            if pm.is_none() {
                continue;
            }
            ebuf.clear();
            g.children(p, &mut ebuf);
            if ebuf.iter().any(|e| e.child == child && e.mv == pm) {
                out.insert(p);
            }
        }
    }
    out.into_iter().collect()
}

fn write_backed(g: &mut dyn GraphWrite, id: PosId, b: &Backed) -> Result<(), av2_model::graph::GraphError> {
    let mut core = g.core(id);
    core.backed_eval = b.value.map(|v| v as i16).unwrap_or(NO_CP);
    core.backed_move = b.mv;
    core.backed_plies = b.plies.clamp(0, u16::MAX as i64) as u16;
    g.put_core(id, core)?;
    let ext = g.ext(id);
    if ext.is_some() || b.quality != 0 {
        let mut e = ext.unwrap_or_default();
        e.backed_nodes = b.quality;
        g.put_ext(id, e)?;
    }
    Ok(())
}

fn dedup_in_order(v: impl IntoIterator<Item = PosId>) -> Vec<PosId> {
    let mut seen = HashSet::new();
    v.into_iter().filter(|k| seen.insert(*k)).collect()
}

fn guard_event(
    effects: &mut dyn Effects,
    reason: GuardReason,
    keys: &[PosId],
    processed: u64,
    plies: u64,
    seeds: usize,
    generation: u32,
    dropped: Option<u64>,
) {
    let mut k: Vec<PosId> = keys.to_vec();
    k.sort();
    k.dedup();
    let left = k.len() as u64;
    k.truncate(BACKED_GUARD_KEYS);
    effects.event(Event::BackedGuard { reason, processed, plies, seed_count: seeds as u64, generation, left, keys: k, dropped });
}

/// BKD-7.1 `backup_backed_evals`: recompute the backed value from `seeds` and
/// climb while it matters.
pub fn backup_backed_evals(
    g: &mut dyn GraphWrite,
    seeds: &[PosId],
    opts: AscentOpts,
    env: &mut AscentEnv,
) -> Result<AscentReport, av2_model::graph::GraphError> {
    let cfg = env.cfg;
    let max_plies = opts.max_plies.unwrap_or(cfg.backed_max_plies);
    let mut frontier: Vec<PosId> = dedup_in_order(seeds.iter().copied());
    let mut visits: HashMap<PosId, u32> = HashMap::new();
    let mut report = AscentReport::default();
    let mut dropped: Vec<PosId> = Vec::new();
    let mut returning: BTreeSet<PosId> = BTreeSet::new();
    let mut settling = false;
    let mut looked: HashSet<PosId> = HashSet::new();
    let mut guard: Option<GuardReason> = None;
    let mut left: Vec<PosId> = Vec::new();
    env.loops.begin_ascent();
    let n = g.len();
    let mut plies = 0u64;
    loop {
        if frontier.is_empty() {
            break;
        }
        if plies >= max_plies {
            guard = Some(GuardReason::PlyGuard);
            left = frontier.clone();
            break;
        }
        plies += 1;
        // Rows of the frontier, in id order (D3), proven ones out with open_only.
        let mut ids: Vec<PosId> = frontier.iter().copied().filter(|k| k.0 < n).collect();
        ids.sort();
        let rows: Vec<Row> =
            ids.into_iter().map(|id| row_of(&*g, id)).filter(|r| !opts.open_only || r.core.status() == Status::Unknown).collect();
        if rows.is_empty() {
            break;
        }
        report.processed += rows.len() as u64;
        let level = decide_level(&*g, &rows, &mut *env.loops);
        returning.extend(level.returning.iter().copied());
        let mut propagate: Vec<PosId> = Vec::new();
        let mut standing: Vec<PosId> = Vec::new();
        let mut dirty: Vec<(PosId, Backed)> = Vec::new();
        for r in &rows {
            if level.waiting.contains(&r.id) {
                env.loops.wrote(r.id, r.core.backed_move);
                continue;
            }
            let d = level.decided[&r.id];
            let stored = stored_of(&*g, r.id);
            if stored == d {
                report.rested_keys.push(r.id);
                continue;
            }
            if !cfg.noise_cut || worth_propagating(&stored, d.value, d.mv) {
                propagate.push(r.id);
            } else {
                standing.push(r.id);
            }
            env.loops.wrote(r.id, d.mv);
            dirty.push((r.id, d));
        }
        if !dirty.is_empty() {
            for (id, d) in &dirty {
                write_backed(g, *id, d)?;
            }
            report.changed += dirty.len() as u64;
            report.written_keys.extend(dirty.iter().map(|(k, _)| *k));
        }
        report.bought += queue_quality_convergence(&*g, &level.discrepancies, cfg, &mut *env.tasks, &mut *env.effects);
        report.bought += queue_cycle_disambiguation(&*g, &level.cycle_picks, cfg, &mut *env.tasks, &mut *env.effects);
        let mut wake: Vec<PosId> = {
            let mut w: Vec<PosId> = level.wake.clone();
            w.sort();
            w.dedup();
            w.into_iter().filter(|k| !looked.contains(k)).collect()
        };
        looked.extend(wake.iter().copied());
        wake.extend(level.waiting.iter().copied());
        if propagate.is_empty() && standing.is_empty() {
            if !wake.is_empty() {
                frontier = wake;
                continue;
            }
            // BKD-7.6 the settling look.
            if settling || returning.is_empty() {
                frontier.clear();
                break;
            }
            frontier = std::mem::take(&mut returning).into_iter().collect();
            settling = true;
            continue;
        }
        settling = false;
        let woken = parents_to_wake(&*g, &propagate, &standing);
        if report.processed >= cfg.backed_max_nodes {
            guard = Some(GuardReason::NodeBudget);
            left = woken.into_iter().chain(wake).collect();
            break;
        }
        frontier = Vec::new();
        for k in dedup_in_order(woken.into_iter().chain(wake)) {
            let seen = visits.get(&k).copied().unwrap_or(0);
            if seen < cfg.backed_max_revisits {
                visits.insert(k, seen + 1);
                frontier.push(k);
            } else {
                dropped.push(k);
            }
        }
    }
    report.levels = plies;
    if let Some(reason) = guard {
        guard_event(&mut *env.effects, reason, &left, report.processed, plies, seeds.len(), opts.generation, None);
        if reason == GuardReason::PlyGuard {
            report.carry = left.clone();
        }
    }
    if !dropped.is_empty() {
        guard_event(
            &mut *env.effects,
            GuardReason::RevisitCap,
            &dropped,
            report.processed,
            plies,
            seeds.len(),
            opts.generation,
            Some(dropped.len() as u64),
        );
    }
    report.guard = guard.or(if dropped.is_empty() { None } else { Some(GuardReason::RevisitCap) });
    let mut all: HashSet<PosId> = left.iter().copied().collect();
    all.extend(dropped.iter().copied());
    report.left = all.len() as u64;
    Ok(report)
}

/// BKD-12.1 `stale_backed_rows`: the open rows among `keys` that one level
/// of the ascent would rewrite now, as `(key, stored, decided)` in id order.
/// Writes and buys nothing.
pub fn stale_backed_rows(g: &dyn GraphRead, keys: &[PosId], loops: &mut dyn LoopRules) -> Vec<(PosId, Backed, Backed)> {
    let mut ids: Vec<PosId> = dedup_in_order(keys.iter().copied()).into_iter().filter(|k| k.0 < g.len()).collect();
    ids.sort();
    let rows: Vec<Row> = ids.into_iter().map(|id| row_of(g, id)).filter(|r| r.core.status() == Status::Unknown).collect();
    if rows.is_empty() {
        return Vec::new();
    }
    loops.begin_ascent();
    let level = decide_level(g, &rows, loops);
    rows.iter()
        .filter_map(|r| {
            let stored = stored_of(g, r.id);
            let d = level.decided[&r.id];
            (d != stored).then_some((r.id, stored, d))
        })
        .collect()
}
