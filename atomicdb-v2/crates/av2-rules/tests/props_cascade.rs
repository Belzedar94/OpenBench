//! Property tests of the status cascade (step 4, plan section 7.5): it
//! terminates on any graph (cycles included), never reopens a decided row,
//! comes to a fixed point that a second cascade does not move, and on an
//! acyclic graph reaches the same rows whatever the seed order (the claim of
//! D3: pop order only matters where a guard bites), except for the witness
//! of a closure that no distance names.

use av2_model::record::{F_BLACK_TO_MOVE, F_EXPANDED};
use av2_model::{Closure, Core, Edge, GraphRead, GraphWrite, Key, Move, PosId, Proof, Status};
use av2_rules::config::RulesConfig;
use av2_rules::effects::Event;
use av2_rules::testkit::Fixture;
use proptest::prelude::*;

#[derive(Clone, Debug)]
struct NodeSpec {
    black: bool,
    expanded: bool,
    state: u8,
    mate: Option<u8>,
    slack: Option<u8>,
    edges: Vec<usize>,
}

fn node_spec(n: usize) -> impl Strategy<Value = NodeSpec> {
    (
        any::<bool>(),
        any::<bool>(),
        0u8..12,
        proptest::option::of(0u8..20),
        proptest::option::of(0u8..101),
        proptest::collection::vec(0..n, 0..5),
    )
        .prop_map(|(black, expanded, state, mate, slack, edges)| NodeSpec { black, expanded, state, mate, slack, edges })
}

fn graph_spec() -> impl Strategy<Value = Vec<NodeSpec>> {
    (2usize..28).prop_flat_map(|n| proptest::collection::vec(node_spec(n), n))
}

/// A move unique to edge `j` of a parent.
fn edge_move(j: usize) -> Move {
    Move::new((j % 8) as u8, (8 + j % 48) as u8, 0)
}

fn build(spec: &[NodeSpec], acyclic: bool) -> Fixture {
    let mut f = Fixture::new();
    for (i, s) in spec.iter().enumerate() {
        let mut core = Core::default();
        if s.black {
            core.flags |= F_BLACK_TO_MOVE;
        }
        core.set_flag(F_EXPANDED, s.expanded);
        // Most nodes open; the rest closed in assorted ways.
        let (st, cl, pr) = match s.state {
            0..=5 => (Status::Unknown, Closure::None, Proof::None),
            6 => (Status::WhiteWin, Closure::Terminal, Proof::None),
            7 => (Status::BlackWin, Closure::MatePv, Proof::Engine),
            8 => (Status::WhiteWin, Closure::MatePv, Proof::AndOr),
            9 => (Status::Draw, Closure::Tb, Proof::None),
            // No MINIMAX row in the initial state: a MINIMAX row is derived, and
            // one whose stored slack, grade or distance disagrees with its
            // children makes the fixed point depend on visit order (a parent
            // that read the old slack closed; one that read the new did not).
            10 => (Status::BlackWin, Closure::Tb, Proof::None),
            _ => (Status::WhiteWin, Closure::Tb, Proof::None),
        };
        core.set_status(st, cl, pr);
        if st != Status::Unknown {
            // A tablebase closure names no distance.
            if cl != Closure::Tb {
                core.mate_in = s.mate.map(u16::from).unwrap_or(av2_model::record::NO_MATE);
            }
            core.clock_slack = s.slack.unwrap_or(av2_model::record::NO_SLACK);
        }
        let fp = Key::of_canonical_fen(&format!("node {i}")).fingerprint();
        let id = f.g.create(fp, None, core).unwrap();
        // A MATE_PV row carries its line: a row without one stands on its
        // stored number (DTM-4.1, historic rows), which makes it its own
        // witness and the fixed point path-dependent, in v1 as here.
        if cl == Closure::MatePv {
            let plies = s.mate.unwrap_or(3).max(1) as usize;
            f.set_won_line(id, &vec!["a2a3"; plies].join(" "));
        }
    }
    for (i, s) in spec.iter().enumerate() {
        let mut edges: Vec<Edge> = Vec::new();
        for (j, &t) in s.edges.iter().enumerate() {
            // A terminal position has no moves.
            if t == i || (acyclic && t <= i) || s.state == 6 {
                continue;
            }
            edges.push(Edge { child: PosId(t as u64), mv: edge_move(j) });
        }
        if !edges.is_empty() {
            f.g.set_children(PosId(i as u64), &edges).unwrap();
        }
    }
    f
}

