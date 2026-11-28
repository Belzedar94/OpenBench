//! What the pages read: the `SiteRead` trait and the records it returns.
//!
//! The pages never see the store.  They ask `SiteRead` for page-shaped
//! records whose fields are already decided by the rules (a row's score,
//! whether its value is backed, borrowed or a repetition) and only format
//! them.  The store implements `SiteRead` on top of `av2_model::GraphRead`
//! (positions, edges) plus the queue, the projection (contributors, openings,
//! snapshots) and the move generator (legal moves and SAN); until it lands,
//! [`crate::fixture::FixtureSite`] implements it from sample data taken from
//! production's public API.
//!
//! Every field names the v1 source it ports (`views._child_moves`,
//! `roots.roots_page`, ...).  Values are from the side to move of the
//! position that owns them unless said otherwise, as in v1.
//!
//! The trait is object safe and `Send + Sync`: the HTTP handlers hold an
//! `Arc<dyn SiteRead>` and call it from many threads at once.

use serde::{Deserialize, Serialize};

/// v1 `Position.status`.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default, Serialize, Deserialize)]
pub enum Status {
    #[default]
    #[serde(rename = "UNKNOWN")]
    Unknown,
    #[serde(rename = "WHITE_WIN")]
    WhiteWin,
    #[serde(rename = "BLACK_WIN")]
    BlackWin,
    #[serde(rename = "DRAW")]
    Draw,
}

impl Status {
    pub fn v1(self) -> &'static str {
        match self {
            Status::Unknown => "UNKNOWN",
            Status::WhiteWin => "WHITE_WIN",
            Status::BlackWin => "BLACK_WIN",
            Status::Draw => "DRAW",
        }
    }
    /// The status that means "the side to move wins" for a side.
    pub fn win_for(white: bool) -> Status {
        if white { Status::WhiteWin } else { Status::BlackWin }
    }
    pub fn is_closed(self) -> bool {
        self != Status::Unknown
    }
}

impl From<av2_model::Status> for Status {
    fn from(s: av2_model::Status) -> Status {
        match s {
            av2_model::Status::Unknown => Status::Unknown,
            av2_model::Status::WhiteWin => Status::WhiteWin,
            av2_model::Status::BlackWin => Status::BlackWin,
            av2_model::Status::Draw => Status::Draw,
        }
    }
}

/// How a value is backed (v1 `backed_plies` and the "walked" light variant).
#[derive(Clone, Copy, PartialEq, Eq, Debug, Serialize, Deserialize)]
pub struct Backed {
    /// v1 `Position.backed_plies`: plies below where the value comes from.
    pub plies: u32,
    /// v1 `backed_light`: carried up a line a visitor walked, no search yet.
    pub light: bool,
}

/// One line of the raw engine output under the board (v1
/// `_analysis_lines_for_display`).
#[derive(Clone, PartialEq, Eq, Debug, Serialize, Deserialize)]
pub struct RawLine {
    pub raw: String,
    /// From the earlier, wider pass (`prior_pass`).
    pub prior: bool,
}

/// The position a page is about (v1 `explore` context, `pos` and the
/// header values).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct PositionView {
    /// `sha256(canonical_fen)`, 64 hex characters.
    pub key: String,
    /// Canonical FEN.
    pub fen: String,
    pub status: Status,
    /// v1 `Position.closure` text (`TB`, `MATE_PV`, `MINIMAX`, `TERMINAL`,
    /// `SOLVE`), `None` while open.
    pub closure: Option<String>,
    /// v1 `ingest.best_known_eval(pos)` flipped to the side to move; `None`
    /// when closed or never valued.
    pub score: Option<i32>,
    pub backed: Option<Backed>,
    /// v1 `_walked_value`: the value came up a walked line, no search here.
    pub walked: bool,
    /// v1 `Position.mate_in` (plies), closed positions only.
    pub mate_in: Option<u32>,
    /// v1 `Position.won_line` (UCI) and its SAN.
    pub won_line: Vec<String>,
    pub won_line_san: String,
    /// v1 `_arrow_move`: the move the board's arrow shows.
    pub best_move: Option<String>,
    pub visits: u64,
    pub nodes_invested: u64,
    pub time_invested: f64,
    pub analysis: Vec<RawLine>,
    /// v1 `_moves_summary`, without its "Moves here: " prefix.
    pub moves_summary: String,
}

