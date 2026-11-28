//! Steps 2 to 5 of the port, through the cascade: v1's portable tests of
//! groups CAS-1, CAS-3, CAS-4, CAS-5, CAS-7, DTM-1, DTM-2, DTM-3, DTM-4 and
//! DTM-5, translated fixture for fixture.  Each test names its v1 original.

use av2_model::{Closure, PosId, Proof, Status};
use av2_rules::config::RulesConfig;
use av2_rules::distance::{DtmOpts, mate_distance_refresh, support_reaches};
use av2_rules::effects::Event;
use av2_rules::testkit::{Fixture, START_FEN, after, developed_moves, mv, opening_moves};

use Closure::{MatePv, Minimax, Tb, Terminal};
use Status::*;

fn mate(f: &Fixture, id: PosId) -> Option<u16> {
    f.core(id).mate()
}

fn best(f: &Fixture, id: PosId) -> String {
    f.core(id).best_move.to_uci()
}

// ------------------------------------------------------------ tests.py

/// tests.py::BackupTests::test_minimax_win_propagates
#[test]
fn backup_minimax_win_propagates() {
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    let kids = f.expand_opening(root);
    f.close(kids[0], WhiteWin, MatePv, Proof::None);
    f.cascade(&[kids[0]]);
    assert_eq!(f.core(root).status(), WhiteWin);
    assert_eq!(f.core(root).closure(), Minimax);
}

/// tests.py::BackupTests::test_minimax_loss_requires_full_expansion
#[test]
fn backup_minimax_loss_requires_full_expansion() {
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    let child = f.child(root, "e2e4");
    f.close(child, BlackWin, Closure::None, Proof::None);
    f.cascade(&[child]);
    assert_eq!(f.core(root).status(), Unknown);
}

/// tests.py::BackupTests::test_backup_deterministic_replay
#[test]
fn backup_deterministic_replay() {
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    let kids = f.expand_opening(root);
    for &k in &kids[..5] {
        f.set(k, |c| c.eval_cp = 100);
    }
    f.cascade(&[root]);
    let v1 = f.core(root).eval_cp;
    f.cascade(&[root]);
    assert_eq!(f.core(root).eval_cp, v1);
    assert_eq!(f.core(root).eval(), None);
}

/// tests.py::PartialExpansionTests::test_partial_edges_do_not_poison_eval
#[test]
fn partial_edges_do_not_poison_eval() {
    let mut f = Fixture::new();
    let p = f.pos(&after(&["g1f3"]));
    f.set(p, |c| c.eval_cp = 441);
    let child = f.child(p, "d7d5");
    f.close(child, WhiteWin, MatePv, Proof::None);
    f.cascade(&[child]);
    assert_eq!(f.core(p).status(), Unknown);
    assert_eq!(f.core(p).eval(), Some(441));
}

/// tests.py::DtmRefineTests::test_shorter_mate_refines_distance_and_witness
#[test]
fn shorter_mate_refines_distance_and_witness() {
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    f.expand_opening(root);
    let a = f.edge_child(root, "e2e4");
    f.closed(a, WhiteWin, MatePv, Proof::None, Some(4), None, "");
    f.cascade(&[a]);
    assert_eq!((f.core(root).status(), mate(&f, root), best(&f, root).as_str()), (WhiteWin, Some(5), "e2e4"));
    let b = f.edge_child(root, "d2d4");
    f.closed(b, WhiteWin, MatePv, Proof::None, Some(2), None, "");
    f.cascade(&[b]);
    assert_eq!((mate(&f, root), best(&f, root).as_str()), (Some(3), "d2d4"));
}

fn all_children_lost(dists: impl Fn(usize) -> Option<u16>) -> (Fixture, PosId) {
    let mut f = Fixture::new();
    let p = f.pos(&after(&["g1f3"]));
    let kids = f.expand_opening(p);
    for (i, &c) in kids.iter().enumerate() {
        f.closed(c, WhiteWin, MatePv, Proof::None, dists(i), Some(50), "");
    }
    f.cascade(&[kids[0]]);
    (f, p)
}

/// tests.py::DtmTests::test_loss_takes_max_plus_one
#[test]
fn dtm_loss_takes_max_plus_one() {
    let (f, p) = all_children_lost(|i| Some(if i == 0 { 4 } else { 2 }));
    assert_eq!((f.core(p).status(), mate(&f, p)), (WhiteWin, Some(5)));
}

