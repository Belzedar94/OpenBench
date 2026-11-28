//! Adapters from v1 call signatures to the ported rules (`av2-rules`).
//!
//! Each adapter decodes a v1 call (`Args`), runs the Rust rule on the
//! shim graph, and turns what v1 does besides writing positions into the
//! shim's write-back channels: `DBEvent`s as facts, out-parameters as
//! `$out`, side effects owned by other ports as `$calls`.

use crate::registry::{Args, Ctx, EntryError};
use av2_model::PosId;
use av2_rules::cascade::{self, Env};
use av2_rules::config::RulesConfig;
use av2_rules::effects::{Attribution, Recorder};
use av2_rules::exact::BoardZeroing;
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
    // v1 fills `report['changed']` when a dict was passed (by keyword or
    // as the second positional argument).
    if let Some(slot) = [("1", args.args.get(1)), ("report", args.kwargs.get("report"))]
        .into_iter()
        .find_map(|(slot, v)| v.filter(|v| !v.is_null()).map(|_| slot))
    {
        ctx.writes.insert("$out".into(), json!({slot: {"changed": changed}}));
    }
    if !calls.is_empty() {
        ctx.writes.insert("$calls".into(), Value::Array(calls));
    }
    Ok(json!(report.changed.len()))
}