impl PositionView {
    pub fn white_to_move(&self) -> bool {
        self.fen.split(' ').nth(1) != Some("b")
    }
}

/// What a request on a position would buy (v1 `ingest.request_ladder_state`
/// and the explorer's request row).
#[derive(Clone, PartialEq, Eq, Debug, Default, Serialize, Deserialize)]
pub struct RequestState {
    /// Label of the next rung, e.g. `"128M"`.
    pub due_label: String,
    /// v1 `frontier_exhausted`: the ladder is spent; the button queues the
    /// subtree instead.
    pub frontier_exhausted: bool,
    /// v1 `SUBTREE_TASK_CAP` as applied to this position.
    pub subtree_cap: u32,
    /// v1 `ingest.unexplored_children` count.
    pub unexplored_count: u32,
    /// v1 `_pv_verify_lines`: `(index, label)` candidates of "Verify PV".
    pub pv_verify_lines: Vec<(u32, String)>,
}

/// The live request of a position (v1 `live_request.context` /
/// `api/live-request`): the server composes the whole sentence.
#[derive(Clone, PartialEq, Eq, Debug, Serialize, Deserialize)]
pub struct LiveRequest {
    pub text: String,
    /// `cold` (waiting) or `hot` (running).
    pub chip: String,
}

/// A queued or running mark on a row (v1 `_with_pending_marks`).
#[derive(Clone, PartialEq, Eq, Debug, Serialize, Deserialize)]
pub struct Pending {
    /// `writing`, `running` or `queued`.
    pub label: String,
    pub tip: String,
}

/// One row of the move table (v1 `_child_moves` + `_with_search_depth` +
/// `_with_pending_marks`).  Rows come in display order.
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct ChildRow {
    pub uci: String,
    pub san: String,
    pub key: String,
    pub status: Status,
    pub closure: Option<String>,
    /// v1 `unexplored`: no engine has looked at this move ("unexplored"
    /// rather than "unsolved").
    pub unexplored: bool,
    /// v1 `score`, from the parent's side to move.
    pub score: Option<i32>,
    /// Plies to the end from the parent, for proven rows (v1 `mate`).
    pub mate_plies: Option<u32>,
    pub backed: Option<Backed>,
    pub borrowed: bool,
    /// v1 `claim`: an unconfirmed claim shown in italics.
    pub claim: Option<String>,
    pub repetition: bool,
    /// v1 `blocked`: the move repeats a position of the route.
    pub blocked: bool,
    pub returns: bool,
    /// v1 `own_search`: `(cp, nodes)` of this position's own search, shown in
    /// the backed chip's tooltip.
    pub own_search: Option<(i32, u64)>,
    /// v1 `ProofNode` `(pn, dn)` of the active campaign.
    pub proof: Option<(u64, u64)>,
    /// v1 `searched_nodes`: the deepest search of the child.
    pub searched_nodes: Option<u64>,
    pub visits: u64,
    pub pending: Option<Pending>,
    /// v1 `enters_opening`.
    pub enters_opening: Option<String>,
}

/// A move of the route above the board (v1 `_numbered_line`).
#[derive(Clone, PartialEq, Eq, Debug, Serialize, Deserialize)]
pub struct LineStep {
    /// `"1."`, `"1..."` or empty.
    pub num: String,
    pub san: String,
    pub uci: String,
    pub key: String,
    pub url: String,
}

/// The opening name of a position (v1 `opening` context).
#[derive(Clone, PartialEq, Eq, Debug, Default, Serialize, Deserialize)]
pub struct Opening {
    pub name: String,
    /// The catalogue name a community name replaced.
    pub catalog_name: Option<String>,
    /// Key of the position the name belongs to (link to the roots page).
    pub position_key: Option<String>,
    pub reference_line_san: Option<String>,
    pub community: bool,
    pub named_by: Option<String>,
}

/// A transposition: another parent of the position.
#[derive(Clone, PartialEq, Eq, Debug, Serialize, Deserialize)]
pub struct ParentLink {
    pub key: String,
    pub uci: String,
    pub san: String,
}

/// A legal move with its SAN (from the move generator).
#[derive(Clone, PartialEq, Eq, Debug, Serialize, Deserialize)]
pub struct LegalMove {
    pub uci: String,
    pub san: String,
}

