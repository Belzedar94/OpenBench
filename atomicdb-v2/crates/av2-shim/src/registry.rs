//! The entry points the shim can call, by v1 name.
//!
//! An entry is a v1 function as v2 implements it: it gets the graph loaded
//! from v1's rows, the call's arguments as JSON (the Python adapter encodes
//! them, see `atomicdb/v2shim/bridge.py`), and returns a JSON result.  What
//! it changed in the graph is collected by the caller and written back.
//!
//! # Registering a ported function
//!
//! One line in [`ENTRIES`]:
//!
//! ```text
//! entry!("ingest.backup_backed_evals", av2_rules::shim::backup_backed_evals),
//! ```
//!
//! where the function has the signature [`EntryFn`].  The name is the v1
//! dotted name (`module.function` under `atomicdb/`): the Python adapter
//! routes every call of that function to v2 as soon as the name is listed,
//! and `av2-golden` uses `propagation_queue.drain_batch` and
//! `ingest_queue.apply_job` for the recorded batches.  [`Args`] decodes the
//! usual argument shapes in a line or two.

use crate::graph::{Changes, ShimGraph};
use crate::v1::MapError;
use av2_facts::Fact;
use av2_model::graph::GraphError;
use av2_model::{GraphRead, Move, PosId};
use av2_rules::RulesError;
use serde_json::{Map, Value, json};

/// What an entry gets besides its arguments.
pub struct Ctx<'a> {
    pub graph: &'a mut ShimGraph,
    /// Facts the entry emits (projected by the Python adapter: an `Opaque`
    /// fact of kind `dbevent` becomes a `DBEvent` row).
    pub facts: Vec<Fact>,
    /// Extra v1 tables the caller exported (`Entry::tables`), by table name.
    pub tables: &'a Map<String, Value>,
    /// Rows for tables outside the graph that the entry wants written back
    /// (for example `atomicdb_proofnode`), by table name: created, or
    /// updated by primary key (`atomicdb/v2shim/bridge.py`, `WRITEBACK` for
    /// anything else).  Two keys are special: `$out` (`{"report": {...}}`)
    /// fills a dict or list the caller passed, by keyword or position, as
    /// v1's out-parameters do; `$calls` (`[{"name": "proof.refresh_proof_numbers",
    /// "args": [...]}]`) asks the adapter to make follow-up v1 calls after
    /// the write-back (side effects another port owns; they go to v2 once
    /// that port is registered).
    pub writes: Map<String, Value>,
}

#[derive(Debug)]
pub enum EntryError {
    /// No entry by that name: the v1 function is not ported yet.
    NotPorted(String),
    /// The arguments do not have the expected shape.
    BadArgs(String),
    Rules(RulesError),
    Graph(GraphError),
    /// A v1 value the v2 records cannot hold.
    Unrepresentable(Vec<MapError>),
}

impl std::fmt::Display for EntryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EntryError::NotPorted(n) => write!(f, "not ported to v2: {n}"),
            EntryError::BadArgs(s) => write!(f, "bad arguments: {s}"),
            EntryError::Rules(e) => write!(f, "rules: {e}"),
            EntryError::Graph(e) => write!(f, "graph: {e}"),
            EntryError::Unrepresentable(e) => {
                write!(f, "not representable in v2:")?;
                for x in e {
                    write!(f, " [{x}]")?;
                }
                Ok(())
            }
        }
    }
}
impl std::error::Error for EntryError {}

impl EntryError {
    pub fn code(&self) -> &'static str {
        match self {
            EntryError::NotPorted(_) => "not_ported",
            EntryError::BadArgs(_) => "bad_args",
            EntryError::Rules(RulesError::NotImplemented(_)) => "not_ported",
            EntryError::Rules(_) => "rules",
            EntryError::Graph(_) => "graph",
            EntryError::Unrepresentable(_) => "unrepresentable",
        }
    }
}

