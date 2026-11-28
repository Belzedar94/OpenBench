//! `SiteRead` from sample data, until the store implements it.
//!
//! The data was read from production's public API on 28-Nov-2026
//! (`fixtures/build_fixture.py`, `fixtures/build_site_fixture.py`): the start
//! position, its 20 children and the 20 children of 1. Nf3 with their rows,
//! raw engine lines and legal moves; the roots page tables; the queue and the
//! contributor boards.  What the API does not expose is derived here the
//! simplest faithful way and says so (request ladder, summaries).

use crate::format::thousands;
use crate::read::*;
use serde::Deserialize;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Deserialize)]
struct FxMove {
    uci: String,
    san: String,
    key: String,
    status: Status,
    closure: Option<String>,
    score: Option<i32>,
    point: Option<i32>,
    mate: Option<serde_json::Value>,
    backed_plies: Option<u32>,
    #[serde(default)]
    visits: u64,
    searched_nodes: Option<u64>,
    #[serde(default)]
    floor: bool,
    #[serde(default)]
    borrowed: bool,
    #[serde(default)]
    repetition: bool,
    #[serde(default)]
    returns: bool,
    enters_opening: Option<String>,
    pn_dn: Option<(String, String)>,
}

#[derive(Deserialize)]
struct FxLine {
    raw: String,
    prior: bool,
}

#[derive(Deserialize)]
struct FxPosition {
    fen: String,
    key: String,
    status: Status,
    closure: Option<String>,
    score: Option<i32>,
    backed_plies: Option<u32>,
    best_move: Option<String>,
    #[serde(default)]
    visits: u64,
    #[serde(default)]
    nodes: u64,
    seconds: Option<f64>,
    #[serde(default)]
    analysis: Vec<FxLine>,
    #[serde(default)]
    legal: Vec<(String, String)>,
    #[serde(default)]
    moves: Vec<FxMove>,
    opening: Option<String>,
}

#[derive(Deserialize)]
struct FxExplorer {
    positions: Vec<FxPosition>,
}

#[derive(Deserialize)]
struct FxRoots {
    since: Option<String>,
    positions: Vec<RootRow>,
    openings: Vec<RootRow>,
}

#[derive(Deserialize)]
struct FxQueue {
    rows: Vec<QueueRow>,
    fleet_nodes_last_hour: u64,
}

#[derive(Deserialize)]
struct FxBoard {
    window: String,
    rows: Vec<ContributorRow>,
}

#[derive(Deserialize)]
struct FxSite {
    queue: FxQueue,
    boards: Vec<FxBoard>,
}

pub const EXPLORER_JSON: &str = include_str!("../fixtures/explorer.json");
pub const ROOTS_JSON: &str = include_str!("../fixtures/roots.json");
pub const SITE_JSON: &str = include_str!("../fixtures/site.json");

/// The request ladder of v1 (`ingest.REQUEST_LADDER`), for the fixture's
/// "Request <next rung>" label.
const LADDER: [u64; 4] = [128_000_000, 512_000_000, 2_000_000_000, 10_000_000_000];

pub struct FixtureSite {
    root: String,
    positions: HashMap<String, FxPosition>,
    parents: HashMap<String, Vec<ParentLink>>,
    roots: FxRoots,
    site: FxSite,
    epoch: AtomicU64,
}

impl FixtureSite {
    pub fn load() -> Result<FixtureSite, serde_json::Error> {
        Self::from_json(EXPLORER_JSON, ROOTS_JSON, SITE_JSON)
    }

    pub fn from_json(explorer: &str, roots: &str, site: &str) -> Result<FixtureSite, serde_json::Error> {
        let ex: FxExplorer = serde_json::from_str(explorer)?;
        let root = ex.positions.first().map(|p| p.key.clone()).unwrap_or_default();
        let mut parents: HashMap<String, Vec<ParentLink>> = HashMap::new();
        for p in &ex.positions {
            for m in &p.moves {
                parents.entry(m.key.clone()).or_default().push(ParentLink { key: p.key.clone(), uci: m.uci.clone(), san: m.san.clone() });
            }
        }
        Ok(FixtureSite {
            root,
            positions: ex.positions.into_iter().map(|p| (p.key.clone(), p)).collect(),
            parents,
            roots: serde_json::from_str(roots)?,
            site: serde_json::from_str(site)?,
            epoch: AtomicU64::new(1),
        })
    }

