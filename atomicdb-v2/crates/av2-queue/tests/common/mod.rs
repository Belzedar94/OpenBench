//! Test harness: a queue, an in-memory graph environment and a clock.
//!
//! Every input goes through `H::apply`, which also checks, after EVERY
//! input of every test, that (1) the queue invariants hold and (2) the
//! deltas of the input rebuild exactly the new state from the old one.  So
//! each ported v1 test is also a recovery test.
#![allow(dead_code)]

use av2_queue::consts::*;
use av2_queue::*;

pub const T0: Ms = 1_800_000_000_000;
pub const B128: u64 = 128_000_000;
pub const B512: u64 = 512_000_000;
pub const B2: u64 = 2_000_000_000;
pub const B10: u64 = 10_000_000_000;

pub struct H {
    pub q: QueueState,
    pub env: MemEnv,
    pub now: Ms,
    pub tok: u64,
    pub next_pos: u64,
    pub refill: Box<dyn Refill>,
}

impl Default for H {
    fn default() -> Self {
        H::new()
    }
}

impl H {
    pub fn new() -> H {
        H::with(QueueConfig::default())
    }

    pub fn with(config: QueueConfig) -> H {
        H { q: QueueState::new(config), env: MemEnv::new(), now: T0, tok: 0, next_pos: 1, refill: Box::new(NoRefill) }
    }

    pub fn config(&mut self, f: impl FnOnce(&mut QueueConfig)) {
        let mut c = self.q.config().clone();
        f(&mut c);
        self.apply(Input::SetConfig { now: self.now, config: Box::new(c) });
    }

    pub fn advance(&mut self, ms: Ms) {
        self.now += ms;
    }

    /// A fresh open position.
    pub fn pos(&mut self) -> u64 {
        let p = self.next_pos;
        self.next_pos += 1;
        self.env.add(p);
        p
    }

    /// Insert a v1 row directly (as the migration would), on a fresh
    /// position unless the closure sets `pos`.
    pub fn row(&mut self, budget: u64, f: impl FnOnce(&mut Task)) -> u64 {
        let id = self.q.max_task_id() + 1;
        let pos = self.pos();
        let mut t = Task::new(id, pos, 0, budget, self.now);
        f(&mut t);
        self.env.add(t.pos);
        self.q.import_task(t);
        id
    }

    pub fn user(&mut self, account: &str, budget: u64) -> u64 {
        let a = account.to_string();
        self.row(budget, |t| {
            t.source = Source::User;
            t.requested_by = a;
        })
    }

    pub fn auto(&mut self, budget: u64) -> u64 {
        self.row(budget, |_| {})
    }

    /// Apply an input, checking invariants and delta replay.
    pub fn apply(&mut self, input: Input) -> Applied {
        let before = self.q.clone();
        let applied = self.q.apply(&input, &self.env, self.refill.as_mut());
        // The writer puts the folded ladders into the ext records.
        for (pos, l) in &applied.ladder {
            self.env.ladders.insert(*pos, *l);
        }
        self.q.check_invariants().unwrap_or_else(|e| panic!("invariant after {input:?}: {e}"));
        let mut rebuilt = before;
        rebuilt.apply_deltas(&applied.deltas);
        assert_eq!(rebuilt.digest(), self.q.digest(), "deltas do not rebuild the state after {input:?}");
        // The fact round-trips too.
        let fact = av2_queue::fact::record(&input, &applied);
        let (back, deltas) = av2_queue::fact::decode(&fact).unwrap().unwrap();
        assert_eq!(back, input);
        assert_eq!(deltas, applied.deltas);
        applied
    }

    pub fn token(&mut self) -> String {
        self.tok += 1;
        format!("tok{:04}", self.tok)
    }

    pub fn lease_req(&mut self, account: &str, machine: &str) -> LeaseReq {
        LeaseReq {
            now: self.now,
            account: account.into(),
            machine: machine.into(),
            telemetry: Telemetry { threads: 16, ..Default::default() },
            build: LEASE_TOKEN_BUILD,
            session: format!("s-{machine}-{}", self.tok + 1),
            token: self.token(),
            max_budget: None,
            tb: false,
        }
    }

    pub fn lease_out(&mut self, r: LeaseReq) -> LeaseOut {
        match self.apply(Input::Lease(r)).output {
            Output::Lease(o) => o,
            o => panic!("{o:?}"),
        }
    }

    /// Lease once; the task id, if any.
    pub fn lease(&mut self, account: &str, machine: &str) -> Option<u64> {
        let r = self.lease_req(account, machine);
        match self.lease_out(r) {
            LeaseOut::Task { grant } => Some(grant.id),
            _ => None,
        }
    }

    pub fn lease_with(&mut self, account: &str, machine: &str, f: impl FnOnce(&mut LeaseReq)) -> Option<u64> {
        let mut r = self.lease_req(account, machine);
        f(&mut r);
        match self.lease_out(r) {
            LeaseOut::Task { grant } => Some(grant.id),
            _ => None,
        }
    }

    pub fn t(&self, id: u64) -> &Task {
        self.q.task(id).unwrap()
    }

    pub fn submit_req(&self, id: u64) -> SubmitReq {
        let t = self.t(id);
        SubmitReq {
            now: self.now,
            account: "w".into(),
            machine: t.machine.clone(),
            task: id,
            token: t.token.clone(),
            nodes: t.budget,
            elapsed: 1.0,
            lines: "[]".into(),
            ..Default::default()
        }
    }

    pub fn submit(&mut self, r: SubmitReq) -> SubmitOut {
        match self.apply(Input::Submit(r)).output {
            Output::Submit(o) => o,
            o => panic!("{o:?}"),
        }
    }

    /// Submit a leased task with its own credentials.
    pub fn complete(&mut self, id: u64) {
        let r = self.submit_req(id);
        assert!(matches!(self.submit(r), SubmitOut::Accepted { .. }));
    }

    /// Lease `n` times from distinct slots `m0..`, returning the ids.
    pub fn serve(&mut self, account: &str, n: usize) -> Vec<u64> {
        (0..n)
            .filter_map(|i| {
                let got = self.lease(account, &format!("slot{i}#{}", self.tok));
                self.advance(1);
                got
            })
            .collect()
    }

    /// Lease and complete `n` times from one slot (no running debt).
    pub fn drain(&mut self, account: &str, n: usize) -> Vec<u64> {
        let mut out = Vec::new();
        for _ in 0..n {
            match self.lease(account, "drainer") {
                Some(id) => {
                    self.complete(id);
                    self.advance(1);
                    out.push(id);
                }
                None => break,
            }
        }
        out
    }

    pub fn request(&mut self, account: &str, pos: u64, f: impl FnOnce(&mut RequestReq)) -> RequestOut {
        let mut r = RequestReq { now: self.now, account: account.into(), pos, ip: "10.0.0.1".into(), ..Default::default() };
        f(&mut r);
        match self.apply(Input::Request(r)).output {
            Output::Request(o) => o,
            o => panic!("{o:?}"),
        }
    }

    pub fn status(&mut self, input: Input) -> String {
        match self.apply(input).output {
            Output::Status { status } => status,
            o => panic!("{o:?}"),
        }
    }

    pub fn ping(&mut self, account: &str, machine: &str) {
        let r = HeartbeatReq { now: self.now, account: account.into(), machine: machine.into(), ..Default::default() };
        self.apply(Input::Heartbeat(r));
    }
}
