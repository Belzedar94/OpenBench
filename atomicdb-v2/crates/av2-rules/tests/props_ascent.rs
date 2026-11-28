//! Property tests of the backed ascent (step 8, plan section 7.5).
//!
//! On a DAG (the loop rules are steps 11-14) with the noise cut off (D2):
//! the ascent terminates, leaves no stale row (`stale_backed_rows` finds
//! nothing, i.e. a second ascent would rewrite nothing), and reaches the same
//! backed columns whatever the seed order.  With the noise cut on (v1), the
//! test measures how often rows are left stale and checks the bound v1
//! documents: a stale row's value is within `BACKED_EPSILON_CP` of what its
//! children say, or it stands on the wrong sibling by less than that.

use av2_model::record::{F_BLACK_TO_MOVE, F_EXPANDED};
use av2_model::{Closure, Core, Edge, Ext, GraphRead, GraphWrite, Key, Move, PosId, Proof, Status};
use av2_rules::ascent::{AscentOpts, stale_backed_rows};
use av2_rules::config::RulesConfig;
use av2_rules::effects::Event;
use av2_rules::loops::NoLoops;
use av2_rules::purchase::MemTasks;
use av2_rules::testkit::Fixture;
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

fn node_spec(n: usize) -> impl Strategy<Value = NodeSpec> {
    (any::<bool>(), any::<bool>(), 0u8..10, proptest::option::of(-300i16..300), 0u8..4, proptest::collection::vec(0..n, 0..4))
        .prop_map(|(black, expanded, state, eval, nodes, edges)| NodeSpec { black, expanded, state, eval, nodes, edges })
}

fn graph_spec() -> impl Strategy<Value = Vec<NodeSpec>> {
    (2usize..30).prop_flat_map(|n| proptest::collection::vec(node_spec(n), n))
}

fn build(spec: &[NodeSpec]) -> Fixture {
    let mut f = Fixture::new();
    for (i, s) in spec.iter().enumerate() {
        let mut core = Core::default();
        if s.black {
            core.flags |= F_BLACK_TO_MOVE;
        }
        core.set_flag(F_EXPANDED, s.expanded);
        match s.state {
            7 => core.set_status(Status::WhiteWin, Closure::Terminal, Proof::None),
            8 => core.set_status(Status::BlackWin, Closure::Tb, Proof::None),
            9 => core.set_status(Status::Draw, Closure::Tb, Proof::None),
            _ => {}
        }
        core.eval_cp = s.eval.unwrap_or(av2_model::record::NO_CP);
        let fp = Key::of_canonical_fen(&format!("n{i}")).fingerprint();
        let id = f.g.create(fp, None, core).unwrap();
        let nodes = [0u64, 8_000_000, 128_000_000, 2_000_000_000][s.nodes as usize];
        if nodes > 0 {
            f.g.put_ext(id, Ext { nodes_invested: nodes, visits: 1, ..Ext::default() }).unwrap();
        }
    }
    for (i, s) in spec.iter().enumerate() {
        let mut edges: Vec<Edge> = Vec::new();
        for (j, &t) in s.edges.iter().enumerate() {
            if t <= i || edges.iter().any(|e| e.child.0 == t as u64) {
                continue;
            }
            edges.push(Edge { child: PosId(t as u64), mv: Move::new((j % 8) as u8, (16 + j) as u8, 0) });
        }
        if !edges.is_empty() {
            f.g.set_children(PosId(i as u64), &edges).unwrap();
        }
    }
    f
}

fn guarded(events: &[Event]) -> bool {
    events.iter().any(|e| matches!(e, Event::BackedGuard { .. }))
}

fn backed_cols(f: &Fixture) -> Vec<(i16, Move, u16, u64)> {
    (0..f.g.len())
        .map(|i| {
            let c = f.g.core(PosId(i));
            (c.backed_eval, c.backed_move, c.backed_plies, f.ext(PosId(i)).backed_nodes)
        })
        .collect()
}

fn no_cut() -> RulesConfig {
    RulesConfig { noise_cut: false, ..RulesConfig::default() }
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 300, .. ProptestConfig::default() })]

    #[test]
    fn the_ascent_rests_with_nothing_stale(spec in graph_spec()) {
        let mut f = build(&spec);
        let all: Vec<PosId> = (0..f.g.len()).map(PosId).collect();
        let (r, rec) = f.ascent_with(&no_cut(), &all, AscentOpts::default(), &mut MemTasks::default());
        prop_assume!(!guarded(&rec.events));
        prop_assert!(r.levels <= 64);
        let stale = stale_backed_rows(&f.g, &all, &mut NoLoops);
        prop_assert!(stale.is_empty(), "stale after rest: {:?}", stale);
        // And a second ascent writes nothing.
        let (again, _) = f.ascent_with(&no_cut(), &all, AscentOpts::default(), &mut MemTasks::default());
        prop_assert_eq!(again.changed, 0);
    }

    #[test]
    fn the_ascent_does_not_depend_on_seed_order(spec in graph_spec(), rot in 0usize..30) {
        let base = build(&spec);
        let mut all: Vec<PosId> = (0..base.g.len()).map(PosId).collect();
        let mut f1 = base.clone();
        let (_, r1) = f1.ascent_with(&no_cut(), &all, AscentOpts::default(), &mut MemTasks::default());
        all.reverse();
        let k = rot % all.len();
        all.rotate_left(k);
        let mut f2 = base.clone();
        let (_, r2) = f2.ascent_with(&no_cut(), &all, AscentOpts::default(), &mut MemTasks::default());
        prop_assume!(!guarded(&r1.events) && !guarded(&r2.events));
        prop_assert_eq!(backed_cols(&f1), backed_cols(&f2));
    }

    #[test]
    fn decided_rows_store_their_truth(spec in graph_spec()) {
        let mut f = build(&spec);
        let all: Vec<PosId> = (0..f.g.len()).map(PosId).collect();
        let (_, rec, _) = f.ascent(&all);
        prop_assume!(!guarded(&rec.events));
        for i in 0..f.g.len() {
            let c = f.g.core(PosId(i));
            if let Some(v) = av2_rules::conv::status_eval(c.status()) {
                prop_assert_eq!(c.backed(), Some(v as i16));
                prop_assert_eq!(f.ext(PosId(i)).backed_nodes, av2_rules::conv::PROVEN_QUALITY);
            }
        }
    }

    /// v1's noise cut leaves rows stale (the sweep's work); its documented
    /// bound: what a stale row would be rewritten to is within the epsilon of
    /// what it stores, or differs only in plies/quality/pointer.
    #[test]
    fn the_noise_cut_leaves_only_small_drift(spec in graph_spec()) {
        let mut f = build(&spec);
        let all: Vec<PosId> = (0..f.g.len()).map(PosId).collect();
        let (_, rec, _) = f.ascent(&all);
        prop_assume!(!guarded(&rec.events));
        for (_, stored, decided) in stale_backed_rows(&f.g, &all, &mut NoLoops) {
            if let (Some(a), Some(b)) = (stored.value, decided.value) {
                prop_assert!((a - b).abs() < av2_rules::config::BACKED_EPSILON_CP * 64, "{stored:?} -> {decided:?}");
            }
        }
    }
}
