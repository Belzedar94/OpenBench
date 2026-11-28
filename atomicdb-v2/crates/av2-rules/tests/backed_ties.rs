//! Steps 6 and 7 of the port: ties (BKD-6), the claim veto (BKD-5.2), the
//! echo rule (BKD-1.3).  v1's portable tests of `test_backup_ties.py`,
//! `test_backup_echo.py` and `test_backprop_claims.py`, translated fixture
//! for fixture.  Each test names its v1 original.
//!
//! v1's fixtures put every position on the same FEN (`4k3/8/.../4K3 {stm}`)
//! and tell them apart by key; here they are FEN-less named positions with
//! the side to move set (the backed rules read nothing else of the FEN).
//! `_ChildValue`s with move names that are not UCI (`'loss'`, `'open'`...)
//! get UCI moves in the same alphabetical order.

use av2_model::record::{F_BLACK_TO_MOVE, F_EXPANDED, NO_CP};
use av2_model::{Closure, Core, Ext, Move, PosId, Proof, Status};
use av2_rules::backed::{Backed, ChildValue, Discrepancy, Row, Showcase, backed_for, best_child, child_contribution};
use av2_rules::config::BACKED_ECHO_PLIES;
use av2_rules::conv::{PROVEN_QUALITY, TB_CLAMP_CEILING};
use av2_rules::testkit::Fixture;

const ENGINE: u64 = 128_000_000;
const DEEP: u64 = 512_000_000;
const PROVEN: u64 = PROVEN_QUALITY;
const ROW: PosId = PosId(777);

// ------------------------------------------------------------ fixtures

