//! Steps 6 and 7 of the port: the showcase bound (BKD-4) and floors
//! (BKD-3).  v1's portable tests of `test_showcase_bound.py` and
//! `test_backprop_floor.py`, translated fixture for fixture.  Each test
//! names its v1 original.
//!
//! v1's fixtures put every position on the same FEN and tell them apart by
//! key; here they are FEN-less named positions with the side to move set.

use av2_model::record::{F_EXPANDED, NO_CP};
use av2_model::{Closure, Move, PosId, Proof, Status};
use av2_rules::backed::{is_floor, refuses_floor, showcase_from_graph};
use av2_rules::purchase::{MemTasks, Purchase, PurchaseKind};
use av2_rules::testkit::Fixture;

/// v1's `**kw` of `Position.objects.create`.
#[derive(Clone)]
enum K {
    Expanded,
    Eval(i32),
    Nodes(u64),
    Backed(i32),
    BPlies(u16),
    Status(Status),
    Analysis(String),
}

fn pos(f: &mut Fixture, name: &str, stm: char, kw: &[K]) -> PosId {
    let id = f.named(name, stm == 'b');
    for k in kw {
        match k.clone() {
            K::Expanded => f.set(id, |c| c.set_flag(F_EXPANDED, true)),
            K::Eval(v) => f.set(id, |c| c.eval_cp = v as i16),
            K::Nodes(n) => f.set_ext(id, |e| e.nodes_invested = n),
            K::Backed(v) => f.set(id, |c| c.backed_eval = v as i16),
            K::BPlies(p) => f.set(id, |c| c.backed_plies = p),
            K::Status(s) => f.close(id, s, Closure::None, Proof::None),
            K::Analysis(j) => f.set_analysis(id, &j),
        }
    }
    id
}

fn backed(f: &Fixture, id: PosId) -> (Option<i32>, String) {
    let b = f.stored(id);
    (b.value, b.mv.to_uci())
}

fn tasks_of(t: &MemTasks, child: PosId) -> Vec<(u64, bool)> {
    t.tasks.iter().filter(|((c, _), _)| *c == child).map(|(_, v)| *v).collect()
}

// ------------------------------------------------------------ test_showcase_bound.py

/// `_showcase(*evals)`: lines `m0`, `m1`... (not moves: they head nothing,
/// as in v1, where no child is named `m0`).
fn sb_showcase(evals: &[i32]) -> String {
    let v: Vec<serde_json::Value> = evals
        .iter()
        .enumerate()
        .map(|(i, e)| serde_json::json!({"move": format!("m{i}"), "eval_cp": e, "pv": [format!("m{i}")]}))
        .collect();
    serde_json::Value::Array(v).to_string()
}

/// `_edge(parent, child)` without a move: `a1a{1 + edges so far}`.  v1's
/// first default name, `a1a1`, is no move in v2's encoding (`Move::NONE`);
/// these edges go to unopened children, so `a1a2` onwards stand for them.
fn default_edge(f: &mut Fixture, parent: PosId, child: PosId) {
    let n = f.children(parent).len();
    f.edge(parent, &format!("a1a{}", 2 + n), child);
}

/// test_showcase_bound.py::ShowcaseBoundTests::test_bound_completes_coverage_and_lets_value_drop
#[test]
fn bound_completes_coverage_and_lets_value_drop() {
    let mut f = Fixture::new();
    let p =
        pos(&mut f, "SB-P", 'w', &[K::Eval(1051), K::Nodes(2_000_000_000), K::Expanded, K::Analysis(sb_showcase(&[1051, 69, 28, 7, 1]))]);
    let best = pos(&mut f, "SB-C", 'b', &[K::Eval(589), K::Nodes(128_000_000)]);
    f.edge(p, "c1f4", best);
    for i in 0..6 {
        let u = pos(&mut f, &format!("SB-U{i}"), 'b', &[]);
        default_edge(&mut f, p, u);
    }
    f.ascent(&[p]);
    let b = f.stored(p);
    assert_eq!(b.value, Some(589));
    assert_eq!(b.mv.to_uci(), "c1f4");
    assert_eq!(b.quality, 128_000_000);
}