impl From<RulesError> for EntryError {
    fn from(e: RulesError) -> Self {
        EntryError::Rules(e)
    }
}
impl From<GraphError> for EntryError {
    fn from(e: GraphError) -> Self {
        EntryError::Graph(e)
    }
}

pub type EntryFn = fn(&mut Ctx<'_>, &Args) -> Result<Value, EntryError>;

pub struct Entry {
    /// v1 dotted name, `module.function` under `atomicdb/`.
    pub name: &'static str,
    pub run: EntryFn,
    /// v1 tables besides positions and edges that the caller must export.
    pub tables: &'static [&'static str],
    pub doc: &'static str,
}

/// `entry!(name, function)` or `entry!(name, function, tables = [...])`.
#[macro_export]
macro_rules! entry {
    ($name:expr, $f:path) => {
        $crate::registry::Entry { name: $name, run: $f, tables: &[], doc: "" }
    };
    ($name:expr, $f:path, tables = [$($t:expr),* $(,)?]) => {
        $crate::registry::Entry { name: $name, run: $f, tables: &[$($t),*], doc: "" }
    };
}

/// Every entry v2 has.  Ported v1 functions go in the second block, one
/// line each.
pub static ENTRIES: &[Entry] = &[
    // ---- the shim's own entries (self-tests of the bridge) ----
    entry!("shim.echo", shim_echo),
    entry!("shim.put_rows", shim_put_rows),
    entry!("shim.create", shim_create),
    entry!("shim.fact", shim_fact),
    entry!("shim.tables", shim_tables, tables = ["atomicdb_dbevent"]),
    entry!("shim.out", shim_out),
    // ---- ported v1 functions (one line each) ----
];

pub fn find(name: &str) -> Option<&'static Entry> {
    ENTRIES.iter().find(|e| e.name == name)
}

pub fn names() -> Vec<&'static str> {
    ENTRIES.iter().map(|e| e.name).collect()
}

/// The outcome of one call.
pub struct Outcome {
    pub result: Value,
    pub changes: Changes,
    pub facts: Vec<Fact>,
    pub writes: Map<String, Value>,
}

/// Call an entry on a loaded graph.
pub fn call(name: &str, graph: &mut ShimGraph, args: &Value, tables: &Map<String, Value>) -> Result<Outcome, EntryError> {
    let entry = find(name).ok_or_else(|| EntryError::NotPorted(name.to_string()))?;
    let args = Args::new(args)?;
    let mut ctx = Ctx { graph, facts: Vec::new(), tables, writes: Map::new() };
    let result = (entry.run)(&mut ctx, &args)?;
    let Ctx { facts, writes, .. } = ctx;
    Ok(Outcome { result, changes: graph.changes(), facts, writes })
}

// ---------------------------------------------------------------- args

/// The arguments of a v1 call: `{"args": [...], "kwargs": {...}}`, with
/// positions encoded as `{"$pos": key}`, model instances as
/// `{"$model": "app.Model", "pk": ..., "fields": {...}}`, sets as
/// `{"$set": [...]}`, tuples as `{"$tuple": [...]}`.
pub struct Args {
    pub args: Vec<Value>,
    pub kwargs: Map<String, Value>,
}

impl Args {
    pub fn new(v: &Value) -> Result<Args, EntryError> {
        match v {
            Value::Null => Ok(Args { args: vec![], kwargs: Map::new() }),
            Value::Object(o) => Ok(Args {
                args: o.get("args").and_then(Value::as_array).cloned().unwrap_or_default(),
                kwargs: o.get("kwargs").and_then(Value::as_object).cloned().unwrap_or_default(),
            }),
            Value::Array(a) => Ok(Args { args: a.clone(), kwargs: Map::new() }),
            _ => Err(EntryError::BadArgs("expected {\"args\": [...], \"kwargs\": {...}}".into())),
        }
    }

