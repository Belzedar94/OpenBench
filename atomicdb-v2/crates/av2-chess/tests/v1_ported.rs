//! v1 tests ported to av2-chess (rules POS-1, POS-4, TB-1.1, TB-1.3, DTM-5.1).
//! Each test names the v1 test it ports (`atomicdb/<file>::<Class>::<test>`,
//! listed in `docs/test-map.csv`) and keeps its fixtures and assertions.

use av2_chess::{self as chess, pf};
use av2_model::Status;
use sha2::{Digest, Sha256};

fn sha(s: &str) -> String {
    hex::encode(Sha256::digest(s.as_bytes()))
}

fn field(fen: &str, i: usize) -> String {
    fen.split_whitespace().nth(i).unwrap_or("").to_string()
}

fn raw_after(before: &str, uci: &str) -> String {
    pf::get_fen(before, &[uci]).unwrap()
}

// test_en_passant_identity.py fixtures.
const OWN_KING_BEFORE: &str = "8/8/8/8/3p4/3k4/4P3/6K1 w - - 0 1";
const OWN_KING_F3_BEFORE: &str = "8/8/8/8/3p4/5k2/4P3/6K1 w - - 0 1";
const PINNED_BEFORE: &str = "3k4/8/8/8/3p4/8/4P3/3RK3 w - - 0 1";
const LEGITIMATE_BEFORE: &str = "7k/8/8/8/3p4/8/4P3/4K3 w - - 0 1";
const WHITE_PHANTOM_BEFORE: &str = "1k6/6p1/7K/7P/8/8/8/8 b - - 0 1";
const QUIET_FEN: &str = "8/8/8/8/8/8/1k6/K6Q w - - 0 1";
// test_clock_slack.py
const CAPTURE_FEN: &str = "4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1";

// ---------------------------------------------------------------- POS-1

/// test_en_passant_identity.py::PhantomIsNotIdentityTests::test_a_capture_that_explodes_its_own_king_leaves_no_right
#[test]
fn a_capture_that_explodes_its_own_king_leaves_no_right() {
    let raw = raw_after(OWN_KING_BEFORE, "e2e4");
    assert_eq!(field(&raw, 3), "e3");
    assert_eq!(field(&chess::canonical_fen(&raw), 3), "-");
}

/// ...::test_the_same_shape_from_the_other_side_of_the_square
#[test]
fn the_same_shape_from_the_other_side_of_the_square() {
    let raw = raw_after(OWN_KING_F3_BEFORE, "e2e4");
    assert_eq!(field(&raw, 3), "e3");
    assert_eq!(field(&chess::canonical_fen(&raw), 3), "-");
}

/// ...::test_a_pinned_capturing_pawn_leaves_no_right
#[test]
fn a_pinned_capturing_pawn_leaves_no_right() {
    let raw = raw_after(PINNED_BEFORE, "e2e4");
    assert_eq!(field(&raw, 3), "e3");
    assert_eq!(field(&chess::canonical_fen(&raw), 3), "-");
}

/// ...::test_a_real_right_is_kept
#[test]
fn a_real_right_is_kept() {
    let raw = raw_after(LEGITIMATE_BEFORE, "e2e4");
    assert_eq!(field(&chess::canonical_fen(&raw), 3), "e3");
}

/// ...::test_the_white_side_phantom_is_dropped_too
#[test]
fn the_white_side_phantom_is_dropped_too() {
    let raw = raw_after(WHITE_PHANTOM_BEFORE, "g7g5");
    assert_eq!(field(&raw, 3), "g6");
    assert_eq!(field(&chess::canonical_fen(&raw), 3), "-");
}

/// ...::test_no_adjacent_enemy_pawn_is_unchanged
#[test]
fn no_adjacent_enemy_pawn_is_unchanged() {
    let raw = raw_after(&chess::start_fen(), "e2e4");
    assert_eq!(field(&raw, 3), "-");
    assert_eq!(field(&chess::canonical_fen(&raw), 3), "-");
}

