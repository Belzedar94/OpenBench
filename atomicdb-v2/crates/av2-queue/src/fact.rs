//! How a queue input goes into the log, and how the log rebuilds the queue.
//!
//! One fact per applied input: `Fact::Queue` (kind 11) whose payload is
//! `{"input": ..., "deltas": [...], "events": [...]}`.  The input is the
//! audit trail (and what a rule-change replay re-applies); the deltas are
//! what recovery applies, so rebuilding the queue needs neither the graph
//! nor the rules.  Logs written before kind 11 existed carried the same
//! payload as `Fact::Opaque { kind: "queue.v1" }`; `decode` still reads them.

use crate::apply::Applied;
use crate::input::Input;
use crate::state::{Delta, Event, QueueState};
use av2_facts::{Fact, RawJson};
use serde::{Deserialize, Serialize};

/// The `Opaque` kind of queue facts before kind 11.
pub const LEGACY_KIND: &str = "queue.v1";

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
    Fact::Queue(RawJson(serde_json::to_string(&r).expect("queue record serialises")))
}

/// Decode a queue fact: `(input, deltas)`.  `None` for any other fact.
pub fn decode(fact: &Fact) -> Option<Result<(Input, Vec<Delta>), String>> {
    let text = match fact {
        Fact::Queue(raw) => &raw.0,
        Fact::Opaque(o) if o.kind == LEGACY_KIND => &o.data.0,
        _ => return None,
    };
    Some(serde_json::from_str::<Record>(text).map(|r| (r.input.into_owned(), r.deltas.into_owned())).map_err(|e| e.to_string()))
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
