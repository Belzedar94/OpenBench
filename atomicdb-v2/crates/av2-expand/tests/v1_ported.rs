//! v1 tests ported to av2-expand (rules POS-2, POS-5, EXP-1), over the
//! in-memory graph (`av2_store::mem::MemGraph`, the store's oracle).  Each
//! test names the v1 test it ports (`docs/test-map.csv`).

use av2_chess as chess;
use av2_expand::{self as ex, NewPosition};
use av2_model::record::{F_EXPANDED, F_REACHABLE, NO_SLACK};
use av2_model::{Closure, Edge, Ext, GraphRead, GraphWrite, Key, Move, PosId, Status};
use av2_store::mem::MemGraph;
use std::collections::BTreeMap;

fn pos(g: &mut MemGraph, fen: &str, campaign: Option<u32>) -> PosId {
    ex::get_or_create_position(g, &NewPosition::from_fen(fen).unwrap(), campaign).unwrap().0
}

fn fen_of(g: &MemGraph, id: PosId) -> String {
    g.fen(id).unwrap()
}

fn expand(g: &mut MemGraph, id: PosId) -> ex::Expansion {
    let prepared = ex::prepare(&fen_of(g, id)).unwrap();
    ex::expand(g, id, &prepared).unwrap()
}

fn campaign(g: &MemGraph, id: PosId) -> Option<u32> {
    g.ext(id).map(|e| e.campaign).filter(|&c| c != 0)
}

fn set_campaign(g: &mut MemGraph, id: PosId, c: u32) {
    let mut e = g.ext(id).unwrap_or_default();
    e.campaign = c;
    g.put_ext(id, e).unwrap();
}

fn set_reachable(g: &mut MemGraph, id: PosId, on: bool) {
    let mut c = g.core(id);
    c.set_flag(F_REACHABLE, on);
    g.put_core(id, c).unwrap();
}

fn reachable(g: &MemGraph, id: PosId) -> bool {
    g.core(id).flag(F_REACHABLE)
}

fn add_edge(g: &mut MemGraph, parent: PosId, uci: &str, child: PosId) {
    let mut e = Vec::new();
    g.children(parent, &mut e);
    e.push(Edge { child, mv: Move::parse(uci).unwrap() });
    g.set_children(parent, &e).unwrap();
}

fn child_by_move(g: &MemGraph, parent: PosId, uci: &str) -> PosId {
    let mut e = Vec::new();
    g.children(parent, &mut e);
    e.iter().find(|e| e.mv == Move::parse(uci).unwrap()).unwrap().child
}

/// `_line(*ucis)` of test_ingest_batched_writes.py: the start position and a
/// line of moves, each with its edge.
fn line(g: &mut MemGraph, ucis: &[&str]) -> PosId {
    let mut parent = pos(g, &chess::start_fen(), None);
    for uci in ucis {
        let child = pos(g, &chess::apply_move(&fen_of(g, parent), uci).unwrap(), None);
        add_edge(g, parent, uci, child);
        parent = child;
    }
    parent
}

/// `quiet_fen` of test_selector_v2.py.
fn quiet_fen(file_index: usize, rank: usize) -> String {
    let (left, right) = (file_index, 7 - file_index);
    let row = format!(
        "{}R{}",
        if left > 0 { left.to_string() } else { String::new() },
        if right > 0 { right.to_string() } else { String::new() }
    );
    let mut rows: Vec<String> = ["7k", "8", "8", "8", "8", "8", "8", "K7"].iter().map(|s| s.to_string()).collect();
    rows[8 - rank] = row;
    format!("{} w - - 0 1", rows.join("/"))
}

/// Everything a position holds, by key (ids differ between two runs that
/// create in different orders).
type Snap = BTreeMap<String, (String, u16, u16, u8, Option<Ext>, Vec<(String, String)>)>;