/// tests.py::DtmTests::test_tb_child_leaves_distance_unknown
#[test]
fn dtm_tb_child_leaves_distance_unknown() {
    let (f, p) = all_children_lost(|i| if i == 0 { None } else { Some(2) });
    assert_eq!(f.core(p).status(), WhiteWin);
    assert_eq!(mate(&f, p), None);
}

// ------------------------------------------------------------ CAS-1.2 fresh context

fn all_replies_lost(slack: Option<u8>) -> (Fixture, PosId, Vec<PosId>) {
    let mut f = Fixture::new();
    let parent = f.pos(START_FEN);
    let kids = f.expand_opening(parent);
    for &c in &kids {
        f.closed(c, BlackWin, MatePv, Proof::AndOr, Some(1), slack, "");
    }
    (f, parent, kids)
}

/// test_clock_slack.py::FreshContextTests::test_a_quiet_edge_with_no_margin_cannot_close_the_parent
#[test]
fn a_quiet_edge_with_no_margin_cannot_close_the_parent() {
    let (mut f, parent, kids) = all_replies_lost(Some(0));
    f.cascade(&kids);
    assert_eq!(f.core(parent).status(), Unknown);
}

/// test_clock_slack.py::FreshContextTests::test_the_same_shape_closes_once_the_children_have_margin
#[test]
fn the_same_shape_closes_once_the_children_have_margin() {
    let (mut f, parent, kids) = all_replies_lost(Some(50));
    f.cascade(&kids);
    let c = f.core(parent);
    assert_eq!((c.status(), c.closure()), (BlackWin, Minimax));
    assert_eq!(c.slack(), Some(49));
}

/// test_clock_slack.py::FreshContextTests::test_a_zeroing_edge_carries_a_closure_with_no_margin_at_all
#[test]
fn a_zeroing_edge_carries_a_closure_with_no_margin_at_all() {
    let (mut f, parent, kids) = all_replies_lost(Some(90));
    let e4 = f.edge_child(parent, "e2e4");
    f.set(e4, |c| c.clock_slack = 0);
    let _ = kids;
    f.cascade(&[e4]);
    assert_eq!(f.core(parent).status(), BlackWin);
}

/// test_clock_slack.py::FreshContextTests::test_the_rule_can_be_switched_off_for_the_deploy_window
#[test]
fn the_rule_can_be_switched_off_for_the_deploy_window() {
    let (mut f, parent, kids) = all_replies_lost(None);
    let cfg = RulesConfig { fresh_context: false, ..RulesConfig::default() };
    f.cascade_cfg(&cfg, &kids);
    assert_eq!(f.core(parent).status(), BlackWin);
}

// ------------------------------------------------------------ CAS-3.1, CAS-1.3

/// test_proofs.py::MinimaxProofInheritanceTests::test_winning_witness_inherits_andor_then_engine
#[test]
fn winning_witness_inherits_andor_then_engine() {
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    f.expand_opening(root);
    let child = f.edge_child(root, "e2e4");
    f.close(child, WhiteWin, MatePv, Proof::AndOr);
    f.cascade(&[child]);
    let c = f.core(root);
    assert_eq!((c.status(), c.closure(), c.proof()), (WhiteWin, Minimax, Proof::AndOr));
    f.close(child, WhiteWin, MatePv, Proof::Engine);
    f.cascade(&[child]);
    assert_eq!(f.core(root).proof(), Proof::Engine);
}

/// test_revocation.py::MinimaxWitnessPreferenceTests::test_verified_winning_child_is_preferred_as_the_witness
#[test]
fn verified_winning_child_is_preferred_as_the_witness() {
    let mut f = Fixture::new();
    let parent = f.pos(START_FEN);
    let kids = f.expand_opening(parent);
    let (engine, verified) = (kids[0], kids[1]);
    f.closed(engine, WhiteWin, MatePv, Proof::Engine, Some(3), None, "");
    f.closed(verified, WhiteWin, MatePv, Proof::AndOr, Some(3), None, "");
    f.cascade(&[engine, verified]);
    let c = f.core(parent);
    assert_eq!(c.status(), WhiteWin);
    assert_eq!(c.proof(), Proof::AndOr);
    assert_eq!(c.best_move, mv(&opening_moves(true)[1]));
}

// ------------------------------------------------------------ CAS-7.1, CAS-4.1

