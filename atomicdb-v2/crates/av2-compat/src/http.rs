//! The HTTP side: one request, one answer, behind a trait so tests can swap it.

use std::collections::BTreeMap;
use std::io::Read;
use std::time::{Duration, Instant};

use flate2::read::GzDecoder;

/// A request to replay or to send in a scenario.
#[derive(Clone, Debug, Default)]
pub struct Request {
    pub method: String,
    pub path: String,
    pub query: Vec<(String, String)>,
    /// Form fields; sent as `application/x-www-form-urlencoded` when present.
    pub form: Option<Vec<(String, String)>>,
    pub headers: Vec<(String, String)>,
}

impl Request {
    pub fn get(path: &str) -> Request {
        Request { method: "GET".into(), path: path.into(), ..Request::default() }
    }

    pub fn post(path: &str, form: Vec<(String, String)>) -> Request {
        Request { method: "POST".into(), path: path.into(), form: Some(form), ..Request::default() }
    }

    /// Path and query string, as sent.
    pub fn target(&self) -> String {
        if self.query.is_empty() { self.path.clone() } else { format!("{}?{}", self.path, encode_pairs(&self.query)) }
    }
}

/// An answer: status, headers (lower-case names) and the decoded body.
#[derive(Clone, Debug, Default)]
pub struct Response {
    pub status: u16,
    pub headers: BTreeMap<String, String>,
    pub body: Vec<u8>,
}

impl Response {
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }
}

/// Anything that can answer a request.
pub trait Transport {
    fn send(&mut self, request: &Request) -> Result<Response, String>;
}

/// `application/x-www-form-urlencoded`, as Python's `urlencode` writes it.
pub fn encode_pairs(pairs: &[(String, String)]) -> String {
    pairs.iter().map(|(k, v)| format!("{}={}", encode(k), encode(v))).collect::<Vec<_>>().join("&")
}

fn encode(text: &str) -> String {
    let mut out = String::new();
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => out.push(byte as char),
            b' ' => out.push('+'),
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

/// A real HTTP client against `base` (`http://127.0.0.1:8000`,
/// `https://staging.example`), at most `rate` requests a second (0: no cap).
pub struct HttpTransport {
    base: String,
    agent: ureq::Agent,
    interval: Option<Duration>,
    last: Option<Instant>,
}

impl HttpTransport {
    pub fn new(base: &str, rate: f64, timeout: Duration) -> HttpTransport {
        let agent = ureq::AgentBuilder::new().timeout(timeout).redirects(0).user_agent("av2-compat/0.1").build();
        let interval = (rate > 0.0).then(|| Duration::from_secs_f64(1.0 / rate));
        HttpTransport { base: base.trim_end_matches('/').to_string(), agent, interval, last: None }
    }

    fn pace(&mut self) {
        if let (Some(interval), Some(last)) = (self.interval, self.last) {
            let elapsed = last.elapsed();
            if elapsed < interval {
                std::thread::sleep(interval - elapsed);
            }
        }
        self.last = Some(Instant::now());
    }
}

impl Transport for HttpTransport {
    fn send(&mut self, request: &Request) -> Result<Response, String> {
        self.pace();
        let url = format!("{}{}", self.base, request.target());
        let mut call = self.agent.request(&request.method, &url);
        for (name, value) in &request.headers {
            call = call.set(name, value);
        }
        let result = match &request.form {
            Some(form) => call.set("Content-Type", "application/x-www-form-urlencoded").send_string(&encode_pairs(form)),
            None => call.call(),
        };
        let response = match result {
            Ok(response) => response,
            Err(ureq::Error::Status(_, response)) => response,
            Err(other) => return Err(format!("{} {url}: {other}", request.method)),
        };
        let status = response.status();
        let mut headers = BTreeMap::new();
        for name in response.headers_names() {
            if let Some(value) = response.header(&name) {
                headers.insert(name.to_ascii_lowercase(), value.to_string());
            }
        }
        let mut raw = Vec::new();
        response.into_reader().take(64 << 20).read_to_end(&mut raw).map_err(|e| format!("{url}: body: {e}"))?;
        let body = if headers.get("content-encoding").is_some_and(|v| v.eq_ignore_ascii_case("gzip")) {
            let mut decoded = Vec::new();
            GzDecoder::new(&raw[..]).read_to_end(&mut decoded).map_err(|e| format!("{url}: gzip: {e}"))?;
            decoded
        } else {
            raw
        };
        Ok(Response { status, headers, body })
    }
}

/// Production hosts that a stateful run must never touch.
pub const PRODUCTION_HOSTS: &[&str] = &["belzedar.duckdns.org", "94.130.239.23", "178.104.66.19"];

/// True when `base` points at production.
pub fn is_production(base: &str) -> bool {
    let lower = base.to_ascii_lowercase();
    let host = lower.split("://").nth(1).unwrap_or(&lower).split(['/', ':']).next().unwrap_or("");
    PRODUCTION_HOSTS.contains(&host)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_like_python_urlencode() {
        let pairs = vec![("fen".to_string(), "rnbqkbnr/pp w KQkq - 0 1".to_string()), ("x".to_string(), "a&b=c+é".to_string())];
        assert_eq!(encode_pairs(&pairs), "fen=rnbqkbnr%2Fpp+w+KQkq+-+0+1&x=a%26b%3Dc%2B%C3%A9");
    }

    #[test]
    fn recognises_production() {
        assert!(is_production("https://belzedar.duckdns.org"));
        assert!(is_production("https://BELZEDAR.duckdns.org/"));
        assert!(is_production("http://94.130.239.23:8000"));
        assert!(!is_production("http://127.0.0.1:8000"));
        assert!(!is_production("https://staging.belzedar.duckdns.org"));
    }
}