fn snapshot(g: &MemGraph) -> Snap {
    let key_of = |id: PosId| Key::of_canonical_fen(&g.fen(id).unwrap()).to_hex();
    let mut out = BTreeMap::new();
    for i in 0..g.len() {
        let id = PosId(i);
        let c = g.core(id);
        let mut e = Vec::new();
        g.children(id, &mut e);
        let edges = e.iter().map(|e| (e.mv.to_uci(), key_of(e.child))).collect();
        out.insert(key_of(id), (g.fen(id).unwrap(), c.flags, c.mate_in, c.clock_slack, g.ext(id), edges));
    }
    out
}

/// `_legacy_expand` of test_ingest_batched_writes.py: the per-move loop
/// before batching, call for call (minus the priority, which v2 does not
/// store).
fn legacy_expand(g: &mut MemGraph, id: PosId) -> Vec<PosId> {
    let core = g.core(id);
    if core.expanded() || core.status() != Status::Unknown {
        return vec![];
    }
    let fen = fen_of(g, id);
    let camp = campaign(g, id);
    let mut children = vec![];
    for uci in chess::legal_moves(&fen).unwrap() {
        let child = pos(g, &chess::apply_move(&fen, &uci).unwrap(), camp);
        let mut e = Vec::new();
        g.children(id, &mut e);
        let mv = Move::parse(&uci).unwrap();
        if !e.iter().any(|x| x.mv == mv) {
            e.push(Edge { child, mv });
            g.set_children(id, &e).unwrap();
        }
        if reachable(g, id) && !reachable(g, child) {
            set_reachable(g, child, true);
        }
        children.push(child);
    }
    let mut c = g.core(id);
    c.set_flag(F_EXPANDED, true);
    g.put_core(id, c).unwrap();
    children
}

// ---------------------------------------------------------------- EXP-1

/// tests.py::BackupTests::test_movegen_completeness_on_expand
#[test]
fn movegen_completeness_on_expand() {
    let mut g = MemGraph::new();
    let root = pos(&mut g, &chess::start_fen(), None);
    let out = expand(&mut g, root);
    let mut e = Vec::new();
    g.children(root, &mut e);
    assert_eq!(e.len(), 20);
    assert_eq!(out.new_edges, 20);
    assert!(g.core(root).expanded());
    // Edge order is movegen order (EXP-1.1).
    let moves: Vec<String> = e.iter().map(|e| e.mv.to_uci()).collect();
    assert_eq!(moves, chess::legal_moves(&chess::start_fen()).unwrap());
}

/// test_ingest_batched_writes.py::ExpandTests::test_a_half_known_expansion_writes_what_the_loop_wrote
#[test]
fn a_half_known_expansion_writes_what_the_loop_wrote() {
    const MINE: u32 = 1;
    const THEIRS: u32 = 2;
    let tree = |g: &mut MemGraph| -> PosId {
        let parent = line(g, &["g1f3"]);
        set_campaign(g, parent, MINE);
        set_reachable(g, parent, true);
        let fen = fen_of(g, parent);
        let moves = chess::legal_moves(&fen).unwrap();
        // moves[0]: a tombstone in v1 (priority not stored in v2).
        pos(g, &chess::apply_move(&fen, &moves[0]).unwrap(), None);
        let owned = pos(g, &chess::apply_move(&fen, &moves[1]).unwrap(), None);
        set_campaign(g, owned, THEIRS);
        let marked = pos(g, &chess::apply_move(&fen, &moves[2]).unwrap(), None);
        set_reachable(g, marked, true);
        // An edge a click already wrote, ahead of the expansion.
        let clicked = pos(g, &chess::apply_move(&fen, &moves[3]).unwrap(), None);
        add_edge(g, parent, &moves[3], clicked);
        parent
    };
    let mut old = MemGraph::new();
    let p_old = tree(&mut old);
    let old_children = legacy_expand(&mut old, p_old);
    let mut new = MemGraph::new();
    let p_new = tree(&mut new);
    let out = expand(&mut new, p_new);

    assert_eq!(snapshot(&new), snapshot(&old));
    let view = |g: &MemGraph, ids: &[PosId]| -> Vec<(String, Option<u32>, bool, Status)> {
        ids.iter().map(|&id| (fen_of(g, id), campaign(g, id), reachable(g, id), g.core(id).status())).collect()
    };
    assert_eq!(view(&new, &out.children), view(&old, &old_children));
    // The pre-existing edge stays first; the owned child keeps its owner.
    let mut e = Vec::new();
    new.children(p_new, &mut e);
    let moves = chess::legal_moves(&fen_of(&new, p_new)).unwrap();
    assert_eq!(e[0].mv.to_uci(), moves[3]);
    assert_eq!(e.len(), moves.len());
    assert_eq!(out.new_edges, moves.len() - 1);
    assert_eq!(campaign(&new, out.children[1]), Some(THEIRS));
    assert_eq!(campaign(&new, out.children[4]), Some(MINE));
}

