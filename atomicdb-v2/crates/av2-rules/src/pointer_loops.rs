//! Steps 13 and 14 of the port: loops of pointers.
//!
//! - LOOP-3.1 `_open_pointer_loops`, `_loop_still_closed`: a level never
//!   writes a loop of `backed_move` pointers at a value other than 0
//! - LOOP-4.1 `_loop_at_best_exit`: a loop nobody can open at the same value
//!   is worth its best exit
//! - LOOP-4.2 `_loop_ways_out`, `_region_children`
//!
//! D1: `BACKED_LOOP_MAX_SECONDS` (1.0 s per ascent) becomes a counted budget
//! (`LoopCosts::loop_budget`), charged with the reads and verdicts v1's
//! `_loop_at_best_exit` makes; the four clock checks stay where they are.

use crate::backed::{
    Backed, ChildValue, Discrepancy, Row, Showcase, backed_for, child_contribution, children_of, grounding, is_floor, showcase_from_graph,
};
use crate::conv::status_eval;
use crate::loops::LevelChildren;
use crate::repetition::{BACKED_CYCLE_MAX_PLIES, LoopCosts, Meter, SpineCache, Verdict, line_verdict, resolve_steps};
use av2_model::{Core, Ext, GraphRead, Move, PosId};
use std::collections::{BTreeMap, BTreeSet, HashMap};

/// LOOP-3.1 `_loop_still_closed`.
pub fn loop_still_closed(cache: &SpineCache, members: &[PosId]) -> bool {
    members.iter().enumerate().all(|(i, &k)| {
        let mv = cache.spine(k);
        matches!(cache.step(k, mv), Some((child, _)) if child == members[(i + 1) % members.len()])
    })
}

