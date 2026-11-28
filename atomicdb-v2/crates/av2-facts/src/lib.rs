//! The facts of the AtomicDB v2 log.
//!
//! Facts are what arrives from outside and is never recomputed (plan section
//! 2.1): searches, proofs, requests, lease events, and the positions and
//! edges they create.  The graph columns, the queue state and the relational
//! projection are views; the facts are the system of record.  **This is the
//! only format that has to live for years**, so it is designed for that:
//!
//! - **Envelope.**  Each fact is `[kind u16][version u16][len u32][payload]`,
//!   little-endian.  A reader can skip a fact it does not know without
//!   parsing it, and keeps it verbatim (`Fact::Unknown`), so a tool from
//!   today can copy tomorrow's log without losing anything.
//! - **Payload.**  UTF-8 JSON object.  Readable with `jq` after
//!   `av2-replay dump`, compresses well in the archive, and evolves the way
//!   JSON does: new fields are optional (`#[serde(default)]`), unknown
//!   fields are ignored, nothing is ever renamed.  Raw worker payloads are
//!   kept byte for byte (`RawJson`).
//! - **Kind codes** are never reused.  A change that cannot be expressed by
//!   adding an optional field gets a new kind (or a bumped `version` that old
//!   readers will still parse, because they ignore what they do not know).
//!
//! The store never parses facts; it writes the bytes the writer hands it
//! into the log frame next to the redo records.

use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use std::collections::BTreeMap;

/// Envelope header size.
pub const HEADER: usize = 8;

/// A JSON value kept exactly as received (key order, spacing, numbers).
#[derive(Clone, PartialEq, Eq, Debug, Default)]
pub struct RawJson(pub String);

impl Serialize for RawJson {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let text = if self.0.is_empty() { "null" } else { &self.0 };
        let raw = serde_json::value::RawValue::from_string(text.to_string()).map_err(serde::ser::Error::custom)?;
        raw.serialize(s)
    }
}
impl<'de> Deserialize<'de> for RawJson {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let raw: Box<serde_json::value::RawValue> = Deserialize::deserialize(d)?;
        Ok(RawJson(raw.get().to_string()))
    }
}

/// Where a set of edges came from (v1 creates edges in four places).
#[derive(Clone, Copy, PartialEq, Eq, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EdgeOrigin {
    /// The full legal move list of a position, at expansion.
    Expansion,
    /// One edge of a materialised mate line.
    MateLine,
    /// A position opened from the explorer by a person.
    Click,
    /// A relink of an existing position under another parent.
    Relink,
    /// Loaded from a v1 export.
    Import,
    /// Anything a later version adds: read as this.
    #[serde(other)]
    Other,
}

/// The import or start of a store.
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct Genesis {
    /// What the store was built from ("pg-export", "empty", "crashtest"...).
    pub source: String,
    #[serde(default)]
    pub created_unix: i64,
    #[serde(default)]
    pub note: String,
    /// Row counts, checksums of the input files, etc.
    #[serde(default)]
    pub counts: BTreeMap<String, u64>,
}

/// A position entered the graph.  Written by the writer for every position
/// it creates, so that a replay can check it assigns the same ids.
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct PositionCreated {
    pub id: u64,
    /// 64 hex characters (or 24 for a slice exported with key prefixes).
    pub key: String,
    #[serde(default)]
    pub fen: Option<String>,
}

#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct EdgesAdded {
    pub parent: u64,
    /// `(move_uci, child id)` in block order.
    pub edges: Vec<(String, u64)>,
    pub origin: Option<EdgeOrigin>,
}