/// test_ingest_batched_writes.py::ExpandTests::test_terminal_children_are_born_closed_as_before
#[test]
fn terminal_children_are_born_closed_as_before() {
    let tree = |g: &mut MemGraph| pos(g, "7k/6p1/8/8/8/8/8/6QK w - - 0 1", None);
    let mut old = MemGraph::new();
    let p = tree(&mut old);
    let old_children = legacy_expand(&mut old, p);
    let mut new = MemGraph::new();
    let p = tree(&mut new);
    let out = expand(&mut new, p);
    assert_eq!(snapshot(&new), snapshot(&old));
    let closures: Vec<Closure> = out.children.iter().map(|&id| new.core(id).closure()).collect();
    assert!(closures.contains(&Closure::Terminal), "{closures:?}");
    let old_closures: Vec<Closure> = old_children.iter().map(|&id| old.core(id).closure()).collect();
    assert_eq!(closures, old_closures);
    for &id in &out.children {
        let c = new.core(id);
        if c.closure() == Closure::Terminal {
            assert_eq!(c.status(), Status::WhiteWin);
            assert_eq!(c.mate(), Some(0));
            assert_eq!(c.slack(), Some(chess::CLOCK_SLACK_MAX));
        }
    }
}

/// test_ingest_batched_writes.py::ExpandTests::test_a_reachable_parent_marks_its_old_children
#[test]
fn a_reachable_parent_marks_its_old_children() {
    let mut g = MemGraph::new();
    let parent = line(&mut g, &["g1f3"]);
    let fen = fen_of(&g, parent);
    let child = pos(&mut g, &chess::apply_move(&fen, &chess::legal_moves(&fen).unwrap()[0]).unwrap(), None);
    assert!(!reachable(&g, child));
    set_reachable(&mut g, parent, true);
    let out = expand(&mut g, parent);
    assert!(out.children.iter().all(|&c| reachable(&g, c)));
    assert!(reachable(&g, child));
}

/// test_link_children_at_creation.py::LinkedAtBirthTests::test_linking_twice_or_over_existing_edges_writes_no_duplicate
/// (the expand half: an expansion over existing edges adds only the missing ones)
#[test]
fn expansion_over_existing_edges_adds_only_the_missing_ones() {
    let mut g = MemGraph::new();
    let root = pos(&mut g, &chess::start_fen(), None);
    let a = pos(&mut g, &chess::apply_move(&chess::start_fen(), "e2e4").unwrap(), None);
    let b = pos(&mut g, &chess::apply_move(&chess::start_fen(), "g1f3").unwrap(), None);
    add_edge(&mut g, root, "g1f3", b);
    add_edge(&mut g, root, "e2e4", a);
    let out = expand(&mut g, root);
    let mut e = Vec::new();
    g.children(root, &mut e);
    assert_eq!(e.len(), 20);
    assert_eq!(out.new_edges, 18);
    assert_eq!(e[0].mv.to_uci(), "g1f3");
    assert_eq!(e[1].mv.to_uci(), "e2e4");
    let mut parents = Vec::new();
    g.parents(a, &mut parents);
    assert_eq!(parents, vec![root]);
}