/// ...::test_a_phantom_and_its_twin_are_now_one_key
#[test]
fn a_phantom_and_its_twin_are_now_one_key() {
    let raw = raw_after(OWN_KING_BEFORE, "e2e4");
    let p: Vec<&str> = raw.split_whitespace().collect();
    let twin = format!("{} {} {} - 0 1", p[0], p[1], p[2]);
    assert_eq!(chess::key_of(&raw), chess::key_of(&twin));
}

/// ...::test_a_real_right_still_distinguishes_two_keys
#[test]
fn a_real_right_still_distinguishes_two_keys() {
    let raw = raw_after(LEGITIMATE_BEFORE, "e2e4");
    let p: Vec<&str> = raw.split_whitespace().collect();
    let twin = format!("{} {} {} - 0 1", p[0], p[1], p[2]);
    assert_ne!(chess::key_of(&raw), chess::key_of(&twin));
}

/// test_en_passant_identity.py::IdentityOfEverythingElseTests::test_positions_without_a_right_keep_their_exact_key
/// and tests.py::LogicTests::test_canonical_strips_counters
#[test]
fn positions_without_a_right_keep_their_exact_key() {
    for fen in [chess::start_fen().as_str(), QUIET_FEN, "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "4k3/8/8/8/8/8/8/4K3 b - - 12 34"] {
        let p: Vec<&str> = fen.split_whitespace().collect();
        let canonical = format!("{} {} {} {} 0 1", p[0], p[1], p[2], p[3]);
        assert_eq!(chess::canonical_fen(fen), canonical);
        assert_eq!(chess::key_of(fen).to_hex(), sha(&canonical));
    }
}

/// ...::test_a_legitimate_right_keeps_its_pre_v2_key
#[test]
fn a_legitimate_right_keeps_its_pre_v2_key() {
    let raw = raw_after(LEGITIMATE_BEFORE, "e2e4");
    let p: Vec<&str> = raw.split_whitespace().collect();
    let legacy = format!("{} {} {} {} 0 1", p[0], p[1], p[2], p[3]);
    assert_eq!(chess::key_of(&raw).to_hex(), sha(&legacy));
}

/// ...::test_a_position_with_no_legal_move_drops_a_meaningless_right
#[test]
fn a_position_with_no_legal_move_drops_a_meaningless_right() {
    let stalemate = "k7/1R6/8/8/8/6K1/8/8 b - e3 0 1";
    assert_eq!(field(&pf::get_fen(stalemate, &[]).unwrap(), 3), "-");
    assert_eq!(field(&chess::canonical_fen(stalemate), 3), "-");
}

/// ...::test_the_canonicalizer_version_is_declared
#[test]
fn the_canonicalizer_version_is_declared() {
    assert_eq!(chess::CANONICAL_VERSION, 2);
    assert_eq!(chess::RULESET_ID, "atomic-fide-claim-v1");
}

/// ...::test_a_phantom_position_becomes_tablebase_applicable (POS-1, TB-1)
#[test]
fn a_phantom_position_becomes_tablebase_applicable() {
    let raw = raw_after(OWN_KING_BEFORE, "e2e4");
    assert!(!chess::tb_applicable(&raw, 6));
    assert!(chess::tb_applicable(&chess::canonical_fen(&raw), 6));
}

/// test_en_passant_identity.py::RepetitionGuardTests::test_the_shuffle_really_returns_to_the_same_board
/// and ::test_v1_would_have_missed_it_and_v2_does_not
#[test]
fn the_shuffle_returns_to_one_key() {
    let shuffle = ["h6h7", "b8a8", "h7h6", "a8b8"];
    let fen = raw_after(WHITE_PHANTOM_BEFORE, "g7g5");
    let mut walked = fen.clone();
    for m in shuffle {
        assert!(pf::legal_moves(&walked).unwrap().iter().any(|x| x == m));
        walked = pf::get_fen(&walked, &[m]).unwrap();
    }
    assert_eq!(field(&walked, 0), field(&fen, 0));
    assert_eq!(field(&walked, 1), field(&fen, 1));
    assert_eq!(field(&fen, 3), "g6");
    assert_eq!(field(&walked, 3), "-");
    assert_eq!(chess::canonical_fen(&fen), chess::canonical_fen(&walked));
}

