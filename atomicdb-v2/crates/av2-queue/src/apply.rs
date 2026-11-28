//! The single entry point of the writer: one input in, one output and its
//! deltas out.

use crate::env::QueueEnv;
use crate::input::*;
use crate::model::*;
use crate::selector::Refill;
use crate::state::{Delta, Event, QueueState};

/// The result of applying one input.
#[derive(Clone, Debug, PartialEq)]
pub struct Applied {
    pub output: Output,
    /// Every row the input changed, as logged (see `state`).
    pub deltas: Vec<Delta>,
    /// Events for the projection (v1 `DBEvent` rows).
    pub events: Vec<Event>,
}

impl QueueState {
    /// Apply one input.  Pure: the result depends only on the state, the
    /// input, the graph as `env` reads it and what `refill` proposes from
    /// them.  The deltas reproduce the change without any of those.
    pub fn apply(&mut self, input: &Input, env: &dyn QueueEnv, refill: &mut dyn Refill) -> Applied {
        self.forget_tx();
        let output = match input {
            Input::Lease(r) => Output::Lease(self.lease(r, env, refill)),
            Input::Heartbeat(r) => Output::Heartbeat(self.heartbeat(r)),
            Input::Submit(r) => Output::Submit(self.submit(r, env)),
            Input::Release(r) => Output::Release { released: self.release(r, env) },
            Input::SolveAcquire(r) => Output::SolveAcquire { task: self.solve_acquire(r) },
            Input::SolveHeartbeat(r) => Output::SolveHeartbeat { ok: self.solve_heartbeat(r) },
            Input::SolveSubmit(r) => Output::SolveSubmit(self.solve_submit(r)),
            Input::Request(r) => Output::Request(self.queue_request(r, env)),
            Input::Withdraw { now, account, task } => Output::Status { status: self.withdraw(account, *task, *now, env).into() },
            Input::Undo { account, task, .. } => Output::Status { status: self.undo(account, *task, env).into() },
            Input::ClearQueue { now, account } => {
                let (withdrawn, handed) = self.clear_own_queue(account, *now, env);
                Output::Cleared { withdrawn, handed }
            }
            Input::Bump { account, task, .. } => Output::Status { status: self.bump(account, *task).into() },
            Input::SetPref { account, pref, .. } => {
                if !account.is_empty() {
                    self.prefs.insert(account.clone(), pref.clone());
                    self.tx.prefs.insert(account.clone());
                }
                Output::Ok
            }
            Input::SetServing { account, machine, serving, .. } => {
                if !account.is_empty() {
                    let base: String = machine_base(machine).chars().take(64).collect();
                    self.serving.insert((account.clone(), base.clone()), serving.clone());
                    self.tx.serving.insert((account.clone(), base));
                }
                Output::Ok
            }
            Input::PositionClosed { now, pos } => {
                // CAS-7: every closure absorbs all PENDING tasks of the position.
                let ids: Vec<u64> = self.live_tasks_of(*pos).iter().map(|t| t.id).collect();
                Output::Absorbed { count: self.absorb(&ids, *now) }
            }
            Input::Landed { now, pos, task, arbiter } => {
                // EXP-5.2 / Q-3.13: PENDING tasks of the position the pass covers.
                let ids: Vec<u64> =
                    self.live_tasks_of(*pos).iter().filter(|t| Some(t.id) != *task && t.budget <= *arbiter).map(|t| t.id).collect();
                Output::Absorbed { count: self.absorb(&ids, *now) }
            }
            Input::Revive { positions, .. } => {
                for &p in positions {
                    self.revive(p);
                }
                Output::Ok
            }
            Input::Mint { now, plan } => {
                let (created, pending) = self.apply_mint(plan, *now, env);
                Output::Minted { created, pending }
            }
            Input::Pool { now, target } => Output::Made { count: self.top_up_pool(env, refill, *now, *target) },
            Input::Arm { now, step } => Output::Made { count: self.arm_step(step, *now, env) },
            Input::Cascade { now, pos, open_before } => Output::Made { count: self.queue_value_cascade(*pos, *open_before, *now, env) },
            Input::BulkChildren { now, account, pos, route, source, children } => Output::Made {
                count: self.enqueue_unexplored_children(
                    *pos,
                    crate::consts::UNEXPLORED_CLICK_CAP,
                    *source,
                    account,
                    route,
                    children.clone(),
                    *now,
                    env,
                ),
            },
            Input::Tick { now } => {
                let recycled_solves = self.recycle_stale_solve_leases(*now);
                let pruned = self.prune(*now, &|p| env.visits(p));
                Output::Tick { recycled_solves, pruned }
            }
            Input::SetConfig { config, .. } => {
                self.config = (**config).clone();
                self.tx.config = true;
                Output::Ok
            }
        };
        let (deltas, events) = self.finish();
        Applied { output, deltas, events }
    }
}
