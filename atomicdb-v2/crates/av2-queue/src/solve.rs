//! Proof leases (Q-2.52..Q-2.61, SOL-5).

use crate::consts::*;
use crate::input::*;
use crate::model::*;
use crate::state::QueueState;

impl QueueState {
    /// SOL-5.1..SOL-5.3: free every proof lease silent for more than 20
    /// minutes (both stamps strictly older); one event when any was freed.
    pub(crate) fn recycle_stale_solve_leases(&mut self, now: Ms) -> usize {
        let stale = now - SOLVE_LEASE_MINUTES;
        let ids: Vec<u64> = self
            .idx
            .solve_leased
            .iter()
            .copied()
            .filter(|id| {
                let s = &self.solves[id];
                s.leased_at.map(|l| l < stale).unwrap_or(false) && s.heartbeat_at.map(|h| h < stale).unwrap_or(true)
            })
            .collect();
        for &id in &ids {
            self.update_solve(id, |s| s.requeue());
        }
        if !ids.is_empty() {
            self.event(
                "SOLVE_LEASE_RECYCLED",
                serde_json::json!({"freed": ids.len(), "minutes": SOLVE_LEASE_MINUTES / MINUTE}),
            );
        }
        ids.len()
    }

    /// Q-2.52..Q-2.57.
    pub(crate) fn solve_acquire(&mut self, r: &SolveAcquireReq) -> Option<SolveGrant> {
        let now = r.now;
        self.touch_worker(now, &r.account, &r.machine, &r.telemetry);
        self.recycle_stale_solve_leases(now);
        let stale = now - SOLVE_LEASE_MINUTES;
        let active = self
            .idx
            .solve_leased
            .iter()
            .map(|id| &self.solves[id])
            .filter(|s| s.machine == r.machine && s.heartbeat_at.map(|h| h >= stale).unwrap_or(false))
            .min_by_key(|s| (s.leased_at, s.id))
            .map(|s| s.id);
        if let Some(id) = active {
            let s = &self.solves[&id];
            if !r.session.is_empty() && s.session == r.session {
                return Some(grant_of(s, true));
            }
            let beat = s.heartbeat_at.or(s.leased_at);
            match beat {
                Some(b) if b < now - RESTART_RECYCLE_MINUTES => {
                    self.update_solve(id, |s| s.requeue());
                }
                _ => return None,
            }
        }
        // Q-2.56: non-debt first, then the largest budget, then the oldest id.
        let pick = self
            .idx
            .solve_pending
            .iter()
            .map(|id| &self.solves[id])
            .filter(|s| r.max_budget.map(|m| s.budget <= m).unwrap_or(true))
            .min_by_key(|s| (s.arm == ARM_DEBT, std::cmp::Reverse(s.budget), s.id))
            .map(|s| s.id)?;
        self.update_solve(pick, |s| {
            s.state = SolveState::Leased;
            s.machine = r.machine.clone();
            s.leased_at = Some(now);
            s.heartbeat_at = Some(now);
            s.token = r.token.clone();
            s.session = r.session.clone();
            s.attempts += 1;
        });
        Some(grant_of(&self.solves[&pick], false))
    }

    /// Q-2.58: exact token (no tokenless allowance).
    pub(crate) fn solve_heartbeat(&mut self, r: &SolveHeartbeatReq) -> bool {
        self.touch_worker(r.now, &r.account, &r.machine, &r.telemetry);
        let ok = self
            .solves
            .get(&r.task)
            .map(|s| s.state == SolveState::Leased && s.machine == r.machine && s.token == r.token)
            .unwrap_or(false);
        if ok {
            let now = r.now;
            self.update_solve(r.task, |s| s.heartbeat_at = Some(now));
        }
        ok
    }

    /// Q-2.59..Q-2.61.  The checks of Q-2.59 run on the current state, which
    /// in the single writer is also the state the transition applies to (the
    /// unlocked-snapshot race of OQ-17 cannot happen).
    pub(crate) fn solve_submit(&mut self, r: &SolveSubmitReq) -> SolveSubmitOut {
        let Some(s) = self.solves.get(&r.task) else { return SolveSubmitOut::UnknownTask };
        if s.state == SolveState::Completed {
            return SolveSubmitOut::Dup;
        }
        if s.state != SolveState::Leased {
            return SolveSubmitOut::NotLeased;
        }
        if r.machine.is_empty() || r.machine != s.machine {
            return SolveSubmitOut::NotYourLease;
        }
        if !s.token.is_empty() && s.token != r.token {
            return SolveSubmitOut::StaleLease;
        }
        let now = r.now;
        let proved = r.outcome == "PROVED" || r.outcome == "DISPROVED_WHITE_WIN";
        let rejected = proved && !r.verdict.verified;
        let reason: String = r.verdict.reason.chars().take(2000).collect();
        let build = {
            let sha = crate::protocol::sha_field(&r.solver_build);
            if sha.is_empty() { r.solver_build.chars().take(64).collect() } else { sha }
        };
        self.update_solve(r.task, |s| {
            s.outcome = r.outcome.clone();
            s.searched_nodes = r.nodes;
            s.elapsed = if r.elapsed.is_finite() { r.elapsed.clamp(0.0, MAX_ELAPSED_SECONDS) } else { 0.0 };
            s.solver_build = build;
            s.completed = Some(now);
            if rejected {
                s.state = SolveState::Failed;
                s.verified = false;
                s.reject_reason = reason.clone();
            } else {
                s.state = SolveState::Completed;
                s.verified = proved && r.verdict.verified;
                s.reject_reason.clear();
            }
        });
        let s = &self.solves[&r.task];
        if rejected {
            let (pos, machine) = (s.pos, s.machine.clone());
            self.event(
                "SOLVE_REJECTED",
                serde_json::json!({"task": r.task, "pos": pos, "reason": reason.chars().take(500).collect::<String>(), "machine": machine}),
            );
            return SolveSubmitOut::Rejected { pos, reason: reason.chars().take(200).collect() };
        }
        SolveSubmitOut::Completed { pos: s.pos, goal: s.goal.clone(), outcome: s.outcome.clone(), verified: s.verified }
    }

    /// A proof task proposed by an arm (Q-8.7, Q-8.14, SOL-4.6).
    pub(crate) fn insert_solve_task(&mut self, pos: u64, goal: &str, stage: &str, budget: u64, arm: &str, now: Ms) -> u64 {
        let id = self.alloc_solve_id();
        self.insert_solve(SolveTask::new(id, pos, goal, stage, budget, arm, now));
        self.note_arm_created(arm, now);
        id
    }
}

fn grant_of(s: &SolveTask, replayed: bool) -> SolveGrant {
    SolveGrant {
        id: s.id,
        pos: s.pos,
        goal: s.goal.clone(),
        stage: s.stage.clone(),
        budget: s.budget,
        token: s.token.clone(),
        replayed,
    }
}