/// LOOP-3.1 `_open_pointer_loops`.  Returns the loop positions outside the
/// level (to wake); fills `ruled`; may rewrite `decided` and append to
/// `cycle_picks`.  `budget` is what the best-exit rule may still spend in
/// this ascent (units of [`LoopCosts`]).
#[allow(clippy::too_many_arguments)]
pub fn open_pointer_loops(
    g: &dyn GraphRead,
    rows: &[Row],
    children: &mut LevelChildren,
    decided: &mut BTreeMap<PosId, Backed>,
    cache: &mut SpineCache,
    showcases: &BTreeMap<PosId, Showcase>,
    cycle_picks: &mut Vec<Discrepancy>,
    budget: &mut i64,
    costs: &LoopCosts,
    ruled: &mut Vec<PosId>,
) -> Vec<PosId> {
    let by_key: BTreeMap<PosId, &Row> = rows.iter().map(|r| (r.id, r)).collect();
    let starts: Vec<PosId> = decided
        .iter()
        .filter(|(k, d)| !d.mv.is_none() && d.value.is_some_and(|v| v != 0) && status_eval(by_key[*k].core.status()).is_none())
        .map(|(k, _)| *k)
        .collect();
    if starts.is_empty() {
        return Vec::new();
    }
    for &k in &starts {
        let mv = decided[&k].mv;
        if let Some(c) = children.get(&k).and_then(|ks| ks.iter().find(|c| c.mv == mv)) {
            cache.steps.entry((k, mv)).or_insert((c.id, !c.exact));
        }
    }
    let mut loops: BTreeMap<Vec<PosId>, Vec<PosId>> = BTreeMap::new();
    let mut walkers: Vec<(PosId, PosId, Vec<PosId>)> = starts.iter().map(|&k| (k, k, vec![k])).collect();
    for _ in 0..BACKED_CYCLE_MAX_PLIES {
        if walkers.is_empty() {
            break;
        }
        let pairs: Vec<(PosId, Move)> = walkers.iter().map(|w| (w.1, cache.spine(w.1))).collect();
        resolve_steps(g, cache, &pairs, Some(decided));
        let mut alive = Vec::new();
        for (start, node, mut path) in walkers {
            let mv = cache.spine(node);
            let Some((below, open)) = cache.step(node, mv) else { continue };
            if !open {
                continue;
            }
            if below == start {
                let mut set = path.clone();
                set.sort();
                loops.entry(set).or_insert(path);
                continue;
            }
            if path.contains(&below) {
                continue;
            }
            path.push(below);
            alive.push((start, below, path));
        }
        walkers = alive;
    }
    // `sorted(loops.values())`: the member lists in list order.
    let mut lists: Vec<Vec<PosId>> = loops.into_values().collect();
    lists.sort();
    let mut wake: BTreeSet<PosId> = BTreeSet::new();
    let empty = Showcase::default();
    for members in lists {
        if !loop_still_closed(cache, &members) {
            continue;
        }
        let mut ring = members.clone();
        ring.push(members[0]);
        let mut opened = false;
        let mut in_level: Vec<PosId> = members.iter().copied().filter(|k| by_key.contains_key(k)).collect();
        in_level.sort();
        for key in in_level {
            let d = decided[&key];
            let next = ring[ring.iter().position(|&k| k == key).unwrap() + 1];
            let Some(kids) = children.get_mut(&key) else { continue };
            let Some(li) = kids.iter().position(|c| c.mv == d.mv && c.id == next) else { continue };
            let was = kids[li].returns;
            kids[li].returns = true;
            let show = showcases.get(&key).unwrap_or(&empty);
            let again = backed_for(by_key[&key], kids, None, show, None);
            kids[li].returns = was;
            let out = kids.iter().find(|c| c.mv == again.mv);
            if again.value == d.value && out.is_some_and(|o| o.mv != d.mv && grounding(o) == 2) {
                decided.insert(key, again);
                cache.wrote(key, again.mv);
                opened = true;
                break;
            }
        }
        if !opened {
            if *budget > 0 {
                let mut meter = Meter::new(*budget as u64);
                let mut picks = Vec::new();
                let worth = loop_at_best_exit(g, &members, &by_key, decided, cache, &mut meter, costs, &mut picks);
                *budget -= meter.used as i64;
                if let Some(worth) = worth {
                    for &key in &members {
                        if by_key.contains_key(&key) && worth[&key] != decided[&key] {
                            decided.insert(key, worth[&key]);
                            cache.wrote(key, worth[&key].mv);
                            if !ruled.contains(&key) {
                                ruled.push(key);
                            }
                        }
                    }
                    cycle_picks.extend(picks.into_iter().filter(|p| by_key.contains_key(&p.1)));
                }
            }
            wake.extend(members.iter().copied().filter(|k| !by_key.contains_key(k)));
        }
    }
    wake.into_iter().collect()
}

/// LOOP-4.2 `_region_children`: the members' children; a child the level
/// has just decided counts with what it is about to be written with.
pub fn region_children(
    g: &dyn GraphRead,
    members: &[PosId],
    by_key: &BTreeMap<PosId, &Row>,
    decided: &BTreeMap<PosId, Backed>,
) -> LevelChildren {
    let mut out: LevelChildren = BTreeMap::new();
    let mut buf = Vec::new();
    for &m in members {
        let mut kids = children_of(g, m, &mut buf);
        for k in kids.iter_mut() {
            let (Some(row), Some(d)) = (by_key.get(&k.id), decided.get(&k.id)) else { continue };
            let mut core = Core { backed_eval: d.value.map(|v| v as i16).unwrap_or(av2_model::record::NO_CP), ..row.core };
            core.backed_move = d.mv;
            core.backed_plies = d.plies.clamp(0, u16::MAX as i64) as u16;
            core.mate_in = k.mate_in.map(|m| m as u16).unwrap_or(av2_model::record::NO_MATE);
            let ext = Ext { nodes_invested: row.nodes_invested, backed_nodes: d.quality, ..Ext::default() };
            *k = child_contribution(k.mv, k.id, &core, &ext);
        }
        if !kids.is_empty() {
            out.insert(m, kids);
        }
    }
    out
}

