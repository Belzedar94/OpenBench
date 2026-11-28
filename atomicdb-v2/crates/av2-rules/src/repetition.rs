//! Steps 11 and 12 of the port: repetition marking and returning lines.
//!
//! - LOOP-1.1 `_draw_cycling_children`: mark a child whose value comes back
//!   to its parent (or to a position of the line under evaluation)
//! - LOOP-1.2 `_SpineCache`, `_resolve_spine_steps`, `_resolve_level_steps`
//! - LOOP-2.1 `_resolve_returning_children`: value a returning child on the
//!   line that returns
//! - LOOP-2.2 `_value_on_line`, LOOP-2.3 `_line_verdict`
//!
//! **Owner decision D1: no wall clock.**  v1 stops the returning-line
//! valuation when `BACKED_CYCLE_MAX_SECONDS` (1.0 s) have passed since the
//! top-level call.  Here a [`Meter`] counts the work v1 would have done, each
//! operation weighted by what it costs v1 on production (calibrated, see
//! [`LoopCosts`] and `docs/rules-port-status.md`, "D1 calibration"), and the
//! valuation stops where v1's clock would: when the count passes the budget
//! of the same name.  The check sits exactly where v1 reads the clock.

use crate::backed::{Backed, ChildValue, Discrepancy, Row, Showcase, backed_for, children_of, showcase_from_graph};
use crate::conv::better_for_mover;
use crate::loops::LevelChildren;
use av2_model::{Edge, GraphRead, Move, PosId, Status};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

/// `ingest.BACKED_CYCLE_MAX_PLIES`.
pub const BACKED_CYCLE_MAX_PLIES: usize = 32;
/// `ingest.BACKED_CYCLE_MAX_NODES`: re-reads per returning child.
pub const BACKED_CYCLE_MAX_NODES: i64 = 64;

/// What one operation of the loop rules costs, in units of one microsecond
/// of v1 on production (D1).  `BACKED_CYCLE_MAX_SECONDS` and
/// `BACKED_LOOP_MAX_SECONDS` (1.0 s each) become budgets of 1,000,000 units.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LoopCosts {
    /// One `_value_on_line` re-read: the row, its children with their
    /// columns, the marking and the verdict.
    pub reread: u64,
    /// One query round of pointer steps (`_resolve_spine_steps`,
    /// `_resolve_level_steps`) that had steps to fetch.
    pub step_query: u64,
    /// One verdict of a best-exit lap (`_line_verdict` / `_backed_for`).
    pub verdict: u64,
    /// The two reads of a best-exit call (the members' children, then their
    /// rows), charged half at each.
    pub loop_reads: u64,
    /// `BACKED_CYCLE_MAX_SECONDS` in units.
    pub cycle_budget: u64,
    /// `BACKED_LOOP_MAX_SECONDS` in units.
    pub loop_budget: u64,
}

/// Calibrated on production, 28-Nov-2026 (read-only: v1's own
/// `stale_backed_rows` on 12,651 positions of the start position's spine, a
/// page sample and 24 known loop families, every loop function wrapped and
/// timed; `docs/rules-port-status.md`, "D1 calibration").  A least-squares
/// fit of each top-level valuation's wall time on its re-reads and step
/// queries gives 2.26 ms and 1.16 ms (R² = 0.983, 48 valuations); the
/// best-exit calls give 30 µs per lap verdict and 4.0 ms of reads.
pub const LOOP_COSTS: LoopCosts =
    LoopCosts { reread: 2_260, step_query: 1_160, verdict: 30, loop_reads: 4_000, cycle_budget: 1_000_000, loop_budget: 1_000_000 };
impl Default for LoopCosts {
    fn default() -> Self {
        LOOP_COSTS
    }
}

/// The counted clock of one top-level valuation (v1 `_cycle_deadline`).
#[derive(Clone, Copy, Debug, Default)]
pub struct Meter {
    pub used: u64,
    pub limit: u64,
}

impl Meter {
    pub fn new(limit: u64) -> Meter {
        Meter { used: 0, limit }
    }
    /// `time.monotonic() > deadline`.
    #[inline]
    pub fn expired(&self) -> bool {
        self.used > self.limit
    }
    #[inline]
    pub fn charge(&mut self, units: u64) {
        self.used = self.used.saturating_add(units);
    }
}

// ---------------------------------------------------------------- spine cache

/// LOOP-1.2 `_SpineCache`: `steps[(node, move)] = (child, child is open)`
/// (graph facts) and `spines[node]` = the node's current pointer (as the
/// ascent decides it, `wrote`).
#[derive(Clone, Debug, Default)]
pub struct SpineCache {
    pub steps: HashMap<(PosId, Move), (PosId, bool)>,
    pub spines: HashMap<PosId, Move>,
}

impl SpineCache {
    pub fn wrote(&mut self, key: PosId, mv: Move) {
        self.spines.insert(key, mv);
    }
    #[inline]
    pub fn spine(&self, key: PosId) -> Move {
        self.spines.get(&key).copied().unwrap_or(Move::NONE)
    }
    #[inline]
    pub fn step(&self, node: PosId, mv: Move) -> Option<(PosId, bool)> {
        if mv.is_none() { None } else { self.steps.get(&(node, mv)).copied() }
    }
}