/// test_proofs.py::MovegenShortcutTests::test_en_passant_probe_answers_the_next_question
/// (the identity half; the memo half is v1 plumbing)
#[test]
fn en_passant_probe_keeps_a_real_square() {
    let fen = "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 1";
    let canon = chess::canonical_fen(fen);
    assert_eq!(field(&canon, 3), "f6");
    assert!(chess::legal_moves(&canon).unwrap().iter().any(|m| m == "e5f6"));
}

/// test_proofs.py::MovegenMemoTests::test_legal_moves_hands_out_a_fresh_list
#[test]
fn legal_moves_hands_out_a_fresh_list() {
    let fen = chess::start_fen();
    let mut first = chess::legal_moves(&fen).unwrap();
    first.reverse();
    first.push("junk".into());
    assert_eq!(chess::legal_moves(&fen).unwrap(), pf::legal_moves(&fen).unwrap());
    assert_eq!(chess::legal_moves(&fen).unwrap().len(), 20);
}

/// test_openings.py::AtomicOpeningCatalogTests::test_position_matching_recognizes_transpositions
/// (the identity it relies on: two move orders, one key)
#[test]
fn transpositions_share_a_key() {
    let s = chess::start_fen();
    let a = pf::get_fen(&s, &["e2e3", "e7e6", "g1f3", "g8f6", "d2d3"]).unwrap();
    let b = pf::get_fen(&s, &["d2d3", "g8f6", "g1f3", "e7e6", "e2e3"]).unwrap();
    assert_eq!(chess::key_of(&a), chess::key_of(&b));
    let c = pf::get_fen(&s, &["e2e3", "e7e6", "g1f3", "g8f6", "f3g1", "f6g8"]).unwrap();
    assert_ne!(c, chess::start_fen(), "counters moved");
    assert_eq!(chess::key_of(&pf::get_fen(&c, &["d2d3"]).unwrap()), chess::key_of(&pf::get_fen(&s, &["e2e3", "e7e6", "d2d3"]).unwrap()));
}

// ---------------------------------------------------------------- POS-4

/// test_proofs.py::MovegenShortcutTests (FENS): real positions plus edge cases.
const SHORTCUT_FENS: &[&str] = &[
    "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 1",
    "rnbqkbnr/pppp1ppp/8/8/3Pp3/8/PPP1PPPP/RNBQKBNR b KQkq d3 0 1",
    "r3k2r/pppq1ppp/2n1bn2/3pp3/3PP3/2N1BN2/PPPQ1PPP/R3K2R w KQkq - 0 1",
    "8/8/8/8/8/8/8/K7 b - - 0 1",
    "k7/8/8/8/8/8/8/8 w - - 0 1",
    "k7/1p6/8/8/8/8/8/1R5K w - - 0 1",
    "rnbq1bnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQ - 0 1",
    "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 0 1",
    "k7/1R6/8/8/8/6K1/8/8 b - - 0 1",
];

fn shortcut_positions() -> Vec<String> {
    let mut seen = Vec::new();
    for fen in SHORTCUT_FENS {
        let fen = chess::canonical_fen(fen);
        for uci in pf::legal_moves(&fen).unwrap() {
            seen.push(chess::canonical_fen(&pf::get_fen(&fen, &[&uci]).unwrap()));
        }
        seen.push(fen);
    }
    seen
}

