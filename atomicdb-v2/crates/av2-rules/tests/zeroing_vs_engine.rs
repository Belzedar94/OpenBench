//! The zeroing oracle against the engine library.
//!
//! `exact::BoardZeroing` answers "does this move reset the fifty-move
//! counter?" from the parent's board (a pawn move, or a move onto an occupied
//! square).  v1 asks the pinned Fairy-Stockfish build (`logic.is_zeroing`:
//! the counter after the move is 0); `av2_chess::is_zeroing` is that same
//! question through the same library.  Seeded random games, every legal move
//! of every position on the way, must agree.
//!
//! `AV2_ZEROING_GAMES=<n>` plays more games (the default keeps `cargo test`
//! short); `AV2_ZEROING_SEED=<s>` changes the seed.

use av2_model::Move;
use av2_rules::exact::{BoardZeroing, Zeroing};

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        // xorshift64*
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
}

fn env_u64(name: &str, default: u64) -> u64 {
    std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}

#[derive(Default, Debug)]
struct Tally {
    games: u64,
    positions: u64,
    pairs: u64,
    zeroing: u64,
    captures: u64,
    promotions: u64,
    en_passant: u64,
    castling: u64,
    mismatches: Vec<(String, String, bool, bool)>,
}

fn play(seed: u64, games: u64, max_plies: usize) -> Tally {
    let mut rng = Rng(seed | 1);
    let mut t = Tally::default();
    let mut oracle = BoardZeroing;
    for _ in 0..games {
        t.games += 1;
        let mut fen = av2_chess::start_fen();
        for _ in 0..max_plies {
            if av2_chess::immediate_end(&fen).unwrap_or(true) {
                break;
            }
            let moves = av2_chess::legal_moves(&fen).expect("legal moves");
            if moves.is_empty() {
                break;
            }
            t.positions += 1;
            let board = av2_rules::exact::Board::parse(&fen).expect("board");
            let mut captures = Vec::new();
            for uci in &moves {
                let mv = Move::parse(uci).expect("uci");
                let ours = oracle.is_zeroing(&fen, mv);
                let engine = av2_chess::is_zeroing(&fen, uci).expect("engine");
                t.pairs += 1;
                t.zeroing += engine as u64;
                let from = board.0[mv.from_sq() as usize];
                let to = board.0[mv.to_sq() as usize];
                let pawn = from == b'P' || from == b'p';
                if to != 0 {
                    t.captures += 1;
                    captures.push(uci.clone());
                }
                if uci.len() == 5 {
                    t.promotions += 1;
                }
                if pawn && to == 0 && uci.as_bytes()[0] != uci.as_bytes()[2] {
                    t.en_passant += 1;
                }
                if (from == b'K' || from == b'k') && (uci.as_bytes()[0] as i32 - uci.as_bytes()[2] as i32).abs() == 2 {
                    t.castling += 1;
                }
                if ours != engine && t.mismatches.len() < 20 {
                    t.mismatches.push((fen.clone(), uci.clone(), ours, engine));
                }
            }
            // Half the time take a capture when there is one: explosions,
            // thin endgames and promotions come sooner.
            let pick = if !captures.is_empty() && rng.below(2) == 0 {
                captures[rng.below(captures.len())].clone()
            } else {
                moves[rng.below(moves.len())].clone()
            };
            fen = av2_chess::fen_after(&fen, &pick).expect("fen after");
        }
    }
    t
}

#[test]
fn board_zeroing_agrees_with_the_engine_library() {
    let games = env_u64("AV2_ZEROING_GAMES", 120);
    let seed = env_u64("AV2_ZEROING_SEED", 0x5eed_2026_1128);
    let t = play(seed, games, 160);
    eprintln!(
        "zeroing differential: {} games, {} positions, {} (fen, move) pairs, {} zeroing, {} captures, {} promotions, {} en passant, {} castling, {} mismatches",
        t.games,
        t.positions,
        t.pairs,
        t.zeroing,
        t.captures,
        t.promotions,
        t.en_passant,
        t.castling,
        t.mismatches.len()
    );
    assert!(t.mismatches.is_empty(), "BoardZeroing and the engine disagree: {:#?}", t.mismatches);
    assert!(t.pairs > 10_000 && t.captures > 0 && t.zeroing > 0);
}
