//! Stub rules, until `av2-rules` holds the real ones: each input either
//! logs a fact as it is, or lands a synthetic search with production's write
//! shape (20 to 38 children with FENs, an analysis blob, five values rewritten
//! up the first-parent chain, a search fact).  Deterministic in the input.

use av2_facts::{Fact, RawJson, SearchLanded};
use av2_model::record::F_EXPANDED;
use av2_model::{Core, Edge, GraphWrite, Move, PosId, Rng};
use av2_rules::{Rules, RulesError};

#[derive(Debug, Clone)]
pub enum StubInput {
    /// Log the fact, change nothing.
    Fact(Box<Fact>),
    /// Land a synthetic search; the seed makes it deterministic.
    Landing { seed: u64 },
}

#[derive(Debug, Default)]
pub struct StubRules;

fn fen_pool() -> &'static [String] {
    static POOL: std::sync::OnceLock<Vec<String>> = std::sync::OnceLock::new();
    POOL.get_or_init(|| {
        let mut rng = Rng::new(0xFE11);
        (0..4096).map(|_| av2_store::testkit::random_fen(&mut rng)).collect()
    })
}

fn landing(g: &mut dyn GraphWrite, seed: u64, facts: &mut Vec<Fact>) -> Result<(), RulesError> {
    let mut rng = Rng::new(seed);
    if g.is_empty() {
        g.create(av2_store::testkit::random_fp(&mut rng), Some(&fen_pool()[0]), Core::default())?;
    }
    let n = g.len();
    let mut es = Vec::new();
    // An unexpanded position (most positions are: 73% in production).
    let mut p = PosId(rng.below(n));
    for _ in 0..64 {
        es.clear();
        g.children(p, &mut es);
        if es.is_empty() {
            break;
        }
        p = PosId(rng.below(n));
    }
    let k = 20 + rng.below(19) as usize;
    let pool = fen_pool();
    let mut block = es.clone();
    for i in 0..k {
        let fen = &pool[rng.below(pool.len() as u64) as usize];
        let c =
            g.create(av2_store::testkit::random_fp(&mut rng), Some(fen), Core { eval_cp: rng.below(400) as i16 - 200, ..Core::default() })?;
        block.push(Edge { child: c, mv: Move::new((i / 8) as u8, (i % 8 + 16) as u8, 0) });
    }
    if block.len() <= u16::MAX as usize {
        g.set_children(p, &block)?;
    }
    let mut core = g.core(p);
    core.set_flag(F_EXPANDED, true);
    core.eval_cp = rng.below(400) as i16 - 200;
    g.put_core(p, core)?;
    let mut e = g.ext(p).unwrap_or_default();
    e.nodes_invested += 128_000_000;
    e.visits += 1;
    let mut blob = vec![0u8; 1200];
    rng.fill(&mut blob);
    e.analysis = g.put_blob(&blob)?;
    g.put_ext(p, e)?;
    let mut ps = Vec::new();
    let mut cur = p;
    for _ in 0..5 {
        ps.clear();
        g.parents(cur, &mut ps);
        let Some(&up) = ps.first() else { break };
        let mut c = g.core(up);
        c.backed_eval = rng.below(400) as i16 - 200;
        g.put_core(up, c)?;
        cur = up;
    }
    facts.push(Fact::SearchLanded(SearchLanded {
        task: seed,
        position: format!("{:x}", p.0),
        budget_nodes: 128_000_000,
        payload: RawJson(format!("{{\"seed\":{seed},\"lines\":[{}]}}", "\"e2e4 e7e5 g1f3 b8c6\",".repeat(40).trim_end_matches(','))),
        ..Default::default()
    }));
    Ok(())
}

impl Rules for StubRules {
    type Input = StubInput;
    type Output = ();

    fn apply(&mut self, graph: &mut dyn GraphWrite, inputs: &[StubInput], facts: &mut Vec<Fact>) -> Result<Vec<()>, RulesError> {
        for i in inputs {
            match i {
                StubInput::Fact(f) => facts.push((**f).clone()),
                StubInput::Landing { seed } => landing(graph, *seed, facts)?,
            }
        }
        Ok(vec![(); inputs.len()])
    }
}