/// A closed or already expanded position is left alone (EXP-1.1 statement).
#[test]
fn closed_or_expanded_positions_are_left_alone() {
    let mut g = MemGraph::new();
    let mated = pos(&mut g, "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 0 1", None);
    let before = snapshot(&g);
    assert!(!expand(&mut g, mated).expanded);
    assert_eq!(snapshot(&g), before);
    let root = pos(&mut g, &chess::start_fen(), None);
    assert!(expand(&mut g, root).expanded);
    let before = snapshot(&g);
    assert!(!expand(&mut g, root).expanded);
    assert_eq!(snapshot(&g), before);
}

/// The writer refuses a prepared expansion of another position.
#[test]
fn a_prepared_expansion_must_belong_to_the_position() {
    let mut g = MemGraph::new();
    let root = pos(&mut g, &chess::start_fen(), None);
    let other = ex::prepare(&quiet_fen(0, 5)).unwrap();
    assert!(matches!(ex::expand(&mut g, root, &other), Err(ex::ExpandError::WrongPosition { .. })));
    assert_eq!(g.len(), 1);
}

/// Two moves to one child: two edges, one child, one parent entry
/// (link_existing_children's comment; EXP-1.1).
#[test]
fn two_moves_to_one_child_give_two_edges() {
    // Re5xd5 and Bc4xd5: both attackers stand next to d5, so either capture
    // blows up the queen and both of them: one child.
    let fen = "7k/8/8/3qR3/2B5/8/8/K7 w - - 0 1";
    let mut g = MemGraph::new();
    let p = pos(&mut g, fen, None);
    let prepared = ex::prepare(fen).unwrap();
    let mut by_key: BTreeMap<Key, Vec<String>> = BTreeMap::new();
    for c in &prepared.children {
        by_key.entry(c.key).or_default().push(c.uci.clone());
    }
    let shared: Vec<_> = by_key.values().filter(|v| v.len() > 1).collect();
    assert!(!shared.is_empty(), "fixture must have a transposition inside one expansion");
    let out = ex::expand(&mut g, p, &prepared).unwrap();
    let mut e = Vec::new();
    g.children(p, &mut e);
    assert_eq!(e.len(), prepared.children.len());
    assert_eq!(out.children.len(), prepared.children.len());
    let mut kids: Vec<PosId> = out.children.clone();
    kids.sort();
    kids.dedup();
    assert_eq!(kids.len(), by_key.len());
    let twin = child_by_move(&g, p, &shared[0][0]);
    let mut parents = Vec::new();
    g.parents(twin, &mut parents);
    assert_eq!(parents, vec![p]);
}

// ---------------------------------------------------------------- POS-2

/// test_clock_slack.py::ClosureSlackTests::test_a_terminal_closure_is_born_with_the_maximum
#[test]
fn a_terminal_closure_is_born_with_the_maximum() {
    let mut g = MemGraph::new();
    let mated = pos(&mut g, "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 0 1", None);
    let c = g.core(mated);
    assert_eq!(c.status(), Status::BlackWin);
    assert_eq!(c.closure(), Closure::Terminal);
    assert_eq!(c.slack(), Some(chess::CLOCK_SLACK_MAX));
    assert_eq!(c.mate(), Some(0));
}

/// test_clock_slack.py::ClosureSlackTests::test_a_terminal_draw_carries_no_slack
#[test]
fn a_terminal_draw_carries_no_slack() {
    let mut g = MemGraph::new();
    let stalemate = pos(&mut g, "k7/1R6/8/8/8/6K1/8/8 b - - 0 1", None);
    let c = g.core(stalemate);
    assert_eq!(c.status(), Status::Draw);
    assert_eq!(c.clock_slack, NO_SLACK);
    assert!(c.black_to_move());
}

/// test_campaigns.py::InheritanceTests::test_a_child_born_under_a_tagged_parent_inherits_the_tag
#[test]
fn a_child_born_under_a_tagged_parent_inherits_the_tag() {
    let mut g = MemGraph::new();
    let parent = line(&mut g, &["g1f3"]);
    set_campaign(&mut g, parent, 7);
    let out = expand(&mut g, parent);
    assert!(!out.children.is_empty());
    assert!(out.children.iter().all(|&c| campaign(&g, c) == Some(7)));
}

