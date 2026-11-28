//! A small Prometheus registry: counters, gauges and histograms with fixed
//! buckets, rendered in the text exposition format (version 0.0.4).
//!
//! Hand-written on purpose: the writer records a handful of numbers per
//! batch, the endpoint is scraped every few seconds, and a dependency the
//! size of a metrics framework would buy nothing here.

use std::fmt::Write as _;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};

/// Latency buckets in seconds: 25 us to 10 s, roughly x2.5 apart.
pub const LATENCY_BUCKETS: &[f64] =
    &[25e-6, 50e-6, 100e-6, 250e-6, 500e-6, 1e-3, 2.5e-3, 5e-3, 10e-3, 25e-3, 50e-3, 100e-3, 250e-3, 500e-3, 1.0, 2.5, 5.0, 10.0];
/// Size buckets (inputs per batch, frame kilobytes).
pub const COUNT_BUCKETS: &[f64] = &[1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024.0, 4096.0];

pub struct Counter(AtomicU64);
impl Counter {
    pub const fn new() -> Counter {
        Counter(AtomicU64::new(0))
    }
    pub fn inc(&self, n: u64) {
        self.0.fetch_add(n, Ordering::Relaxed);
    }
    pub fn get(&self) -> u64 {
        self.0.load(Ordering::Relaxed)
    }
}
impl Default for Counter {
    fn default() -> Self {
        Counter::new()
    }
}

/// A gauge holding an `f64` (stored as bits).
pub struct Gauge(AtomicU64);
impl Gauge {
    pub const fn new() -> Gauge {
        Gauge(AtomicU64::new(0))
    }
    pub fn set(&self, v: f64) {
        self.0.store(v.to_bits(), Ordering::Relaxed);
    }
    pub fn get(&self) -> f64 {
        f64::from_bits(self.0.load(Ordering::Relaxed))
    }
}
impl Default for Gauge {
    fn default() -> Self {
        Gauge::new()
    }
}

struct HistState {
    counts: Vec<u64>,
    sum: f64,
    n: u64,
}

pub struct Histogram {
    bounds: &'static [f64],
    st: Mutex<HistState>,
}

impl Histogram {
    pub fn new(bounds: &'static [f64]) -> Histogram {
        Histogram { bounds, st: Mutex::new(HistState { counts: vec![0; bounds.len() + 1], sum: 0.0, n: 0 }) }
    }
    pub fn observe(&self, v: f64) {
        let i = self.bounds.iter().position(|&b| v <= b).unwrap_or(self.bounds.len());
        let mut s = self.st.lock().unwrap();
        s.counts[i] += 1;
        s.sum += v;
        s.n += 1;
    }
    pub fn count(&self) -> u64 {
        self.st.lock().unwrap().n
    }
    pub fn sum(&self) -> f64 {
        self.st.lock().unwrap().sum
    }
    /// Upper bound of the bucket holding quantile `q` (what a dashboard's
    /// `histogram_quantile` would estimate, without the interpolation).
    pub fn quantile_bound(&self, q: f64) -> f64 {
        let s = self.st.lock().unwrap();
        let target = (q * s.n as f64).ceil() as u64;
        let mut acc = 0u64;
        for (i, c) in s.counts.iter().enumerate() {
            acc += c;
            if acc >= target.max(1) {
                return self.bounds.get(i).copied().unwrap_or(f64::INFINITY);
            }
        }
        f64::INFINITY
    }
    fn render(&self, out: &mut String, name: &str, labels: &str) {
        let s = self.st.lock().unwrap();
        let sep = if labels.is_empty() { "" } else { "," };
        let mut acc = 0u64;
        for (i, b) in self.bounds.iter().enumerate() {
            acc += s.counts[i];
            let _ = writeln!(out, "{name}_bucket{{{labels}{sep}le=\"{b}\"}} {acc}");
        }
        acc += s.counts[self.bounds.len()];
        let _ = writeln!(out, "{name}_bucket{{{labels}{sep}le=\"+Inf\"}} {acc}");
        let lb = if labels.is_empty() { String::new() } else { format!("{{{labels}}}") };
        let _ = writeln!(out, "{name}_sum{lb} {}", s.sum);
        let _ = writeln!(out, "{name}_count{lb} {}", s.n);
    }
}

/// One metric family to render.
pub enum Family<'a> {
    Counter { name: &'a str, help: &'a str, samples: Vec<(String, f64)> },
    Gauge { name: &'a str, help: &'a str, samples: Vec<(String, f64)> },
    Histogram { name: &'a str, help: &'a str, samples: Vec<(String, &'a Histogram)> },
}

/// Render families in the Prometheus text format.  Sample labels are given
/// pre-formatted (`stage="fsync"`), empty for none.
pub fn render(families: &[Family<'_>]) -> String {
    let mut out = String::with_capacity(8192);
    for f in families {
        match f {
            Family::Counter { name, help, samples } | Family::Gauge { name, help, samples } => {
                let kind = if matches!(f, Family::Counter { .. }) { "counter" } else { "gauge" };
                let _ = writeln!(out, "# HELP {name} {help}");
                let _ = writeln!(out, "# TYPE {name} {kind}");
                for (labels, v) in samples {
                    if labels.is_empty() {
                        let _ = writeln!(out, "{name} {v}");
                    } else {
                        let _ = writeln!(out, "{name}{{{labels}}} {v}");
                    }
                }
            }
            Family::Histogram { name, help, samples } => {
                let _ = writeln!(out, "# HELP {name} {help}");
                let _ = writeln!(out, "# TYPE {name} histogram");
                for (labels, h) in samples {
                    h.render(&mut out, name, labels);
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn histogram_renders_cumulative_buckets() {
        let h = Histogram::new(&[1.0, 2.0]);
        for v in [0.5, 1.5, 1.5, 9.0] {
            h.observe(v);
        }
        let s = render(&[Family::Histogram { name: "x_seconds", help: "test", samples: vec![("stage=\"a\"".into(), &h)] }]);
        assert!(s.contains("x_seconds_bucket{stage=\"a\",le=\"1\"} 1"), "{s}");
        assert!(s.contains("x_seconds_bucket{stage=\"a\",le=\"2\"} 3"), "{s}");
        assert!(s.contains("x_seconds_bucket{stage=\"a\",le=\"+Inf\"} 4"), "{s}");
        assert!(s.contains("x_seconds_count{stage=\"a\"} 4"), "{s}");
        assert_eq!(h.quantile_bound(0.5), 2.0);
        assert_eq!(h.quantile_bound(1.0), f64::INFINITY);
    }

    #[test]
    fn counters_and_gauges() {
        let s = render(&[
            Family::Counter { name: "a_total", help: "h", samples: vec![(String::new(), 3.0)] },
            Family::Gauge { name: "b", help: "h", samples: vec![("file=\"core.col\"".into(), 1.5)] },
        ]);
        assert!(s.contains("# TYPE a_total counter\na_total 3\n"), "{s}");
        assert!(s.contains("b{file=\"core.col\"} 1.5"), "{s}");
    }
}