/// A worker's search result, as accepted (v1: `IngestJob.payload` plus the
/// task columns it completes).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct SearchLanded {
    pub task: u64,
    /// Key of the searched position.
    pub position: String,
    #[serde(default)]
    pub budget_nodes: u64,
    #[serde(default)]
    pub multipv: u32,
    #[serde(default)]
    pub nodes: u64,
    #[serde(default)]
    pub time_s: f64,
    #[serde(default)]
    pub engine_sha: String,
    #[serde(default)]
    pub net_sha: String,
    /// Authenticated account that delivered it (`AnalysisTask.delivered_by`).
    #[serde(default)]
    pub account: String,
    #[serde(default)]
    pub machine: String,
    /// Wall clock at the server when the submit arrived (data, never read by rules).
    #[serde(default)]
    pub received_unix_ms: i64,
    /// The submit body, byte for byte.
    #[serde(default)]
    pub payload: RawJson,
}

/// A closure the server accepted (v1: the `status/closure/proof/won_line/
/// mate_in/clock_slack` columns and `ClosureProvenance`).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct ProofAccepted {
    pub position: String,
    /// v1 strings: `WHITE_WIN`, `BLACK_WIN`, `DRAW`.
    pub status: String,
    /// v1 strings: `TB`, `MATE_PV`, `MINIMAX`, `TERMINAL`, `SOLVE`.
    pub closure: String,
    #[serde(default)]
    pub proof: String,
    #[serde(default)]
    pub mate_in: Option<u32>,
    #[serde(default)]
    pub clock_slack: Option<i32>,
    #[serde(default)]
    pub won_line: Option<String>,
    /// Attribution (`ClosureProvenance.source`).
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub task: Option<u64>,
    #[serde(default)]
    pub account: String,
    #[serde(default)]
    pub engine_sha: String,
    /// The certificate or verdict the server produced, if any.
    #[serde(default)]
    pub evidence: Option<RawJson>,
}

#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct ProofRevoked {
    pub position: String,
    #[serde(default)]
    pub closure: String,
    #[serde(default)]
    pub reason: String,
}

/// A task entered the queue (v1 `AnalysisTask` creation or revival).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct RequestQueued {
    pub task: u64,
    pub position: String,
    pub budget_nodes: u64,
    #[serde(default)]
    pub multipv: u32,
    #[serde(default)]
    pub generation: i32,
    /// `USER`, `FILL`, `AUTO`.
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub arm: String,
    #[serde(default)]
    pub account: String,
    #[serde(default)]
    pub background: bool,
    #[serde(default)]
    pub route: String,
    #[serde(default)]
    pub then_verify: bool,
}

#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct RequestChanged {
    pub task: u64,
    /// `cancel`, `bump`, `join`, `budget`... (v1 `views.api_queue_*`).
    pub action: String,
    #[serde(default)]
    pub account: String,
    #[serde(default)]
    pub value: Option<RawJson>,
}

/// A lease event (v1 `AnalysisTask.leased_at / lease_token / lease_session /
/// lease_heartbeat_at` and the release paths).
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct LeaseEvent {
    pub task: u64,
    /// `leased`, `heartbeat`, `released`, `expired`.
    pub event: String,
    #[serde(default)]
    pub machine: String,
    #[serde(default)]
    pub account: String,
    #[serde(default)]
    pub token: String,
    #[serde(default)]
    pub session: String,
    #[serde(default)]
    pub at_unix_ms: i64,
    #[serde(default)]
    pub reason: String,
}

/// Something v2 does not model yet (a `DBEvent`, a vote...), kept as JSON.
#[derive(Clone, PartialEq, Debug, Default, Serialize, Deserialize)]
pub struct Opaque {
    pub kind: String,
    #[serde(default)]
    pub data: RawJson,
}

#[derive(Clone, PartialEq, Debug)]
pub enum Fact {
    Genesis(Genesis),
    PositionCreated(PositionCreated),
    EdgesAdded(EdgesAdded),
    SearchLanded(SearchLanded),
    ProofAccepted(ProofAccepted),
    ProofRevoked(ProofRevoked),
    RequestQueued(RequestQueued),
    RequestChanged(RequestChanged),
    LeaseEvent(LeaseEvent),
    Opaque(Opaque),
    /// A kind this build does not know, kept byte for byte.
    Unknown {
        kind: u16,
        version: u16,
        payload: Vec<u8>,
    },
}

