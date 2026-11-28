//! The hooks the loop rules (steps 11 to 14 of the port: LOOP-1 to LOOP-6,
//! BKD-6.4 grounding, BKD-7.2 steps 2, 4 and 7, BKD-7.6) plug into.
//!
//! The ascent (`ascent.rs`) calls these at the points where v1's
//! `_decide_level` and `backup_backed_evals` call the loop code, in the same
//! order.  [`NoLoops`] is the loop-free behaviour of steps 1 to 9: no child
//! is marked as a repetition, no line is valued as returning, no pointer loop
//! is opened, every grounding is 2.  The agent that ports steps 11 to 14
//! implements [`LoopRules`] (with its spine cache, `_SpineCache`, as state)
//! and changes nothing else.

use crate::backed::{Backed, ChildValue, Discrepancy, Row, Showcase};
use av2_model::{GraphRead, Move, PosId};
use std::collections::BTreeMap;

/// The children of every row of a level, by parent (v1
/// `_backed_children_by_parent`).
pub type LevelChildren = BTreeMap<PosId, Vec<ChildValue>>;

/// What the loop rules decide for a level besides `_backed_for`.
#[derive(Default, Debug)]
pub struct LoopVerdict {
    /// Loop positions outside the level to look at once (`wake`).
    pub wake: Vec<PosId>,
    /// Rows the loop rule decided (`ruled`): they never wait.
    pub ruled: Vec<PosId>,
}

pub trait LoopRules {
    /// A new ascent starts (`_SpineCache()` and `loop_budget` are per call).
    fn begin_ascent(&mut self) {}

    /// `spines.wrote(key, backed_move)`: the pointer a row stores or is about
    /// to store (BKD-7.1, BKD-7.2 step 6).
    fn wrote(&mut self, _id: PosId, _backed_move: Move) {}

    /// BKD-7.2 step 2 (LOOP-1.1 `_draw_cycling_children`): mark the children
    /// whose value comes back to their parent (`cycles = true`) and the
    /// `(parent, move)` pairs whose chain runs into a loop elsewhere.
    /// Returns the parents with a marked child (`returning`, open rows only
    /// are kept by the caller) and the `elsewhere` pairs.
    fn mark_returning(&mut self, _g: &dyn GraphRead, _children: &mut LevelChildren) -> (Vec<PosId>, Vec<(PosId, Move)>) {
        (Vec::new(), Vec::new())
    }

    /// BKD-7.2 step 4 (LOOP-2.1 `_resolve_returning_children`): value the
    /// returning children of the open rows on the line that returns.
    fn value_returning(&mut self, _g: &dyn GraphRead, _children: &mut LevelChildren, _open_rows: &[PosId]) {}

    /// BKD-7.2 step 7 (LOOP-3.1, LOOP-4.1 `_open_pointer_loops`): open the
    /// pointer loops the level is about to write, or value them at their best
    /// exit; may change `decided` and append to `cycle_picks`.
    fn open_pointer_loops(
        &mut self,
        _g: &dyn GraphRead,
        _rows: &[Row],
        _children: &mut LevelChildren,
        _decided: &mut BTreeMap<PosId, Backed>,
        _showcases: &BTreeMap<PosId, Showcase>,
        _cycle_picks: &mut Vec<Discrepancy>,
    ) -> LoopVerdict {
        LoopVerdict::default()
    }
}

/// Steps 1 to 9: no loop rules.
#[derive(Default, Debug, Clone, Copy)]
pub struct NoLoops;

impl LoopRules for NoLoops {}
