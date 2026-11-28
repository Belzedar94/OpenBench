//! Adapters from v1 call signatures to the ported rules (`av2-rules`).
//!
//! Each adapter decodes a v1 call (`Args`), runs the Rust rule on the
//! shim graph, and turns what v1 does besides writing positions into the
//! shim's write-back channels: `DBEvent`s as facts, out-parameters as
//! `$out`, side effects owned by other ports as `$calls`.

use crate::registry::{Args, Ctx, EntryError};
use av2_model::PosId;
use av2_rules::ascent::{self, AscentEnv, AscentOpts};
use av2_rules::cascade::{self, Env};
use av2_rules::climb::{self, ClimbEnv};
use av2_rules::config::RulesConfig;
use av2_rules::effects::{Attribution, ClosureSource, Recorder};
use av2_rules::exact::BoardZeroing;
use av2_rules::loops::NoLoops;
use av2_rules::purchase::{Purchase, TaskSink};
use serde_json::{Value, json};

/// The positions of a list argument that exist in the graph, in order
/// (v1 reads seeds with a `key__in` filter: an unknown key is no row).
fn known_positions(ctx: &Ctx<'_>, args: &Args, i: usize, name: &str) -> Result<Vec<PosId>, EntryError> {
    let items = crate::registry::items(args.require(i, name)?)?;
    Ok(items.iter().filter_map(|v| crate::registry::pos_of(ctx.graph, v).ok()).collect())
}

/// The closures and events a rule reported, as v1 writes them: a
/// `NODE_CLOSED` closure runs v1's `_emit_closure_events` after the
/// write-back (provenance, pending claims, task absorption and campaigns
/// belong to the stage (a) and queue ports), every other event becomes
/// its `DBEvent`, and the proof-number refresh is a follow-up call of
/// `proof.refresh_proof_numbers` (rules-pn's port).
fn effects_to_v1(ctx: &mut Ctx<'_>, rec: Recorder) -> Vec<Value> {
    let mut calls = Vec::new();
    for c in &rec.closed {
        let mut kwargs = serde_json::Map::new();
        for (k, v) in &c.extra {
            kwargs.insert(k.clone(), v.clone());
        }
        calls.push(json!({"name": "ingest._emit_closure_events", "args": [{"$pos": ctx.graph.key(c.pos)}], "kwargs": kwargs}));
    }
    let key = |id: PosId| ctx.graph.key(id).to_string();
    for e in &rec.events {
        ctx.facts.push(e.to_fact(&key));
    }
    for seeds in &rec.proof_refreshes {
        let keys: Vec<String> = seeds.iter().map(|&s| ctx.graph.key(s).to_string()).collect();
        calls.push(json!({"name": "proof.refresh_proof_numbers", "args": [keys]}));
    }
    calls
}

/// The rules' configuration from v1's constants and settings as the
/// caller had them (defaults where the caller sent none).
pub fn config_from(ctx: &Ctx<'_>) -> RulesConfig {
    let mut cfg = RulesConfig::default();
    let int = |name: &str| ctx.constant("ingest", name).and_then(Value::as_u64);
    if let Some(v) = int("CASCADE_GUARD_LIMIT") {
        cfg.cascade_guard_limit = v;
    }
    if let Some(v) = int("CASCADE_REWRITE_LIMIT") {
        cfg.cascade_rewrite_limit = v as u32;
    }
    if let Some(v) = int("MATE_SUPPORT_HOPS") {
        cfg.mate_support_hops = v as usize;
    }
    if let Some(v) = int("BACKED_MAX_PLIES") {
        cfg.backed_max_plies = v;
    }
    if let Some(v) = int("BACKED_MAX_REVISITS") {
        cfg.backed_max_revisits = v as u32;
    }
    if let Some(v) = int("BACKED_MAX_NODES") {
        cfg.backed_max_nodes = v;
    }
    if let Some(v) = ctx.setting("ATOMICDB_FRESH_CONTEXT").and_then(Value::as_bool) {
        cfg.fresh_context = v;
    }
    if let Some(v) = ctx.setting("ATOMICDB_AUTO_INTERIOR_DEEP_RUNGS").and_then(Value::as_bool) {
        cfg.auto_interior_deep_rungs = v;
    }
    cfg
}

