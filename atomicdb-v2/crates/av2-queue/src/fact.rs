//! How a queue input goes into the log, and how the log rebuilds the queue.
//!
//! One fact per applied input: `Fact::Opaque { kind: "queue.v1", data }`
//! with `data = {"input": ..., "deltas": [...], "events": [...]}`.  The input
//! is the audit trail (and what a rule-change replay re-applies); the deltas
//! are what recovery applies, so rebuilding the queue needs neither the graph
//! nor the rules.  TODO(core): give it a kind code of its own in
//! `av2-facts` (proposed: 11 `QUEUE`); the payload stays the same.

use crate::apply::Applied;
use crate::input::Input;
use crate::state::{Delta, Event, QueueState};
use av2_facts::{Fact, Opaque, RawJson};
use serde::{Deserialize, Serialize};

pub const KIND: &str = "queue.v1";

#[derive(Serialize, Deserialize)]
struct Record<'a> {
    input: std::borrow::Cow<'a, Input>,
    deltas: std::borrow::Cow<'a, [Delta]>,
    #[serde(default)]
    events: std::borrow::Cow<'a, [Event]>,
}

/// The fact to log for an applied input.
pub fn record(input: &Input, applied: &Applied) -> Fact {
    let r = Record {
        input: std::borrow::Cow::Borrowed(input),
        deltas: std::borrow::Cow::Borrowed(&applied.deltas),
        events: std::borrow::Cow::Borrowed(&applied.events),
    };
    Fact::Opaque(Opaque { kind: KIND.to_string(), data: RawJson(serde_json::to_string(&r).expect("queue record serialises")) })
}

/// Decode a queue fact: `(input, deltas)`.  `None` for any other fact.
pub fn decode(fact: &Fact) -> Option<Result<(Input, Vec<Delta>), String>> {
    let Fact::Opaque(o) = fact else { return None };
    if o.kind != KIND {
        return None;
    }
    Some(
        serde_json::from_str::<Record>(&o.data.0)
            .map(|r| (r.input.into_owned(), r.deltas.into_owned()))
            .map_err(|e| e.to_string()),
    )
}

/// Replay the queue facts of a log onto a state (recovery after the last
/// queue snapshot).  Other facts are skipped.  Returns how many were applied.
pub fn replay<'a>(state: &mut QueueState, facts: impl IntoIterator<Item = &'a Fact>) -> Result<usize, String> {
    let mut n = 0;
    for f in facts {
        if let Some(r) = decode(f) {
            let (_, deltas) = r?;
            state.apply_deltas(&deltas);
            n += 1;
        }
    }
    Ok(n)
}