fn cores(f: &Fixture) -> Vec<Core> {
    (0..f.g.len()).map(|i| f.g.core(PosId(i))).collect()
}

fn guarded(events: &[Event]) -> bool {
    events.iter().any(|e| matches!(e, Event::CascadeGuard { .. } | Event::CascadeCycle { .. }))
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 400, .. ProptestConfig::default() })]

    #[test]
    fn terminates_and_never_reopens(spec in graph_spec(), seed in 0usize..28) {
        let mut f = build(&spec, false);
        let before = cores(&f);
        let n = f.g.len() as usize;
        let cfg = RulesConfig { cascade_guard_limit: 20_000, ..RulesConfig::default() };
        let (r, _) = f.cascade_cfg(&cfg, &[PosId((seed % n) as u64)]);
        prop_assert!(r.pops <= 20_000);
        for (b, a) in before.iter().zip(cores(&f)) {
            if b.status() != Status::Unknown {
                prop_assert_eq!(a.status(), b.status());
            }
            // Only the seven cascade columns change.
            prop_assert_eq!((a.eval_cp, a.backed_eval, a.backed_move, a.backed_plies, a.flags & !0xFF),
                            (b.eval_cp, b.backed_eval, b.backed_move, b.backed_plies, b.flags & !0xFF));
        }
    }

    #[test]
    fn a_dag_comes_to_a_fixed_point(spec in graph_spec()) {
        let mut f = build(&spec, true);
        let all: Vec<PosId> = (0..f.g.len()).map(PosId).collect();
        let (_, rec) = f.cascade(&all);
        prop_assume!(!guarded(&rec.events));
        let (again, rec2) = f.cascade(&all);
        prop_assert!(again.changed.is_empty(), "second cascade wrote {:?}", again.changed);
        prop_assert!(rec2.closed.is_empty());
    }

    #[test]
    fn a_dag_does_not_depend_on_seed_order(spec in graph_spec(), rot in 0usize..28) {
        let base = build(&spec, true);
        let mut all: Vec<PosId> = (0..base.g.len()).map(PosId).collect();
        let mut f1 = base.clone();
        let (_, r1) = f1.cascade(&all);
        all.reverse();
        let k = rot % all.len();
        all.rotate_left(k);
        let mut f2 = base.clone();
        let (_, r2) = f2.cascade(&all);
        prop_assume!(!guarded(&r1.events) && !guarded(&r2.events));
        // Every column but the witness: a new closure's `best_move` is the
        // first winning child closed AT THE MOMENT it closes (CAS-1.3), and the
        // distance rule keeps it when no child has a distance (DTM-1.1), so it
        // depends on visit order in v1 too (found by this test; see
        // docs/rules-port-status.md, CAS-1.3).
        let strip = |v: Vec<Core>| v.into_iter().map(|c| Core { best_move: Move::NONE, ..c }).collect::<Vec<_>>();
        prop_assert_eq!(strip(cores(&f1)), strip(cores(&f2)));
        let mut c1: Vec<PosId> = r1.closed.iter().map(|c| c.pos).collect();
        let mut c2: Vec<PosId> = r2.closed.iter().map(|c| c.pos).collect();
        c1.sort();
        c2.sort();
        prop_assert_eq!(c1, c2);
    }

    #[test]
    fn a_cyclic_graph_rests_or_reports(spec in graph_spec()) {
        let mut f = build(&spec, false);
        let all: Vec<PosId> = (0..f.g.len()).map(PosId).collect();
        let (r, rec) = f.cascade(&all);
        // Either the frontier emptied (no guard event) or the guard says so.
        prop_assert_eq!(r.remaining > 0, rec.events.iter().any(|e| matches!(e, Event::CascadeGuard { .. })));
        // Every row the rewrite limit stopped is reported.
        prop_assert_eq!(!r.cycling.is_empty(), rec.events.iter().any(|e| matches!(e, Event::CascadeCycle { .. })));
    }
}
