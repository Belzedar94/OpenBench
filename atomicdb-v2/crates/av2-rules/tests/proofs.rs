//! Step 10 wiring: the cascade's proof-refresh calls (PN-8.1) reach
//! `av2_proof::Proofs::after_batch`, and replaying them after the batch gives
//! the numbers an inline refresh would have given.

use av2_model::record::{F_BLACK_TO_MOVE, F_EXPANDED};
use av2_model::{Closure, Core, Edge, GraphRead, GraphWrite, Key, Move, PosId, Proof, Status};
use av2_proof::{Campaign, Proofs, RefreshConfig};
use av2_rules::climb::{ClimbEnv, climb};
use av2_rules::config::RulesConfig;
use av2_rules::effects::{Attribution, Recorder};
use av2_rules::exact::BoardZeroing;
use av2_rules::loops::NoLoops;
use av2_rules::proofs::ProofCalls;
use av2_rules::purchase::MemTasks;
use av2_rules::testkit::{Fixture, START_FEN, TestGraph};
use proptest::prelude::*;

fn proofs_for(root: PosId) -> Proofs {
    Proofs::new(vec![Campaign::default_for(1, root)], RefreshConfig::default())
}

/// Run one climb with the proof calls collected; return the calls.
fn climb_collect(g: &mut TestGraph, cascade: &[PosId], backed: &[PosId]) -> Vec<Vec<PosId>> {
    let mut rec = Recorder::default();
    let mut calls = ProofCalls::new(&mut rec);
    let cfg = RulesConfig::default();
    let mut z = BoardZeroing;
    let mut tasks = MemTasks::default();
    let mut loops = NoLoops;
    let attribution = Attribution::default();
    let mut env =
        ClimbEnv { cfg: &cfg, effects: &mut calls, zeroing: &mut z, tasks: &mut tasks, loops: &mut loops, attribution: &attribution };
    climb(g, cascade, backed, &mut env).unwrap();
    let out = calls.calls.clone();
    assert_eq!(rec.proof_refreshes, out, "the calls are forwarded unchanged");
    out
}

#[test]
fn a_closure_reaches_the_proof_numbers() {
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    let kids = f.expand_opening(root);
    let mut proofs = proofs_for(root);
    proofs.refresh(&f.g, &[root], 1, 0.0);
    let before = proofs.root_numbers(&f.g, 1).unwrap();
    f.close(kids[0], Status::WhiteWin, Closure::Terminal, Proof::None);
    let calls = climb_collect(&mut f.g, &[kids[0]], &[kids[0], root]);
    assert_eq!(calls, vec![vec![kids[0], root]], "seeds + changed, PN-8.1");
    proofs.after_batch(&f.g, &calls, 2, 1.0);
    let after = proofs.root_numbers(&f.g, 1).unwrap();
    assert_ne!(before, after);
    assert_eq!(after.0, 0, "the root is proved for the white-win campaign: pn = 0");
}

#[test]
fn a_landing_call_is_replayed_in_order() {
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    f.expand_opening(root);
    let mut rec = Recorder::default();
    let mut pc = ProofCalls::new(&mut rec);
    pc.landing(root);
    assert_eq!(pc.calls, vec![vec![root]]);
    let mut proofs = proofs_for(root);
    let r = proofs.after_batch(&f.g, &pc.calls, 1, 0.0);
    assert!(r.written > 0);
}

// ---------------------------------------------------------------- inline == after commit

#[derive(Clone, Debug)]
struct NodeSpec {
    black: bool,
    expanded: bool,
    state: u8,
    eval: Option<i16>,
    edges: Vec<usize>,
}

fn graph_spec() -> impl Strategy<Value = Vec<NodeSpec>> {
    (2usize..24).prop_flat_map(|n| {
        proptest::collection::vec(
            (any::<bool>(), any::<bool>(), 0u8..9, proptest::option::of(-400i16..400), proptest::collection::vec(0..n, 0..4))
                .prop_map(|(black, expanded, state, eval, edges)| NodeSpec { black, expanded, state, eval, edges }),
            n,
        )
    })
}

fn build(spec: &[NodeSpec]) -> TestGraph {
    let mut g = TestGraph::new();
    for (i, s) in spec.iter().enumerate() {
        let mut c = Core::default();
        if s.black {
            c.flags |= F_BLACK_TO_MOVE;
        }
        c.set_flag(F_EXPANDED, s.expanded);
        match s.state {
            6 => c.set_status(Status::WhiteWin, Closure::Terminal, Proof::None),
            7 => c.set_status(Status::BlackWin, Closure::Tb, Proof::None),
            8 => c.set_status(Status::Draw, Closure::Tb, Proof::None),
            _ => {}
        }
        c.eval_cp = s.eval.unwrap_or(av2_model::record::NO_CP);
        g.create(Key::of_canonical_fen(&format!("p{i}")).fingerprint(), None, c).unwrap();
    }
    for (i, s) in spec.iter().enumerate() {
        let mut es: Vec<Edge> = Vec::new();
        for (j, &t) in s.edges.iter().enumerate() {
            if t == i || s.state >= 6 || es.iter().any(|e| e.child.0 == t as u64) {
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

proptest! {
    #![proptest_config(ProptestConfig { cases: 200, .. ProptestConfig::default() })]

    /// The refresh after the commit (final graph) equals the refresh made
    /// right after the cascade (before the ascent wrote backed columns).
    #[test]
    fn replay_after_commit_equals_inline(spec in graph_spec(), seed in 0usize..24) {
        let mut g = build(&spec);
        let n = g.len();
        let s = PosId((seed as u64) % n);
        let mut parents = Vec::new();
        g.parents(s, &mut parents);
        // The cascade alone, then a snapshot: what an inline refresh reads.
        let mut snap = g.clone();
        {
            let mut rec = Recorder::default();
            let cfg = RulesConfig::default();
            let mut z = BoardZeroing;
            let att = Attribution::default();
            let mut env = av2_rules::cascade::Env { cfg: &cfg, effects: &mut rec, zeroing: &mut z, attribution: &att };
            av2_rules::cascade::backup_cascade(&mut snap, &[s], &mut env).unwrap();
        }
        let mut backed = vec![s];
        backed.extend(parents);
        let calls = climb_collect(&mut g, &[s], &backed);
        let mut inline = proofs_for(PosId(0));
        inline.after_batch(&snap, &calls, 1, 0.0);
        let mut replay = proofs_for(PosId(0));
        replay.after_batch(&g, &calls, 1, 0.0);
        prop_assert!(inline.table(1).unwrap().same_as(replay.table(1).unwrap()));
    }
}