    /// Pretend the graph committed something (tests of the page cache).
    pub fn bump_epoch(&self) {
        self.epoch.fetch_add(1, Ordering::Relaxed);
    }

    fn row(&self, m: &FxMove) -> ChildRow {
        let child = self.positions.get(&m.key);
        let mate_plies = m.mate.as_ref().and_then(|v| v.as_i64()).map(|n| if n > 0 { (2 * n - 1) as u32 } else { (2 * -n) as u32 });
        let own_search = match (m.point, child) {
            (Some(p), Some(c)) if c.nodes > 0 => Some((p, c.nodes)),
            (Some(p), None) => m.searched_nodes.map(|n| (p, n)),
            _ => None,
        };
        ChildRow {
            uci: m.uci.clone(),
            san: m.san.clone(),
            key: m.key.clone(),
            status: m.status,
            closure: m.closure.clone(),
            unexplored: m.score.is_none() && m.visits == 0,
            score: m.score,
            mate_plies,
            backed: m.backed_plies.filter(|&p| p > 0).map(|plies| Backed { plies, light: false }),
            borrowed: m.borrowed,
            claim: None,
            repetition: m.repetition,
            blocked: false,
            returns: m.returns && !m.floor,
            own_search,
            proof: m.pn_dn.as_ref().and_then(|(p, d)| Some((p.parse().ok()?, d.parse().ok()?))),
            searched_nodes: m.searched_nodes.filter(|&n| n > 0),
            visits: m.visits,
            pending: None,
            enters_opening: m.enters_opening.clone(),
        }
    }

    /// v1 `_numbered_line` over a route from the start position.
    fn line(&self, route: &[String]) -> Option<(Vec<LineStep>, String)> {
        let mut at = self.positions.get(&self.root)?;
        let mut steps = Vec::new();
        for (i, uci) in route.iter().enumerate() {
            let m = at.moves.iter().find(|m| &m.uci == uci)?;
            let white = at.fen.split(' ').nth(1) != Some("b");
            let fullmove = i / 2 + 1;
            let num = if white {
                format!("{fullmove}.")
            } else if i == 0 {
                format!("{fullmove}...")
            } else {
                String::new()
            };
            let play = route[..=i].join(",");
            steps.push(LineStep {
                num,
                san: m.san.clone(),
                uci: m.uci.clone(),
                key: m.key.clone(),
                url: format!("/atomicdb/explore/{}/?play={}", m.key, crate::format::query_value(&play)),
            });
            at = self.positions.get(&m.key)?;
        }
        Some((steps, at.key.clone()))
    }
}

impl SiteRead for FixtureSite {
    fn epoch(&self) -> u64 {
        self.epoch.load(Ordering::Relaxed)
    }

    fn root_key(&self) -> String {
        self.root.clone()
    }

