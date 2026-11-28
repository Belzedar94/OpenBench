//! Step 15: batch-size independence and termination (plan sections 2.2.6 and
//! 7.5).
//!
//! From a graph at rest, a few re-searches land (a position's eval moves and
//! it gains nodes).  Climbing after each one, or once for all of them with the
//! merged seeds (EXP-6.2), must end in the same state.  v1 does not promise
//! this (the noise cut, BKD-8.1; the per-call caps); the plan's D2 drops what
//! breaks it once the exact port passes.  So:
//!
//! - on DAGs with the noise cut off (D2) the property is asserted;
//! - with v1's noise cut on, and on graphs with cycles under the loop rules,
//!   it is measured: `batch_independence_rates` prints how often it fails.
//!
//! Termination: every climb on any graph (cycles, loop rules on) ends, and an
//! ascent that does not come to rest says so with a guard event.

use av2_model::record::{F_BLACK_TO_MOVE, F_EXPANDED};
use av2_model::{Closure, Core, Edge, Ext, GraphRead, GraphWrite, Key, Move, PosId, Proof, Status};
use av2_rules::climb::{ClimbEnv, ClimbReport, climb, merge_seeds};
use av2_rules::config::RulesConfig;
use av2_rules::effects::{Attribution, Event, Recorder};
use av2_rules::exact::BoardZeroing;
use av2_rules::loops::{LoopRules, NoLoops, V1Loops};
use av2_rules::purchase::MemTasks;
use av2_rules::testkit::TestGraph;
use proptest::prelude::*;

#[derive(Clone, Debug)]
struct NodeSpec {
    black: bool,
    expanded: bool,
    state: u8,
    eval: Option<i16>,
    nodes: u8,
    edges: Vec<usize>,
}

fn graph_spec(max: usize) -> impl Strategy<Value = Vec<NodeSpec>> {
    graph_spec_dense(max, 4)
}

fn graph_spec_dense(max: usize, fan: usize) -> impl Strategy<Value = Vec<NodeSpec>> {
    (3usize..max).prop_flat_map(move |n| {
        proptest::collection::vec(
            (any::<bool>(), any::<bool>(), 0u8..12, proptest::option::of(-400i16..400), 0u8..4, proptest::collection::vec(0..n, 0..fan))
                .prop_map(|(black, expanded, state, eval, nodes, edges)| NodeSpec { black, expanded, state, eval, nodes, edges }),
            n,
        )
    })
}

fn build(spec: &[NodeSpec], acyclic: bool) -> TestGraph {
    let mut g = TestGraph::new();
    for (i, s) in spec.iter().enumerate() {
        let mut c = Core::default();
        if s.black {
            c.flags |= F_BLACK_TO_MOVE;
        }
        c.set_flag(F_EXPANDED, s.expanded);
        match s.state {
            9 => c.set_status(Status::WhiteWin, Closure::Terminal, Proof::None),
            10 => c.set_status(Status::BlackWin, Closure::Tb, Proof::None),
            11 => c.set_status(Status::Draw, Closure::Tb, Proof::None),
            _ => {}
        }
        c.eval_cp = s.eval.unwrap_or(av2_model::record::NO_CP);
        let id = g.create(Key::of_canonical_fen(&format!("b{i}")).fingerprint(), None, c).unwrap();
        let nodes = [0u64, 8_000_000, 128_000_000, 2_000_000_000][s.nodes as usize];
        if nodes > 0 {
            g.put_ext(id, Ext { nodes_invested: nodes, visits: 1, ..Ext::default() }).unwrap();
        }
    }
    for (i, s) in spec.iter().enumerate() {
        let mut es: Vec<Edge> = Vec::new();
        for (j, &t) in s.edges.iter().enumerate() {
            if t == i || (acyclic && t < i) || s.state == 9 || es.iter().any(|e| e.child.0 == t as u64) {
                continue;
            }
            es.push(Edge { child: PosId(t as u64), mv: Move::new((j % 8) as u8, (16 + j) as u8, 0) });
        }
        if !es.is_empty() {
            g.set_children(PosId(i as u64), &es).unwrap();
        }
    }
    g
}