/// test_campaigns.py::InheritanceTests::test_a_child_that_already_existed_keeps_having_no_owner
#[test]
fn a_child_that_already_existed_keeps_having_no_owner() {
    let mut g = MemGraph::new();
    let parent = line(&mut g, &["g1f3"]);
    let stranger_fen = chess::apply_move(&fen_of(&g, parent), "g8f6").unwrap();
    let stranger = pos(&mut g, &stranger_fen, None);
    set_campaign(&mut g, parent, 7);
    expand(&mut g, parent);
    assert_eq!(campaign(&g, stranger), None);
}

/// test_campaigns.py::InheritanceTests::test_an_untagged_parent_does_not_invent_an_owner
#[test]
fn an_untagged_parent_does_not_invent_an_owner() {
    let mut g = MemGraph::new();
    let parent = line(&mut g, &["g1f3"]);
    let out = expand(&mut g, parent);
    assert!(out.children.iter().all(|&c| campaign(&g, c).is_none()));
}

/// get_or_create returns the existing row and never adopts it.
#[test]
fn an_existing_position_keeps_its_owner() {
    let mut g = MemGraph::new();
    let fen = quiet_fen(3, 5);
    let (a, created) = ex::get_or_create_position(&mut g, &NewPosition::from_fen(&fen).unwrap(), None).unwrap();
    assert!(created);
    let (b, created) = ex::get_or_create_position(&mut g, &NewPosition::from_fen(&fen).unwrap(), Some(9)).unwrap();
    assert!(!created);
    assert_eq!(a, b);
    assert_eq!(campaign(&g, a), None);
}

// ---------------------------------------------------------------- POS-5

/// test_selector_v2.py::ReachablePropagationTests::test_the_root_is_reachable_by_construction
#[test]
fn the_root_is_reachable_by_construction() {
    let mut g = MemGraph::new();
    let root = pos(&mut g, &chess::start_fen(), None);
    assert!(reachable(&g, root));
}

/// ...::test_a_pre_existing_root_gets_the_mark_on_the_next_pass
#[test]
fn a_pre_existing_root_gets_the_mark_on_the_next_pass() {
    let mut g = MemGraph::new();
    let root = pos(&mut g, &chess::start_fen(), None);
    set_reachable(&mut g, root, false);
    let again = pos(&mut g, &chess::start_fen(), None);
    assert_eq!(again, root);
    assert!(reachable(&g, root));
}

/// ...::test_expand_hands_the_mark_to_the_children
#[test]
fn expand_hands_the_mark_to_the_children() {
    let mut g = MemGraph::new();
    let root = pos(&mut g, &chess::start_fen(), None);
    let out = expand(&mut g, root);
    assert!(!out.children.is_empty());
    assert!((0..g.len()).all(|i| reachable(&g, PosId(i))));
}

/// ...::test_an_unreachable_parent_hands_out_nothing
#[test]
fn an_unreachable_parent_hands_out_nothing() {
    let mut g = MemGraph::new();
    let loose = pos(&mut g, &quiet_fen(0, 5), None);
    assert!(!reachable(&g, loose));
    let out = expand(&mut g, loose);
    assert!(out.children.iter().all(|&c| !reachable(&g, c)));
}

/// ...::test_a_new_position_is_not_reachable_just_for_existing
#[test]
fn a_new_position_is_not_reachable_just_for_existing() {
    let mut g = MemGraph::new();
    let p = pos(&mut g, &quiet_fen(2, 4), None);
    assert!(!reachable(&g, p));
}

