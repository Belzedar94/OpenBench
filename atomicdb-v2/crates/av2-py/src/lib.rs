//! `av2`: the Python extension the v1 test suite uses to run v2 (plan
//! section 7.1).
//!
//! The protocol is JSON both ways (`av2_shim::call_json`): Python exports
//! the rows a call needs, Rust loads them into the in-memory v2 graph, runs
//! the entry point registered under the v1 function's name, and returns the
//! result plus the rows to write back.  JSON keeps the boundary small and
//! versionable; the rows of a test database are a few hundred at most.
//!
//! ```python
//! import av2
//! av2.entries()                       # ported v1 functions, by v1 name
//! reply = json.loads(av2.call("ingest.backup_backed_evals", json.dumps(request)))
//! g = av2.Graph(json.dumps({"positions": rows, "edges": edges}))
//! g.call("shim.put_rows", json.dumps({"args": [...]}))
//! g.row(key); g.changes()
//! ```
//!
//! Build: `python atomicdb-v2/scripts/build_av2_py.py` (cargo with the
//! `extension-module` feature, then copies the library next to the Python
//! adapter as `av2.pyd` / `av2.so`).

use av2_shim::{ShimGraph, call_json, registry};
use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::prelude::*;
use serde_json::{Map, Value};

fn parse(s: &str, what: &str) -> PyResult<Value> {
    serde_json::from_str(s).map_err(|e| PyValueError::new_err(format!("{what}: bad JSON: {e}")))
}

/// Crate version and the number of entries.
#[pyfunction]
fn version() -> String {
    format!("av2-py {} ({} entries)", env!("CARGO_PKG_VERSION"), registry::ENTRIES.len())
}

/// The v1 names of every entry v2 has.
#[pyfunction]
fn entries() -> Vec<&'static str> {
    registry::names()
}

/// The `atomicdb_position` columns v2 models (what the shim exports).
#[pyfunction]
fn columns() -> Vec<&'static str> {
    av2_shim::v1::MODELED.to_vec()
}

/// The extra v1 tables an entry wants exported.
#[pyfunction]
fn entry_tables(name: &str) -> PyResult<Vec<&'static str>> {
    registry::find(name).map(|e| e.tables.to_vec()).ok_or_else(|| PyKeyError::new_err(name.to_string()))
}

/// One call on a fresh graph: request JSON in, reply JSON out.
#[pyfunction]
fn call(py: Python<'_>, name: &str, request: &str) -> PyResult<String> {
    let req = parse(request, "request")?;
    let name = name.to_string();
    Ok(py.detach(move || call_json(&name, &req).to_string()))
}

/// A graph kept across calls (changes accumulate since it was loaded).
#[pyclass(unsendable)]
struct Graph {
    g: ShimGraph,
}

#[pymethods]
impl Graph {
    #[new]
    fn new(request: &str) -> PyResult<Graph> {
        let req = parse(request, "request")?;
        let rows = req
            .get("positions")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(|r| r.as_object().cloned()).collect())
            .unwrap_or_default();
        let edges = match req.get("edges") {
            Some(e) => serde_json::from_value(e.clone()).map_err(|e| PyValueError::new_err(format!("edges: {e}")))?,
            None => Vec::new(),
        };
        Ok(Graph { g: ShimGraph::load(rows, edges) })
    }

    fn __len__(&self) -> usize {
        av2_model::GraphRead::len(&self.g) as usize
    }

    /// Load issues (JSON list).
    fn issues(&self) -> String {
        serde_json::to_string(&self.g.issues).unwrap_or_default()
    }

    /// Every modeled column of a position (JSON), or None.
    fn row(&self, key: &str) -> Option<String> {
        self.g.id(key).map(|id| Value::Object(self.g.row(id)).to_string())
    }

    /// Changes since the load (JSON: updated, created, edges).
    fn changes(&self) -> String {
        serde_json::to_string(&self.g.changes()).unwrap_or_default()
    }

    /// Call an entry on this graph; reply JSON as `av2.call`.
    #[pyo3(signature = (name, args, tables=None))]
    fn call(&mut self, name: &str, args: &str, tables: Option<&str>) -> PyResult<String> {
        let args = parse(args, "args")?;
        let tables = match tables {
            Some(t) => parse(t, "tables")?.as_object().cloned().unwrap_or_default(),
            None => Map::new(),
        };
        let issues = self.g.issues.clone();
        Ok(match registry::call(name, &mut self.g, &args, &tables) {
            Ok(o) => av2_shim::outcome_json(o, &issues),
            Err(e) => av2_shim::error_json(&e, &issues),
        }
        .to_string())
    }
}

#[pymodule]
fn av2(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(entries, m)?)?;
    m.add_function(wrap_pyfunction!(columns, m)?)?;
    m.add_function(wrap_pyfunction!(entry_tables, m)?)?;
    m.add_function(wrap_pyfunction!(call, m)?)?;
    m.add_class::<Graph>()?;
    Ok(())
}
