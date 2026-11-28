//! Deterministic synthetic batches for tests, the crash harness and the
//! benchmarks.  A batch is a function of the graph it reads and the RNG, so
//! two graphs with the same content given the same seed produce the same
//! batch: that is what lets the crash test replay "batches 1..n" on the
//! in-memory model and compare.

use av2_model::graph::GraphError;
use av2_model::record::{F_EXPANDED, F_REACHABLE};
use av2_model::{BlobRef, Closure, Core, Edge, Ext, Fingerprint, GraphWrite, Move, PosId, Proof, Rng, Status};

/// A fingerprint drawn from the RNG (uniform, like sha256 prefixes).
pub fn random_fp(rng: &mut Rng) -> Fingerprint {
    let mut f = [0u8; 12];
    rng.fill(&mut f);
    Fingerprint(f)
}

/// A synthetic FEN: usually canonical (packs), sometimes not (stored as a blob).
pub fn random_fen(rng: &mut Rng) -> String {
    const P: &[u8] = b"pnbrqkPNBRQK";
    let mut s = String::new();
    for r in 0..8 {
        if r > 0 {
            s.push('/');
        }
        let mut empty = 0;
        for _ in 0..8 {
            if rng.chance(0.3) {
                if empty > 0 {
                    s.push((b'0' + empty) as char);
                    empty = 0;
                }
                s.push(P[rng.below(P.len() as u64) as usize] as char);
            } else {
                empty += 1;
            }
        }
        if empty > 0 {
            s.push((b'0' + empty) as char);
        }
    }
    let stm = if rng.chance(0.5) { "w" } else { "b" };
    let castle = ["-", "KQkq", "Kq", "k"][rng.below(4) as usize];
    let ep = if rng.chance(0.1) { "e3" } else { "-" };
    let counters = if rng.chance(0.05) { "3 17" } else { "0 1" };
    format!("{s} {stm} {castle} {ep} {counters}")
}

fn random_core(rng: &mut Rng) -> Core {
    let mut c = Core::default();
    let st = [Status::Unknown, Status::WhiteWin, Status::BlackWin, Status::Draw][rng.below(4) as usize];
    let cl = [Closure::None, Closure::MatePv, Closure::Minimax, Closure::Terminal][rng.below(4) as usize];
    let pr = [Proof::None, Proof::AndOr, Proof::Engine][rng.below(3) as usize];
    c.set_status(st, cl, pr);
    c.set_flag(F_EXPANDED, rng.chance(0.2));
    c.set_flag(F_REACHABLE, rng.chance(0.7));
    if rng.chance(0.6) {
        c.eval_cp = (rng.below(20_001) as i32 - 10_000) as i16;
    }
    if rng.chance(0.3) {
        c.backed_eval = (rng.below(20_001) as i32 - 10_000) as i16;
        c.backed_move = Move::new(rng.below(64) as u8, rng.below(63) as u8 + 1, 0);
        c.backed_plies = rng.below(80) as u16;
    }
    if rng.chance(0.1) {
        c.mate_in = rng.below(72) as u16;
        c.clock_slack = rng.below(101) as u8;
    }
    c
}

fn mv(i: usize) -> Move {
    let k = i + 1;
    Move::new((k / 64 % 64) as u8, (k % 64) as u8, 0)
}

/// Apply one synthetic batch of about `size` new positions to `g`.
pub fn random_batch(g: &mut dyn GraphWrite, rng: &mut Rng, size: usize) -> Result<(), GraphError> {
    let size = size.max(1);
    if g.is_empty() {
        let fen = random_fen(rng);
        g.create(random_fp(rng), Some(&fen), random_core(rng))?;
    }
    let mut es = Vec::new();
    let mut made = 0usize;
    while made < size {
        let n = g.len();
        // Recent positions are the ones that get expanded, as in production.
        let parent = PosId(n - 1 - rng.below(n.min(5_000)));
        es.clear();
        g.children(parent, &mut es);
        if es.is_empty() {
            let k = 1 + rng.below(size as u64 - made as u64 + 2) as usize;
            for i in 0..k {
                let child = if n > 2 && rng.chance(0.08) {
                    PosId(rng.below(n))
                } else {
                    let fen = if rng.chance(0.9) { Some(random_fen(rng)) } else { None };
                    made += 1;
                    g.create(random_fp(rng), fen.as_deref(), random_core(rng))?
                };
                es.push(Edge { child, mv: mv(i) });
            }
            g.set_children(parent, &es)?;
        } else if es.len() < 60 {
            // One more edge (a mate line, a click): sometimes a transposition.
            let child = if rng.chance(0.3) {
                PosId(rng.below(g.len()))
            } else {
                made += 1;
                g.create(random_fp(rng), Some(&random_fen(rng)), random_core(rng))?
            };
            let i = es.len();
            es.push(Edge { child, mv: mv(i) });
            g.set_children(parent, &es)?;
        } else {
            made += 1;
        }
    }
    // Value updates on existing positions, as a climb makes.
    let n = g.len();
    for _ in 0..(1 + rng.below(2 * size as u64 + 1)) {
        let id = PosId(rng.below(n));
        let c = random_core(rng);
        g.put_core(id, c)?;
    }
    // Searched positions get an ext record and blobs.
    for _ in 0..rng.below(3) {
        let id = PosId(rng.below(n));
        let mut e = g.ext(id).unwrap_or_default();
        e.nodes_invested += 1 + rng.below(1 << 30);
        e.visits += 1;
        e.time_invested += 0.5;
        e.backed_nodes = rng.below(1 << 40);
        if rng.chance(0.7) {
            let len = 50 + rng.below(1200) as usize;
            let mut b = vec![0u8; len];
            rng.fill(&mut b);
            e.analysis = g.put_blob(&b)?;
        }
        if rng.chance(0.2) {
            e.won_line = g.put_blob(b"e2e4 e7e5 g1f3")?;
        }
        if rng.chance(0.05) {
            e.analysis = BlobRef::NONE;
        }
        g.put_ext(id, e)?;
    }
    // A duplicate create must be refused without changing anything.
    if rng.chance(0.05) && n > 0 {
        let probe = random_fp(rng);
        if g.lookup(&probe).is_none() {
            let id = g.create(probe, None, Core::default())?;
            assert!(matches!(g.create(probe, None, Core::default()), Err(GraphError::Duplicate(d)) if d == id));
        }
    }
    let _ = Ext::default();
    Ok(())
}