/// test_adversarial_explorer.py::ClosureAttributionTests::test_a_certificate_closure_emits_one_event_not_two
/// (the cascade's half: one NODE_CLOSED per closure, carrying the source).
#[test]
fn one_closure_event_per_closure_with_its_source() {
    use av2_rules::cascade::{Env, backup_cascade};
    use av2_rules::effects::{Attribution, ClosureSource, Provenance, Recorder};
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    let kids = f.expand_opening(root);
    f.close(kids[0], WhiteWin, Terminal, Proof::None);
    f.close(kids[1], WhiteWin, Terminal, Proof::None);
    let mut rec = Recorder::default();
    let attribution = Attribution { source: ClosureSource::parse("fill"), provenance: Some(Provenance::clean(Some(7), "acct", "sha")) };
    let cfg = RulesConfig::default();
    let mut z = av2_rules::exact::BoardZeroing;
    let mut env = Env { cfg: &cfg, effects: &mut rec, zeroing: &mut z, attribution: &attribution };
    backup_cascade(&mut f.g, &[kids[0], kids[1]], &mut env).unwrap();
    backup_cascade(&mut f.g, &[kids[0], kids[1]], &mut env).unwrap();
    assert_eq!(rec.closed.len(), 1);
    let c = &rec.closed[0];
    assert_eq!((c.pos, c.status, c.closure, c.proof, c.source), (root, WhiteWin, Minimax, Proof::AndOr, ClosureSource::Fill));
    assert_eq!(c.provenance.as_ref().unwrap().task, Some(7));
    // CAS-4.1: an unknown label is NONE.
    assert_eq!(ClosureSource::parse("weird"), ClosureSource::None);
    // PN-8.1: the refresh of seeds + changed after each cascade.
    assert_eq!(rec.proof_refreshes[0], vec![kids[0], kids[1], root]);
    assert_eq!(rec.proof_refreshes[1], vec![kids[0], kids[1]]);
}

/// POS-2.1 / CAS-7.1: only the cascade's own closures pass through the
/// closure door; a row closed before the call fires nothing.
#[test]
fn an_already_closed_row_fires_nothing() {
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    let kids = f.expand_opening(root);
    f.close(kids[0], WhiteWin, Terminal, Proof::None);
    f.close(root, WhiteWin, MatePv, Proof::Engine);
    let (_, rec) = f.cascade(&[kids[0]]);
    assert!(rec.closed.is_empty());
}

// ------------------------------------------------------------ CAS-5

/// test_performance_guards.py::CascadeGuardTests::test_guard_exhaustion_emits_event
#[test]
fn guard_exhaustion_emits_event() {
    let mut f = Fixture::new();
    let root = f.pos(START_FEN);
    let kids = f.expand_opening(root);
    let cfg = RulesConfig { cascade_guard_limit: 1, ..RulesConfig::default() };
    let (_, rec) = f.cascade_cfg(&cfg, &[kids[0]]);
    let ev = rec.events.iter().find_map(|e| match e {
        Event::CascadeGuard { iterations, remaining, .. } => Some((*iterations, *remaining)),
        _ => None,
    });
    let (iterations, remaining) = ev.expect("CASCADE_GUARD");
    assert_eq!(iterations, 1);
    assert!(remaining > 0);
}

/// The knight shuffle 1.Nf3 Nf6 2.Ng1 Ng8: four closed nodes whose only
/// edges are the shuffle (test_performance_guards.py::CascadeCycleTests::_cycle,
/// test_mate_distance.py::BackfillCycleTests).
fn shuffle_cycle(f: &mut Fixture) -> Vec<PosId> {
    let moves = ["g1f3", "g8f6", "f3g1", "f6g8"];
    let mut fens = vec![START_FEN.to_string()];
    for m in &moves[..3] {
        fens.push(av2_rules::testkit::play(fens.last().unwrap(), m));
    }
    let nodes: Vec<PosId> = fens.iter().map(|fe| f.pos(fe)).collect();
    for (i, &n) in nodes.iter().enumerate() {
        f.closed(n, WhiteWin, Minimax, Proof::Engine, Some(10), Some(90), moves[i]);
        f.set(n, |c| c.set_flag(av2_model::record::F_EXPANDED, true));
    }
    for (i, &n) in nodes.iter().enumerate() {
        let child = f.pos(&av2_rules::testkit::play(&fens[i], moves[i]));
        assert_eq!(child, nodes[(i + 1) % 4]);
        f.edge(n, moves[i], child);
    }
    nodes
}

