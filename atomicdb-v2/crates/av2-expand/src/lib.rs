//! Stage (a), steps 1 and 2 of the port (rules-spec section 16): creating a
//! position (POS-2.1), expanding one (EXP-1.1) and the reachability mark
//! (POS-5.1), written against `av2_model::{GraphRead, GraphWrite}` only.
//!
//! The work splits the way the plan wants it (section 3.1):
//!
//! - **prepare** (any thread, before the writer): [`prepare`] runs the movegen
//!   and computes every child's canonical FEN, key and terminal status
//!   ([`av2_chess::expansion`]).  Pure.
//! - **apply** (the writer, inside a batch): [`expand`] checks the prepared
//!   data belongs to the position, creates the missing children, writes the
//!   edges, hands down the reachability mark and sets `expanded`.  It calls no
//!   movegen.
//!
//! # Differences from v1 that are not semantic
//!
//! - **Ids.**  v1 positions have no numeric id (the key is the primary key);
//!   v2 ids are dense in creation order.  New children are created in key
//!   order, the order of v1's bulk insert (`create_positions`), so a batch's
//!   ids do not depend on movegen order.  Edges keep v1's order: the edges a
//!   position already had, then the new ones in movegen order, which is the
//!   order v1's edge ids give them (EXP-1.1).
//! - **Tombstones.**  v1 revives a child whose `priority <= DEAD/2` when an
//!   expansion reaches it.  v2 does not store priority (owner decision D5:
//!   the selector computes it in memory), so there is nothing to write; the
//!   selector must treat "a new edge into a node" as v1's revival.  See
//!   [`Expansion::children`], which lists every child the expansion reached.
//! - **`updated`.**  Not stored (the store stamps `version`).

use av2_chess::{self as chess, Child};
use av2_model::graph::GraphError;
use av2_model::record::{F_BLACK_TO_MOVE, F_EXPANDED, F_REACHABLE, NO_SLACK};
use av2_model::{Closure, Core, Edge, Ext, GraphRead, GraphWrite, Key, Move, PosId, Proof, Status};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExpandError {
    Graph(GraphError),
    Chess(chess::ChessError),
    /// The prepared expansion was computed for another position.
    WrongPosition {
        position: PosId,
    },
    /// The position has no stored FEN to check the prepared data against.
    NoFen(PosId),
    /// A UCI move the 16-bit encoding cannot hold (never for atomic).
    BadMove(String),
    /// Two different canonical FENs share a 96-bit fingerprint.
    FingerprintCollision {
        key: Key,
        stored: PosId,
    },
}

impl std::fmt::Display for ExpandError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ExpandError::Graph(e) => write!(f, "{e}"),
            ExpandError::Chess(e) => write!(f, "{e}"),
            ExpandError::WrongPosition { position } => write!(f, "prepared expansion does not belong to {position}"),
            ExpandError::NoFen(id) => write!(f, "{id} has no stored FEN"),
            ExpandError::BadMove(m) => write!(f, "move {m:?} does not fit the 16-bit encoding"),
            ExpandError::FingerprintCollision { key, stored } => {
                write!(f, "fingerprint of {key} is already taken by {stored}, a different position")
            }
        }
    }
}
impl std::error::Error for ExpandError {}

impl From<GraphError> for ExpandError {
    fn from(e: GraphError) -> Self {
        ExpandError::Graph(e)
    }
}
impl From<chess::ChessError> for ExpandError {
    fn from(e: chess::ChessError) -> Self {
        ExpandError::Chess(e)
    }
}

pub type Result<T> = std::result::Result<T, ExpandError>;

// ------------------------------------------------------------- POS-2.1

/// A position to create, with what creation needs: its canonical FEN, key and
/// terminal status (computed by the preparers).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NewPosition {
    pub fen: String,
    pub key: Key,
    pub terminal: Option<Status>,
}

impl NewPosition {
    /// Compute everything from any FEN (canonicalises it).  Runs the movegen.
    pub fn from_fen(fen: &str) -> Result<NewPosition> {
        let fen = chess::canonical_fen(fen);
        let key = Key::of_canonical_fen(&fen);
        let terminal = chess::terminal_status(&fen)?;
        Ok(NewPosition { fen, key, terminal })
    }
}

impl From<&Child> for NewPosition {
    fn from(c: &Child) -> Self {
        NewPosition { fen: c.fen.clone(), key: c.key, terminal: c.terminal }
    }
}

