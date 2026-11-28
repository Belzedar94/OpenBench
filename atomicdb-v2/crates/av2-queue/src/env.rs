//! What the queue reads of the graph.
//!
//! The queue rules run inside the writer, which owns the graph overlay; they
//! read positions through [`QueueEnv`] and never write the graph.  Every
//! decision taken from these reads ends up in the deltas of the input
//! (`state::Delta`), so the queue state can be rebuilt from the log without
//! the graph.
//!
//! [`GraphEnv`] implements the trait over any `GraphRead` (the committed
//! store, the writer's batch overlay, or `av2_store::mem::MemGraph` in tests).

use av2_model::{GraphRead, PosId, Status};

/// The columns of a position the queue rules read.  All values are the v1
/// columns: `eval_cp`/`backed_eval` White's point of view, `None` = NULL.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct PosInfo {
    pub exists: bool,
    pub status: Status,
    pub visits: i32,
    /// `campaign_id`, 0 = NULL.
    pub campaign: u32,
    pub expanded: bool,
    pub reachable: bool,
    pub eval_cp: Option<i32>,
    pub backed_eval: Option<i32>,
    /// `bool(backed_move)` (PRI-5.7 `is_interior`).
    pub backed_move: bool,
    pub mate_in: Option<i32>,
    /// `mate` of the first current line of `last_analysis` (PRI-5.4):
    /// `Some(0)` when that line has no mate, `None` when there is no line.
    pub first_line_mate: Option<i64>,
    pub white_to_move: bool,
    pub nodes_invested: u64,
    pub backed_nodes: u64,
    /// `logic.tb_applicable(fen)` with the default 6 men (Q-2.27).
    pub tb_applicable: bool,
}

impl PosInfo {
    pub fn open(&self) -> bool {
        self.status == Status::Unknown
    }
    /// PRI-1.2 `known_eval_of`.
    pub fn known_eval(&self) -> Option<i32> {
        match self.status {
            Status::WhiteWin => Some(10000),
            Status::BlackWin => Some(-10000),
            Status::Draw => Some(0),
            Status::Unknown => self.backed_eval.or(self.eval_cp),
        }
    }
}

pub trait QueueEnv {
    fn pos(&self, pos: u64) -> PosInfo;
    /// Cheap reads the lease makes for every candidate (defaults: `pos`).
    fn status(&self, pos: u64) -> Status {
        self.pos(pos).status
    }
    fn visits(&self, pos: u64) -> i32 {
        self.pos(pos).visits
    }
    fn campaign(&self, pos: u64) -> u32 {
        self.pos(pos).campaign
    }
    fn tb_applicable(&self, pos: u64) -> bool {
        self.pos(pos).tb_applicable
    }
    /// Materialised out-edges `(move uci, child)` in edge order.
    fn children(&self, pos: u64) -> Vec<(String, u64)>;
    /// Parents of a position (incoming edges), in the order they were added.
    fn parents(&self, pos: u64) -> Vec<u64>;
    /// Q-2.26 `server_tables_wdl(fen) is not None`: the server's tables
    /// answer for this position.  v2 recommendation: close such positions at
    /// creation (preparer) so that this is never true for an open position.
    fn server_tables_cover(&self, _pos: u64) -> bool {
        false
    }
    /// The v1 **stored** `Position.priority` (the value the last priority
    /// pass wrote; 0.0 for positions born after 29-Aug, when production
    /// stopped recomputing it).  Tombstones and revivals are queue state and
    /// override it (`QueueState::queue_rank`).  The graph does not store it
    /// (D5); for the exact port it must come from the v1 export (see
    /// docs/queue.md, decision for the store).
    fn queue_rank(&self, _pos: u64) -> f64 {
        0.0
    }
    /// The priority computed in memory (`RegretSelector`, D5), used only
    /// when `QueueConfig::computed_priority` is switched on.
    fn computed_priority(&self, _pos: u64) -> Option<f64> {
        None
    }
}

/// A [`QueueEnv`] over a graph, with optional overrides for the two values
/// the graph does not hold (server tables, imported priority).
pub struct GraphEnv<'a, G: GraphRead + ?Sized> {
    pub graph: &'a G,
    pub priority: Option<&'a std::collections::HashMap<u64, f64>>,
}

impl<'a, G: GraphRead + ?Sized> GraphEnv<'a, G> {
    pub fn new(graph: &'a G) -> Self {
        GraphEnv { graph, priority: None }
    }
}

/// `logic.tb_applicable(fen, max_men)`: piece count <= max, no castling, no en passant.
pub fn tb_applicable(fen: &str, max_men: u32) -> bool {
    let mut parts = fen.split_whitespace();
    let Some(board) = parts.next() else { return false };
    let _stm = parts.next();
    let castling = parts.next().unwrap_or("-");
    let ep = parts.next().unwrap_or("-");
    let men = board.chars().filter(|c| c.is_ascii_alphabetic()).count() as u32;
    men <= max_men && castling == "-" && ep == "-"
}

/// The `mate` of the first current line of a v1 `last_analysis` blob
/// (PRI-5.4): the first dict line that is not a `prior_pass`.
pub fn first_line_mate(analysis_json: &[u8]) -> Option<i64> {
    let v: serde_json::Value = serde_json::from_slice(analysis_json).ok()?;
    let lines = match &v {
        serde_json::Value::Array(a) => a.as_slice(),
        serde_json::Value::Object(o) => match o.get("lines") {
            Some(serde_json::Value::Array(a)) => a.as_slice(),
            _ => return None,
        },
        _ => return None,
    };
    for line in lines {
        let Some(obj) = line.as_object() else { continue };
        if obj.get("prior_pass").map(truthy).unwrap_or(false) {
            continue;
        }
        return Some(obj.get("mate").and_then(|m| m.as_i64()).unwrap_or(0));
    }
    None
}