/// test_performance_guards.py::CascadeCycleTests::test_a_cycle_that_never_rests_is_cut_by_the_rewrite_limit
#[test]
fn a_cycle_that_never_rests_is_cut_by_the_rewrite_limit() {
    let mut f = Fixture::new();
    let nodes = shuffle_cycle(&mut f);
    let cfg = RulesConfig { cascade_guard_limit: 5_000, mate_support_hops: 1, cascade_rewrite_limit: 3, ..RulesConfig::default() };
    let (_, rec) = f.cascade_cfg(&cfg, &[nodes[0]]);
    assert!(!rec.events.iter().any(|e| matches!(e, Event::CascadeGuard { .. })));
    let (limit, count, iterations) = rec
        .events
        .iter()
        .find_map(|e| match e {
            Event::CascadeCycle { limit, count, iterations, .. } => Some((*limit, *count, *iterations)),
            _ => None,
        })
        .expect("CASCADE_CYCLE");
    assert_eq!(limit, 3);
    assert!(count >= 1);
    assert!(iterations < 40);
    for &n in &nodes {
        assert!(mate(&f, n).unwrap() <= 10 + 4 * 3 + 4);
        assert_eq!(f.core(n).status(), WhiteWin);
    }
}

/// test_performance_guards.py::CascadeCycleTests::test_a_long_chain_is_not_touched_by_the_limit
#[test]
fn a_long_chain_is_not_touched_by_the_limit() {
    let moves = ["g1f3", "g8f6", "b1c3", "b8c6", "e2e3", "e7e6", "d2d3", "d7d6", "a2a3", "a7a6", "h2h3", "h7h6"];
    let mut f = Fixture::new();
    let mut fens = vec![START_FEN.to_string()];
    for m in moves {
        fens.push(av2_rules::testkit::play(fens.last().unwrap(), m));
    }
    let n = moves.len();
    let nodes: Vec<PosId> = (0..n).map(|i| f.pos(&fens[i])).collect();
    for &x in &nodes {
        f.closed(x, WhiteWin, Minimax, Proof::Engine, Some(100), Some(90), "");
        f.set(x, |c| c.set_flag(av2_model::record::F_EXPANDED, true));
    }
    let terminal = f.pos(&fens[n]);
    f.closed(terminal, WhiteWin, Terminal, Proof::None, Some(0), Some(90), "");
    for i in 0..n {
        let child = if i + 1 < n { nodes[i + 1] } else { terminal };
        f.edge(nodes[i], moves[i], child);
    }
    let cfg = RulesConfig { cascade_rewrite_limit: 1, ..RulesConfig::default() };
    let (_, rec) = f.cascade_cfg(&cfg, &[terminal]);
    assert!(!rec.events.iter().any(|e| matches!(e, Event::CascadeCycle { .. })));
    for (depth, &x) in nodes.iter().enumerate() {
        assert_eq!(mate(&f, x), Some((n - depth) as u16));
    }
}

// ------------------------------------------------------------ test_mate_distance.py

fn reported_graph(child_mate_in: u16) -> (Fixture, PosId, PosId) {
    let mut f = Fixture::new();
    let parent = f.pos(&after(&["g1f3", "d7d5"]));
    f.expand_with(parent, &developed_moves(true));
    f.closed(parent, WhiteWin, Minimax, Proof::Engine, Some(3), Some(90), "f3e5");
    let child = f.edge_child(parent, "f3e5");
    f.closed(child, WhiteWin, MatePv, Proof::Engine, Some(child_mate_in), Some(90), "f7f6");
    f.set_won_line(child, "f7f6 e5d7 e7e6 d7f8");
    (f, parent, child)
}

/// test_mate_distance.py::ReportedShapeTests::test_the_parent_follows_a_child_that_corrected_upwards
#[test]
fn the_parent_follows_a_child_that_corrected_upwards() {
    let (mut f, parent, child) = reported_graph(4);
    f.cascade(&[child]);
    assert_eq!(mate(&f, parent), Some(5));
    assert_eq!(best(&f, parent), "f3e5");
    assert_eq!((mate(&f, parent).unwrap() + 1) / 2, 3);
}

/// test_mate_distance.py::ReportedShapeTests::test_the_parent_still_shortens_when_a_child_improves
#[test]
fn the_parent_still_shortens_when_a_child_improves() {
    let (mut f, parent, child) = reported_graph(1);
    f.cascade(&[child]);
    assert_eq!(mate(&f, parent), Some(2));
    assert_eq!(best(&f, parent), "f3e5");
}