    fn explorer(&self, key: &str, route: &[String]) -> Option<ExplorerView> {
        let p = self.positions.get(key)?;
        let rows: Vec<ChildRow> = p.moves.iter().map(|m| self.row(m)).collect();
        let searched = rows.iter().filter(|r| r.searched_nodes.is_some()).count() as u64;
        let from_lines = rows.iter().filter(|r| r.searched_nodes.is_none() && r.score.is_some()).count() as u64;
        let mut summary = vec![];
        if searched > 0 {
            summary.push(format!("{} searched", thousands(searched)));
        }
        if from_lines > 0 {
            summary.push(format!("{} from lines only", thousands(from_lines)));
        }
        let pos = PositionView {
            key: p.key.clone(),
            fen: p.fen.clone(),
            status: p.status,
            closure: p.closure.clone(),
            score: if p.status.is_closed() { None } else { p.score },
            backed: p.backed_plies.filter(|&n| n > 0).map(|plies| Backed { plies, light: false }),
            walked: false,
            mate_in: None,
            won_line: vec![],
            won_line_san: String::new(),
            best_move: p.best_move.clone(),
            visits: p.visits,
            nodes_invested: p.nodes,
            time_invested: p.seconds.unwrap_or(0.0),
            analysis: p.analysis.iter().map(|l| RawLine { raw: l.raw.clone(), prior: l.prior }).collect(),
            moves_summary: summary.join(" \u{b7} "),
        };
        // The route counts only if it leads here from the start position.
        let (line, line_from_root) = match self.line(route) {
            Some((steps, end)) if end == key => (steps, true),
            _ => (vec![], key == self.root),
        };
        let deepest = rows.iter().filter_map(|r| r.searched_nodes).max().unwrap_or(0).max(p.nodes.min(LADDER[3]));
        let next = LADDER.iter().find(|&&n| n > deepest).copied();
        let unexplored = rows.iter().filter(|r| r.unexplored).count() as u32;
        let pv_verify_lines: Vec<(u32, String)> = p
            .analysis
            .iter()
            .filter(|l| !l.prior)
            .enumerate()
            .filter_map(|(i, l)| {
                let first = l.raw.split(" pv ").nth(1)?.split(' ').next()?;
                let san = p.legal.iter().find(|(u, _)| u == first).map(|(_, s)| s.clone()).unwrap_or_else(|| first.to_string());
                Some((i as u32 + 1, san))
            })
            .collect();
        Some(ExplorerView {
            opening: p.opening.as_ref().map(|name| Opening { name: name.clone(), position_key: Some(p.key.clone()), ..Default::default() }),
            parents: self.parents.get(key).cloned().unwrap_or_default(),
            legal: if p.closure.as_deref() == Some("TERMINAL") {
                vec![]
            } else {
                p.legal.iter().map(|(u, s)| LegalMove { uci: u.clone(), san: s.clone() }).collect()
            },
            line_top_key: None,
            line,
            line_from_root,
            request: RequestState {
                due_label: next.map(crate::format::budget_label).unwrap_or_else(|| "128M".into()),
                frontier_exhausted: next.is_none(),
                subtree_cap: 78,
                unexplored_count: unexplored,
                pv_verify_lines,
            },
            live: None,
            rows,
            pos,
        })
    }

    fn roots(&self) -> RootsView {
        RootsView {
            since: self.roots.since.clone(),
            positions: self.roots.positions.clone(),
            openings: self.roots.openings.clone(),
            main_line: None,
            max_plies: 60,
            name_min_votes: 5,
        }
    }

    fn queue(&self) -> QueueView {
        QueueView {
            rows: self.site.queue.rows.clone(),
            ingest_pending: 0,
            ingest_oldest_seconds: None,
            propagation_backlog: 0,
            fleet_nodes_last_hour: self.site.queue.fleet_nodes_last_hour,
        }
    }

    fn leaderboard(&self) -> Leaderboard {
        let pick = |w: &str| self.site.boards.iter().find(|b| b.window.starts_with(w)).map(|b| b.rows.clone()).unwrap_or_default();
        Leaderboard { today: pick("Today"), last_24h: pick("Last 24"), all_time: pick("All time") }
    }

    fn workers(&self) -> WorkersView {
        WorkersView::default()
    }

    fn totals(&self) -> TreeTotals {
        TreeTotals {
            positions: 204_000_000,
            closed: 0,
            nodes: self.site.queue.fleet_nodes_last_hour,
            analyses_24h: 0,
            closed_24h: 0,
            workers: 0,
            cores: 0,
        }
    }

    fn live_request(&self, _key: &str) -> Option<LiveRequest> {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixture_loads_and_walks() {
        let f = FixtureSite::load().unwrap();
        let root = f.root_key();
        assert_eq!(root, "b1791d7fc9ae3d38966568c257ffb3a02cbf8394cdb4805bc70f64fc3c0b6879");
        let e = f.explorer(&root, &[]).unwrap();
        assert_eq!(e.rows.len(), 20);
        assert_eq!(e.legal.len(), 20);
        assert!(e.line_from_root && e.line.is_empty());
        let nf3 = &e.rows[0];
        assert_eq!((nf3.uci.as_str(), nf3.san.as_str()), ("g1f3", "Nf3"));
        let child = f.explorer(&nf3.key, &["g1f3".into()]).unwrap();
        assert_eq!(child.line.len(), 1);
        assert_eq!(child.line[0].num, "1.");
        assert!(child.parents.iter().any(|p| p.key == root));
        // A wrong route is dropped, not trusted.
        let lost = f.explorer(&nf3.key, &["e2e4".into()]).unwrap();
        assert!(lost.line.is_empty() && !lost.line_from_root);
        assert!(f.roots().openings.len() > 100);
        assert!(!f.queue().rows.is_empty());
        assert!(!f.leaderboard().all_time.is_empty());
    }
}