fn truthy(v: &serde_json::Value) -> bool {
    match v {
        serde_json::Value::Null => false,
        serde_json::Value::Bool(b) => *b,
        serde_json::Value::Number(n) => n.as_f64().map(|x| x != 0.0).unwrap_or(false),
        serde_json::Value::String(s) => !s.is_empty(),
        serde_json::Value::Array(a) => !a.is_empty(),
        serde_json::Value::Object(o) => !o.is_empty(),
    }
}

impl<G: GraphRead + ?Sized> QueueEnv for GraphEnv<'_, G> {
    fn pos(&self, pos: u64) -> PosInfo {
        if pos >= self.graph.len() {
            return PosInfo::default();
        }
        let id = PosId(pos);
        let core = self.graph.core(id);
        let ext = self.graph.ext(id).unwrap_or_default();
        let fen = self.graph.fen(id);
        let white = !core.black_to_move();
        PosInfo {
            exists: true,
            status: core.status(),
            visits: ext.visits,
            campaign: ext.campaign,
            expanded: core.expanded(),
            reachable: core.flag(av2_model::record::F_REACHABLE),
            eval_cp: core.eval().map(i32::from),
            backed_eval: core.backed().map(i32::from),
            backed_move: !core.backed_move.is_none(),
            mate_in: core.mate().map(i32::from),
            first_line_mate: if ext.analysis.is_none() { None } else { self.graph.blob(ext.analysis).and_then(|b| first_line_mate(&b)) },
            white_to_move: white,
            nodes_invested: ext.nodes_invested,
            backed_nodes: ext.backed_nodes,
            tb_applicable: fen.as_deref().map(|f| tb_applicable(f, 6)).unwrap_or(false),
        }
    }

    fn status(&self, pos: u64) -> Status {
        if pos >= self.graph.len() { Status::Unknown } else { self.graph.core(PosId(pos)).status() }
    }
    fn visits(&self, pos: u64) -> i32 {
        if pos >= self.graph.len() { 0 } else { self.graph.ext(PosId(pos)).map(|e| e.visits).unwrap_or(0) }
    }
    fn campaign(&self, pos: u64) -> u32 {
        if pos >= self.graph.len() { 0 } else { self.graph.ext(PosId(pos)).map(|e| e.campaign).unwrap_or(0) }
    }
    fn tb_applicable(&self, pos: u64) -> bool {
        pos < self.graph.len() && self.graph.fen(PosId(pos)).map(|f| tb_applicable(&f, 6)).unwrap_or(false)
    }

    fn children(&self, pos: u64) -> Vec<(String, u64)> {
        let mut out = Vec::new();
        if pos < self.graph.len() {
            self.graph.children(PosId(pos), &mut out);
        }
        out.into_iter().map(|e| (e.mv.to_uci(), e.child.0)).collect()
    }

    fn parents(&self, pos: u64) -> Vec<u64> {
        let mut out = Vec::new();
        if pos < self.graph.len() {
            self.graph.parents(PosId(pos), &mut out);
        }
        out.into_iter().map(|p| p.0).collect()
    }

    fn queue_rank(&self, pos: u64) -> f64 {
        self.priority.and_then(|m| m.get(&pos).copied()).unwrap_or(0.0)
    }
}

/// An in-memory environment for tests and the shadow: positions are plain
/// structs, edges are lists.
#[derive(Clone, Debug, Default)]
pub struct MemEnv {
    pub positions: std::collections::BTreeMap<u64, PosInfo>,
    pub edges: std::collections::BTreeMap<u64, Vec<(String, u64)>>,
    pub tables: std::collections::BTreeSet<u64>,
    pub priority: std::collections::BTreeMap<u64, f64>,
}

impl MemEnv {
    pub fn new() -> MemEnv {
        MemEnv::default()
    }
    /// An open position with v1 defaults.
    pub fn add(&mut self, pos: u64) -> &mut PosInfo {
        self.positions.entry(pos).or_insert_with(|| PosInfo { exists: true, white_to_move: true, ..PosInfo::default() })
    }
    pub fn info(&mut self, pos: u64) -> &mut PosInfo {
        self.add(pos)
    }
    pub fn close(&mut self, pos: u64, status: Status) {
        self.add(pos).status = status;
    }
    pub fn edge(&mut self, parent: u64, mv: &str, child: u64) {
        self.add(parent);
        self.add(child);
        self.edges.entry(parent).or_default().push((mv.to_string(), child));
    }
}

impl QueueEnv for MemEnv {
    fn pos(&self, pos: u64) -> PosInfo {
        self.positions.get(&pos).cloned().unwrap_or_default()
    }
    fn children(&self, pos: u64) -> Vec<(String, u64)> {
        self.edges.get(&pos).cloned().unwrap_or_default()
    }
    fn parents(&self, pos: u64) -> Vec<u64> {
        let mut out = Vec::new();
        for (p, kids) in &self.edges {
            if kids.iter().any(|(_, c)| *c == pos) && !out.contains(p) {
                out.push(*p);
            }
        }
        out
    }
    fn server_tables_cover(&self, pos: u64) -> bool {
        self.tables.contains(&pos)
    }
    fn queue_rank(&self, pos: u64) -> f64 {
        self.priority.get(&pos).copied().unwrap_or(0.0)
    }
}