fn and_mirror(first_child_mate_in: u16) -> (Fixture, PosId, PosId) {
    let mut f = Fixture::new();
    let parent = f.pos(&after(&["g1f3"]));
    let kids = f.expand_opening(parent);
    f.closed(parent, WhiteWin, Minimax, Proof::Engine, Some(3), Some(90), "");
    for (i, &c) in kids.iter().enumerate() {
        f.closed(c, WhiteWin, MatePv, Proof::Engine, Some(if i == 0 { first_child_mate_in } else { 2 }), Some(90), "");
    }
    (f, parent, kids[0])
}

/// test_mate_distance.py::AndMirrorTests::test_the_and_parent_recomputes_exactly_upwards
#[test]
fn the_and_parent_recomputes_exactly_upwards() {
    let (mut f, parent, child) = and_mirror(8);
    f.cascade(&[child]);
    assert_eq!(mate(&f, parent), Some(9));
}

/// test_mate_distance.py::AndMirrorTests::test_the_and_parent_recomputes_exactly_downwards
#[test]
fn the_and_parent_recomputes_exactly_downwards() {
    let (mut f, parent, child) = and_mirror(1);
    f.cascade(&[child]);
    assert_eq!(mate(&f, parent), Some(3));
}

fn loser_floor(reply_mate_in: u16) -> (Fixture, PosId, PosId) {
    let own_line = "g8f6 f3e5 f6e4";
    let mut f = Fixture::new();
    let parent = f.pos(&after(&["g1f3"]));
    f.expand_opening(parent);
    f.closed(parent, WhiteWin, MatePv, Proof::Engine, Some(3), Some(90), "g8f6");
    f.set_won_line(parent, own_line);
    let child = f.edge_child(parent, "d7d5");
    f.closed(child, WhiteWin, MatePv, Proof::Engine, Some(reply_mate_in), Some(90), "");
    (f, parent, child)
}

/// test_mate_distance.py::LoserWitnessFloorTests::test_a_longer_closed_reply_raises_the_witness
#[test]
fn a_longer_closed_reply_raises_the_witness() {
    let (mut f, parent, child) = loser_floor(7);
    f.cascade(&[child]);
    let c = f.core(parent);
    assert_eq!(mate(&f, parent), Some(8));
    assert_eq!(best(&f, parent), "g8f6");
    assert_eq!((c.status(), c.closure()), (WhiteWin, MatePv));
}

/// test_mate_distance.py::LoserWitnessFloorTests::test_a_shorter_closed_reply_leaves_the_witness
#[test]
fn a_shorter_closed_reply_leaves_the_witness() {
    let (mut f, parent, child) = loser_floor(1);
    f.cascade(&[child]);
    assert_eq!(mate(&f, parent), Some(3));
}

/// test_mate_distance.py::LoserWitnessFloorTests::test_the_number_comes_back_down_when_the_reply_shortens
#[test]
fn the_number_comes_back_down_when_the_reply_shortens() {
    let (mut f, parent, child) = loser_floor(7);
    f.cascade(&[child]);
    f.set(child, |c| c.mate_in = 4);
    f.cascade(&[child]);
    assert_eq!(mate(&f, parent), Some(5));
}

fn witnessed_parent(child_mate_in: u16) -> (Fixture, PosId, PosId) {
    let mut f = Fixture::new();
    let parent = f.pos(&after(&["g1f3", "d7d5"]));
    f.expand_with(parent, &developed_moves(true));
    f.closed(parent, WhiteWin, MatePv, Proof::Engine, Some(3), Some(90), "f3e5");
    f.set_won_line(parent, "f3e5 d8d6 e5f7");
    let child = f.edge_child(parent, "e2e4");
    f.closed(child, WhiteWin, MatePv, Proof::Engine, Some(child_mate_in), Some(90), "");
    (f, parent, child)
}

/// test_mate_distance.py::OwnWitnessTests::test_a_longer_child_cannot_stretch_the_stored_witness
#[test]
fn a_longer_child_cannot_stretch_the_stored_witness() {
    let (mut f, parent, child) = witnessed_parent(20);
    f.cascade(&[child]);
    assert_eq!(mate(&f, parent), Some(3));
    assert_eq!(best(&f, parent), "f3e5");
}