/// The record a position is born with (`_get_or_create_position` /
/// `create_positions`): open, or closed as `TERMINAL` with `mate_in = 0` and
/// `clock_slack = CLOCK_SLACK_MAX` unless the result is a draw (slack NULL).
pub fn birth_core(p: &NewPosition, reachable: bool) -> Core {
    let mut core = Core::default();
    if p.fen.split_whitespace().nth(1) == Some("b") {
        core.set_flag(F_BLACK_TO_MOVE, true);
    }
    core.set_flag(F_REACHABLE, reachable);
    if let Some(status) = p.terminal {
        core.set_status(status, Closure::Terminal, Proof::None);
        core.mate_in = 0;
        core.clock_slack = if status == Status::Draw { NO_SLACK } else { chess::CLOCK_SLACK_MAX };
    }
    core
}

/// Find a position by key; a fingerprint hit whose stored FEN has another key
/// is a collision and an error, never a silent merge.
pub fn find(g: &dyn GraphRead, key: &Key) -> Result<Option<PosId>> {
    let Some(id) = g.lookup(&key.fingerprint()) else { return Ok(None) };
    if let Some(fen) = g.fen(id)
        && Key::of_canonical_fen(&fen) != *key
    {
        return Err(ExpandError::FingerprintCollision { key: *key, stored: id });
    }
    Ok(Some(id))
}

/// The root fix-up v1 applies to an existing start-position row read through
/// these functions: it gets `reachable` if it lacks it.
fn fix_root(g: &mut dyn GraphWrite, id: PosId, key: &Key) -> Result<()> {
    if *key == chess::root_key() {
        let mut core = g.core(id);
        if !core.flag(F_REACHABLE) {
            core.set_flag(F_REACHABLE, true);
            g.put_core(id, core)?;
        }
    }
    Ok(())
}

fn create(g: &mut dyn GraphWrite, p: &NewPosition, campaign: Option<u32>, born_reachable: bool) -> Result<PosId> {
    let reachable = p.key == chess::root_key() || born_reachable;
    let id = g.create(p.key.fingerprint(), Some(&p.fen), birth_core(p, reachable))?;
    if let Some(c) = campaign.filter(|&c| c != 0) {
        g.put_ext(id, Ext { campaign: c, ..Ext::default() })?;
    }
    Ok(id)
}

/// `ingest._get_or_create_position` (POS-2.1): the position for `p`, created
/// if missing with `campaign` (only at birth: an existing position keeps its
/// owner, even none) and `reachable` only for the start position.  Returns
/// `(id, created)`.  A terminal position is born closed and emits nothing.
pub fn get_or_create_position(g: &mut dyn GraphWrite, p: &NewPosition, campaign: Option<u32>) -> Result<(PosId, bool)> {
    if let Some(id) = find(g, &p.key)? {
        fix_root(g, id, &p.key)?;
        return Ok((id, false));
    }
    Ok((create(g, p, campaign, false)?, true))
}

/// `ingest.positions_for` (POS-2.1, many at once): `{key: id}` for every
/// wanted position, creating the missing ones in key order with their
/// campaign and `born_reachable`.  Existing positions are returned as they
/// are (plus the root fix-up).
pub fn positions_for(
    g: &mut dyn GraphWrite,
    wanted: &BTreeMap<Key, (NewPosition, Option<u32>)>,
    born_reachable: bool,
) -> Result<BTreeMap<Key, PosId>> {
    let mut rows = BTreeMap::new();
    let mut missing = Vec::new();
    for (key, (p, campaign)) in wanted {
        match find(g, key)? {
            Some(id) => {
                fix_root(g, id, key)?;
                rows.insert(*key, id);
            }
            None => missing.push((p, *campaign)),
        }
    }
    // BTreeMap iteration is key order: v1's `for key in sorted(new)`.
    for (p, campaign) in missing {
        let id = create(g, p, campaign, born_reachable)?;
        rows.insert(p.key, id);
    }
    Ok(rows)
}

// ------------------------------------------------------------- EXP-1.1

/// What the preparers hand the writer for one expansion.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreparedExpansion {
    /// Key of the position the children were generated from.
    pub parent: Key,
    /// Every legal move in movegen order (`av2_chess::expansion`).
    pub children: Vec<Child>,
}

/// The pure half of `ingest.expand`: run it anywhere, before the writer.
pub fn prepare(fen: &str) -> Result<PreparedExpansion> {
    let canon = chess::canonical_fen(fen);
    Ok(PreparedExpansion { parent: Key::of_canonical_fen(&canon), children: chess::expansion(&canon)? })
}