/// What `_loop_ways_out` sorts a member's moves into.
#[derive(Default, Debug)]
pub struct WaysOut {
    pub inside: Vec<(Move, PosId)>,
    pub out: Vec<ChildValue>,
    pub back: Vec<(ChildValue, Vec<PosId>)>,
}

/// LOOP-4.2 `_loop_ways_out`.
pub fn loop_ways_out(
    g: &dyn GraphRead,
    members: &[PosId],
    children: &LevelChildren,
    cache: &mut SpineCache,
    decided: &BTreeMap<PosId, Backed>,
    meter: &mut Meter,
    costs: &LoopCosts,
) -> HashMap<PosId, WaysOut> {
    let mut ways: HashMap<PosId, WaysOut> = members.iter().map(|&k| (k, WaysOut::default())).collect();
    // (member, child, node, chain)
    let mut walkers: Vec<(PosId, ChildValue, PosId, Vec<PosId>)> = Vec::new();
    for &key in members {
        for c in children.get(&key).map(|v| v.as_slice()).unwrap_or(&[]) {
            if members.contains(&c.id) {
                ways.get_mut(&key).unwrap().inside.push((c.mv, c.id));
                continue;
            }
            if !c.spine.is_none() {
                cache.spines.entry(c.id).or_insert(c.spine);
            }
            if c.exact || c.value.is_none() || cache.spine(c.id).is_none() {
                ways.get_mut(&key).unwrap().out.push(*c);
                continue;
            }
            walkers.push((key, *c, c.id, vec![c.id]));
        }
    }
    for _ in 0..BACKED_CYCLE_MAX_PLIES {
        if walkers.is_empty() {
            break;
        }
        let pairs: Vec<(PosId, Move)> = walkers.iter().map(|w| (w.2, cache.spine(w.2))).collect();
        if resolve_steps(g, cache, &pairs, Some(decided)) {
            meter.charge(costs.step_query);
        }
        let mut alive = Vec::new();
        for (key, mut child, node, mut chain) in walkers {
            let mv = cache.spine(node);
            let Some((below, open)) = cache.step(node, mv) else {
                ways.get_mut(&key).unwrap().out.push(child);
                continue;
            };
            if members.contains(&below) {
                ways.get_mut(&key).unwrap().back.push((child, chain));
                continue;
            }
            if !open || cache.spine(below).is_none() {
                ways.get_mut(&key).unwrap().out.push(child);
                continue;
            }
            if chain.contains(&below) {
                child.circular = true;
                ways.get_mut(&key).unwrap().out.push(child);
                continue;
            }
            chain.push(below);
            alive.push((key, child, below, chain));
        }
        walkers = alive;
    }
    for (key, child, _, _) in walkers {
        ways.get_mut(&key).unwrap().out.push(child);
    }
    ways
}

fn repeated(mv: Move, key: PosId) -> ChildValue {
    let mut c = ChildValue::uninformed(mv, key);
    c.value = Some(0);
    c.cycles = true;
    c.returns = true;
    c
}

