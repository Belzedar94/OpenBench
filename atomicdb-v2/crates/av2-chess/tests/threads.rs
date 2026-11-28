//! Plan open question 3: the library called from many threads of one process.
//! Every thread must get exactly the single-threaded answers.  (The large
//! version of this check is `examples/equiv.rs --threads N` over the golden
//! files; see the PR.)

use av2_chess::{self as chess, pf};

/// Positions from a seeded random walk: openings, middlegames, explosions,
/// en-passant squares.
fn sample(n: usize) -> Vec<String> {
    let mut rng = av2_model::Rng::new(20261128);
    let mut out = Vec::new();
    while out.len() < n {
        let mut fen = chess::start_fen();
        for _ in 0..120 {
            out.push(fen.clone());
            let moves = chess::legal_moves(&fen).unwrap();
            if moves.is_empty() || chess::terminal_status(&fen).unwrap().is_some() {
                break;
            }
            let m = &moves[(rng.next_u64() % moves.len() as u64) as usize];
            fen = pf::get_fen(&fen, &[m]).unwrap();
        }
    }
    out.truncate(n);
    out
}

fn answer(fen: &str) -> String {
    let canon = chess::canonical_fen(fen);
    let kids = chess::expansion(&canon).unwrap();
    let mut s = format!("{canon}|{}|{:?}|", chess::key_of(fen), chess::terminal_status(&canon).unwrap());
    for k in kids {
        s.push_str(&format!("{} {} {} {:?};", k.uci, k.fen, k.key, k.terminal));
    }
    s
}

#[test]
fn many_threads_get_the_single_threaded_answers() {
    let fens = sample(600);
    let reference: Vec<String> = fens.iter().map(|f| answer(f)).collect();
    std::thread::scope(|s| {
        for t in 0..6 {
            let fens = &fens;
            let reference = &reference;
            s.spawn(move || {
                // Each thread walks the list from a different offset, so
                // different positions are in flight at the same time.
                for round in 0..3 {
                    for i in 0..fens.len() {
                        let j = (i * 7 + t * 101 + round * 13) % fens.len();
                        assert_eq!(answer(&fens[j]), reference[j], "thread {t}, {}", fens[j]);
                    }
                }
            });
        }
    });
}