/// test_proofs.py::MovegenShortcutTests::test_immediate_end_matches_the_movegen_everywhere
#[test]
fn immediate_end_matches_the_movegen_everywhere() {
    let mut missing_king = 0;
    for fen in shortcut_positions() {
        let truth = pf::is_immediate_game_end(&fen).unwrap().0;
        assert_eq!(chess::immediate_end(&fen).unwrap(), truth, "{fen}");
        let board = field(&fen, 0);
        if board.contains('K') && board.contains('k') {
            assert!(!truth, "{fen}");
        } else {
            missing_king += 1;
            assert!(truth, "{fen}");
        }
    }
    assert!(missing_king >= 3);
}

/// test_proofs.py::MovegenShortcutTests::test_terminal_status_matches_the_movegen_everywhere
#[test]
fn terminal_status_matches_the_movegen_everywhere() {
    for fen in shortcut_positions() {
        let moves = pf::legal_moves(&fen).unwrap();
        let immediate = pf::is_immediate_game_end(&fen).unwrap().0;
        let reference = if !moves.is_empty() && !immediate {
            None
        } else {
            let res = pf::game_result(&fen).unwrap();
            let white = field(&fen, 1) == "w";
            Some(if res > 0 {
                if white { Status::WhiteWin } else { Status::BlackWin }
            } else if res < 0 {
                if white { Status::BlackWin } else { Status::WhiteWin }
            } else {
                Status::Draw
            })
        };
        assert_eq!(chess::terminal_status(&fen).unwrap(), reference, "{fen}");
    }
}

/// tests.py::LogicTests::test_terminal_explosion_win (the start position is not terminal)
#[test]
fn the_start_position_is_not_terminal() {
    assert_eq!(chess::terminal_status(&chess::start_fen()).unwrap(), None);
}

/// test_clock_slack.py::ClosureSlackTests::test_a_terminal_closure_is_born_with_the_maximum (the status half)
/// and ::test_a_terminal_draw_carries_no_slack
#[test]
fn terminal_fixtures() {
    let mated = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 0 1";
    assert_eq!(chess::terminal_status(mated).unwrap(), Some(Status::BlackWin));
    // Atomic stalemate: Rb7 covers a7 and b8, and a king may not capture.
    assert_eq!(chess::terminal_status("k7/1R6/8/8/8/6K1/8/8 b - - 0 1").unwrap(), Some(Status::Draw));
}

/// test_survive.py::SurvivalCertificateTests::test_the_claim_automaton_is_blind_to_the_clock
/// (terminal_status ignores the halfmove clock)
#[test]
fn terminal_status_is_blind_to_the_clock() {
    for clock in [0, 98, 99, 100, 120] {
        let gone = format!("8/8/8/8/8/8/8/K7 b - - {clock} 80");
        assert_eq!(chess::terminal_status(&gone).unwrap(), Some(Status::WhiteWin), "{gone}");
        let stale = format!("k7/1R6/8/8/8/6K1/8/8 b - - {clock} 80");
        assert_eq!(chess::terminal_status(&stale).unwrap(), Some(Status::Draw), "{stale}");
    }
}

/// test_ingest_batched_writes.py::ExpandTests::test_terminal_children_are_born_closed_as_before
/// (the movegen half: the king-exploding child is terminal)
#[test]
fn a_king_exploding_child_is_terminal() {
    let parent = "7k/6p1/8/8/8/8/8/6QK w - - 0 1";
    let kids = chess::expansion(parent).unwrap();
    let won: Vec<_> = kids.iter().filter(|c| c.terminal == Some(Status::WhiteWin)).collect();
    assert!(!won.is_empty(), "{kids:?}");
    for c in &kids {
        assert_eq!(c.terminal, chess::terminal_status(&c.fen).unwrap());
    }
}

// ---------------------------------------------------------------- DTM-5.1

/// test_clock_slack.py::ZeroingTests::test_pawn_moves_and_captures_reset_the_counter
#[test]
fn pawn_moves_and_captures_reset_the_counter() {
    assert!(chess::is_zeroing(&chess::start_fen(), "e2e4").unwrap());
    assert!(chess::is_zeroing(CAPTURE_FEN, "e4d5").unwrap());
}

