//! The normalization rules (`tests/golden/normalize.json`) and their paths.
//!
//! A path is `$` followed by `.name`, `.*` (every key of an object) or
//! `[*]` (every element of an array), e.g. `$.moves[*].score`.  A rule names
//! a node; whatever it does applies to the node's whole subtree.

use std::collections::BTreeMap;
use std::fmt;

use serde::Deserialize;

/// One step of the path of a node inside a JSON value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Step {
    Key(String),
    Index(usize),
}

/// The path of a node, printed as `$.moves[3].score`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Path(pub Vec<Step>);

impl Path {
    pub fn child_key(&self, key: &str) -> Path {
        let mut steps = self.0.clone();
        steps.push(Step::Key(key.to_string()));
        Path(steps)
    }

    pub fn child_index(&self, index: usize) -> Path {
        let mut steps = self.0.clone();
        steps.push(Step::Index(index));
        Path(steps)
    }
}

impl fmt::Display for Path {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("$")?;
        for step in &self.0 {
            match step {
                Step::Key(key) => write!(f, ".{key}")?,
                Step::Index(index) => write!(f, "[{index}]")?,
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum Seg {
    Key(String),
    AnyKey,
    AnyIndex,
    Index(usize),
}

/// A compiled rule path.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Pattern {
    source: String,
    segs: Vec<Seg>,
}

impl Pattern {
    pub fn parse(source: &str) -> Result<Pattern, String> {
        let rest = source.strip_prefix('$').ok_or_else(|| format!("{source:?}: a path starts with $"))?;
        let bytes = rest.as_bytes();
        let mut segs = Vec::new();
        let mut i = 0;
        while i < bytes.len() {
            match bytes[i] {
                b'.' => {
                    let start = i + 1;
                    let mut end = start;
                    while end < bytes.len() && bytes[end] != b'.' && bytes[end] != b'[' {
                        end += 1;
                    }
                    let name = &rest[start..end];
                    if name.is_empty() {
                        return Err(format!("{source:?}: empty key"));
                    }
                    segs.push(if name == "*" { Seg::AnyKey } else { Seg::Key(name.to_string()) });
                    i = end;
                }
                b'[' => {
                    let end = rest[i..].find(']').map(|offset| i + offset).ok_or_else(|| format!("{source:?}: unclosed ["))?;
                    let inner = &rest[i + 1..end];
                    segs.push(if inner == "*" {
                        Seg::AnyIndex
                    } else {
                        Seg::Index(inner.parse().map_err(|_| format!("{source:?}: bad index {inner:?}"))?)
                    });
                    i = end + 1;
                }
                _ => return Err(format!("{source:?}: unexpected character at {i}")),
            }
        }
        Ok(Pattern { source: source.to_string(), segs })
    }

    /// Does this pattern name exactly the node at `path`?
    pub fn matches(&self, path: &Path) -> bool {
        self.segs.len() == path.0.len()
            && self.segs.iter().zip(&path.0).all(|(seg, step)| match (seg, step) {
                (Seg::Key(name), Step::Key(key)) => name == key,
                (Seg::AnyKey, Step::Key(_)) => true,
                (Seg::AnyIndex, Step::Index(_)) => true,
                (Seg::Index(want), Step::Index(index)) => want == index,
                _ => false,
            })
    }

    pub fn source(&self) -> &str {
        &self.source
    }
}

/// A path in the rules file: a bare string, or an object with the reason.
#[derive(Clone, Debug, Deserialize)]
#[serde(untagged)]
enum RawPath {
    Bare(String),
    Explained {
        path: String,
        #[allow(dead_code)]
        #[serde(default)]
        why: String,
    },
}

impl RawPath {
    fn path(&self) -> &str {
        match self {
            RawPath::Bare(path) => path,
            RawPath::Explained { path, .. } => path,
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
struct RawEndpoint {
    #[serde(default)]
    volatile: Vec<RawPath>,
    #[serde(default)]
    budgeted: Vec<RawPath>,
    #[serde(default)]
    growth: Vec<RawPath>,
    #[serde(default)]
    optional: Vec<RawPath>,
    /// Arrays compared element by element through a key field instead of by
    /// position: `{"$.moves": "uci"}`.
    #[serde(default)]
    keyed: BTreeMap<String, String>,
    /// Response headers compared for this endpoint besides the global ones.
    #[serde(default)]
    headers: Vec<String>,
    /// Headers of this endpoint never compared (volatile).
    #[serde(default)]
    volatile_headers: Vec<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
struct RawHeaders {
    #[serde(default)]
    compare: Vec<String>,
    #[serde(default)]
    through_nginx: Vec<String>,
    #[serde(default)]
    volatile: Vec<String>,
}

/// An answer that is accepted for a request instead of the recorded one
/// (production and the repository differ there, see the contract, section 0).
#[derive(Clone, Debug, Deserialize)]
pub struct Alternative {
    pub endpoint: String,
    #[serde(default)]
    pub method: Option<String>,
    #[serde(default)]
    pub path: Option<String>,
    pub why: String,
    pub accept: Vec<AcceptedAnswer>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct AcceptedAnswer {
    pub status: u16,
    /// The body, as JSON text; absent means any body.
    #[serde(default)]
    pub body: Option<String>,
}

/// An owner decision that makes a difference intentional.
#[derive(Clone, Debug, Deserialize)]
pub struct Decision {
    pub id: String,
    pub endpoint: String,
    pub path: String,
    pub why: String,
}

#[derive(Clone, Debug, Deserialize)]
struct RawRules {
    version: u32,
    #[serde(default)]
    headers: RawHeaders,
    #[serde(default)]
    endpoints: BTreeMap<String, RawEndpoint>,
    #[serde(default)]
    alternatives: Vec<Alternative>,
    #[serde(default)]
    decisions: Vec<Decision>,
}

/// The rules for one endpoint, with the `"*"` entry merged in.
#[derive(Clone, Debug, Default)]
pub struct EndpointRules {
    pub volatile: Vec<Pattern>,
    pub budgeted: Vec<Pattern>,
    pub growth: Vec<Pattern>,
    pub optional: Vec<Pattern>,
    pub decisions: Vec<(Pattern, String)>,
    pub keyed: Vec<(Pattern, String)>,
    pub headers: Vec<String>,
    pub volatile_headers: Vec<String>,
}

impl EndpointRules {
    pub fn is_volatile(&self, path: &Path) -> bool {
        self.volatile.iter().any(|p| p.matches(path))
    }
    pub fn decision(&self, path: &Path) -> Option<&str> {
        self.decisions.iter().find(|(p, _)| p.matches(path)).map(|(_, id)| id.as_str())
    }
    pub fn is_budgeted(&self, path: &Path) -> bool {
        self.budgeted.iter().any(|p| p.matches(path))
    }
    pub fn is_growth(&self, path: &Path) -> bool {
        self.growth.iter().any(|p| p.matches(path))
    }
    pub fn is_optional(&self, path: &Path) -> bool {
        self.optional.iter().any(|p| p.matches(path))
    }
    pub fn key_field(&self, path: &Path) -> Option<&str> {
        self.keyed.iter().find(|(p, _)| p.matches(path)).map(|(_, field)| field.as_str())
    }
}

/// The whole rules file, compiled.
#[derive(Clone, Debug)]
pub struct Rules {
    pub version: u32,
    pub compare_headers: Vec<String>,
    pub nginx_headers: Vec<String>,
    pub volatile_headers: Vec<String>,
    endpoints: BTreeMap<String, RawEndpoint>,
    pub alternatives: Vec<Alternative>,
    pub decisions: Vec<Decision>,
}

fn compile(paths: &[RawPath]) -> Result<Vec<Pattern>, String> {
    paths.iter().map(|p| Pattern::parse(p.path())).collect()
}

impl Rules {
    pub fn from_json(text: &str) -> Result<Rules, String> {
        let raw: RawRules = serde_json::from_str(text).map_err(|e| format!("normalize.json: {e}"))?;
        let rules = Rules {
            version: raw.version,
            compare_headers: lower(&raw.headers.compare),
            nginx_headers: lower(&raw.headers.through_nginx),
            volatile_headers: lower(&raw.headers.volatile),
            endpoints: raw.endpoints,
            alternatives: raw.alternatives,
            decisions: raw.decisions,
        };
        // Compile every endpoint once, so a typo fails at load time.
        for name in rules.endpoints.keys() {
            rules.for_endpoint(name)?;
        }
        Ok(rules)
    }

    pub fn load(path: &std::path::Path) -> Result<Rules, String> {
        let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
        Rules::from_json(&text)
    }

    /// The rules of `endpoint`, merged with the `"*"` entry.
    pub fn for_endpoint(&self, endpoint: &str) -> Result<EndpointRules, String> {
        let mut merged = EndpointRules::default();
        for name in ["*", endpoint] {
            let Some(raw) = self.endpoints.get(name) else { continue };
            if name == "*" && endpoint == "*" {
                continue;
            }
            merged.volatile.extend(compile(&raw.volatile)?);
            merged.budgeted.extend(compile(&raw.budgeted)?);
            merged.growth.extend(compile(&raw.growth)?);
            merged.optional.extend(compile(&raw.optional)?);
            for (path, field) in &raw.keyed {
                merged.keyed.push((Pattern::parse(path)?, field.clone()));
            }
            merged.headers.extend(lower(&raw.headers));
            merged.volatile_headers.extend(lower(&raw.volatile_headers));
        }
        for decision in &self.decisions {
            if decision.endpoint == endpoint || decision.endpoint == "*" {
                merged.decisions.push((Pattern::parse(&decision.path)?, decision.id.clone()));
            }
        }
        Ok(merged)
    }
}

fn lower(names: &[String]) -> Vec<String> {
    names.iter().map(|n| n.to_ascii_lowercase()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn path(steps: &[&str]) -> Path {
        Path(
            steps
                .iter()
                .map(|s| match s.parse::<usize>() {
                    Ok(i) => Step::Index(i),
                    Err(_) => Step::Key((*s).to_string()),
                })
                .collect(),
        )
    }

    #[test]
    fn patterns_match_exactly() {
        let p = Pattern::parse("$.moves[*].score").unwrap();
        assert!(p.matches(&path(&["moves", "3", "score"])));
        assert!(!p.matches(&path(&["moves", "3"])));
        assert!(!p.matches(&path(&["moves", "3", "score", "x"])));
        assert!(!p.matches(&path(&["moves", "x", "score"])));
        let any = Pattern::parse("$.positions.*.score").unwrap();
        assert!(any.matches(&path(&["positions", "rnb/... w", "score"])));
        let root = Pattern::parse("$").unwrap();
        assert!(root.matches(&Path::default()));
        let index = Pattern::parse("$.lines[0]").unwrap();
        assert!(index.matches(&path(&["lines", "0"])) && !index.matches(&path(&["lines", "1"])));
    }

    #[test]
    fn bad_patterns_are_refused() {
        for bad in ["moves", "$.", "$..a", "$.a[", "$.a[x]", "$a"] {
            assert!(Pattern::parse(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn paths_print() {
        assert_eq!(path(&["moves", "3", "score"]).to_string(), "$.moves[3].score");
        assert_eq!(Path::default().to_string(), "$");
    }

    #[test]
    fn star_merges_into_every_endpoint() {
        let rules = Rules::from_json(
            r#"{"version": 1,
                "endpoints": {"*": {"volatile": ["$.a"]},
                              "query": {"volatile": [{"path": "$.b", "why": "x"}],
                                        "keyed": {"$.moves": "uci"}}},
                "decisions": [{"id": "D9", "endpoint": "query", "path": "$.c", "why": "y"}]}"#,
        )
        .unwrap();
        let q = rules.for_endpoint("query").unwrap();
        assert!(q.is_volatile(&path(&["a"])) && q.is_volatile(&path(&["b"])));
        assert_eq!(q.decision(&path(&["c"])), Some("D9"));
        assert_eq!(q.key_field(&path(&["moves"])), Some("uci"));
        let other = rules.for_endpoint("roots").unwrap();
        assert!(other.is_volatile(&path(&["a"])) && !other.is_volatile(&path(&["b"])));
        assert!(Rules::from_json(r#"{"version": 1, "endpoints": {"x": {"volatile": ["nope"]}}}"#).is_err());
    }
}