/// Kind codes.  Never reuse or renumber one.
pub mod kind {
    pub const GENESIS: u16 = 1;
    pub const POSITION_CREATED: u16 = 2;
    pub const EDGES_ADDED: u16 = 3;
    pub const SEARCH_LANDED: u16 = 4;
    pub const PROOF_ACCEPTED: u16 = 5;
    pub const PROOF_REVOKED: u16 = 6;
    pub const REQUEST_QUEUED: u16 = 7;
    pub const REQUEST_CHANGED: u16 = 8;
    pub const LEASE_EVENT: u16 = 9;
    pub const OPAQUE: u16 = 10;
}

/// Current payload version written for every kind.
pub const VERSION: u16 = 1;

#[derive(Debug)]
pub enum FactError {
    Truncated { at: usize },
    Payload { kind: u16, at: usize, err: serde_json::Error },
}
impl std::fmt::Display for FactError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FactError::Truncated { at } => write!(f, "fact envelope truncated at byte {at}"),
            FactError::Payload { kind, at, err } => write!(f, "fact kind {kind} at byte {at}: {err}"),
        }
    }
}
impl std::error::Error for FactError {}

impl Fact {
    pub fn kind(&self) -> u16 {
        match self {
            Fact::Genesis(_) => kind::GENESIS,
            Fact::PositionCreated(_) => kind::POSITION_CREATED,
            Fact::EdgesAdded(_) => kind::EDGES_ADDED,
            Fact::SearchLanded(_) => kind::SEARCH_LANDED,
            Fact::ProofAccepted(_) => kind::PROOF_ACCEPTED,
            Fact::ProofRevoked(_) => kind::PROOF_REVOKED,
            Fact::RequestQueued(_) => kind::REQUEST_QUEUED,
            Fact::RequestChanged(_) => kind::REQUEST_CHANGED,
            Fact::LeaseEvent(_) => kind::LEASE_EVENT,
            Fact::Opaque(_) => kind::OPAQUE,
            Fact::Unknown { kind, .. } => *kind,
        }
    }

    pub fn name(&self) -> &'static str {
        match self {
            Fact::Genesis(_) => "genesis",
            Fact::PositionCreated(_) => "position_created",
            Fact::EdgesAdded(_) => "edges_added",
            Fact::SearchLanded(_) => "search_landed",
            Fact::ProofAccepted(_) => "proof_accepted",
            Fact::ProofRevoked(_) => "proof_revoked",
            Fact::RequestQueued(_) => "request_queued",
            Fact::RequestChanged(_) => "request_changed",
            Fact::LeaseEvent(_) => "lease_event",
            Fact::Opaque(_) => "opaque",
            Fact::Unknown { .. } => "unknown",
        }
    }

    fn payload_json(&self) -> Result<Vec<u8>, serde_json::Error> {
        match self {
            Fact::Genesis(x) => serde_json::to_vec(x),
            Fact::PositionCreated(x) => serde_json::to_vec(x),
            Fact::EdgesAdded(x) => serde_json::to_vec(x),
            Fact::SearchLanded(x) => serde_json::to_vec(x),
            Fact::ProofAccepted(x) => serde_json::to_vec(x),
            Fact::ProofRevoked(x) => serde_json::to_vec(x),
            Fact::RequestQueued(x) => serde_json::to_vec(x),
            Fact::RequestChanged(x) => serde_json::to_vec(x),
            Fact::LeaseEvent(x) => serde_json::to_vec(x),
            Fact::Opaque(x) => serde_json::to_vec(x),
            Fact::Unknown { payload, .. } => Ok(payload.clone()),
        }
    }

    /// Append the envelope of this fact to `out`.
    pub fn encode(&self, out: &mut Vec<u8>) {
        let payload = self.payload_json().expect("facts always serialise");
        let version = match self {
            Fact::Unknown { version, .. } => *version,
            _ => VERSION,
        };
        out.extend_from_slice(&self.kind().to_le_bytes());
        out.extend_from_slice(&version.to_le_bytes());
        out.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        out.extend_from_slice(&payload);
    }

    /// One JSON object per fact: `{"kind":"search_landed","v":1,"fact":{...}}`.
    pub fn to_json_line(&self) -> String {
        let payload = self.payload_json().unwrap_or_default();
        let body = std::str::from_utf8(&payload)
            .ok()
            .filter(|s| serde_json::from_str::<serde::de::IgnoredAny>(s).is_ok())
            .map(str::to_string)
            .unwrap_or_else(|| serde_json::to_string(&String::from_utf8_lossy(&payload)).unwrap());
        let version = match self {
            Fact::Unknown { version, .. } => *version,
            _ => VERSION,
        };
        format!("{{\"kind\":\"{}\",\"code\":{},\"v\":{},\"fact\":{}}}", self.name(), self.kind(), version, body)
    }
}