fn run(g: &mut TestGraph, cfg: &RulesConfig, cascade: &[PosId], backed: &[PosId], loops: &mut dyn LoopRules) -> (ClimbReport, Recorder) {
    let mut rec = Recorder::default();
    let mut z = BoardZeroing;
    let mut tasks = MemTasks::default();
    let att = Attribution::default();
    let mut env = ClimbEnv { cfg, effects: &mut rec, zeroing: &mut z, tasks: &mut tasks, loops, attribution: &att };
    let r = climb(g, cascade, backed, &mut env).unwrap();
    (r, rec)
}

fn guarded(rec: &Recorder) -> bool {
    rec.events.iter().any(|e| matches!(e, Event::BackedGuard { .. } | Event::CascadeGuard { .. } | Event::CascadeCycle { .. }))
}

/// Climb from every position until nothing is written (at most 8 passes).
fn settle(g: &mut TestGraph, cfg: &RulesConfig, loops: &mut dyn LoopRules) -> bool {
    let all: Vec<PosId> = (0..g.len()).map(PosId).collect();
    for _ in 0..8 {
        let (r, rec) = run(g, cfg, &all, &all, loops);
        if guarded(&rec) {
            return false;
        }
        if r.cascade.changed.is_empty() && r.ascent.changed == 0 {
            return true;
        }
    }
    false
}

fn parents(g: &TestGraph, p: PosId) -> Vec<PosId> {
    let mut v = Vec::new();
    g.parents(p, &mut v);
    v
}

fn research(g: &mut TestGraph, p: PosId, delta: i16) {
    let mut c = g.core(p);
    c.eval_cp = c.eval().unwrap_or(0).saturating_add(delta).clamp(-8999, 8999);
    g.put_core(p, c).unwrap();
    let mut e = g.ext(p).unwrap_or_default();
    e.nodes_invested += 128_000_000;
    e.visits += 1;
    g.put_ext(p, e).unwrap();
}

/// The rule-visible state: every core field but the store's ext pointer and
/// version (an ext record created for a value that later went back to 0 is
/// the same record as none), and the backed quality.
fn state(g: &TestGraph) -> Vec<(Core, u64)> {
    (0..g.len()).map(|i| (Core { ext: 0, version: 0, ..g.core(PosId(i)) }, g.ext(PosId(i)).map(|e| e.backed_nodes).unwrap_or(0))).collect()
}

/// `None` when a run was cut by a guard (nothing to compare); else whether
/// one-at-a-time and batched climbs agree.
fn compare(base: &TestGraph, cfg: &RulesConfig, picks: &[(usize, i16)], loops: &mut dyn FnMut() -> Box<dyn LoopRules>) -> Option<bool> {
    let n = base.len() as usize;
    let opens: Vec<PosId> = (0..n).map(|i| PosId(i as u64)).filter(|&p| base.core(p).is_open()).collect();
    if opens.is_empty() {
        return None;
    }
    let chosen: Vec<(PosId, i16)> = {
        let mut v: Vec<(PosId, i16)> = picks.iter().map(|&(i, d)| (opens[i % opens.len()], d)).collect();
        v.dedup_by_key(|x| x.0);
        v
    };
    let mut one = base.clone();
    for &(p, d) in &chosen {
        research(&mut one, p, d);
        let mut backed = vec![p];
        backed.extend(parents(&one, p));
        let (_, rec) = run(&mut one, cfg, &[p], &backed, &mut *loops());
        if guarded(&rec) {
            return None;
        }
    }
    let mut all = base.clone();
    let mut cl: Vec<Vec<PosId>> = Vec::new();
    let mut bl: Vec<Vec<PosId>> = Vec::new();
    for &(p, d) in &chosen {
        research(&mut all, p, d);
        cl.push(vec![p]);
        let mut backed = vec![p];
        backed.extend(parents(&all, p));
        bl.push(backed);
    }
    let cs: Vec<&[PosId]> = cl.iter().map(|v| v.as_slice()).collect();
    let bs: Vec<&[PosId]> = bl.iter().map(|v| v.as_slice()).collect();
    let (_, rec) = run(&mut all, cfg, &merge_seeds(&cs), &merge_seeds(&bs), &mut *loops());
    if guarded(&rec) {
        return None;
    }
    Some(state(&one) == state(&all))
}