/// v1's `**kw` of `Position.objects.create`.
#[derive(Clone)]
enum K {
    Expanded,
    Eval(i32),
    Best(&'static str),
    Nodes(u64),
    Backed(i32),
    BMove(&'static str),
    BPlies(u16),
    BNodes(u64),
    Decided(Status, Closure, Proof, Option<u16>),
    Analysis(String),
}

fn pos(f: &mut Fixture, name: &str, stm: char, kw: &[K]) -> PosId {
    let id = f.named(name, stm == 'b');
    let mut ext_set = false;
    for k in kw {
        match k.clone() {
            K::Expanded => f.set(id, |c| c.set_flag(F_EXPANDED, true)),
            K::Eval(v) => f.set(id, |c| c.eval_cp = v as i16),
            K::Best(m) => f.set(id, |c| c.best_move = Move::parse_opt(m).unwrap()),
            K::Nodes(n) => {
                ext_set = true;
                f.set_ext(id, |e| e.nodes_invested = n)
            }
            K::Backed(v) => f.set(id, |c| c.backed_eval = v as i16),
            K::BMove(m) => f.set(id, |c| c.backed_move = Move::parse_opt(m).unwrap()),
            K::BPlies(p) => f.set(id, |c| c.backed_plies = p),
            K::BNodes(n) => {
                ext_set = true;
                f.set_ext(id, |e| e.backed_nodes = n)
            }
            K::Decided(s, c, p, m) => f.set(id, |core| {
                core.set_status(s, c, p);
                if let Some(m) = m {
                    core.mate_in = m;
                }
            }),
            K::Analysis(j) => f.set_analysis(id, &j),
        }
    }
    let _ = ext_set;
    id
}

/// `_decided(name, stm, mate_in, status='WHITE_WIN')`.
fn decided(f: &mut Fixture, name: &str, stm: char, mate_in: u16) -> PosId {
    pos(f, name, stm, &[K::Decided(Status::WhiteWin, Closure::Minimax, Proof::Engine, Some(mate_in)), K::Nodes(ENGINE)])
}

/// `_showcase(*lines)`: `(move, eval_cp)` in the engine's order.
fn showcase(lines: &[(&str, i32)]) -> String {
    let v: Vec<serde_json::Value> = lines.iter().map(|(m, e)| serde_json::json!({"move": m, "eval_cp": e, "pv": [m]})).collect();
    serde_json::Value::Array(v).to_string()
}

/// `_backed(row)`: the four stored columns, the move as v1's text.
fn backed(f: &Fixture, id: PosId) -> (Option<i32>, String, i64, u64) {
    let b = f.stored(id);
    (b.value, b.mv.to_uci(), b.plies, b.quality)
}

/// A move for a v1 test name (UCI as is; the words in alphabetical order).
fn m(name: &str) -> Move {
    if let Ok(mv) = Move::parse(name) {
        return mv;
    }
    let words = ["deep", "draw", "heavy", "late", "light", "loss", "none", "open", "own", "short", "soon", "win"];
    let i = words.iter().position(|w| *w == name).unwrap_or_else(|| panic!("no move for {name}"));
    // a2a3 < a2a4 < b2b3 < b2b4 < ... : alphabetical like the words.
    let file = (i / 2) as u8;
    Move::new(8 + file, if i % 2 == 0 { 16 + file } else { 24 + file }, 0)
}

fn child(mv: &str, value: Option<i32>, quality: u64, plies: i64) -> ChildValue {
    let mut c = ChildValue::uninformed(m(mv), PosId(0));
    c.value = value;
    c.quality = quality;
    c.plies = plies;
    c
}

fn proven(mv: &str, value: i32, mate_in: Option<i64>) -> ChildValue {
    let mut c = child(mv, Some(value), PROVEN, 0);
    c.exact = true;
    c.mate_in = mate_in;
    c
}

fn open(mv: &str, value: i32) -> ChildValue {
    child(mv, Some(value), ENGINE, 1)
}

fn open_q(mv: &str, value: i32, quality: u64, plies: i64) -> ChildValue {
    child(mv, Some(value), quality, plies)
}

fn claim(mv: &str, value: i32) -> ChildValue {
    child(mv, Some(value), 0, 0)
}

fn repetition(mv: &str) -> ChildValue {
    let mut c = child(mv, Some(1_000), DEEP, 0);
    c.cycles = true;
    c
}

fn unopened(mv: &str) -> ChildValue {
    child(mv, None, 0, 0)
}

/// `ingest._best_child(children, stm_white, own_move)`: the chosen move.
fn best(children: &[ChildValue], stm_white: bool, own: Option<&str>) -> Move {
    let idx: Vec<usize> = (0..children.len()).collect();
    children[best_child(children, &idx, stm_white, own.map(m).unwrap_or(Move::NONE))].mv
}

/// `_row(stm, own, nodes=DEEP, best='own')`.
fn row(stm: char, own: i32, best: Option<&str>) -> Row {
    let mut core = Core::default();
    if stm == 'b' {
        core.flags |= F_BLACK_TO_MOVE;
    }
    core.set_flag(F_EXPANDED, true);
    core.eval_cp = own as i16;
    // 'own' names no child: the same as no move for every comparison.
    core.best_move = best.map(m).unwrap_or(Move::NONE);
    Row { id: ROW, core, nodes_invested: DEEP }
}

fn show(bound: Option<i32>) -> Showcase {
    Showcase { bound, lines: Vec::new() }
}

type Out = (Option<i32>, Move, i64, u64);

fn tuple(b: Backed) -> Out {
    (b.value, b.mv, b.plies, b.quality)
}

/// `ingest._backed_for(row, children, discrepancies, bound=, cycle_picks=)`.
fn bf(
    r: &Row,
    mut children: Vec<ChildValue>,
    bound: Option<i32>,
    asked: Option<&mut Vec<Discrepancy>>,
    picks: Option<&mut Vec<Discrepancy>>,
) -> Out {
    tuple(backed_for(r, &mut children, asked, &show(bound), picks))
}

fn out(v: i32, mv: Option<&str>, plies: i64, q: u64) -> Out {
    (Some(v), mv.map(m).unwrap_or(Move::NONE), plies, q)
}

// ------------------------------------------------------------ TieOrderTests

/// test_backup_ties.py::TieOrderTests::test_an_open_move_beats_a_proven_loss_whatever_its_weight
#[test]
fn an_open_move_beats_a_proven_loss_whatever_its_weight() {
    for (stm, lost) in [(true, -10_000), (false, 10_000)] {
        let loss = proven("loss", lost, Some(1));
        for q in [0, ENGINE, PROVEN] {
            let still = open_q("open", lost, q, 1);
            assert_eq!(best(&[loss, still], stm, None), m("open"));
            assert_eq!(best(&[still, loss], stm, None), m("open"));
        }
    }
}

/// test_backup_ties.py::TieOrderTests::test_among_proven_losses_the_latest_mate
#[test]
fn among_proven_losses_the_latest_mate() {
    for (stm, lost) in [(true, -10_000), (false, 10_000)] {
        let (soon, late) = (proven("soon", lost, Some(1)), proven("late", lost, Some(11)));
        let uncounted = proven("none", lost, None);
        assert_eq!(best(&[soon, uncounted, late], stm, None), m("late"));
        assert_eq!(best(&[uncounted, soon], stm, None), m("soon"));
    }
}

/// test_backup_ties.py::TieOrderTests::test_among_proven_wins_the_soonest_mate
#[test]
fn among_proven_wins_the_soonest_mate() {
    for (stm, won) in [(true, 10_000), (false, -10_000)] {
        let (soon, late) = (proven("soon", won, Some(3)), proven("late", won, Some(7)));
        let uncounted = proven("none", won, None);
        assert_eq!(best(&[late, uncounted, soon], stm, None), m("soon"));
        assert_eq!(best(&[uncounted, late], stm, None), m("late"));
    }
}

/// test_backup_ties.py::TieOrderTests::test_a_proven_win_still_beats_an_open_move_worth_the_same
#[test]
fn a_proven_win_still_beats_an_open_move_worth_the_same() {
    for (stm, won) in [(true, 10_000), (false, -10_000)] {
        let win = proven("win", won, Some(21));
        let still = open_q("open", won, PROVEN, 1);
        assert_eq!(best(&[still, win], stm, None), m("win"));
    }
}

/// test_backup_ties.py::TieOrderTests::test_a_proven_draw_still_beats_an_open_zero
#[test]
fn a_proven_draw_still_beats_an_open_zero() {
    for stm in [true, false] {
        let draw = proven("draw", 0, None);
        let still = open_q("open", 0, DEEP, 0);
        assert_eq!(best(&[still, draw], stm, None), m("draw"));
    }
}

/// test_backup_ties.py::TieOrderTests::test_grounding_still_comes_first
#[test]
fn grounding_still_comes_first() {
    for flag in ["returns", "circular"] {
        let loss = proven("loss", 10_000, Some(1));
        let mut still = open("open", 10_000);
        if flag == "returns" {
            still.returns = true;
        } else {
            still.circular = true;
        }
        assert_eq!(best(&[still, loss], false, None), m("loss"));
    }
}

/// test_backup_ties.py::TieOrderTests::test_a_better_value_still_wins
#[test]
fn a_better_value_still_wins() {
    let loss = proven("loss", -10_000, Some(40));
    assert_eq!(best(&[loss, open("open", -900)], true, None), m("open"));
}

/// test_backup_ties.py::TieOrderTests::test_full_ties_go_to_the_first_move_alphabetically
#[test]
fn full_ties_go_to_the_first_move_alphabetically() {
    for stm in [true, false] {
        let moves = ["f5f4", "a7a5", "e8f8"];
        let rev: Vec<&str> = moves.iter().rev().copied().collect();
        for order in [moves.to_vec(), rev] {
            let children: Vec<ChildValue> = order.iter().map(|mv| proven(mv, 10_000, Some(3))).collect();
            assert_eq!(best(&children, stm, None), m("a7a5"));
            let claims: Vec<ChildValue> = order.iter().map(|mv| open_q(mv, 120, 0, 0)).collect();
            assert_eq!(best(&claims, stm, None), m("a7a5"));
        }
    }
}

/// test_backup_ties.py::TieOrderTests::test_a_full_tie_goes_to_the_own_search_move_first
#[test]
fn a_full_tie_goes_to_the_own_search_move_first() {
    for stm in [true, false] {
        let moves = ["f5f4", "a7a5", "e8f8"];
        let rev: Vec<&str> = moves.iter().rev().copied().collect();
        for order in [moves.to_vec(), rev] {
            let children: Vec<ChildValue> = order.iter().map(|mv| proven(mv, 10_000, Some(3))).collect();
            assert_eq!(best(&children, stm, Some("f5f4")), m("f5f4"));
            let claims: Vec<ChildValue> = order.iter().map(|mv| open_q(mv, 120, 0, 0)).collect();
            assert_eq!(best(&claims, stm, Some("e8f8")), m("e8f8"));
            assert_eq!(best(&claims, stm, Some("h7h6")), m("a7a5"));
        }
    }
}

/// test_backup_ties.py::TieOrderTests::test_the_own_move_only_breaks_full_ties
#[test]
fn the_own_move_only_breaks_full_ties() {
    for (stm, lost) in [(true, -10_000), (false, 10_000)] {
        let (loss, still) = (proven("loss", lost, Some(1)), open("open", lost));
        assert_eq!(best(&[loss, still], stm, Some("loss")), m("open"));
        let (heavy, light) = (open_q("heavy", 50, DEEP, 1), open_q("light", 50, ENGINE, 1));
        assert_eq!(best(&[light, heavy], stm, Some("light")), m("heavy"));
        let (short, deep) = (open_q("short", 50, ENGINE, 1), open_q("deep", 50, ENGINE, 4));
        assert_eq!(best(&[deep, short], stm, Some("deep")), m("short"));
    }
}

/// test_backup_ties.py::TieOrderTests::test_the_record_of_the_defect
/// (the `_old_best_child` half records v1's defect B, not a rule: left out).
#[test]
fn the_record_of_the_defect() {
    let children = [proven("f5f4", 10_000, Some(1)), proven("e8f8", 10_000, Some(11)), open("b8c6", 10_000)];
    assert_eq!(best(&children, false, None), m("b8c6"));
    assert_eq!(best(&children[..2], false, None), m("e8f8"));
}

// ------------------------------------------------------------ LongestDefenceTests

/// test_backup_ties.py::LongestDefenceTests::test_a_proven_longer_mate_beats_an_engine_shorter_one
#[test]
fn a_proven_longer_mate_beats_an_engine_shorter_one() {
    for (stm, sign) in [(true, -1), (false, 1)] {
        let engine = open("b4d6", sign * 9_995);
        let pr = proven("f8e8", sign * 10_000, Some(25));
        for order in [[engine, pr], [pr, engine]] {
            assert_eq!(best(&order, stm, None), m("f8e8"));
        }
    }
}

/// test_backup_ties.py::LongestDefenceTests::test_an_engine_longer_mate_beats_a_proven_shorter_one
#[test]
fn an_engine_longer_mate_beats_a_proven_shorter_one() {
    for (stm, sign) in [(true, -1), (false, 1)] {
        let engine = open("b4d6", sign * 9_980);
        let pr = proven("f8e8", sign * 10_000, Some(25));
        assert_eq!(best(&[pr, engine], stm, None), m("b4d6"));
    }
}

/// test_backup_ties.py::LongestDefenceTests::test_equal_lengths_still_prefer_the_open_move
#[test]
fn equal_lengths_still_prefer_the_open_move() {
    for (stm, sign) in [(true, -1), (false, 1)] {
        let engine = open("b4d6", sign * 9_987);
        for plies in [25, 26] {
            let pr = proven("f8e8", sign * 10_000, Some(plies));
            assert_eq!(best(&[pr, engine], stm, None), m("b4d6"));
        }
    }
}

/// test_backup_ties.py::LongestDefenceTests::test_a_move_that_is_not_mated_still_decides_by_value
#[test]
fn a_move_that_is_not_mated_still_decides_by_value() {
    for (stm, sign) in [(true, -1), (false, 1)] {
        let tb = open("a7a6", sign * TB_CLAMP_CEILING);
        let pr = proven("f8e8", sign * 10_000, Some(99));
        assert_eq!(best(&[pr, tb], stm, None), m("a7a6"));
    }
}

/// test_backup_ties.py::LongestDefenceTests::test_the_backed_value_stands_on_the_longest_defence
#[test]
fn the_backed_value_stands_on_the_longest_defence() {
    let mut f = Fixture::new();
    let node = pos(&mut f, "465A-N", 'b', &[K::Expanded, K::Eval(9_995), K::Best("b4d6"), K::Nodes(ENGINE)]);
    let b4d6 = pos(&mut f, "465A-b4d6", 'w', &[K::Eval(9_995), K::Nodes(ENGINE)]);
    f.edge(node, "b4d6", b4d6);
    let f8e8 = decided(&mut f, "465A-f8e8", 'w', 25);
    f.edge(node, "f8e8", f8e8);
    f.ascent(&[node]);
    let (value, mv, _, _) = backed(&f, node);
    assert_eq!(mv, "f8e8");
    assert_eq!(value, Some(10_000));
}

// ------------------------------------------------------------ LostPositionTests

/// test_backup_ties.py::LostPositionTests::test_a_won_position_stands_on_the_soonest_mate
#[test]
fn a_won_position_stands_on_the_soonest_mate() {
    let mut f = Fixture::new();
    let node = pos(&mut f, "WON-N", 'w', &[K::Expanded, K::Eval(900), K::Nodes(640_000_000)]);
    let long = decided(&mut f, "WON-long", 'b', 7);
    f.edge(node, "d1d8", long);
    let short = decided(&mut f, "WON-short", 'b', 3);
    f.edge(node, "h1h8", short);
    let op = pos(
        &mut f,
        "WON-open",
        'b',
        &[K::Expanded, K::Eval(2400), K::Nodes(ENGINE), K::Backed(10_000), K::BMove("a7a6"), K::BPlies(4), K::BNodes(ENGINE)],
    );
    f.edge(node, "c1c8", op);
    f.ascent(&[node]);
    assert_eq!(backed(&f, node), (Some(10_000), "h1h8".into(), 1, PROVEN));
}

// ------------------------------------------------------------ VetoedClaimTests (ties file)

fn unopened_children(f: &mut Fixture, node: PosId, prefix: &str, moves: &[&str]) {
    for mv in moves {
        let c = pos(f, &format!("{prefix}-{mv}"), 'w', &[]);
        f.edge(node, mv, c);
    }
}

/// test_backup_ties.py::VetoedClaimTests::test_e214fcc0_is_no_longer_frozen_at_its_own_search
#[test]
fn e214fcc0_is_no_longer_frozen_at_its_own_search() {
    let mut f = Fixture::new();
    let an = showcase(&[("g6g5", 1304), ("d5d4", 1353), ("b4e7", 1413), ("b4a5", 1435), ("b8d7", 1442)]);
    let node = pos(&mut f, "E214-N", 'b', &[K::Expanded, K::Eval(1304), K::Best("g6g5"), K::Nodes(128_001_325), K::Analysis(an)]);
    for (mv, v) in [("b8d7", 1129), ("b4e7", 1413), ("b4a5", 1435)] {
        let c = pos(&mut f, &format!("E214-{mv}"), 'w', &[K::Eval(v)]);
        f.edge(node, mv, c);
    }
    let g6g5 = pos(
        &mut f,
        "E214-g6g5",
        'w',
        &[K::Eval(1315), K::Nodes(128_006_971), K::Backed(1492), K::BMove("f1g1"), K::BPlies(9), K::BNodes(128_009_182)],
    );
    f.edge(node, "g6g5", g6g5);
    let d5d4 = pos(&mut f, "E214-d5d4", 'w', &[K::Eval(1536), K::Nodes(128_002_263), K::Backed(1536), K::BMove("h5g6"), K::BPlies(1)]);
    f.edge(node, "d5d4", d5d4);
    unopened_children(&mut f, node, "E214", &["a7a5", "a7a6", "h7h6"]);
    f.ascent(&[node]);
    assert_eq!(backed(&f, node), (Some(1413), "b4e7".into(), 1, 0));
}

/// test_backup_ties.py::VetoedClaimTests::test_c8b0b2e2_falls_back_to_its_second_line
#[test]
fn c8b0b2e2_falls_back_to_its_second_line() {
    let mut f = Fixture::new();
    let an = showcase(&[("c8b7", 1049), ("f8d6", 1125), ("b6b5", 1129), ("c8d7", 1148), ("c8d8", 1006)]);
    let node = pos(&mut f, "C8B0-N", 'b', &[K::Expanded, K::Eval(1006), K::Best("c8d8"), K::Nodes(128_002_803), K::Analysis(an)]);
    let b6b5 = pos(&mut f, "C8B0-b6b5", 'w', &[K::Eval(1129)]);
    f.edge(node, "b6b5", b6b5);
    let c8b7 = pos(&mut f, "C8B0-c8b7", 'w', &[K::Eval(1250), K::Nodes(640_004_511), K::Backed(1250), K::BMove("f2g1"), K::BPlies(1)]);
    f.edge(node, "c8b7", c8b7);
    let c8d7 = pos(&mut f, "C8B0-c8d7", 'w', &[K::Eval(911)]);
    f.edge(node, "c8d7", c8d7);
    let c8d8 = pos(
        &mut f,
        "C8B0-c8d8",
        'w',
        &[K::Eval(1134), K::Nodes(128_004_009), K::Backed(1260), K::BMove("h1h3"), K::BPlies(2), K::BNodes(512_007_237)],
    );
    f.edge(node, "c8d8", c8d8);
    let f8d6 = pos(&mut f, "C8B0-f8d6", 'w', &[K::Eval(1125)]);
    f.edge(node, "f8d6", f8d6);
    unopened_children(&mut f, node, "C8B0", &["a7a6", "h6h7"]);
    f.ascent(&[node]);
    assert_eq!(backed(&f, node), (Some(1125), "f8d6".into(), 1, 0));
}

// ------------------------------------------------------------ OwnMoveTests

/// test_backup_ties.py::OwnMoveTests::test_f8481e63_is_not_turned_into_a_loss
#[test]
fn f8481e63_is_not_turned_into_a_loss() {
    let mut f = Fixture::new();
    let an = showcase(&[("d7d6", -118), ("c6c5", 9995), ("a7a6", 9999), ("b7b6", 9999), ("e7e6", 9999)]);
    let node = pos(&mut f, "F848-N", 'b', &[K::Expanded, K::Eval(-118), K::Best("d7d6"), K::Nodes(8_005_586), K::Analysis(an)]);
    let d7d6 = pos(&mut f, "F848-d7d6", 'w', &[K::Eval(-129)]);
    f.edge(node, "d7d6", d7d6);
    for (mv, mate) in [("c6c5", 9), ("a7a6", 1), ("b7b6", 1), ("e7e6", 1)] {
        let c = decided(&mut f, &format!("F848-{mv}"), 'w', mate);
        f.edge(node, mv, c);
    }
    unopened_children(&mut f, node, "F848", &["g7g6", "h6g4", "d8a5"]);
    f.ascent(&[node]);
    assert_eq!(backed(&f, node), (Some(-118), String::new(), 0, 8_005_586));
}

/// test_backup_ties.py::OwnMoveTests::test_2bd63fd6_keeps_its_best_line
#[test]
fn _2bd63fd6_keeps_its_best_line() {
    let mut f = Fixture::new();
    let an = showcase(&[("h5h6", 1137), ("h2f2", 873), ("e4e5", 866), ("d5d6", 831), ("f6f7", 831)]);
    let node = pos(&mut f, "2BD6-N", 'w', &[K::Expanded, K::Eval(1137), K::Best("h5h6"), K::Nodes(128_000_829), K::Analysis(an)]);
    for (mv, v) in [("h5h6", 1285), ("h2f2", 873), ("e4e5", 866), ("d5d6", 831), ("f6f7", 831)] {
        let c = pos(&mut f, &format!("2BD6-{mv}"), 'b', &[K::Eval(v)]);
        f.edge(node, mv, c);
    }
    for mv in ["a2a3", "c1c2"] {
        let c = pos(&mut f, &format!("2BD6-{mv}"), 'b', &[]);
        f.edge(node, mv, c);
    }
    f.ascent(&[node]);
    assert_eq!(backed(&f, node), (Some(1137), String::new(), 0, 128_000_829));
}

/// test_backup_ties.py::OwnMoveTests::test_a_searched_move_that_beats_it_still_wins
#[test]
fn a_searched_move_that_beats_it_still_wins() {
    let o = bf(
        &row('b', 1282, Some("f8g7")),
        vec![claim("f8g7", 1216), open("a6c5", 1250), open("d8c8", 1576), unopened("a6b4")],
        Some(1306),
        None,
        None,
    );
    assert_eq!(o, out(1250, Some("a6c5"), 2, ENGINE));
}

/// test_backup_ties.py::OwnMoveTests::test_the_own_move_searched_is_no_claim
#[test]
fn the_own_move_searched_is_no_claim() {
    let o = bf(
        &row('b', 1282, Some("f8g7")),
        vec![claim("a8b8", 1216), open("f8g7", 1295), open("a6c5", 1285), unopened("a6b4")],
        Some(1306),
        None,
        None,
    );
    assert_eq!(o, out(1285, Some("a6c5"), 2, ENGINE));
}

// ------------------------------------------------------------ VetoGuardTests

/// test_backup_ties.py::VetoGuardTests::test_without_a_bound_the_own_number_still_stands
#[test]
fn without_a_bound_the_own_number_still_stands() {
    let (mut picks, mut asked) = (Vec::new(), Vec::new());
    let o = bf(
        &row('b', 1282, None),
        vec![claim("a8b8", 1216), open("f8g7", 1295), unopened("a6b4")],
        None,
        Some(&mut asked),
        Some(&mut picks),
    );
    assert_eq!(o, out(1282, None, 0, DEEP));
    assert_eq!(asked, vec![(m("a8b8"), ROW, DEEP)]);
}

/// test_backup_ties.py::VetoGuardTests::test_a_searched_move_that_beats_the_own_number_still_wins
#[test]
fn a_searched_move_that_beats_the_own_number_still_wins() {
    let o = bf(
        &row('b', 1282, None),
        vec![claim("a8b8", 1216), open("a6c5", 1250), open("f8g7", 1295), unopened("a6b4")],
        Some(1306),
        None,
        None,
    );
    assert_eq!(o, out(1250, Some("a6c5"), 2, ENGINE));
}

/// test_backup_ties.py::VetoGuardTests::test_with_nothing_but_claims_the_own_number_stands
#[test]
fn with_nothing_but_claims_the_own_number_stands() {
    let o = bf(&row('b', 1282, None), vec![claim("a8b8", 1216), claim("h8g8", 1100), unopened("a6b4")], Some(1306), None, None);
    assert_eq!(o, out(1282, None, 0, DEEP));
}

/// test_backup_ties.py::VetoGuardTests::test_a_repetition_that_beats_the_own_number_keeps_it
/// (the repetition is set by hand, as in v1's test).
#[test]
fn a_repetition_that_beats_the_own_number_keeps_it() {
    for mut children in [vec![repetition("d6f6"), open("d6h6", -80)], vec![claim("c2c4", 300), repetition("d6f6"), open("d6h6", -80)]] {
        children.push(unopened("a2a3"));
        let o = bf(&row('w', -50, None), children, Some(-200), None, None);
        assert_eq!(o, out(-50, None, 0, DEEP));
    }
}

/// test_backup_ties.py::VetoGuardTests::test_a_claim_hiding_a_repetition_hands_over_its_question
#[test]
fn a_claim_hiding_a_repetition_hands_over_its_question() {
    let rest = || vec![repetition("d6f6"), open("d6h6", -80), unopened("a2a3")];
    let (mut picks, mut alone) = (Vec::new(), Vec::new());
    let mut with = vec![claim("c2c4", 300)];
    with.extend(rest());
    let o = bf(&row('w', 50, None), with, Some(-200), None, Some(&mut picks));
    let without = bf(&row('w', 50, None), rest(), Some(-200), None, Some(&mut alone));
    assert_eq!(o, without);
    assert_eq!(o, out(0, Some("d6f6"), 1, 0));
    assert_eq!(picks, alone);
    assert_eq!(picks, vec![(m("d6f6"), ROW, DEEP)]);
}

/// test_backup_ties.py::VetoGuardTests::test_set_aside_is_the_same_as_absent
#[test]
fn set_aside_is_the_same_as_absent() {
    let table = |first: ChildValue, s: (i32, i32)| vec![first, open("a6c5", s.0), open("f8g7", s.1), unopened("a6b4")];
    for bound in [None, Some(1306)] {
        for s in [(1250, 1295), (1285, 1295), (1300, 1400)] {
            let a = bf(&row('b', 1282, None), table(claim("a8b8", 1216), s), bound, None, None);
            let b = bf(&row('b', 1282, None), table(unopened("a8b8"), s), bound, None, None);
            assert_eq!(a, b);
        }
    }
}

// ------------------------------------------------------------ test_backup_echo.py

/// `_contribution(backed_plies, eval_cp=1120, nodes=ENGINE)`.
fn contribution(backed_plies: u16, eval_cp: Option<i32>, nodes: u64) -> ChildValue {
    let core = Core {
        eval_cp: eval_cp.map(|v| v as i16).unwrap_or(NO_CP),
        backed_eval: 1208,
        backed_plies,
        backed_move: m("f8d8"),
        ..Core::default()
    };
    let ext = Ext { backed_nodes: 2_000_000_000, nodes_invested: nodes, ..Ext::default() };
    child_contribution(m("h6f6"), PosId(1), &core, &ext)
}

/// test_backup_echo.py::ContributionTests::test_an_echo_is_read_as_the_childs_own_measure
#[test]
fn an_echo_is_read_as_the_childs_own_measure() {
    let c = contribution(BACKED_ECHO_PLIES + 1, Some(1120), ENGINE);
    assert_eq!((c.value, c.quality, c.plies), (Some(1120), ENGINE, 0));
    assert_eq!(c.spine, Move::NONE);
}

/// test_backup_echo.py::ContributionTests::test_an_echo_with_no_measure_of_its_own_is_no_value
#[test]
fn an_echo_with_no_measure_of_its_own_is_no_value() {
    let c = contribution(3365, None, 0);
    assert_eq!(c.value, None);
}

/// test_backup_echo.py::ContributionTests::test_a_chain_up_to_the_bound_is_read_as_before
#[test]
fn a_chain_up_to_the_bound_is_read_as_before() {
    for plies in [0, 47, BACKED_ECHO_PLIES] {
        let c = contribution(plies, Some(1120), ENGINE);
        assert_eq!((c.value, c.plies, c.spine), (Some(1208), plies as i64, m("f8d8")));
    }
}

/// test_backup_echo.py::ContributionTests::test_a_proven_child_is_never_an_echo
#[test]
fn a_proven_child_is_never_an_echo() {
    let mut core = Core::default();
    core.set_status(Status::WhiteWin, Closure::None, Proof::None);
    core.eval_cp = 9999;
    core.backed_eval = 10_000;
    core.backed_plies = 5000;
    core.backed_move = m("f8d8");
    core.mate_in = 7;
    let ext = Ext { backed_nodes: 0, nodes_invested: ENGINE, ..Ext::default() };
    let c = child_contribution(m("h6f6"), PosId(1), &core, &ext);
    assert_eq!((c.value, c.exact, c.mate_in), (Some(10_000), true, Some(7)));
}

// ------------------------------------------------------------ test_backprop_claims.py

/// `VetoedClaimTests._node(name, searched=1131)`.
fn claims_node(f: &mut Fixture, name: &str, searched: i32) -> (PosId, PosId) {
    let node = pos(f, &format!("{name}-N"), 'b', &[K::Eval(1207), K::Nodes(640_000_000)]);
    let cl = pos(f, &format!("{name}-claim"), 'w', &[K::Eval(1117)]);
    f.edge(node, "h7g7", cl);
    let s = pos(f, &format!("{name}-searched"), 'w', &[K::Eval(searched), K::Nodes(512_000_000)]);
    f.edge(node, "b7b6", s);
    let u = pos(f, &format!("{name}-unopened"), 'w', &[]);
    f.edge(node, "a8d8", u);
    (node, cl)
}

/// test_backprop_claims.py::VetoedClaimTests::test_the_searched_reply_backs_up_past_the_refused_claim
#[test]
fn the_searched_reply_backs_up_past_the_refused_claim() {
    let mut f = Fixture::new();
    let (node, cl) = claims_node(&mut f, "PAST", 1131);
    let (_, _, tasks) = f.ascent(&[node]);
    let (v, mv, _, q) = backed(&f, node);
    assert_eq!((v, mv.as_str()), (Some(1131), "b7b6"));
    assert_eq!(q, 512_000_000);
    assert!(tasks.tasks.keys().any(|(c, _)| *c == cl));
}

/// test_backprop_claims.py::VetoedClaimTests::test_a_refused_claim_with_nothing_searched_behind_it_changes_nothing
#[test]
fn a_refused_claim_with_nothing_searched_behind_it_changes_nothing() {
    let mut f = Fixture::new();
    let (node, cl) = claims_node(&mut f, "KEEP", 1300);
    let (_, _, tasks) = f.ascent(&[node]);
    let (v, mv, _, _) = backed(&f, node);
    assert_eq!((v, mv.as_str()), (Some(1207), ""));
    assert!(tasks.tasks.keys().any(|(c, _)| *c == cl));
}