/// Everything the explorer page of one position needs.
#[derive(Clone, PartialEq, Debug, Default)]
pub struct ExplorerView {
    pub pos: PositionView,
    pub rows: Vec<ChildRow>,
    /// Legal moves (empty for a terminal position).
    pub legal: Vec<LegalMove>,
    pub line: Vec<LineStep>,
    /// The line starts at the start position.
    pub line_from_root: bool,
    pub line_top_key: Option<String>,
    pub opening: Option<Opening>,
    pub parents: Vec<ParentLink>,
    pub request: RequestState,
    pub live: Option<LiveRequest>,
}

// ---------------------------------------------------------------- roots

/// One row of the roots page and of `api/roots` (v1 `roots.API_FIELDS`).
/// Values are from White's side.
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct RootRow {
    pub key: String,
    pub label: String,
    /// `start`, `move` or `opening`.
    pub kind: String,
    #[serde(default)]
    pub line_san: String,
    #[serde(default)]
    pub community: bool,
    #[serde(default)]
    pub named_by: Option<String>,
    #[serde(default)]
    pub present: bool,
    pub value: Option<i32>,
    pub status: Option<Status>,
    pub best_value: Option<i32>,
    pub best_status: Option<Status>,
    pub best_at: Option<String>,
    pub worst_value: Option<i32>,
    pub worst_status: Option<Status>,
    pub worst_at: Option<String>,
    pub changes: Option<u64>,
    #[serde(default)]
    pub route: Vec<String>,
    #[serde(default)]
    pub route_san: Option<String>,
    #[serde(default)]
    pub votes_up: Option<u32>,
    #[serde(default)]
    pub votes_down: Option<u32>,
    #[serde(default)]
    pub under_review: Option<bool>,
}

#[derive(Clone, PartialEq, Debug, Default)]
pub struct MainLine {
    pub value: Option<i32>,
    pub status: Option<Status>,
    pub plies: u32,
    pub san: String,
    pub end_text: Option<String>,
    pub end_key: Option<String>,
    pub at: String,
}

/// The start position's best-record history for the roots chart (v1`RootRecord` and `ProgressSnapshot`), raw: the page computes the
/// geometry (`pages::root_chart`).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct RootChartData {
    /// `(unix seconds, value from White's side, status)`, oldest first.
    pub records: Vec<(i64, i32, Status)>,
    /// `(unix seconds, cumulative engine nodes)`, oldest first.
    pub nodes: Vec<(i64, u64)>,
    /// When the chart was measured (its right edge).
    pub end: i64,
}

/// One day of `Main line by day` (v1 `MainLineSnapshot`, last of each day).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct HistoryDay {
    pub day: String,
    pub value: Option<i32>,
    #[serde(default)]
    pub status: Option<Status>,
    pub plies: u32,
    pub san: String,
}

#[derive(Clone, PartialEq, Debug, Default)]
pub struct RootsView {
    /// ISO time the best/worst tracking started.
    pub since: Option<String>,
    pub chart: Option<RootChartData>,
    pub history: Vec<HistoryDay>,
    pub history_days: u32,
    /// The start position first, then its moves.
    pub positions: Vec<RootRow>,
    pub openings: Vec<RootRow>,
    pub main_line: Option<MainLine>,
    pub max_plies: u32,
    pub name_min_votes: u32,
}

// ---------------------------------------------------------------- queue

/// One account's row of the queue page (v1 `lanes.queue_table`).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct QueueRow {
    /// Empty for anonymous visitors.
    pub user: String,
    pub waiting: u64,
    pub running: u64,
    /// Nodes asked for (sum of budgets).
    pub nodes: u64,
    /// Nodes served in the last hour.
    pub served: u64,
    /// Age of the oldest waiting request, in seconds.
    pub oldest_seconds: Option<u64>,
    /// Share of the fleet in the last hour, percent.
    pub share: f64,
}

/// One community backlog (v1 `backlog.BACKLOGS` with its counts).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct BacklogRow {
    pub name: String,
    pub title: String,
    pub about: String,
    /// Positions left to adopt.
    pub open: u64,
    /// Kept for a later round.
    #[serde(default)]
    pub held: u64,
    pub adopted: u64,
    #[serde(default)]
    pub adopters: u64,
    /// Batch sizes offered (v1 `sizes_of`).
    pub sizes: Vec<u32>,
    /// Nothing left: shown folded under `Show N fully adopted backlogs`.
    #[serde(default)]
    pub fully_adopted: bool,
}