/// `_resolve_spine_steps` / `_resolve_level_steps`: one query
/// (`parent IN nodes AND move IN moves`) for the unknown steps; every row it
/// returns is cached, pairs nobody asked for included, and each child's
/// stored pointer replaces the cached one (except, for the level variant,
/// a child the level has just decided).  Returns whether a query was made.
pub fn resolve_steps(
    g: &dyn GraphRead,
    cache: &mut SpineCache,
    pairs: &[(PosId, Move)],
    decided: Option<&BTreeMap<PosId, Backed>>,
) -> bool {
    let unknown: Vec<(PosId, Move)> = pairs.iter().copied().filter(|&(n, m)| !m.is_none() && !cache.steps.contains_key(&(n, m))).collect();
    if unknown.is_empty() {
        return false;
    }
    let nodes: BTreeSet<PosId> = unknown.iter().map(|p| p.0).collect();
    let moves: HashSet<Move> = unknown.iter().map(|p| p.1).collect();
    let mut buf: Vec<Edge> = Vec::new();
    for n in nodes {
        buf.clear();
        g.children(n, &mut buf);
        for e in &buf {
            if !moves.contains(&e.mv) {
                continue;
            }
            let c = g.core(e.child);
            cache.steps.insert((n, e.mv), (e.child, c.status() == Status::Unknown));
            if decided.is_none_or(|d| !d.contains_key(&e.child)) {
                cache.spines.insert(e.child, c.backed_move);
            }
        }
    }
    true
}

// ---------------------------------------------------------------- marking

struct Walk {
    parent: PosId,
    idx: usize,
    node: PosId,
    mv: Move,
    seen: Vec<PosId>,
}

/// LOOP-1.1 `_draw_cycling_children`: mark (`cycles = true`, `plies = 0`)
/// every child whose pointer chain comes back to its parent or onto `line`;
/// a child on `line` itself is a repetition at once (value 0).  Collects in
/// `circular` the `(parent, move)` whose chain closes on itself elsewhere.
/// Returns the marked `(parent, move)`.  `meter` is charged one read per
/// query round when a valuation's clock is running.
pub fn draw_cycling_children(
    g: &dyn GraphRead,
    by_parent: &mut LevelChildren,
    cache: &mut SpineCache,
    line: &[PosId],
    mut circular: Option<&mut HashSet<(PosId, Move)>>,
    mut meter: Option<(&mut Meter, &LoopCosts)>,
) -> Vec<(PosId, Move)> {
    let mut cycled: Vec<(PosId, Move)> = Vec::new();
    let mut walks: Vec<Walk> = Vec::new();
    for (&parent, kids) in by_parent.iter_mut() {
        for (idx, c) in kids.iter_mut().enumerate() {
            if line.contains(&c.id) {
                c.cycles = true;
                c.plies = 0;
                c.value = Some(0);
                cycled.push((parent, c.mv));
                continue;
            }
            if c.value.is_none() || c.spine.is_none() {
                continue;
            }
            cache.spines.entry(c.id).or_insert(c.spine);
            walks.push(Walk { parent, idx, node: c.id, mv: c.spine, seen: vec![c.id] });
        }
    }
    for _ in 0..BACKED_CYCLE_MAX_PLIES {
        if walks.is_empty() {
            break;
        }
        let pairs: Vec<(PosId, Move)> = walks.iter().map(|w| (w.node, w.mv)).collect();
        if resolve_steps(g, cache, &pairs, None) {
            if let Some((m, costs)) = meter.as_mut() {
                m.charge(costs.step_query);
            }
        }
        let mut alive = Vec::new();
        for mut w in walks {
            let Some((below, open)) = cache.step(w.node, w.mv) else { continue };
            if below == w.parent || line.contains(&below) {
                let c = &mut by_parent.get_mut(&w.parent).unwrap()[w.idx];
                c.cycles = true;
                c.plies = 0;
                cycled.push((w.parent, c.mv));
                continue;
            }
            let spine = cache.spine(below);
            if w.seen.contains(&below) {
                if let Some(set) = circular.as_deref_mut() {
                    set.insert((w.parent, by_parent[&w.parent][w.idx].mv));
                }
                continue;
            }
            if spine.is_none() || !open {
                continue;
            }
            w.seen.push(below);
            w.node = below;
            w.mv = spine;
            alive.push(w);
        }
        walks = alive;
    }
    cycled
}

// ---------------------------------------------------------------- returning lines

/// What a position is worth on a line: `(value, quality, plies, move,
/// repeats)` (v1's tuple).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Verdict {
    pub value: Option<i32>,
    pub quality: u64,
    pub plies: i64,
    pub mv: Move,
    pub repeats: bool,
}