/// What an expansion did.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Expansion {
    /// False when the position was closed or already expanded (v1 returns
    /// `[]` and writes nothing).
    pub expanded: bool,
    /// The child of every legal move, in movegen order (a child reached by two
    /// moves appears twice), as v1 returns them.
    pub children: Vec<PosId>,
    /// Children created by this expansion, in creation (key) order.
    pub created: Vec<PosId>,
    /// Edges added (moves that had no edge yet).
    pub new_edges: usize,
    /// Children that got the reachability mark from this parent.
    pub marked_reachable: Vec<PosId>,
}

/// `ingest.expand` (EXP-1.1), the writer half.  Runs only on an open,
/// unexpanded position.  Creates the missing children with the parent's
/// campaign and `born_reachable = parent.reachable` (terminal children are
/// born closed), writes one edge per legal move that has none (existing edges
/// first, new ones in movegen order), marks every child reachable when the
/// parent is (POS-5.1, one ply), and sets `expanded`.
pub fn expand(g: &mut dyn GraphWrite, pos: PosId, prepared: &PreparedExpansion) -> Result<Expansion> {
    let core = g.core(pos);
    if core.expanded() || core.status() != Status::Unknown {
        return Ok(Expansion::default());
    }
    let fen = g.fen(pos).ok_or(ExpandError::NoFen(pos))?;
    if Key::of_canonical_fen(&fen) != prepared.parent {
        return Err(ExpandError::WrongPosition { position: pos });
    }
    let campaign = g.ext(pos).map(|e| e.campaign).filter(|&c| c != 0);
    let reachable = core.flag(F_REACHABLE);

    let mut wanted = BTreeMap::new();
    for c in &prepared.children {
        wanted.entry(c.key).or_insert_with(|| (NewPosition::from(c), campaign));
    }
    let before = g.len();
    let rows = positions_for(g, &wanted, reachable)?;
    let mut created: Vec<PosId> = rows.values().copied().filter(|id| id.0 >= before).collect();
    created.sort();

    // Edges: one per legal move, `ignore_conflicts` on (parent, move).
    let mut edges = Vec::new();
    g.children(pos, &mut edges);
    let had = edges.len();
    let mut children = Vec::with_capacity(prepared.children.len());
    for c in &prepared.children {
        let child = rows[&c.key];
        children.push(child);
        let mv = Move::parse(&c.uci).map_err(|_| ExpandError::BadMove(c.uci.clone()))?;
        if !edges.iter().any(|e| e.mv == mv) {
            edges.push(Edge { child, mv });
        }
    }
    let new_edges = edges.len() - had;
    if new_edges > 0 {
        g.set_children(pos, &edges)?;
    }

    // Reachability, one ply (in key order, as v1's `sorted(unmarked)`).
    let mut marked_reachable = Vec::new();
    if reachable {
        for id in rows.values() {
            let mut c = g.core(*id);
            if !c.flag(F_REACHABLE) {
                c.set_flag(F_REACHABLE, true);
                g.put_core(*id, c)?;
                marked_reachable.push(*id);
            }
        }
    }

    let mut core = g.core(pos);
    core.set_flag(F_EXPANDED, true);
    g.put_core(pos, core)?;
    Ok(Expansion { expanded: true, children, created, new_edges, marked_reachable })
}

// ------------------------------------------------------------- POS-5.1

/// `backfill_reachable` (POS-5.1): recompute the whole column.  Clears every
/// mark, then walks from the start position through **open** parents only:
/// a closed node is marked but nothing is inherited through it.  Returns
/// `(cleared, marked)`.  A store without the start position is left alone.
pub fn backfill_reachable(g: &mut dyn GraphWrite) -> Result<(u64, u64)> {
    let Some(root) = find(g, &chess::root_key())? else { return Ok((0, 0)) };
    let mut cleared = 0;
    for i in 0..g.len() {
        let id = PosId(i);
        let mut c = g.core(id);
        if c.flag(F_REACHABLE) {
            c.set_flag(F_REACHABLE, false);
            g.put_core(id, c)?;
            cleared += 1;
        }
    }
    let mut marked = 0;
    let mut seen = BTreeSet::new();
    let mut queue = VecDeque::new();
    seen.insert(root);
    queue.push_back(root);
    let mut kids = Vec::new();
    while let Some(id) = queue.pop_front() {
        let mut c = g.core(id);
        c.set_flag(F_REACHABLE, true);
        g.put_core(id, c)?;
        marked += 1;
        if c.status() != Status::Unknown {
            continue;
        }
        kids.clear();
        g.children(id, &mut kids);
        for e in &kids {
            if seen.insert(e.child) {
                queue.push_back(e.child);
            }
        }
    }
    Ok((cleared, marked))
}