    /// Positional argument `i`, or keyword `name`.
    pub fn get(&self, i: usize, name: &str) -> Option<&Value> {
        self.args.get(i).or_else(|| self.kwargs.get(name))
    }

    pub fn require(&self, i: usize, name: &str) -> Result<&Value, EntryError> {
        self.get(i, name).ok_or_else(|| EntryError::BadArgs(format!("missing argument {i} ({name})")))
    }

    /// A position argument (`{"$pos": key}`, a bare key, or a model of Position).
    pub fn pos(&self, g: &ShimGraph, i: usize, name: &str) -> Result<PosId, EntryError> {
        pos_of(g, self.require(i, name)?)
    }

    /// A list (or set, tuple, queryset) of positions.
    pub fn positions(&self, g: &ShimGraph, i: usize, name: &str) -> Result<Vec<PosId>, EntryError> {
        items(self.require(i, name)?)?.iter().map(|v| pos_of(g, v)).collect()
    }

    pub fn i64_or(&self, i: usize, name: &str, default: i64) -> Result<i64, EntryError> {
        match self.get(i, name) {
            None | Some(Value::Null) => Ok(default),
            Some(v) => v.as_i64().ok_or_else(|| EntryError::BadArgs(format!("{name}: expected an integer, got {v}"))),
        }
    }

    pub fn bool_or(&self, i: usize, name: &str, default: bool) -> Result<bool, EntryError> {
        match self.get(i, name) {
            None | Some(Value::Null) => Ok(default),
            Some(v) => v.as_bool().ok_or_else(|| EntryError::BadArgs(format!("{name}: expected a boolean, got {v}"))),
        }
    }

    pub fn str_or<'a>(&'a self, i: usize, name: &str, default: &'a str) -> Result<&'a str, EntryError> {
        match self.get(i, name) {
            None | Some(Value::Null) => Ok(default),
            Some(v) => v.as_str().ok_or_else(|| EntryError::BadArgs(format!("{name}: expected a string, got {v}"))),
        }
    }

    pub fn mv(&self, i: usize, name: &str) -> Result<Move, EntryError> {
        let s = self.require(i, name)?.as_str().ok_or_else(|| EntryError::BadArgs(format!("{name}: expected a UCI move")))?;
        Move::parse(s).map_err(|e| EntryError::BadArgs(e.to_string()))
    }
}

/// The items of a list-like argument.
pub fn items(v: &Value) -> Result<Vec<Value>, EntryError> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::Object(o) => {
            for tag in ["$set", "$tuple", "$list"] {
                if let Some(Value::Array(a)) = o.get(tag) {
                    return Ok(a.clone());
                }
            }
            Ok(vec![v.clone()])
        }
        Value::Null => Ok(vec![]),
        other => Ok(vec![other.clone()]),
    }
}

pub fn pos_of(g: &ShimGraph, v: &Value) -> Result<PosId, EntryError> {
    let key = match v {
        Value::String(s) => s.as_str(),
        Value::Object(o) => o
            .get("$pos")
            .or_else(|| (o.get("$model").and_then(Value::as_str) == Some("atomicdb.Position")).then(|| o.get("pk")).flatten())
            .and_then(Value::as_str)
            .ok_or_else(|| EntryError::BadArgs(format!("not a position: {v}")))?,
        _ => return Err(EntryError::BadArgs(format!("not a position: {v}"))),
    };
    g.id(key).ok_or_else(|| EntryError::BadArgs(format!("unknown position {key}")))
}

/// A position as an entry returns it (the Python adapter turns it into a
/// `Position` instance).
pub fn pos_value(g: &ShimGraph, id: PosId) -> Value {
    json!({"$pos": g.key(id)})
}

// ---------------------------------------------------------------- the shim's own entries