/// test_clock_slack.py::ZeroingTests::test_quiet_moves_do_not
#[test]
fn quiet_moves_do_not() {
    assert!(!chess::is_zeroing(&chess::start_fen(), "g1f3").unwrap());
    assert!(!chess::is_zeroing(QUIET_FEN, "h1h2").unwrap());
}

// ---------------------------------------------------------------- EXP-1 (movegen)

/// tests.py::BackupTests::test_movegen_completeness_on_expand (the movegen half)
#[test]
fn movegen_completeness() {
    let kids = chess::expansion(&chess::start_fen()).unwrap();
    assert_eq!(kids.len(), 20);
    let moves: Vec<_> = kids.iter().map(|c| c.uci.clone()).collect();
    assert_eq!(moves, chess::legal_moves(&chess::start_fen()).unwrap());
}

/// test_en_passant_identity.py::IngestUsesTheNewIdentityTests::test_expanding_a_parent_stores_the_child_without_the_phantom
#[test]
fn expanding_a_parent_gives_the_child_without_the_phantom() {
    let kids = chess::expansion(OWN_KING_BEFORE).unwrap();
    let child = kids.iter().find(|c| c.uci == "e2e4").unwrap();
    assert_eq!(field(&child.fen, 3), "-");
    assert_eq!(child.key, chess::key_of(&raw_after(OWN_KING_BEFORE, "e2e4")));
}

/// An illegal move is refused like pyffish's ValueError
/// (test_ingest_batched_writes.py::MaterialiseTests::test_an_illegal_move_stops_the_chain_where_the_walk_stopped uses a1a1).
#[test]
fn an_illegal_move_is_refused() {
    let err = chess::apply_move(&chess::start_fen(), "a1a1").unwrap_err();
    assert!(matches!(err, chess::ChessError::IllegalMove { index: 0, .. }));
    let err = pf::get_fen(&chess::start_fen(), &["e2e4", "e2e4"]).unwrap_err();
    assert!(matches!(err, chess::ChessError::IllegalMove { index: 1, .. }));
}

// ---------------------------------------------------------------- TB-1

/// tests.py::TablebaseTests::test_applicability
#[test]
fn tablebase_applicability() {
    assert!(chess::tb_applicable("8/8/8/8/8/8/1k6/K6Q w - - 0 1", 6));
    assert!(!chess::tb_applicable("8/8/8/8/8/8/1k6/K6Q w K - 0 1", 6));
    assert!(!chess::tb_applicable(&chess::start_fen(), 6));
    assert_eq!(chess::piece_count(&chess::start_fen()), 32);
}

/// tests.py::TablebaseTests::test_wdl_mapping
#[test]
fn wdl_mapping() {
    assert_eq!(chess::wdl_to_status(2, true), Status::WhiteWin);
    assert_eq!(chess::wdl_to_status(2, false), Status::BlackWin);
    assert_eq!(chess::wdl_to_status(-2, true), Status::BlackWin);
    assert_eq!(chess::wdl_to_status(-2, false), Status::WhiteWin);
    assert_eq!(chess::wdl_to_status(1, true), Status::Draw);
    assert_eq!(chess::wdl_to_status(-1, false), Status::Draw);
    assert_eq!(chess::wdl_to_status(0, true), Status::Draw);
}

// ---------------------------------------------------------------- the library

/// The banner, and validation of untrusted input.
#[test]
fn library_identity_and_validation() {
    assert!(pf::info().starts_with("Fairy-Stockfish"));
    assert!(pf::info().contains(" LB"), "pyffish is built with LARGEBOARDS: {}", pf::info());
    assert_eq!(pf::validate_fen(&chess::start_fen()), 1);
    assert!(pf::validate_fen("not a fen") < 0);
    assert_eq!(pf::start_fen(), "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
}