/// test_mate_distance.py::OwnWitnessTests::test_a_shorter_child_still_shortens_it
#[test]
fn a_shorter_child_still_shortens_it() {
    let (mut f, parent, child) = witnessed_parent(1);
    f.cascade(&[child]);
    assert_eq!(mate(&f, parent), Some(2));
    assert_eq!(best(&f, parent), "e2e4");
}

/// test_mate_distance.py::OwnWitnessTests::test_the_witness_comes_back_when_the_short_child_goes_away,
/// with the revocation (REV-1, phase 3) replaced by what it leaves: the
/// child reopened, then the cascade from it.
#[test]
fn the_witness_comes_back_when_the_short_child_goes_away() {
    let (mut f, parent, child) = witnessed_parent(1);
    f.cascade(&[child]);
    assert_eq!(mate(&f, parent), Some(2));
    f.closed(child, Unknown, Closure::None, Proof::None, None, None, "");
    f.cascade(&[child]);
    assert_eq!(f.core(parent).status(), WhiteWin);
    assert_eq!(mate(&f, parent), Some(3));
    assert_eq!(best(&f, parent), "f3e5");
}

/// test_mate_distance.py::UnnameableDistanceTests::test_a_minimax_or_without_a_measured_winner_loses_its_number
#[test]
fn a_minimax_or_without_a_measured_winner_loses_its_number() {
    let mut f = Fixture::new();
    let parent = f.pos(&after(&["g1f3", "d7d5"]));
    f.expand_with(parent, &developed_moves(true));
    f.closed(parent, WhiteWin, Minimax, Proof::Engine, Some(3), Some(90), "f3e5");
    let child = f.edge_child(parent, "f3e5");
    f.closed(child, WhiteWin, Tb, Proof::None, None, Some(90), "");
    f.cascade(&[child]);
    assert_eq!(f.core(parent).status(), WhiteWin);
    assert_eq!(mate(&f, parent), None);
}

/// test_chain_link_distance.py::EquivalentWitnessTests::test_a_tie_between_verified_witnesses_is_left_alone
/// (the recurrence half; the backfill command is phase 3's).
#[test]
fn a_tie_between_verified_witnesses_takes_the_first_edge() {
    let mut f = Fixture::new();
    let node = f.pos(&after(&["g1f3", "g8f6"]));
    f.closed(node, WhiteWin, Minimax, Proof::AndOr, Some(1), Some(90), "b1c3");
    let first = f.child(node, "a2a3");
    let second = f.child(node, "b1c3");
    f.closed(first, WhiteWin, Terminal, Proof::None, Some(0), Some(90), "");
    f.closed(second, WhiteWin, Terminal, Proof::None, Some(0), Some(90), "");
    let kids = f.kids(node);
    let got = mate_distance_refresh(&f.g, node, &f.core(node), &kids, DtmOpts::default());
    assert_eq!(got, (Some(1), mv("a2a3")));
}

// ------------------------------------------------------------ test_mate_distance_cycles.py (DTM-3.1)

const OWN_LINE: &str = "e7e5 f3e5 d7d6 e5f7 e8f7 d1e2 f7g6 e2e4 g6h6";

#[derive(Clone, Copy, PartialEq)]
enum Anchor {
    Partial,
    Full,
    Alone,
}

struct Loop {
    f: Fixture,
    start: PosId,
    pa: PosId,
    pb: PosId,
    pc: PosId,
}

impl Loop {
    fn mates(&self, ps: &[PosId]) -> Vec<Option<u16>> {
        ps.iter().map(|&p| mate(&self.f, p)).collect()
    }
}

fn close_w(f: &mut Fixture, id: PosId, mate_in: Option<u16>, closure: Closure, best_move: &str) {
    f.closed(id, WhiteWin, closure, Proof::Engine, mate_in, Some(90), best_move);
}

