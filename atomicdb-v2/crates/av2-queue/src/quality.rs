//! Q-8.15 / BKD-11: the quality arm, which buys from inside the backed
//! ascent.
//!
//! The rules port (`av2_rules::purchase`) decides *what* each level of the
//! ascent buys (the rung, the interior cap, the child's own investment, the
//! server floor, the order) and asks a [`TaskSink`] for the room and for each
//! purchase.  This is the queue's answer, v1's `_queue_quality_convergence`
//! and `_queue_cycle_disambiguation` task writes:
//!
//! - room = `min(hourly room of the 'quality' arm, QUALITY_QUEUE_CAP -
//!   PENDING tasks of the arm)`, 0 when the arm is off or its hour is spent
//!   (the `ARM_RATE_BUDGET` receipt is written as v1 writes it);
//! - a purchase is `get_or_create(position=child, generation=visits)` with
//!   the budget, `multipv_for(visits, budget)`, source `FILL`, arm `quality`;
//!   an existing PENDING task with a smaller budget is raised to it (and
//!   moved to the arm); either counts.  A created task enters the arm's
//!   hourly log; a raise does not (v1 counts rows by `created`).
//!
//! The writer opens a [`QualityBuyer`] around the batch's ascent, passes it
//! as the ascent's `TaskSink`, and takes the deltas and events with
//! [`QualityBuyer::finish`], to log them with the batch like any other queue
//! change.

use crate::budget::multipv_for;
use crate::consts::{ARM_QUALITY, Ms, QUALITY_QUEUE_CAP};
use crate::model::{Source, TState, Task};
use crate::state::{Delta, Event, QueueState};
use av2_rules::purchase::{Purchase, TaskSink};

/// The queue as the ascent's task sink, for one batch.
pub struct QualityBuyer<'a> {
    q: &'a mut QueueState,
    now: Ms,
    /// Purchases that counted (created or raised).
    pub made: usize,
}

impl QueueState {
    /// Open the quality arm for one batch at the batch's logical time.
    pub fn quality_buyer(&mut self, now: Ms) -> QualityBuyer<'_> {
        self.forget_tx();
        QualityBuyer { q: self, now, made: 0 }
    }
}

impl QualityBuyer<'_> {
    /// The deltas and events of the purchases, as `QueueState::apply` returns
    /// them for an input.
    pub fn finish(self) -> (Vec<Delta>, Vec<Event>) {
        self.q.finish()
    }

    /// Finish and return the fact to log for the purchases (`queue.v1`,
    /// input `Purchases`), or `None` when nothing changed.
    pub fn finish_fact(self) -> Option<av2_facts::Fact> {
        let now = self.now;
        let (deltas, events) = self.finish();
        if deltas.is_empty() && events.is_empty() {
            return None;
        }
        let applied = crate::apply::Applied { output: crate::input::Output::Ok, deltas, events };
        Some(crate::fact::record(&crate::input::Input::Purchases { now }, &applied))
    }
}

impl TaskSink for QualityBuyer<'_> {
    fn quality_room(&mut self) -> u64 {
        let hourly = match self.q.take_arm_room(ARM_QUALITY, self.now) {
            Some(0) => return 0,
            Some(r) => r,
            None => usize::MAX,
        };
        let pending = self.q.pending_of_arm(ARM_QUALITY);
        hourly.min(QUALITY_QUEUE_CAP.saturating_sub(pending)) as u64
    }

    fn buy(&mut self, p: &Purchase) -> bool {
        let child = p.child.0;
        let visits = p.generation;
        let counted = match self.q.slot(child, visits).map(|t| (t.id, t.state, t.budget)) {
            None => {
                let id = self.q.alloc_task_id();
                let mut t = Task::new(id, child, visits, p.budget, self.now);
                t.multipv = multipv_for(visits, Some(p.budget), false, None);
                t.source = Source::Fill;
                t.arm = ARM_QUALITY.to_string();
                self.q.insert_task(t);
                self.q.note_arm_created(ARM_QUALITY, self.now);
                true
            }
            Some((id, TState::Pending, budget)) if budget < p.budget => {
                let to = p.budget;
                self.q.update_task(id, |t| {
                    t.budget = to;
                    t.source = Source::Fill;
                    t.arm = ARM_QUALITY.to_string();
                });
                true
            }
            Some(_) => false,
        };
        if counted {
            self.made += 1;
        }
        counted
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::QueueConfig;
    use av2_model::PosId;
    use av2_rules::purchase::PurchaseKind;

    fn buy(child: u64, generation: i32, budget: u64) -> Purchase {
        Purchase { kind: PurchaseKind::QualityConvergence, child: PosId(child), generation, budget }
    }

    #[test]
    fn creates_raises_and_counts_like_v1() {
        let mut q = QueueState::new(QueueConfig::default());
        let mut b = q.quality_buyer(1_000);
        assert_eq!(b.quality_room(), 50, "min(120 an hour, cap 50 - 0 pending)");
        assert!(b.buy(&buy(7, 0, 128_000_000)), "created");
        assert!(!b.buy(&buy(7, 0, 128_000_000)), "same slot, same budget: nothing");
        assert!(b.buy(&buy(7, 0, 512_000_000)), "raised");
        assert!(b.buy(&buy(7, 1, 128_000_000)), "another generation is another slot");
        assert_eq!(b.made, 3);
        assert_eq!(b.quality_room(), 48, "two PENDING tasks of the arm");
        let (deltas, _events) = b.finish();
        assert!(!deltas.is_empty());
        let t = q.slot(7, 0).unwrap();
        assert_eq!((t.budget, t.source, t.arm.as_str(), t.state), (512_000_000, Source::Fill, ARM_QUALITY, TState::Pending));
        assert_eq!(q.pending_of_arm(ARM_QUALITY), 2);
    }

    #[test]
    fn the_purchases_fact_rebuilds_the_queue() {
        let mut q = QueueState::new(QueueConfig::default());
        let fact = {
            let mut b = q.quality_buyer(2_000);
            b.buy(&buy(9, 2, 512_000_000));
            b.finish_fact().expect("something changed")
        };
        let mut rebuilt = QueueState::new(QueueConfig::default());
        assert_eq!(crate::fact::replay(&mut rebuilt, [&fact]).unwrap(), 1);
        assert_eq!(rebuilt.digest(), q.digest());
        assert!(q.quality_buyer(3_000).finish_fact().is_none());
    }

    #[test]
    fn the_hourly_rate_stops_creations_but_not_raises() {
        let mut q = QueueState::new(QueueConfig::default());
        {
            let mut b = q.quality_buyer(0);
            for c in 0..50u64 {
                assert!(b.buy(&buy(100 + c, 0, 128_000_000)));
            }
            assert_eq!(b.quality_room(), 0, "the arm's own cap of 50 PENDING");
            b.finish();
        }
        assert_eq!(q.arm_budget_room(ARM_QUALITY, 1).0, Some(70));
    }
}
