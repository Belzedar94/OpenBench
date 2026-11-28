//! Golden records: what `tools/golden/record_v1.py` writes, one per line.

use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read};
use std::path::Path;

use flate2::read::GzDecoder;
use serde::{Deserialize, Serialize};

/// One recorded request and v1's answer.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Record {
    pub id: u64,
    pub endpoint: String,
    pub method: String,
    pub path: String,
    /// Query parameters in order; a number in the file is read as its text.
    #[serde(default, deserialize_with = "pairs_as_text")]
    pub query: Vec<(String, String)>,
    /// Form fields of a POST (`null` for none).
    #[serde(default)]
    pub form: Option<BTreeMap<String, serde_json::Value>>,
    #[serde(default)]
    pub request_headers: BTreeMap<String, String>,
    #[serde(default)]
    pub note: String,
    #[serde(default)]
    pub recorded_at: String,
    pub status: u16,
    /// Response headers, lower-case names.
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
    /// The raw body text.
    pub body: String,
}

impl Record {
    /// Form fields as `(name, value)` pairs; a list value repeats the field.
    pub fn form_pairs(&self) -> Vec<(String, String)> {
        let mut pairs = Vec::new();
        for (name, value) in self.form.iter().flatten() {
            match value {
                serde_json::Value::String(text) => pairs.push((name.clone(), text.clone())),
                serde_json::Value::Array(items) => {
                    for item in items {
                        pairs.push((name.clone(), json_text(item)));
                    }
                }
                other => pairs.push((name.clone(), json_text(other))),
            }
        }
        pairs
    }

    /// A stable identity for matching two recordings of the same request.
    pub fn request_key(&self) -> String {
        let query: Vec<String> = self.query.iter().map(|(k, v)| format!("{k}={v}")).collect();
        let form: Vec<String> = self.form_pairs().iter().map(|(k, v)| format!("{k}={v}")).collect();
        let headers: Vec<String> = self.request_headers.iter().map(|(k, v)| format!("{k}={v}")).collect();
        format!("{} {}?{}|{}|{}", self.method, self.path, query.join("&"), form.join("&"), headers.join("&"))
    }
}

fn pairs_as_text<'de, D: serde::Deserializer<'de>>(deserializer: D) -> Result<Vec<(String, String)>, D::Error> {
    let raw: Vec<(String, serde_json::Value)> = Vec::deserialize(deserializer)?;
    Ok(raw.into_iter().map(|(name, value)| (name, json_text(&value))).collect())
}

fn json_text(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// Read a `.jsonl` or `.jsonl.gz` file of records.
pub fn read_records(path: &Path) -> Result<Vec<Record>, String> {
    let file = std::fs::File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let reader: Box<dyn Read> =
        if path.extension().is_some_and(|ext| ext == "gz") { Box::new(GzDecoder::new(file)) } else { Box::new(file) };
    let mut records = Vec::new();
    for (number, line) in BufReader::new(reader).lines().enumerate() {
        let line = line.map_err(|e| format!("{}: line {}: {e}", path.display(), number + 1))?;
        if line.trim().is_empty() {
            continue;
        }
        let record: Record = serde_json::from_str(&line).map_err(|e| format!("{}: line {}: {e}", path.display(), number + 1))?;
        records.push(record);
    }
    Ok(records)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_recorder_format() {
        let line = r#"{"id": 7, "endpoint": "query", "method": "GET", "path": "/atomicdb/api/query",
            "query": [["fen", "8/8 w - - 0 1"], ["opening", "1"]], "form": null, "request_headers": {},
            "note": "x", "recorded_at": "2026-11-28T08:00:00+00:00", "elapsed_ms": 12.5, "status": 404,
            "headers": {"content-type": "application/json"}, "body": "{\"error\": \"unknown position\"}"}"#;
        let record: Record = serde_json::from_str(line).unwrap();
        assert_eq!(record.query[1], ("opening".to_string(), "1".to_string()));
        assert!(record.form_pairs().is_empty());
        assert!(record.request_key().starts_with("GET /atomicdb/api/query?fen=8/8 w - - 0 1&opening=1"));
        let post: Record = serde_json::from_str(
            r#"{"id": 1, "endpoint": "my-queue", "method": "POST", "path": "/p", "form": {"a": "1", "b": ["x", "y"]},
                "status": 403, "body": ""}"#,
        )
        .unwrap();
        assert_eq!(post.form_pairs(), vec![("a".into(), "1".into()), ("b".into(), "x".into()), ("b".into(), "y".into())]);
    }
}