fn parse<T: for<'de> Deserialize<'de>>(kind: u16, at: usize, b: &[u8]) -> Result<T, FactError> {
    serde_json::from_slice(b).map_err(|err| FactError::Payload { kind, at, err })
}

fn decode_one(kind: u16, version: u16, at: usize, b: &[u8]) -> Result<Fact, FactError> {
    Ok(match kind {
        kind::GENESIS => Fact::Genesis(parse(kind, at, b)?),
        kind::POSITION_CREATED => Fact::PositionCreated(parse(kind, at, b)?),
        kind::EDGES_ADDED => Fact::EdgesAdded(parse(kind, at, b)?),
        kind::SEARCH_LANDED => Fact::SearchLanded(parse(kind, at, b)?),
        kind::PROOF_ACCEPTED => Fact::ProofAccepted(parse(kind, at, b)?),
        kind::PROOF_REVOKED => Fact::ProofRevoked(parse(kind, at, b)?),
        kind::REQUEST_QUEUED => Fact::RequestQueued(parse(kind, at, b)?),
        kind::REQUEST_CHANGED => Fact::RequestChanged(parse(kind, at, b)?),
        kind::LEASE_EVENT => Fact::LeaseEvent(parse(kind, at, b)?),
        kind::OPAQUE => Fact::Opaque(parse(kind, at, b)?),
        _ => Fact::Unknown { kind, version, payload: b.to_vec() },
    })
}

pub fn encode_all(facts: &[Fact]) -> Vec<u8> {
    let mut out = Vec::new();
    for f in facts {
        f.encode(&mut out);
    }
    out
}

/// Walk the envelopes of a facts section without parsing payloads.
pub fn envelopes(b: &[u8]) -> impl Iterator<Item = Result<(u16, u16, usize, &[u8]), FactError>> {
    let mut at = 0usize;
    std::iter::from_fn(move || {
        if at >= b.len() {
            return None;
        }
        if at + HEADER > b.len() {
            let e = FactError::Truncated { at };
            at = b.len();
            return Some(Err(e));
        }
        let kind = u16::from_le_bytes([b[at], b[at + 1]]);
        let version = u16::from_le_bytes([b[at + 2], b[at + 3]]);
        let len = u32::from_le_bytes(b[at + 4..at + 8].try_into().unwrap()) as usize;
        if at + HEADER + len > b.len() {
            let e = FactError::Truncated { at };
            at = b.len();
            return Some(Err(e));
        }
        let item = (kind, version, at, &b[at + HEADER..at + HEADER + len]);
        at += HEADER + len;
        Some(Ok(item))
    })
}

pub fn decode_all(b: &[u8]) -> Result<Vec<Fact>, FactError> {
    envelopes(b).map(|r| r.and_then(|(k, v, at, p)| decode_one(k, v, at, p))).collect()
}