/// test_showcase_bound.py::ShowcaseBoundTests::test_bound_gives_permission_but_never_wins_the_negamax
#[test]
fn bound_gives_permission_but_never_wins_the_negamax() {
    let mut f = Fixture::new();
    let p = pos(
        &mut f,
        "BP-P",
        'b',
        &[K::Eval(1287), K::Nodes(2_000_000_000), K::Expanded, K::Analysis(sb_showcase(&[1287, 1305, 1327, 1319, 1326]))],
    );
    let best = pos(&mut f, "BP-C", 'w', &[K::Backed(1341), K::BPlies(3), K::Nodes(128_000_000)]);
    f.edge(p, "e8c8", best);
    let c2 = pos(&mut f, "BP-C2", 'w', &[K::Backed(1377), K::BPlies(1), K::Nodes(2_000_000_000)]);
    f.edge(p, "a7a6", c2);
    for i in 0..4 {
        let u = pos(&mut f, &format!("BP-U{i}"), 'w', &[]);
        default_edge(&mut f, p, u);
    }
    f.ascent(&[p]);
    let b = f.stored(p);
    assert_eq!(b.value, Some(1341));
    assert_eq!(b.mv.to_uci(), "e8c8");
    assert_eq!(b.plies, 4);
}

/// test_showcase_bound.py::ShowcaseBoundTests::test_single_line_showcase_gives_no_bound
#[test]
fn single_line_showcase_gives_no_bound() {
    let mut f = Fixture::new();
    let p = pos(&mut f, "SL-P", 'w', &[K::Eval(1051), K::Nodes(2_000_000_000), K::Expanded, K::Analysis(sb_showcase(&[1051]))]);
    let c = pos(&mut f, "SL-C", 'b', &[K::Eval(589), K::Nodes(128_000_000)]);
    f.edge(p, "c1f4", c);
    let u = pos(&mut f, "SL-U", 'b', &[]);
    default_edge(&mut f, p, u);
    f.ascent(&[p]);
    assert_eq!(backed(&f, p), (Some(1051), String::new()));
}

/// test_showcase_bound.py::ShowcaseBoundTests::test_bound_alone_never_backs_without_a_real_witness
#[test]
fn bound_alone_never_backs_without_a_real_witness() {
    let mut f = Fixture::new();
    let p = pos(&mut f, "NW-P", 'w', &[K::Eval(400), K::Nodes(512_000_000), K::Expanded, K::Analysis(sb_showcase(&[400, 120]))]);
    for i in 0..3 {
        let u = pos(&mut f, &format!("NW-U{i}"), 'b', &[]);
        default_edge(&mut f, p, u);
    }
    f.ascent(&[p]);
    assert_eq!(f.core(p).backed_eval, NO_CP);
}

/// test_showcase_bound.py::ShowcaseBoundTests::test_quality_guard_propagates_and_still_buys_convergence
#[test]
fn quality_guard_propagates_and_still_buys_convergence() {
    let mut f = Fixture::new();
    let p = pos(&mut f, "QG-P", 'w', &[K::Eval(1000), K::Nodes(2_000_000_000), K::Expanded]);
    let weak = pos(&mut f, "QG-C", 'b', &[K::Eval(1200), K::Nodes(1_000_000)]);
    f.edge(p, "d2d4", weak);
    let u = pos(&mut f, "QG-U", 'b', &[]);
    default_edge(&mut f, p, u);
    let (_, _, tasks) = f.ascent(&[p]);
    assert_eq!(backed(&f, p), (Some(1200), "d2d4".into()));
    // Every purchase of the ascent is arm `quality` (Purchase::ARM).
    assert_eq!(Purchase::ARM, "quality");
    assert_eq!(tasks_of(&tasks, weak).len(), 1);
    assert!(tasks.log.iter().filter(|p| p.child == weak).all(|p| p.kind == PurchaseKind::QualityConvergence));
}

/// test_showcase_bound.py::ShowcaseBoundTests::test_unfavourable_without_bound_still_holds
#[test]
fn unfavourable_without_bound_still_holds() {
    let mut f = Fixture::new();
    let p = pos(&mut f, "DF-P", 'w', &[K::Eval(1000), K::Nodes(512_000_000), K::Expanded]);
    let c = pos(&mut f, "DF-C", 'b', &[K::Eval(300), K::Nodes(512_000_000)]);
    f.edge(p, "d2d4", c);
    let u = pos(&mut f, "DF-U", 'b', &[]);
    default_edge(&mut f, p, u);
    f.ascent(&[p]);
    assert_eq!(f.stored(p).value, Some(1000));
}