fn build_loop(anchor: Anchor, a: Option<u16>, b: Option<u16>, c: Option<u16>, s: Option<u16>, a_best: &str) -> Loop {
    let mut f = Fixture::new();
    let start = f.pos(START_FEN);
    let pa = f.pos(&after(&["g1f3"]));
    let pb = f.pos(&after(&["g1f3", "g8f6"]));
    let pc = f.pos(&after(&["g1f3", "g8f6", "f3g1"]));
    f.expand_opening(start);
    f.expand_with(pb, &developed_moves(true));
    f.expand_with(pc, &developed_moves(false));
    assert_eq!(f.edge_child(pc, "f6g8"), start);
    if anchor == Anchor::Alone {
        f.edge(pa, "g8f6", pb);
    } else {
        f.expand_opening(pa);
    }
    let a_edges = f.children(pa);
    for e in &a_edges {
        let u = e.mv.to_uci();
        let d = match anchor {
            Anchor::Partial => match u.as_str() {
                "d7d5" => Some(5),
                "b8c6" => Some(3),
                _ => None,
            },
            Anchor::Full if u != "g8f6" => Some(if u == "d7d5" { 7 } else { 2 }),
            _ => None,
        };
        if let Some(d) = d {
            close_w(&mut f, e.child, Some(d), MatePv, "");
        }
    }
    for e in f.children(pc) {
        let u = e.mv.to_uci();
        if u != "f6g8" {
            close_w(&mut f, e.child, Some(if u == "e7e5" { 5 } else { 1 }), MatePv, "");
        }
    }
    if anchor == Anchor::Partial {
        close_w(&mut f, pa, a, MatePv, a_best);
        f.set_won_line(pa, OWN_LINE);
    } else {
        close_w(&mut f, pa, a, Minimax, a_best);
    }
    close_w(&mut f, pb, b, Minimax, "f3g1");
    close_w(&mut f, pc, c, Minimax, "f6g8");
    close_w(&mut f, start, s, Minimax, "g1f3");
    Loop { f, start, pa, pb, pc }
}

/// test_mate_distance_cycles.py::RepetitionLoopTests::test_the_loop_converges_to_the_acyclic_distances
#[test]
fn the_loop_converges_to_the_acyclic_distances() {
    let mut l = build_loop(Anchor::Partial, Some(9), None, None, None, "e7e5");
    for p in [l.pb, l.pc, l.start] {
        l.f.set(p, |c| c.mate_in = 3);
    }
    let (pa, pb, pc, s) = (l.pa, l.pb, l.pc, l.start);
    l.f.cascade(&[pc, s, pb, pa]);
    assert_eq!(l.mates(&[pa, s, pc, pb]), vec![Some(9), Some(10), Some(11), Some(12)]);
    for seeds in [vec![pa], vec![pb], vec![pc], vec![s], vec![pa, pb, pc, s]] {
        l.f.cascade(&seeds);
        assert_eq!(l.mates(&[pa, s, pc, pb]), vec![Some(9), Some(10), Some(11), Some(12)]);
    }
}

/// test_mate_distance_cycles.py::RepetitionLoopTests::test_an_inflated_loop_that_moves_comes_down_in_the_cascade
#[test]
fn an_inflated_loop_that_moves_comes_down_in_the_cascade() {
    let mut l = build_loop(Anchor::Partial, Some(289), Some(288), Some(287), Some(286), "e7e5");
    let pa = l.pa;
    l.f.cascade(&[pa]);
    assert_eq!(l.mates(&[l.pa, l.start, l.pc, l.pb]), vec![Some(9), Some(10), Some(11), Some(12)]);
}

/// test_mate_distance_cycles.py::RepetitionLoopTests::test_the_exported_defence_does_not_point_into_the_loop
/// (the `_backed_for` half is in `tests/backed.rs`).
#[test]
fn the_exported_defence_does_not_point_into_the_loop() {
    let mut l = build_loop(Anchor::Partial, Some(9), None, None, None, "g8f6");
    let (pa, pb, pc, s) = (l.pa, l.pb, l.pc, l.start);
    l.f.cascade(&[pc, s, pb, pa]);
    let row = l.f.core(pa);
    assert_eq!(row.mate(), Some(9));
    assert_eq!(row.best_move, mv("e7e5"));
    assert_eq!((row.status(), row.closure()), (WhiteWin, MatePv));
}

/// test_mate_distance_cycles.py::RepetitionLoopTests::test_a_full_information_anchor_keeps_its_longest_outside_reply
#[test]
fn a_full_information_anchor_keeps_its_longest_outside_reply() {
    let mut l = build_loop(Anchor::Full, Some(8), None, None, None, "e7e5");
    let (pa, pb, pc, s) = (l.pa, l.pb, l.pc, l.start);
    l.f.cascade(&[pc, s, pb, pa]);
    assert_eq!(l.mates(&[pa, s, pc, pb]), vec![Some(8), Some(9), Some(10), Some(11)]);
    l.f.cascade(&[pa, pb, pc, s]);
    assert_eq!(l.mates(&[pa, s, pc, pb]), vec![Some(8), Some(9), Some(10), Some(11)]);
}