/// `ingest.backup_cascade(seed_keys, report=None) -> changed_total`.
pub fn backup_cascade(ctx: &mut Ctx<'_>, args: &Args) -> Result<Value, EntryError> {
    let seeds = known_positions(ctx, args, 0, "seed_keys")?;
    let cfg = config_from(ctx);
    let mut rec = Recorder::default();
    let mut zeroing = BoardZeroing;
    let attribution = Attribution::default();
    let report = {
        let mut env = Env { cfg: &cfg, effects: &mut rec, zeroing: &mut zeroing, attribution: &attribution };
        cascade::backup_cascade(ctx.graph, &seeds, &mut env)?
    };
    let changed: Vec<String> = report.changed.iter().map(|&k| ctx.graph.key(k).to_string()).collect();
    let calls = effects_to_v1(ctx, rec);
    out_param(ctx, args, 1, "report", json!({"changed": changed}));
    push_calls(ctx, calls);
    Ok(json!(report.changed.len()))
}

/// Fill an out-parameter dict the caller passed, by position or keyword.
fn out_param(ctx: &mut Ctx<'_>, args: &Args, index: usize, name: &str, value: Value) {
    let slot = if args.args.get(index).is_some_and(|v| !v.is_null()) {
        index.to_string()
    } else if args.kwargs.get(name).is_some_and(|v| !v.is_null()) {
        name.to_string()
    } else {
        return;
    };
    let out = ctx.writes.entry("$out").or_insert_with(|| json!({}));
    out[slot] = value;
}

fn push_calls(ctx: &mut Ctx<'_>, calls: Vec<Value>) {
    if calls.is_empty() {
        return;
    }
    match ctx.writes.entry("$calls").or_insert_with(|| json!([])) {
        Value::Array(a) => a.extend(calls),
        other => *other = Value::Array(calls),
    }
}

/// The quality arm as the ascent's purchases see it (BKD-11, v1
/// `_queue_quality_convergence` / `_queue_cycle_disambiguation`): the room
/// and the existing tasks come from v1 (`prepare.py`, the exported
/// `atomicdb_analysistask` rows), every purchase that counts becomes a
/// follow-up `apply_purchase` that writes the task as v1 does, and a full
/// arm leaves v1's receipt.
struct ShimTasks {
    keys: Vec<String>,
    rate: i64,
    made: i64,
    pending: i64,
    cap: i64,
    arm: String,
    /// (position key, generation) -> (budget, PENDING, arm is quality).
    tasks: std::collections::HashMap<(String, i64), (u64, bool, bool)>,
    calls: Vec<Value>,
    receipt: bool,
}

impl ShimTasks {
    fn new(ctx: &Ctx<'_>) -> ShimTasks {
        let q = ctx.extra.prepared.get("quality").cloned().unwrap_or(Value::Null);
        let int = |k: &str| q.get(k).and_then(Value::as_i64).unwrap_or(0);
        let arm = q.get("arm").and_then(Value::as_str).unwrap_or("quality").to_string();
        let mut tasks = std::collections::HashMap::new();
        if let Some(rows) = ctx.extra.tables.get("atomicdb_analysistask").and_then(Value::as_array) {
            for r in rows {
                let (Some(pos), Some(gen_)) = (r.get("position_id").and_then(Value::as_str), r.get("generation").and_then(Value::as_i64))
                else {
                    continue;
                };
                let budget = r.get("budget_nodes").and_then(Value::as_u64).unwrap_or(0);
                let pending = r.get("state").and_then(Value::as_str) == Some("PENDING");
                let quality = r.get("arm").and_then(Value::as_str) == Some(arm.as_str());
                tasks.insert((pos.to_string(), gen_), (budget, pending, quality));
            }
        }
        let keys = (0..av2_model::GraphRead::len(&*ctx.graph)).map(|i| ctx.graph.key(PosId(i)).to_string()).collect();
        ShimTasks {
            keys,
            rate: int("rate"),
            made: int("made"),
            pending: int("pending"),
            cap: int("cap"),
            arm,
            tasks,
            calls: Vec::new(),
            receipt: false,
        }
    }
}

impl TaskSink for ShimTasks {
    fn quality_room(&mut self) -> u64 {
        if self.rate <= 0 {
            return 0;
        }
        let hourly = self.rate - self.made;
        if hourly <= 0 {
            if !self.receipt {
                self.receipt = true;
                self.calls.push(json!({"name": "v2shim.prepare.arm_rate_receipt", "args": [self.arm, self.rate, self.made]}));
            }
            return 0;
        }
        hourly.min((self.cap - self.pending).max(0)).max(0) as u64
    }