/// test_showcase_bound.py::ShowcaseBoundTests::test_proof_authority_does_not_cross_the_bound
#[test]
fn proof_authority_does_not_cross_the_bound() {
    let mut f = Fixture::new();
    let p = pos(&mut f, "PA-P", 'w', &[K::Eval(400), K::Nodes(5_000), K::Expanded, K::Analysis(sb_showcase(&[400, 50]))]);
    let won = pos(&mut f, "PA-C", 'b', &[K::Status(Status::WhiteWin)]);
    f.edge(p, "d2d4", won);
    let u = pos(&mut f, "PA-U", 'b', &[]);
    default_edge(&mut f, p, u);
    f.ascent(&[p]);
    let b = f.stored(p);
    assert_eq!(b.value, Some(10_000));
    assert_eq!(b.quality, 5_000);
}

// ------------------------------------------------------------ test_backprop_floor.py

fn floor_showcase() -> String {
    r#"[{"move": "e7e5", "eval_cp": 743}, {"move": "e7e6", "eval_cp": 800}]"#.to_string()
}

struct Family {
    f: Fixture,
    node: PosId,
    floor: PosId,
    tasks: MemTasks,
}

/// `_family(name, floor_move='c8h3', reply=363, searched=776, showcase=_SHOWCASE)`.
fn family(name: &str, floor_move: &str, reply: i32, searched: i32, showcase: String) -> Family {
    let mut f = Fixture::new();
    let node = pos(&mut f, &format!("{name}-N"), 'b', &[K::Eval(743), K::Nodes(648_000_000), K::Expanded, K::Analysis(showcase)]);
    let floor = pos(&mut f, &format!("{name}-floor"), 'w', &[]);
    f.edge(node, floor_move, floor);
    let r = pos(&mut f, &format!("{name}-reply"), 'b', &[K::Eval(reply), K::Nodes(8_003_625)]);
    f.edge(floor, "c3e4", r);
    if floor_move != "e7e5" {
        let s = pos(&mut f, &format!("{name}-searched"), 'w', &[K::Eval(searched), K::Nodes(136_000_000)]);
        f.edge(node, "e7e5", s);
    }
    let u = pos(&mut f, &format!("{name}-unopened"), 'w', &[]);
    f.edge(node, "a7a6", u);
    let (_, _, tasks) = f.ascent(&[floor]);
    Family { f, node, floor, tasks }
}

fn default_family(name: &str) -> Family {
    family(name, "c8h3", 363, 776, floor_showcase())
}

/// `ingest.refuses_floor(node, floor, move)` on the stored rows.
fn refuses(fam: &Family, mv: &str) -> bool {
    let show = showcase_from_graph(&fam.f.g, fam.node);
    refuses_floor(&fam.f.core(fam.node), &show, &fam.f.core(fam.floor), &fam.f.ext(fam.floor), Move::parse(mv).unwrap())
}

/// test_backprop_floor.py::FloorTests::test_a_floor_outside_the_lines_does_not_beat_them
#[test]
fn a_floor_outside_the_lines_does_not_beat_them() {
    let fam = default_family("OUTSIDE");
    assert_eq!(backed(&fam.f, fam.floor), (Some(363), "c3e4".into()));
    let fl = fam.f.core(fam.floor);
    assert!(is_floor(fl.status(), fam.f.ext(fam.floor).nodes_invested, fl.backed().map(i32::from)));
    assert_eq!(backed(&fam.f, fam.node), (Some(776), "e7e5".into()));
    assert_eq!(fam.f.stored(fam.node).quality, 136_000_000);
}

/// test_backprop_floor.py::FloorTests::test_the_search_that_would_settle_it_is_bought
#[test]
fn the_search_that_would_settle_it_is_bought() {
    let fam = default_family("BOUGHT");
    let t = tasks_of(&fam.tasks, fam.floor);
    assert_eq!(t.len(), 1);
    assert_eq!(t[0].0, 128_000_000);
}

/// test_backprop_floor.py::FloorTests::test_the_bound_is_the_last_line_and_not_the_own_number
#[test]
fn the_bound_is_the_last_line_and_not_the_own_number() {
    let fam = family("BOUND", "c8h3", 760, 776, floor_showcase());
    assert_eq!(backed(&fam.f, fam.node), (Some(776), "e7e5".into()));
}

/// test_backprop_floor.py::FloorTests::test_a_floor_that_respects_the_bound_still_travels
#[test]
fn a_floor_that_respects_the_bound_still_travels() {
    let fam = family("RESPECTS", "c8h3", 900, 1200, floor_showcase());
    assert_eq!(backed(&fam.f, fam.node), (Some(900), "c8h3".into()));
    assert!(!refuses(&fam, "c8h3"));
}

