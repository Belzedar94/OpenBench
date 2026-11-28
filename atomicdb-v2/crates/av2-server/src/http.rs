//! A minimal HTTP/1.1 endpoint (std only) for `/metrics` and `/healthz`.
//! The public site and API are another crate's (`av2-web`); this one only
//! has to answer a scraper.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;

/// `path -> (status, content type, body)`.
pub type Handler = Arc<dyn Fn(&str) -> (u16, &'static str, String) + Send + Sync>;

/// Serve `handler` on `addr` from a background thread; returns the bound address.
pub fn serve(addr: &str, handler: Handler) -> std::io::Result<(SocketAddr, JoinHandle<()>)> {
    let l = TcpListener::bind(addr)?;
    let local = l.local_addr()?;
    let h = std::thread::Builder::new().name("av2-metrics".into()).spawn(move || {
        for s in l.incoming().flatten() {
            let _ = handle(s, &handler);
        }
    })?;
    Ok((local, h))
}

fn handle(mut s: TcpStream, handler: &Handler) -> std::io::Result<()> {
    s.set_read_timeout(Some(Duration::from_secs(5)))?;
    let mut buf = [0u8; 8192];
    let mut n = 0;
    while n < buf.len() {
        let k = s.read(&mut buf[n..])?;
        if k == 0 {
            break;
        }
        n += k;
        if buf[..n].windows(4).any(|w| w == b"\r\n\r\n") {
            break;
        }
    }
    let req = String::from_utf8_lossy(&buf[..n]);
    let mut first = req.lines().next().unwrap_or("").split_whitespace();
    let (method, path) = (first.next().unwrap_or(""), first.next().unwrap_or("/"));
    let (code, ctype, body) =
        if method == "GET" { handler(path.split('?').next().unwrap_or("/")) } else { (405, "text/plain", "GET only\n".into()) };
    let reason = match code {
        200 => "OK",
        404 => "Not Found",
        405 => "Method Not Allowed",
        _ => "Error",
    };
    write!(s, "HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len())?;
    s.write_all(body.as_bytes())?;
    s.flush()
}

/// GET `path` from `addr` (for tests and the bench mode).
pub fn get(addr: SocketAddr, path: &str) -> std::io::Result<String> {
    let mut s = TcpStream::connect(addr)?;
    write!(s, "GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")?;
    let mut out = String::new();
    s.read_to_string(&mut out)?;
    Ok(out.split_once("\r\n\r\n").map(|(_, b)| b.to_string()).unwrap_or_default())
}