/// What a top-level valuation shares with its nested calls.
pub struct LineCtx<'a> {
    pub g: &'a dyn GraphRead,
    pub cache: &'a mut SpineCache,
    pub clock: Meter,
    pub costs: LoopCosts,
    /// `verdicts[(key, played)] = (move, value, repeats)` when asked.
    pub verdicts: Option<&'a mut HashMap<(PosId, Vec<PosId>), (Move, Option<i32>, bool)>>,
}

fn sorted_set(v: &[PosId]) -> Vec<PosId> {
    let mut s = v.to_vec();
    s.sort();
    s.dedup();
    s
}

/// LOOP-2.3 `_line_verdict`.
pub fn line_verdict(row: &Row, kids: &mut [ChildValue], played: &[PosId], show: &Showcase) -> Verdict {
    let mut picks: Vec<Discrepancy> = Vec::new();
    let b = backed_for(row, kids, None, show, Some(&mut picks));
    if !picks.is_empty() && !b.mv.is_none() && b.mv == picks[0].0 {
        return Verdict { value: Some(0), quality: 0, plies: 0, mv: b.mv, repeats: true };
    }
    let own_return = kids.iter().find(|c| played.contains(&c.id)).map(|c| c.mv);
    if let Some(m) = own_return {
        let stm = crate::conv::stm_white(&row.core);
        if b.value.is_none_or(|v| better_for_mover(0, v, stm)) {
            return Verdict { value: Some(0), quality: 0, plies: 0, mv: m, repeats: true };
        }
    }
    match b.value {
        None => Verdict { value: row.core.eval().map(i32::from), quality: row.nodes_invested, plies: 0, mv: Move::NONE, repeats: false },
        Some(v) => Verdict { value: Some(v), quality: b.quality.max(row.nodes_invested), plies: b.plies, mv: b.mv, repeats: false },
    }
}

/// LOOP-2.2 `_value_on_line`: what `key` is worth to a line that already
/// went through `played`; `None` when the budget is spent.
pub fn value_on_line(ctx: &mut LineCtx, key: PosId, played: &[PosId], budget: &mut i64) -> Option<Verdict> {
    if *budget <= 0 || played.len() >= BACKED_CYCLE_MAX_PLIES || ctx.clock.expired() {
        return None;
    }
    *budget -= 1;
    if key.0 >= ctx.g.len() {
        return None;
    }
    // The re-read: row, children, marking and verdict.
    ctx.clock.charge(ctx.costs.reread);
    let row = Row { id: key, core: ctx.g.core(key), nodes_invested: ctx.g.ext(key).map(|e| e.nodes_invested).unwrap_or(0) };
    let mut children: LevelChildren = BTreeMap::new();
    let kids = children_of(ctx.g, key, &mut Vec::new());
    if !kids.is_empty() {
        children.insert(key, kids);
    }
    let costs = ctx.costs;
    draw_cycling_children(ctx.g, &mut children, ctx.cache, played, None, Some((&mut ctx.clock, &costs)));
    resolve_returning_children(ctx, &mut children, played, Some(budget), None, None);
    let show = showcase_from_graph(ctx.g, key);
    let mut none = Vec::new();
    let kids = children.get_mut(&key).unwrap_or(&mut none);
    let v = line_verdict(&row, kids, played, &show);
    if let Some(vs) = ctx.verdicts.as_deref_mut() {
        vs.insert((key, sorted_set(played)), (v.mv, v.value, v.repeats));
    }
    Some(v)
}

/// LOOP-2.1 `_resolve_returning_children`.  A top-level call (`budget` None)
/// has just opened the clock (`ctx.clock`, a fresh [`Meter`]); nested calls
/// share it and the caller's re-read budget.
pub fn resolve_returning_children(
    ctx: &mut LineCtx,
    by_parent: &mut LevelChildren,
    line: &[PosId],
    mut budget: Option<&mut i64>,
    only: Option<&[PosId]>,
    mut unresolved: Option<&mut Vec<(PosId, Move)>>,
) {
    let parents: Vec<PosId> = by_parent.keys().copied().collect();
    for parent in parents {
        if only.is_some_and(|o| !o.contains(&parent)) {
            continue;
        }
        let mut played: Vec<PosId> = line.to_vec();
        if !played.contains(&parent) {
            played.push(parent);
        }
        let n = by_parent[&parent].len();
        for i in 0..n {
            let (cycles, cid, cmv) = {
                let c = &by_parent[&parent][i];
                (c.cycles, c.id, c.mv)
            };
            if !cycles || played.contains(&cid) {
                continue;
            }
            let mut own = BACKED_CYCLE_MAX_NODES;
            let seen = match budget.as_deref_mut() {
                Some(b) => value_on_line(ctx, cid, &played, b),
                None => value_on_line(ctx, cid, &played, &mut own),
            };
            let Some(v) = seen else {
                if let Some(u) = unresolved.as_deref_mut() {
                    u.push((parent, cmv));
                }
                continue;
            };
            if v.repeats {
                continue;
            }
            let c = &mut by_parent.get_mut(&parent).unwrap()[i];
            c.cycles = false;
            c.value = v.value;
            c.quality = v.quality;
            c.plies = v.plies;
            c.spine = v.mv;
        }
    }
}