#[derive(Clone, PartialEq, Debug, Default)]
pub struct QueueView {
    pub rows: Vec<QueueRow>,
    pub backlog: Vec<BacklogRow>,
    pub ingest_pending: u64,
    pub ingest_oldest_seconds: Option<u64>,
    pub propagation_backlog: u64,
    pub fleet_nodes_last_hour: u64,
}

// ---------------------------------------------------------------- people

/// One row of the contributors leaderboard (v1 `contributors.leaderboard`).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct ContributorRow {
    pub user: String,
    pub nodes: u64,
    pub tasks: u64,
    pub machines: u32,
}

#[derive(Clone, PartialEq, Debug, Default)]
pub struct Leaderboard {
    pub today: Vec<ContributorRow>,
    pub last_24h: Vec<ContributorRow>,
    pub all_time: Vec<ContributorRow>,
}

/// One active worker (v1 `active_workers.workers_page`).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct WorkerRow {
    pub user: String,
    pub machine: String,
    pub threads: u32,
    pub hash_mb: u32,
    pub build: String,
    /// What it searches now: the line label and the key.
    pub line: Option<String>,
    pub key: Option<String>,
    pub budget: Option<u64>,
    pub nps: Option<u64>,
}

#[derive(Clone, PartialEq, Debug, Default)]
pub struct WorkersView {
    pub workers: Vec<WorkerRow>,
}

/// The tree's headline numbers (v1 `metrics.tree_totals` + `activity_totals`).
#[derive(Clone, PartialEq, Debug, Default)]
pub struct TreeTotals {
    pub positions: u64,
    pub closed: u64,
    pub nodes: u64,
    pub analyses_24h: u64,
    pub closed_24h: u64,
    pub workers: u32,
    pub cores: u32,
}

// ---------------------------------------------------------------- feeds

/// One notice of the bell and the notifications page (v1
/// `RequestNotification`, as `notifications.identity` words it).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct Notice {
    pub key: String,
    #[serde(default)]
    pub play: String,
    pub san: String,
    pub full: String,
    /// Already worded (`3 hours ago`).
    pub ago: String,
    pub seen: bool,
}

/// A signed-in account's notices: the unseen count and the latest rows.
#[derive(Clone, PartialEq, Debug, Default)]
pub struct Notices {
    pub unseen: u32,
    pub rows: Vec<Notice>,
}

/// One event of the milestones feed (v1 `DBEvent`).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct Milestone {
    pub ts: i64,
    pub text: String,
    pub san: Option<String>,
    pub full: Option<String>,
    pub key: Option<String>,
}

/// How far a proposed campaign's line reaches (v1 `proposed_campaigns.stored`).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct Reach {
    /// The count stopped at its cap: numbers are lower bounds.
    pub capped: bool,
    pub total: u64,
    pub explored: u64,
    pub resolved: u64,
    pub solved_pct: f64,
    /// Already worded (`4 hours, 14 minutes`).
    pub measured_ago: Option<String>,
}

/// Proof numbers of a proposed campaign's root.
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct CampaignProof {
    pub proven: bool,
    pub refuted: bool,
    pub pn: Option<u64>,
    pub dn: Option<u64>,
}
// ---------------------------------------------------------------- home

/// A running community campaign (v1 `_campaign_context` cards).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct CampaignCard {
    #[serde(default)]
    pub id: u64,
    pub root_key: String,
    pub title: String,
    pub line_shown: String,
    pub votes: u64,
    pub explored: u64,
    pub total: u64,
    pub resolved: u64,
    pub solved_pct: f64,
    pub proposed_by: String,
}

/// A proposed campaign waiting for votes.
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct CampaignRow {
    pub id: u64,
    pub root_key: String,
    pub title: String,
    pub line_shown: String,
    pub proposed_by: String,
    pub votes: u64,
    #[serde(default)]
    pub reach: Option<Reach>,
    #[serde(default)]
    pub proof: Option<CampaignProof>,
}

/// A line of the "Now analyzing" / "Up next" feeds (v1 home `analyzing`,
/// `upnext`): the line label, its full text and what is being done.
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct FeedRow {
    pub key: String,
    #[serde(default)]
    pub play: String,
    pub san: String,
    pub full: String,
    /// `USER`, `AUTO`, `FILL`... (shown title-cased).
    pub source: Option<String>,
    pub opening: Option<String>,
    pub budget: Option<u64>,
    pub machine: Option<String>,
}