/// LOOP-4.1 `_loop_at_best_exit`: `members` in pointer order.  `None`
/// (leave the loop) when the meter passes its limit at one of v1's four
/// clock checks, when a member is missing or decided, or when the result
/// fails the final check.
#[allow(clippy::too_many_arguments)]
pub fn loop_at_best_exit(
    g: &dyn GraphRead,
    members: &[PosId],
    by_key: &BTreeMap<PosId, &Row>,
    decided: &BTreeMap<PosId, Backed>,
    cache: &mut SpineCache,
    meter: &mut Meter,
    costs: &LoopCosts,
    picks: &mut Vec<Discrepancy>,
) -> Option<BTreeMap<PosId, Backed>> {
    if meter.expired() {
        return None;
    }
    let size = members.len();
    let index: HashMap<PosId, usize> = members.iter().enumerate().map(|(i, &k)| (k, i)).collect();
    // `_backed_children_by_parent(members)`: the first of the two reads.
    meter.charge(costs.loop_reads / 2);
    let region = region_children(g, members, by_key, decided);
    let ways = loop_ways_out(g, members, &region, cache, decided, meter, costs);
    if meter.expired() {
        return None;
    }
    // The members' rows, fresh: the second read.
    meter.charge(costs.loop_reads - costs.loop_reads / 2);
    let mut rows: HashMap<PosId, (Row, Showcase)> = HashMap::new();
    for &k in members {
        if k.0 >= g.len() {
            return None;
        }
        let core = g.core(k);
        if status_eval(core.status()).is_some() {
            return None;
        }
        let row = Row { id: k, core, nodes_invested: g.ext(k).map(|e| e.nodes_invested).unwrap_or(0) };
        rows.insert(k, (row, showcase_from_graph(g, k)));
    }
    let member =
        |mv: Move, key: PosId, start: usize, here: usize, lap: &HashMap<usize, Verdict>, rows: &HashMap<PosId, (Row, Showcase)>| {
            let position = (index[&key] + size - start) % size;
            if position <= here {
                return repeated(mv, key);
            }
            let v = lap[&position];
            if v.repeats {
                return repeated(mv, key);
            }
            let mut c = ChildValue::uninformed(mv, key);
            c.value = v.value;
            c.quality = v.quality;
            c.plies = v.plies;
            c.spine = v.mv;
            let r = &rows[&key].0;
            c.floor = is_floor(r.core.status(), r.nodes_invested, if v.mv.is_none() { None } else { v.value });
            c.returns = true;
            c
        };
    let mut worth: BTreeMap<PosId, Backed> = BTreeMap::new();
    for start in 0..size {
        if meter.expired() {
            return None;
        }
        let mut lap: HashMap<usize, Verdict> = HashMap::new();
        for here in (1..size).rev() {
            let key = members[(start + here) % size];
            let w = &ways[&key];
            let mut kids: Vec<ChildValue> = w.out.clone();
            kids.extend(w.inside.iter().map(|&(mv, other)| member(mv, other, start, here, &lap, &rows)));
            let played: Vec<PosId> = (0..here).map(|s| members[(start + s) % size]).collect();
            meter.charge(costs.verdict);
            let (row, show) = &rows[&key];
            lap.insert(here, line_verdict(row, &mut kids, &played, show));
        }
        let key = members[start];
        let w = &ways[&key];
        let mut kids: Vec<ChildValue> = w.out.clone();
        kids.extend(w.inside.iter().map(|&(mv, other)| member(mv, other, start, 0, &lap, &rows)));
        let (row, show) = &rows[&key];
        let mut asked = Vec::new();
        meter.charge(costs.verdict);
        worth.insert(key, backed_for(row, &mut kids, None, show, Some(&mut asked)));
        picks.extend(asked);
    }
    // The final check: a cycle of chosen inside moves whose values are not
    // all 0 is a loop this rule did not understand.
    let mut onward: HashMap<PosId, PosId> = HashMap::new();
    for &k in members {
        for &(mv, other) in &ways[&k].inside {
            if mv == worth[&k].mv {
                onward.insert(k, other);
            }
        }
    }
    for &k in members {
        let mut seen: Vec<PosId> = Vec::new();
        let mut node = k;
        while onward.contains_key(&node) && !seen.contains(&node) {
            seen.push(node);
            node = onward[&node];
        }
        if let Some(at) = seen.iter().position(|&x| x == node)
            && seen[at..].iter().any(|o| worth[o].value.is_some_and(|v| v != 0))
        {
            return None;
        }
    }
    Some(worth)
}