fn picks() -> impl Strategy<Value = Vec<(usize, i16)>> {
    proptest::collection::vec((0usize..64, -150i16..150), 1..5)
}

/// Small moves (around the noise cut's 10 cp) and more of them.
fn small_picks() -> impl Strategy<Value = Vec<(usize, i16)>> {
    proptest::collection::vec((0usize..64, -14i16..14), 2..8)
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 300, .. ProptestConfig::default() })]

    /// D2 (noise cut off), a DAG, no guard: one climb per landing or one for
    /// the batch end in the same state.
    #[test]
    fn a_batch_climbs_to_the_same_state_on_a_dag(spec in graph_spec(26), p in picks()) {
        let cfg = RulesConfig { noise_cut: false, ..RulesConfig::default() };
        let mut g = build(&spec, true);
        prop_assume!(settle(&mut g, &cfg, &mut NoLoops));
        let same = compare(&g, &cfg, &p, &mut || Box::new(NoLoops));
        prop_assume!(same.is_some());
        prop_assert!(same.unwrap(), "one-at-a-time and batched climbs differ");
    }

    /// Any graph, cycles included, v1's settings and loop rules: every climb
    /// ends; an ascent cut by a bound reports it.
    #[test]
    fn every_climb_terminates_with_the_loop_rules(spec in graph_spec(30), seed in 0usize..30) {
        let cfg = RulesConfig::default();
        let mut g = build(&spec, false);
        let n = g.len();
        let all: Vec<PosId> = (0..n).map(PosId).collect();
        let (r, rec) = run(&mut g, &cfg, &all, &all, &mut V1Loops::default());
        prop_assert!(r.ascent.levels <= cfg.backed_max_plies);
        prop_assert_eq!(r.ascent.guard.is_some(), rec.events.iter().any(|e| matches!(e, Event::BackedGuard { .. })));
        let p = PosId((seed as u64) % n);
        let mut backed = vec![p];
        backed.extend(parents(&g, p));
        let (r2, _) = run(&mut g, &cfg, &[p], &backed, &mut V1Loops::default());
        prop_assert!(r2.ascent.levels <= cfg.backed_max_plies);
    }
}

/// The measured half of step 15: how often batch-size independence fails
/// with v1's noise cut on (DAGs), and with the loop rules on graphs with
/// cycles (noise cut off and on).  Printed, not asserted: v1 does not have
/// the property, and D2 is the owner's to take.
#[test]
fn batch_independence_rates() {
    use proptest::strategy::ValueTree;
    use proptest::test_runner::TestRunner;
    let mut runner = TestRunner::deterministic();
    let cases = 1500;
    let mut tally = [[0u32; 2]; 4]; // [setting][differ, compared]
    let settings: [(&str, bool, bool); 4] = [
        ("dag, cut off", false, true),
        ("dag, cut on (v1)", true, true),
        ("cycles + loops, cut off", false, false),
        ("cycles + loops, cut on (v1)", true, false),
    ];
    for _ in 0..cases {
        let spec = graph_spec_dense(24, 7).new_tree(&mut runner).unwrap().current();
        let p = small_picks().new_tree(&mut runner).unwrap().current();
        for (si, &(_, cut, acyclic)) in settings.iter().enumerate() {
            let cfg = RulesConfig { noise_cut: cut, ..RulesConfig::default() };
            let mut g = build(&spec, acyclic);
            let mut make: Box<dyn FnMut() -> Box<dyn LoopRules>> = if acyclic {
                Box::new(|| Box::new(NoLoops) as Box<dyn LoopRules>)
            } else {
                Box::new(|| Box::new(V1Loops::default()) as Box<dyn LoopRules>)
            };
            let mut settle_loops = make();
            if !settle(&mut g, &cfg, &mut *settle_loops) {
                continue;
            }
            if let Some(same) = compare(&g, &cfg, &p, &mut make) {
                tally[si][1] += 1;
                if !same {
                    tally[si][0] += 1;
                }
            }
        }
    }
    for (si, (name, _, _)) in settings.iter().enumerate() {
        eprintln!("batch independence, {name}: {} of {} compared cases differ", tally[si][0], tally[si][1]);
    }
    // The asserted half: D2 on DAGs never differs.
    assert_eq!(tally[0][0], 0);
}
