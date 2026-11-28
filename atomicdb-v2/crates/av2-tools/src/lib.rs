//! Shared pieces of the AtomicDB v2 tools: argument parsing, latency
//! summaries, the v1 export format and its mapping onto v2 records.

pub mod export;

use std::collections::HashMap;
use std::path::PathBuf;

/// `--key value` / `--flag` arguments.
pub struct Args {
    m: HashMap<String, String>,
    pub positional: Vec<String>,
}

impl Args {
    pub fn parse() -> Args {
        Args::from(std::env::args().skip(1).collect())
    }

    pub fn from(v: Vec<String>) -> Args {
        let mut m = HashMap::new();
        let mut positional = Vec::new();
        let mut i = 0;
        while i < v.len() {
            if let Some(k) = v[i].strip_prefix("--") {
                if i + 1 < v.len() && !v[i + 1].starts_with("--") {
                    m.insert(k.to_string(), v[i + 1].clone());
                    i += 2;
                } else {
                    m.insert(k.to_string(), "true".into());
                    i += 1;
                }
            } else {
                positional.push(v[i].clone());
                i += 1;
            }
        }
        Args { m, positional }
    }

    pub fn get(&self, k: &str) -> Option<&str> {
        self.m.get(k).map(String::as_str)
    }
    pub fn s(&self, k: &str, d: &str) -> String {
        self.get(k).unwrap_or(d).to_string()
    }
    pub fn path(&self, k: &str) -> PathBuf {
        PathBuf::from(self.get(k).unwrap_or_else(|| die(&format!("missing --{k}"))))
    }
    pub fn n(&self, k: &str, d: u64) -> u64 {
        self.get(k).map(|v| v.parse().unwrap_or_else(|_| die(&format!("--{k} {v}: not a number")))).unwrap_or(d)
    }
    pub fn f(&self, k: &str, d: f64) -> f64 {
        self.get(k).map(|v| v.parse().unwrap_or_else(|_| die(&format!("--{k} {v}: not a number")))).unwrap_or(d)
    }
    pub fn flag(&self, k: &str) -> bool {
        self.get(k).is_some_and(|v| v != "false" && v != "0")
    }
}

pub fn die(msg: &str) -> ! {
    eprintln!("error: {msg}");
    std::process::exit(2)
}

/// Latency samples in microseconds (or any unit), summarised as percentiles.
#[derive(Default, Clone)]
pub struct Lat {
    v: Vec<f64>,
}

impl Lat {
    pub fn new() -> Lat {
        Lat::default()
    }
    pub fn add(&mut self, x: f64) {
        self.v.push(x);
    }
    pub fn len(&self) -> usize {
        self.v.len()
    }
    pub fn extend(&mut self, other: Lat) {
        self.v.extend(other.v);
    }
    pub fn is_empty(&self) -> bool {
        self.v.is_empty()
    }
    pub fn pct(&mut self, p: f64) -> f64 {
        if self.v.is_empty() {
            return 0.0;
        }
        self.v.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let i = ((p / 100.0) * (self.v.len() - 1) as f64).round() as usize;
        self.v[i]
    }
    pub fn mean(&self) -> f64 {
        if self.v.is_empty() { 0.0 } else { self.v.iter().sum::<f64>() / self.v.len() as f64 }
    }
    pub fn json(&mut self) -> String {
        format!(
            "{{\"n\":{},\"mean\":{:.2},\"p50\":{:.2},\"p90\":{:.2},\"p99\":{:.2},\"max\":{:.2}}}",
            self.len(),
            self.mean(),
            self.pct(50.0),
            self.pct(90.0),
            self.pct(99.0),
            self.pct(100.0)
        )
    }
}

/// Append a JSON line to `out` (if given) and print it.
pub fn emit(out: &Option<PathBuf>, line: &str) {
    println!("{line}");
    if let Some(p) = out {
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new().create(true).append(true).open(p).expect("open output");
        writeln!(f, "{line}").expect("write output");
    }
}