impl<'de> Deserialize<'de> for Fact {
    /// From a `to_json_line` object (used by tools that read dumps back).
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        struct Line {
            code: u16,
            #[serde(default = "one")]
            v: u16,
            fact: Box<serde_json::value::RawValue>,
        }
        fn one() -> u16 {
            1
        }
        let l = Line::deserialize(d)?;
        decode_one(l.code, l.v, 0, l.fact.get().as_bytes()).map_err(D::Error::custom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Vec<Fact> {
        vec![
            Fact::Genesis(Genesis {
                source: "pg-export".into(),
                created_unix: 1,
                note: String::new(),
                counts: [("positions".to_string(), 3u64)].into(),
            }),
            Fact::PositionCreated(PositionCreated { id: 7, key: "ab".repeat(32), fen: Some("8/8/8/8/8/8/8/K6k w - - 0 1".into()) }),
            Fact::EdgesAdded(EdgesAdded { parent: 1, edges: vec![("e2e4".into(), 7)], origin: Some(EdgeOrigin::Expansion) }),
            Fact::SearchLanded(SearchLanded {
                task: 9,
                position: "cd".repeat(32),
                budget_nodes: 128_000_000,
                payload: RawJson("{\"z\": 1, \"a\":[1,2.50]}".into()),
                ..Default::default()
            }),
            Fact::ProofAccepted(ProofAccepted {
                position: "ef".repeat(32),
                status: "WHITE_WIN".into(),
                closure: "MATE_PV".into(),
                mate_in: Some(9),
                ..Default::default()
            }),
            Fact::LeaseEvent(LeaseEvent { task: 9, event: "leased".into(), ..Default::default() }),
            Fact::Opaque(Opaque { kind: "dbevent".into(), data: RawJson("{\"k\":\"WALL\"}".into()) }),
            Fact::Unknown { kind: 999, version: 3, payload: b"{\"future\":true}".to_vec() },
        ]
    }

    #[test]
    fn roundtrip_binary() {
        let facts = sample();
        let b = encode_all(&facts);
        assert_eq!(decode_all(&b).unwrap(), facts);
        // Raw payloads keep their exact bytes.
        if let Fact::SearchLanded(s) = &decode_all(&b).unwrap()[3] {
            assert_eq!(s.payload.0, "{\"z\": 1, \"a\":[1,2.50]}");
        } else {
            panic!()
        }
    }

    #[test]
    fn roundtrip_json_lines() {
        for f in sample() {
            let line = f.to_json_line();
            let back: Fact = serde_json::from_str(&line).unwrap();
            assert_eq!(back, f, "{line}");
        }
    }

    #[test]
    fn forward_compatible() {
        // A newer writer added a field and a new edge origin: today's reader accepts both.
        let payload = br#"{"parent":1,"edges":[["e2e4",2]],"origin":"teleport","added_in_v9":[1,2]}"#;
        let mut b = Vec::new();
        b.extend_from_slice(&kind::EDGES_ADDED.to_le_bytes());
        b.extend_from_slice(&9u16.to_le_bytes());
        b.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        b.extend_from_slice(payload);
        let f = decode_all(&b).unwrap();
        assert_eq!(f, vec![Fact::EdgesAdded(EdgesAdded { parent: 1, edges: vec![("e2e4".into(), 2)], origin: Some(EdgeOrigin::Other) })]);
        // Missing optional fields default.
        let f: SearchLanded = serde_json::from_str(r#"{"task":1,"position":"x"}"#).unwrap();
        assert_eq!(f.payload, RawJson::default());
    }

    #[test]
    fn truncation_is_reported() {
        let mut b = encode_all(&sample());
        b.truncate(b.len() - 3);
        assert!(matches!(decode_all(&b), Err(FactError::Truncated { .. })));
    }
}