    fn buy(&mut self, p: &Purchase) -> bool {
        let key = self.keys[p.child.index()].clone();
        let k = (key.clone(), p.generation as i64);
        let counts = match self.tasks.get_mut(&k) {
            None => {
                self.tasks.insert(k, (p.budget, true, true));
                self.made += 1;
                self.pending += 1;
                true
            }
            Some((budget, true, quality)) if *budget < p.budget => {
                *budget = p.budget;
                if !*quality {
                    *quality = true;
                    self.pending += 1;
                }
                true
            }
            Some(_) => false,
        };
        if counts {
            self.calls.push(json!({"name": "v2shim.prepare.apply_purchase", "args": [key, p.generation, p.budget]}));
        }
        counts
    }
}

/// `ingest.backup_backed_evals(seed_keys, max_plies=BACKED_MAX_PLIES,
/// open_only=False, generation=0, report=None) -> changed_total`.
pub fn backup_backed_evals(ctx: &mut Ctx<'_>, args: &Args) -> Result<Value, EntryError> {
    let seeds = known_positions(ctx, args, 0, "seed_keys")?;
    let cfg = config_from(ctx);
    let max_plies = match args.get(1, "max_plies") {
        None | Some(Value::Null) => None,
        Some(v) => Some(v.as_u64().ok_or_else(|| EntryError::BadArgs(format!("max_plies: {v}")))?),
    };
    let opts = AscentOpts {
        max_plies,
        open_only: args.bool_or(2, "open_only", false)?,
        generation: args.i64_or(3, "generation", 0)?.max(0) as u32,
    };
    let mut rec = Recorder::default();
    let mut tasks = ShimTasks::new(ctx);
    let mut loops = NoLoops;
    let report = {
        let mut env = AscentEnv { cfg: &cfg, effects: &mut rec, tasks: &mut tasks, loops: &mut loops };
        ascent::backup_backed_evals(ctx.graph, &seeds, opts, &mut env)?
    };
    let keys = |v: &[PosId]| v.iter().map(|&k| ctx.graph.key(k).to_string()).collect::<Vec<_>>();
    let out = json!({
        "levels": report.levels,
        "processed": report.processed,
        "changed": report.changed,
        "guard": report.guard.map(|g| g.v1()),
        "left": report.left,
        "rested_keys": keys(&report.rested_keys),
        "written_keys": keys(&report.written_keys),
    });
    out_param(ctx, args, 4, "report", out);
    let mut calls = std::mem::take(&mut tasks.calls);
    calls.extend(effects_to_v1(ctx, rec));
    push_calls(ctx, calls);
    Ok(json!(report.changed))
}

/// The landings of a batch of `PropagationSeed` rows (`_merge`): one per
/// position in arrival order, seed lists merged without duplicates.
struct Landing {
    key: String,
    source: String,
    closed: Vec<String>,
    others: Vec<String>,
    parents: Vec<String>,
    audience: Vec<String>,
}

impl Landing {
    fn cascade_seeds(&self) -> Vec<String> {
        std::iter::once(self.key.clone()).chain(self.closed.iter().cloned()).chain(self.others.iter().cloned()).collect()
    }
    fn backed_seeds(&self) -> Vec<String> {
        std::iter::once(self.key.clone())
            .chain(self.parents.iter().cloned())
            .chain(self.others.iter().cloned())
            .chain(self.audience.iter().cloned())
            .collect()
    }
}

fn str_list(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_array).map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect()).unwrap_or_default()
}