#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct Progress {
    pub solved_first: u32,
    pub n_first: u32,
    pub solved_pct: f64,
    pub closed_24h: u64,
    pub nodes_24h: u64,
}

/// v1 `_proof_health`.
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct ProofHealth {
    pub dn_median: u64,
    pub and_nodes: u64,
    pub thin: u64,
    pub thin_pct: f64,
    pub dn_floor: u64,
    pub saturated: u64,
    /// Median time to close a visitor's request, already worded (`2.9h`).
    pub human_close: Option<String>,
    pub human_close_samples: u64,
    pub at: String,
}

#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct Share {
    pub source: String,
    pub pct: f64,
    pub count: u64,
}

/// Closures by source (v1 `attribution_24h` / `_7d`).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct Attribution {
    pub stamped_24h: u64,
    pub total_24h: u64,
    pub rows_24h: Vec<Share>,
    pub stamped_7d: u64,
    pub total_7d: u64,
    pub rows_7d: Vec<Share>,
}

/// The home page's own sections (v1 `views.home`); the board and the first
/// moves come from `explorer(root)`, the boards from `leaderboard()`.
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct HomeView {
    /// `Y-m-d H:i` of the exact counters.
    pub counters_at: Option<String>,
    pub total: Option<u64>,
    pub closed: Option<u64>,
    pub nodes: Option<u64>,
    pub analyses: Option<u64>,
    pub requested: Option<u64>,
    pub workers: Option<u64>,
    pub cores: Option<u64>,
    pub nps: Option<u64>,
    pub positions_pm: Option<u64>,
    pub root_pn: Option<u64>,
    pub root_dn: Option<u64>,
    #[serde(default)]
    pub campaign_cards: Vec<CampaignCard>,
    pub progress: Option<Progress>,
    pub health: Option<ProofHealth>,
    pub attribution: Option<Attribution>,
    #[serde(default)]
    pub analyzing: Vec<FeedRow>,
    #[serde(default)]
    pub upnext: Vec<FeedRow>,
    #[serde(default)]
    pub campaigns_proposed: Vec<CampaignRow>,
    #[serde(default)]
    pub campaigns_proposed_total: usize,
}

/// What a page depends on, for [`SiteRead::page_version`].
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum PageKind {
    Explorer,
    Home,
    Roots,
    Queue,
    Contributors,
    Map,
    Milestones,
    Campaigns,
    /// Pages that change only with a deploy (docs).
    Static,
}

/// The read side of the site.
pub trait SiteRead: Send + Sync {
    /// The graph's commit epoch: changes whenever anything may have changed.
    fn epoch(&self) -> u64;
    /// The version of one page: the page cache and the HTTP `ETag` are keyed
    /// on it, so it must change whenever what the page shows may have
    /// changed, and should change as rarely as possible otherwise.  The
    /// store answers the explorer of `key` with the largest record version
    /// (`Core.version`, the batch sequence number of a record's last change)
    /// among the position, its children and its live request, and the global
    /// pages with the epoch of the snapshot they are built from (v1 rebuilds
    /// those every 15 to 60 s).  Committing elsewhere in the graph must not
    /// invalidate an explorer page.
    fn page_version(&self, kind: PageKind, _key: Option<&str>) -> u64 {
        if kind == PageKind::Static { 0 } else { self.epoch() }
    }
    /// The start position's key.
    fn root_key(&self) -> String;
    /// The explorer page of `key` reached by `route` (UCI moves from the
    /// start position, v1 `?play=`); `None` when the position is unknown.
    fn explorer(&self, key: &str, route: &[String]) -> Option<ExplorerView>;
    fn roots(&self) -> RootsView;
    fn queue(&self) -> QueueView;
    fn leaderboard(&self) -> Leaderboard;
    fn workers(&self) -> WorkersView;
    fn totals(&self) -> TreeTotals;
    fn home(&self) -> HomeView;
    /// The latest events, newest first (v1 shows 12).
    fn milestones(&self) -> Vec<Milestone>;
    /// Every proposed campaign, most voted first.
    fn campaigns(&self) -> Vec<CampaignRow>;
    /// The notices of account `user_id` (bell and notifications page).
    fn notices(&self, user_id: i64) -> Notices;
    /// The live request of a position, as `api/live-request` returns it.
    fn live_request(&self, key: &str) -> Option<LiveRequest>;
}