/// test_selector_v2.py::BehindAClosedWallTests (the column half)
#[test]
fn the_walk_stops_at_the_wall_and_the_wall_is_marked() {
    let mut g = MemGraph::new();
    let root = pos(&mut g, &chess::start_fen(), None);
    expand(&mut g, root);
    let wall = child_by_move(&g, root, "h2h4");
    let mut c = g.core(wall);
    c.set_status(Status::WhiteWin, Closure::Minimax, av2_model::Proof::None);
    c.mate_in = 2;
    g.put_core(wall, c).unwrap();
    let hidden = pos(&mut g, &quiet_fen(0, 5), None);
    add_edge(&mut g, wall, "h4h5", hidden);
    ex::backfill_reachable(&mut g).unwrap();
    assert!(!reachable(&g, hidden));
    assert!(reachable(&g, wall));
}

struct BackfillTree {
    g: MemGraph,
    root: PosId,
    deep: PosId,
    deeper: PosId,
    orphan: PosId,
}

/// test_selector_v2.py::BackfillReachableTests::setUp
fn backfill_tree() -> BackfillTree {
    let mut g = MemGraph::new();
    let root = pos(&mut g, &chess::start_fen(), None);
    expand(&mut g, root);
    let deep = pos(&mut g, &quiet_fen(0, 5), None);
    let deeper = pos(&mut g, &quiet_fen(1, 4), None);
    let mut e = Vec::new();
    g.children(root, &mut e);
    add_edge(&mut g, e[0].child, "h2h3", deep);
    add_edge(&mut g, deep, "h3h4", deeper);
    let orphan = pos(&mut g, &quiet_fen(2, 4), None);
    for i in 0..g.len() {
        set_reachable(&mut g, PosId(i), false);
    }
    BackfillTree { g, root, deep, deeper, orphan }
}

fn unreachable_rows(g: &MemGraph) -> usize {
    (0..g.len()).filter(|&i| !reachable(g, PosId(i))).count()
}

/// ...::test_it_marks_the_whole_ball_and_only_the_ball
#[test]
fn backfill_marks_the_whole_ball_and_only_the_ball() {
    let mut t = backfill_tree();
    ex::backfill_reachable(&mut t.g).unwrap();
    assert_eq!(unreachable_rows(&t.g), 1);
    for n in [t.root, t.deep, t.deeper] {
        assert!(reachable(&t.g, n));
    }
    assert!(!reachable(&t.g, t.orphan));
}

/// ...::test_it_walks_past_the_first_ply
#[test]
fn backfill_walks_past_the_first_ply() {
    let mut t = backfill_tree();
    let (_cleared, marked) = ex::backfill_reachable(&mut t.g).unwrap();
    assert_eq!(marked, t.g.len() - 1);
    assert!(reachable(&t.g, t.deeper));
}

/// ...::test_it_does_not_stall_on_a_partly_marked_base
#[test]
fn backfill_does_not_stall_on_a_partly_marked_base() {
    let mut t = backfill_tree();
    let mut e = Vec::new();
    t.g.children(t.root, &mut e);
    for x in &e {
        set_reachable(&mut t.g, x.child, true);
    }
    set_reachable(&mut t.g, t.root, true);
    ex::backfill_reachable(&mut t.g).unwrap();
    assert!(reachable(&t.g, t.deeper));
}

/// ...::test_a_second_run_reaches_the_same_set
#[test]
fn backfill_second_run_reaches_the_same_set() {
    let mut t = backfill_tree();
    let (_, first) = ex::backfill_reachable(&mut t.g).unwrap();
    let (cleared, again) = ex::backfill_reachable(&mut t.g).unwrap();
    assert_eq!(again, first);
    assert_eq!(cleared, first);
    assert_eq!(unreachable_rows(&t.g), 1);
}

/// A store without the start position: nothing to seed (backfill_reachable's
/// "root": None report).
#[test]
fn backfill_without_a_root_does_nothing() {
    let mut g = MemGraph::new();
    let p = pos(&mut g, &quiet_fen(2, 4), None);
    set_reachable(&mut g, p, true);
    assert_eq!(ex::backfill_reachable(&mut g).unwrap(), (0, 0));
    assert!(reachable(&g, p));
}