/// Changes nothing; returns what was loaded.
fn shim_echo(ctx: &mut Ctx<'_>, _args: &Args) -> Result<Value, EntryError> {
    let mut edges = 0u64;
    let mut list = Vec::new();
    for i in 0..ctx.graph.len() {
        list.clear();
        ctx.graph.children(PosId(i), &mut list);
        edges += list.len() as u64;
    }
    Ok(json!({"positions": ctx.graph.len(), "edges": edges, "issues": ctx.graph.issues.len()}))
}

/// `put_rows({key: {column: value}})`: write columns through the graph.
fn shim_put_rows(ctx: &mut Ctx<'_>, args: &Args) -> Result<Value, EntryError> {
    let rows = args.require(0, "rows")?.as_object().ok_or_else(|| EntryError::BadArgs("rows: expected an object".into()))?;
    let mut errors = Vec::new();
    for (key, cols) in rows {
        let id = ctx.graph.id(key).ok_or_else(|| EntryError::BadArgs(format!("unknown position {key}")))?;
        let cols = cols.as_object().ok_or_else(|| EntryError::BadArgs("columns: expected an object".into()))?;
        errors.extend(ctx.graph.put_row(id, cols)?);
    }
    if !errors.is_empty() {
        return Err(EntryError::Unrepresentable(errors));
    }
    Ok(json!(rows.len()))
}

/// `create(rows, edges)`: create positions (rows with `key`) and edges
/// (`[parent, move, child]`).
fn shim_create(ctx: &mut Ctx<'_>, args: &Args) -> Result<Value, EntryError> {
    let mut created = Vec::new();
    for r in items(args.require(0, "rows")?)? {
        let r = r.as_object().ok_or_else(|| EntryError::BadArgs("row: expected an object".into()))?;
        let (id, errors) = ctx.graph.create_row(r)?;
        if !errors.is_empty() {
            return Err(EntryError::Unrepresentable(errors));
        }
        created.push(pos_value(ctx.graph, id));
    }
    for e in items(args.get(1, "edges").unwrap_or(&Value::Null))? {
        let e = items(&e)?;
        if e.len() != 3 {
            return Err(EntryError::BadArgs("edge: expected [parent, move, child]".into()));
        }
        let parent = pos_of(ctx.graph, &e[0])?;
        let child = pos_of(ctx.graph, &e[2])?;
        let mv = Move::parse(e[1].as_str().unwrap_or("")).map_err(|x| EntryError::BadArgs(x.to_string()))?;
        ctx.graph.add_edge(parent, mv, child)?;
    }
    Ok(Value::Array(created))
}

/// `out(report=..., calls=[...])`: fills `report` with the first position's
/// key and asks for the follow-up `calls`.
fn shim_out(ctx: &mut Ctx<'_>, args: &Args) -> Result<Value, EntryError> {
    if ctx.graph.len() > 0 {
        let key = ctx.graph.key(PosId(0)).to_string();
        ctx.writes.insert("$out".into(), json!({"report": {"changed": [key]}}));
    }
    if let Some(calls) = args.kwargs.get("calls") {
        ctx.writes.insert("$calls".into(), calls.clone());
    }
    Ok(Value::Null)
}

/// `fact(kind, data)`: emit an opaque fact (an upper-case kind is a
/// `DBEvent` kind, written as that event by the Python adapter).
fn shim_fact(ctx: &mut Ctx<'_>, args: &Args) -> Result<Value, EntryError> {
    let kind = args.str_or(0, "kind", "")?.to_string();
    let data = args.get(1, "data").cloned().unwrap_or(Value::Null);
    ctx.facts.push(Fact::Opaque(av2_facts::Opaque { kind, data: av2_facts::RawJson(data.to_string()) }));
    Ok(Value::Null)
}

/// Returns the extra tables it was given, and asks for one row written back.
fn shim_tables(ctx: &mut Ctx<'_>, args: &Args) -> Result<Value, EntryError> {
    if let Some(rows) = args.get(0, "write") {
        ctx.writes.insert("atomicdb_dbevent".into(), rows.clone());
    }
    Ok(Value::Object(ctx.tables.clone()))
}