/// `propagation_queue.drain_batch(limit=BATCH_SIZE)`: the deferred climb of
/// a batch of seed rows (`ingest.propagate_landings`): per closure source,
/// one cascade from the merged cascade seeds and one ascent from the merged
/// backed seeds (one landing keeps its lists in order; several are merged
/// and sorted by key, as v1 does).  The seed rows are the `seed_rows`
/// keyword (golden cases) or the exported `atomicdb_propagationseed` rows.
///
/// Not ported here (they are the queue model's): the arms that run after
/// the climb (`_uncertainty_expand`, `_witness_refuted_revisit`, the value
/// cascade, idle repair, autosolve), the job summaries, the deletion of the
/// seed rows and what `_after_commit` does.
pub fn drain_batch(ctx: &mut Ctx<'_>, args: &Args) -> Result<Value, EntryError> {
    let limit = match args.get(0, "limit") {
        Some(Value::Object(o)) => o.get("limit").and_then(Value::as_u64).unwrap_or(u64::MAX),
        Some(v) => v.as_u64().unwrap_or(u64::MAX),
        None => u64::MAX,
    } as usize;
    let rows: Vec<Value> = match args.kwargs.get("seed_rows").and_then(Value::as_array) {
        Some(r) => r.clone(),
        None => ctx.extra.tables.get("atomicdb_propagationseed").and_then(Value::as_array).cloned().unwrap_or_default(),
    };
    let mut landings: Vec<Landing> = Vec::new();
    for row in rows.iter().take(limit) {
        let Some(key) = row.get("position_id").and_then(Value::as_str) else { continue };
        let payload = match row.get("payload") {
            Some(Value::String(s)) => serde_json::from_str(s).unwrap_or(Value::Null),
            Some(v) => v.clone(),
            None => Value::Null,
        };
        let source = row.get("source").and_then(Value::as_str).unwrap_or("").to_string();
        let incoming = Landing {
            key: key.to_string(),
            source,
            closed: str_list(payload.get("closed")),
            others: str_list(payload.get("others")),
            parents: str_list(payload.get("parents")),
            audience: str_list(payload.get("audience")),
        };
        match landings.iter_mut().find(|l| l.key == incoming.key) {
            None => landings.push(incoming),
            Some(l) => {
                for (into, from) in [
                    (&mut l.closed, &incoming.closed),
                    (&mut l.others, &incoming.others),
                    (&mut l.parents, &incoming.parents),
                    (&mut l.audience, &incoming.audience),
                ] {
                    for k in from {
                        if !into.contains(k) {
                            into.push(k.clone());
                        }
                    }
                }
            }
        }
    }
    // `_merge` drops a landing whose position is gone.
    landings.retain(|l| ctx.graph.id(&l.key).is_some());
    let cfg = config_from(ctx);
    let mut groups: Vec<(String, Vec<&Landing>)> = Vec::new();
    for l in &landings {
        match groups.iter_mut().find(|(s, _)| *s == l.source) {
            Some((_, g)) => g.push(l),
            None => groups.push((l.source.clone(), vec![l])),
        }
    }
    let ids = |g: &crate::ShimGraph, keys: Vec<String>| -> Vec<PosId> { keys.iter().filter_map(|k| g.id(k)).collect() };
    let mut calls = Vec::new();
    let mut changed = 0u64;
    for (source, group) in groups {
        let (cascade_keys, backed_keys) = if group.len() == 1 {
            (group[0].cascade_seeds(), group[0].backed_seeds())
        } else {
            let set = |f: &dyn Fn(&Landing) -> Vec<String>| {
                group
                    .iter()
                    .flat_map(|l| f(l))
                    .filter(|k| !k.is_empty())
                    .collect::<std::collections::BTreeSet<_>>()
                    .into_iter()
                    .collect::<Vec<_>>()
            };
            (set(&Landing::cascade_seeds), set(&Landing::backed_seeds))
        };
        let cascade_seeds = ids(ctx.graph, cascade_keys);
        let backed_seeds = ids(ctx.graph, backed_keys);
        let mut rec = Recorder::default();
        let mut zeroing = BoardZeroing;
        let mut tasks = ShimTasks::new(ctx);
        let mut loops = NoLoops;
        let attribution = Attribution { source: ClosureSource::parse(&source), provenance: None };
        let report = {
            let mut env = ClimbEnv {
                cfg: &cfg,
                effects: &mut rec,
                zeroing: &mut zeroing,
                tasks: &mut tasks,
                loops: &mut loops,
                attribution: &attribution,
            };
            climb::climb(ctx.graph, &cascade_seeds, &backed_seeds, &mut env)?
        };
        changed += report.cascade.changed.len() as u64 + report.ascent.changed;
        calls.extend(std::mem::take(&mut tasks.calls));
        calls.extend(effects_to_v1(ctx, rec));
    }
    push_calls(ctx, calls);
    Ok(json!({"settled": rows.len().min(limit), "landings": landings.len(), "changed": changed}))
}
