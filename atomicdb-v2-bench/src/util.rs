//! Percentiles, process counters and JSON-lines output.

use std::io::Write;

pub struct Lat { v: Vec<u32> }
impl Lat {
    pub fn new() -> Lat { Lat { v: Vec::new() } }
    #[inline] pub fn add_us(&mut self, us: u64) { self.v.push(us.min(u32::MAX as u64) as u32); }
    pub fn merge(&mut self, o: &Lat) { self.v.extend_from_slice(&o.v); }
    pub fn len(&self) -> usize { self.v.len() }
    /// (p50, p95, p99, max, mean) in microseconds.
    pub fn summary(&mut self) -> (u32, u32, u32, u32, f64) {
        if self.v.is_empty() { return (0, 0, 0, 0, 0.0); }
        self.v.sort_unstable();
        let q = |p: f64| self.v[((self.v.len() as f64 * p) as usize).min(self.v.len() - 1)];
        let mean = self.v.iter().map(|&x| x as f64).sum::<f64>() / self.v.len() as f64;
        (q(0.50), q(0.95), q(0.99), *self.v.last().unwrap(), mean)
    }
    pub fn json(&mut self) -> String {
        let (p50, p95, p99, max, mean) = self.summary();
        format!("{{\"n\":{},\"p50_us\":{},\"p95_us\":{},\"p99_us\":{},\"max_us\":{},\"mean_us\":{:.1}}}", self.v.len(), p50, p95, p99, max, mean)
    }
}

#[derive(Default, Clone, Copy, Debug)]
#[allow(dead_code)]
pub struct ProcStats {
    pub rss: u64,           // working set now
    pub peak_rss: u64,
    pub private: u64,       // private commit (heap), excludes mapped files
    pub write_bytes: u64,   // bytes the process handed to WriteFile (mapped-view writes are not counted)
    pub read_bytes: u64,
    pub write_ops: u64,
}

#[cfg(windows)]
pub fn proc_stats() -> ProcStats {
    use windows_sys::Win32::System::ProcessStatus::{K32GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS};
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetProcessIoCounters, IO_COUNTERS};
    unsafe {
        let h = GetCurrentProcess();
        let mut m: PROCESS_MEMORY_COUNTERS = std::mem::zeroed();
        K32GetProcessMemoryInfo(h, &mut m, std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32);
        let mut io: IO_COUNTERS = std::mem::zeroed();
        GetProcessIoCounters(h, &mut io);
        ProcStats { rss: m.WorkingSetSize as u64, peak_rss: m.PeakWorkingSetSize as u64, private: m.PagefileUsage as u64,
                    write_bytes: io.WriteTransferCount, read_bytes: io.ReadTransferCount, write_ops: io.WriteOperationCount }
    }
}

#[cfg(not(windows))]
pub fn proc_stats() -> ProcStats {
    let mut s = ProcStats::default();
    if let Ok(t) = std::fs::read_to_string("/proc/self/status") {
        for l in t.lines() {
            let kb = |l: &str| l.split_whitespace().nth(1).and_then(|v| v.parse::<u64>().ok()).unwrap_or(0) * 1024;
            if l.starts_with("VmRSS:") { s.rss = kb(l); }
            if l.starts_with("VmHWM:") { s.peak_rss = kb(l); }
            if l.starts_with("RssAnon:") { s.private = kb(l); }
        }
    }
    if let Ok(t) = std::fs::read_to_string("/proc/self/io") {
        for l in t.lines() {
            let v = |l: &str| l.split_whitespace().nth(1).and_then(|v| v.parse::<u64>().ok()).unwrap_or(0);
            if l.starts_with("write_bytes:") { s.write_bytes = v(l); }
            if l.starts_with("read_bytes:") { s.read_bytes = v(l); }
            if l.starts_with("syscw:") { s.write_ops = v(l); }
        }
    }
    s
}

/// Append one JSON line to the results file and echo it.
pub fn emit(out: &Option<std::path::PathBuf>, line: &str) {
    println!("{line}");
    if let Some(p) = out {
        let mut f = std::fs::OpenOptions::new().create(true).append(true).open(p).unwrap();
        writeln!(f, "{line}").unwrap();
    }
}

pub struct Args { v: Vec<String> }
impl Args {
    pub fn new() -> Args { Args { v: std::env::args().skip(2).collect() } }
    pub fn get(&self, k: &str) -> Option<String> {
        let key = format!("--{k}");
        self.v.iter().position(|a| *a == key).and_then(|i| self.v.get(i + 1)).cloned()
    }
    pub fn s(&self, k: &str, d: &str) -> String { self.get(k).unwrap_or_else(|| d.to_string()) }
    pub fn n(&self, k: &str, d: u64) -> u64 { self.get(k).map(|v| v.replace('_', "").parse().unwrap_or_else(|_| panic!("--{k}: not a number"))).unwrap_or(d) }
    pub fn f(&self, k: &str, d: f64) -> f64 { self.get(k).map(|v| v.parse().unwrap()).unwrap_or(d) }
    pub fn flag(&self, k: &str) -> bool { self.v.iter().any(|a| *a == format!("--{k}")) }
}