/// test_backprop_floor.py::FloorTests::test_a_move_that_heads_a_line_refines_its_line
#[test]
fn a_move_that_heads_a_line_refines_its_line() {
    let fam = family("LINE", "e7e5", 600, 776, floor_showcase());
    assert_eq!(backed(&fam.f, fam.node), (Some(600), "e7e5".into()));
    assert!(!refuses(&fam, "e7e5"));
}

/// test_backprop_floor.py::FloorTests::test_without_two_lines_there_is_no_certificate_to_hold
#[test]
fn without_two_lines_there_is_no_certificate_to_hold() {
    let fam = family("SINGLE", "c8h3", 363, 776, r#"[{"move": "e7e5", "eval_cp": 743}]"#.to_string());
    assert_eq!(backed(&fam.f, fam.node), (Some(363), "c8h3".into()));
    assert!(!tasks_of(&fam.tasks, fam.floor).is_empty());
}

/// test_backprop_floor.py::FloorTests::test_once_the_move_is_searched_its_value_backs_up
#[test]
fn once_the_move_is_searched_its_value_backs_up() {
    let mut fam = default_family("SEARCHED");
    let floor = fam.floor;
    fam.f.set(floor, |c| {
        c.eval_cp = 500;
        c.backed_eval = NO_CP;
        c.backed_move = Move::NONE;
        c.backed_plies = 0;
    });
    fam.f.set_ext(floor, |e| {
        e.nodes_invested = 128_000_000;
        e.backed_nodes = 0;
    });
    let node = fam.node;
    fam.f.ascent(&[node]);
    assert_eq!(backed(&fam.f, node), (Some(500), "c8h3".into()));
}

/// test_backprop_floor.py::FloorTests::test_only_a_borrowed_number_with_no_search_is_a_floor
#[test]
fn only_a_borrowed_number_with_no_search_is_a_floor() {
    assert!(is_floor(Status::Unknown, 0, Some(363)));
    assert!(!is_floor(Status::Unknown, 8_003_625, Some(363)));
    assert!(!is_floor(Status::Unknown, 0, None));
    assert!(!is_floor(Status::WhiteWin, 0, Some(10_000)));
}

/// test_backprop_floor.py::FloorAndTheBulkButtonTests::test_a_floor_standing_on_a_search_is_bought_by_the_rule
/// (the rule's half: the bulk button, `is_unexplored`,
/// `unexplored_children` and `enqueue_unexplored_children`, is the queue
/// model's, Q-7).
#[test]
fn a_floor_standing_on_a_search_is_bought_by_the_rule() {
    let fam = default_family("ENGINE");
    assert!(refuses(&fam, "c8h3"));
    let t = tasks_of(&fam.tasks, fam.floor);
    assert_eq!(t.len(), 1);
    assert_eq!(t[0].0, 128_000_000);
    // source FILL, arm quality: every purchase of the ascent.
    assert_eq!((Purchase::SOURCE, Purchase::ARM), ("FILL", "quality"));
}

/// test_backprop_floor.py::FloorAndTheBulkButtonTests::test_a_walked_floor_is_refused_and_counted_and_bought_once
/// (the rule's half, as above).
#[test]
fn a_walked_floor_is_refused_and_bought() {
    let mut f = Fixture::new();
    let node = pos(&mut f, "WALKED-N", 'b', &[K::Eval(743), K::Nodes(648_000_000), K::Expanded, K::Analysis(floor_showcase())]);
    let walked = pos(&mut f, "WALKED-walked", 'w', &[]);
    f.edge(node, "c8h3", walked);
    let r = pos(&mut f, "WALKED-reply", 'b', &[K::Eval(363)]);
    f.edge(walked, "c3e4", r);
    let s = pos(&mut f, "WALKED-searched", 'w', &[K::Eval(776), K::Nodes(136_000_000)]);
    f.edge(node, "e7e5", s);
    let (_, _, tasks) = f.ascent(&[walked]);
    let wb = f.stored(walked);
    assert_eq!((wb.value, wb.quality), (Some(363), 0));
    let show = showcase_from_graph(&f.g, node);
    assert!(refuses_floor(&f.core(node), &show, &f.core(walked), &f.ext(walked), Move::parse("c8h3").unwrap()));
    assert_eq!(backed(&f, node), (Some(776), "e7e5".into()));
    let t = tasks_of(&tasks, walked);
    assert_eq!(t.len(), 1);
    assert_eq!(t[0].0, 128_000_000);
}