/// test_mate_distance_cycles.py::RepetitionLoopTests::test_an_anchor_with_nothing_but_the_loop_loses_its_number
#[test]
fn an_anchor_with_nothing_but_the_loop_loses_its_number() {
    let l = build_loop(Anchor::Alone, Some(9), Some(12), Some(11), Some(10), "g8f6");
    let kids = l.f.kids(l.pa);
    let got = mate_distance_refresh(&l.f.g, l.pa, &l.f.core(l.pa), &kids, DtmOpts::default());
    assert_eq!(got, (None, av2_model::Move::NONE));
}

/// test_mate_distance_cycles.py::RepetitionLoopTests::test_a_longer_defence_that_does_not_come_back_is_still_taken
#[test]
fn a_longer_defence_that_does_not_come_back_is_still_taken() {
    let mut l = build_loop(Anchor::Partial, Some(9), None, None, None, "e7e5");
    let d7d5 = l.f.edge_child(l.pa, "d7d5");
    l.f.set(d7d5, |c| c.mate_in = 21);
    l.f.cascade(&[d7d5]);
    assert_eq!(mate(&l.f, l.pa), Some(22));
}

/// test_mate_distance_cycles.py::RepetitionLoopTests::test_an_acyclic_raise_is_unchanged
#[test]
fn an_acyclic_raise_is_unchanged() {
    let mut f = Fixture::new();
    let parent = f.pos(&after(&["g1f3"]));
    let kids = f.expand_opening(parent);
    for (i, &c) in kids.iter().enumerate() {
        close_w(&mut f, c, Some(if i == 0 { 8 } else { 2 }), MatePv, "");
    }
    close_w(&mut f, parent, Some(3), Minimax, "");
    f.cascade(&[kids[0]]);
    assert_eq!(mate(&f, parent), Some(9));
}

/// test_mate_distance_cycles.py::RepetitionLoopTests::test_the_walk_stops_at_the_hop_limit
#[test]
fn the_walk_stops_at_the_hop_limit() {
    let mut l = build_loop(Anchor::Partial, Some(289), Some(288), Some(287), Some(286), "e7e5");
    assert!(support_reaches(&l.f.g, l.pb, l.pa, 4, None));
    assert!(!support_reaches(&l.f.g, l.pb, l.pa, 3, None));
    assert!(support_reaches(&l.f.g, l.pa, l.pb, 64, None));
    let pa = l.pa;
    l.f.set(pa, |c| c.mate_in = 9);
    assert!(!support_reaches(&l.f.g, l.pa, l.pb, 64, None));
}

/// test_mate_distance_cycles.py::RepetitionLoopTests::test_a_loop_longer_than_the_hop_limit_is_not_set_aside
#[test]
fn a_loop_longer_than_the_hop_limit_is_not_set_aside() {
    let l = build_loop(Anchor::Partial, Some(9), Some(12), Some(11), Some(10), "e7e5");
    let kids = l.f.kids(l.pa);
    let row = l.f.core(l.pa);
    let short = DtmOpts { hops: 3, ..DtmOpts::default() };
    assert_eq!(mate_distance_refresh(&l.f.g, l.pa, &row, &kids, short).0, Some(13));
    assert_eq!(mate_distance_refresh(&l.f.g, l.pa, &row, &kids, DtmOpts::default()).0, Some(9));
}

/// test_mate_distance.py::BackfillCycleTests::test_an_ungrounded_cycle_comes_to_rest_instead_of_inflating
/// (through the cascade with `loop_check='raise'`: the backfill command is
/// phase 3's).  Nothing climbs round the loop.
#[test]
fn an_ungrounded_cycle_comes_to_rest_in_the_cascade() {
    let mut f = Fixture::new();
    let nodes = shuffle_cycle(&mut f);
    let (r, rec) = f.cascade(&[nodes[0]]);
    assert!(rec.events.is_empty(), "{:?}", rec.events);
    assert!(r.pops < 200);
    // At rest: a second cascade writes nothing.
    let (again, _) = f.cascade(&nodes);
    assert!(again.changed.is_empty());

    for &n in &nodes {
        assert_eq!(f.core(n).status(), WhiteWin);
        assert!(mate(&f, n).is_none_or(|m| m <= 11));
    }
}
