//! The Django session bridge: a visitor signed in to OpenBench (Django) is
//! signed in to the v2 pages, with no second login.
//!
//! Production (read on 28-Nov-2026, Django 4.2.30): `SESSION_ENGINE` is the
//! default `django.contrib.sessions.backends.db`, the default database is the
//! SQLite file `/opt/openbench/db.sqlite3`, the cookie is `sessionid`
//! (HttpOnly, SameSite=Lax, two weeks), the serializer is JSON and there are
//! no `SECRET_KEY_FALLBACKS`.  So v2 does what Django's
//! `AuthenticationMiddleware` does, read-only:
//!
//! 1. `sessionid` cookie -> `django_session` row with `expire_date` in the
//!    future;
//! 2. `session_data` is `signing.dumps(dict, salt="django.contrib.sessions.SessionStore",
//!    compress=True)`: verify the HMAC-SHA256 signature with `SECRET_KEY`,
//!    base64-decode, inflate when it starts with `.`, parse the JSON;
//! 3. `_auth_user_id` -> `auth_user` row, active;
//! 4. `_auth_user_hash` must equal `salted_hmac(".. get_session_auth_hash",
//!    user.password)`: a password change signs every session out, as in
//!    Django.
//!
//! Answers are cached for a few seconds per session key, so a page view costs
//! one hash-map lookup.  The store never writes: Django keeps owning the
//! sessions (login, logout, expiry, the cleanup job).

use crate::pages::shell::User;
use base64::Engine;
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io::Read;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

const SESSION_SALT: &str = "django.contrib.sessions.SessionStore";
const AUTH_HASH_SALT: &str = "django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash";
const MODEL_BACKEND: &str = "django.contrib.auth.backends.ModelBackend";

/// Django `salted_hmac(key_salt, value, secret, algorithm="sha256")`.
pub fn salted_hmac(key_salt: &str, value: &[u8], secret: &str) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(key_salt.as_bytes());
    h.update(secret.as_bytes());
    let key = h.finalize();
    let mut mac = HmacSha256::new_from_slice(&key).expect("any key length");
    mac.update(value);
    mac.finalize().into_bytes().into()
}

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Django `signing.loads(data, salt=SessionStore)`, without `max_age` (the
/// row's `expire_date` is the expiry, as in `SessionStore.decode`).
pub fn decode_session(data: &str, secret: &str) -> Option<serde_json::Value> {
    let (value, sig) = data.rsplit_once(':')?;
    let expected = salted_hmac(&format!("{SESSION_SALT}signer"), value.as_bytes(), secret);
    let expected = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(expected);
    if !ct_eq(expected.as_bytes(), sig.as_bytes()) {
        return None;
    }
    let (payload, _timestamp) = value.rsplit_once(':')?;
    let (compressed, b64) = match payload.strip_prefix('.') {
        Some(rest) => (true, rest),
        None => (false, payload),
    };
    let raw = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(b64.trim_end_matches('=')).ok()?;
    let json = if compressed {
        let mut out = Vec::new();
        flate2::read::ZlibDecoder::new(&raw[..]).take(1 << 20).read_to_end(&mut out).ok()?;
        out
    } else {
        raw
    };
    serde_json::from_slice(&json).ok()
}

/// Django `AbstractBaseUser.get_session_auth_hash()`.
pub fn session_auth_hash(password: &str, secret: &str) -> String {
    salted_hmac(AUTH_HASH_SALT, password.as_bytes(), secret).iter().map(|b| format!("{b:02x}")).collect()
}

/// `YYYY-MM-DD HH:MM:SS.ffffff`, UTC: how Django stores a datetime in SQLite.
pub fn sqlite_now() -> String {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    let secs = d.as_secs() as i64;
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02} {:02}:{:02}:{:02}.{:06}", rem / 3600, (rem / 60) % 60, rem % 60, d.subsec_micros())
}

/// Who a session cookie belongs to.
pub trait Sessions: Send + Sync {
    fn user(&self, session_key: &str) -> Option<User>;
}

/// No sessions at all: everybody is anonymous (fixture mode without a
/// Django database).
pub struct NoSessions;

impl Sessions for NoSessions {
    fn user(&self, _: &str) -> Option<User> {
        None
    }
}

/// Django's database sessions in its SQLite file, opened read-only.
pub struct DjangoSqliteSessions {
    conn: Mutex<rusqlite::Connection>,
    secret: String,
    ttl: Duration,
    cache: Mutex<HashMap<String, (Instant, Option<User>)>>,
}

impl DjangoSqliteSessions {
    pub fn open(path: &str, secret: String) -> rusqlite::Result<Self> {
        let conn = rusqlite::Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX | rusqlite::OpenFlags::SQLITE_OPEN_URI,
        )?;
        conn.busy_timeout(Duration::from_millis(500))?;
        Ok(Self::with_connection(conn, secret))
    }

    pub fn with_connection(conn: rusqlite::Connection, secret: String) -> Self {
        Self { conn: Mutex::new(conn), secret, ttl: Duration::from_secs(10), cache: Mutex::new(HashMap::new()) }
    }

    fn lookup(&self, session_key: &str) -> Option<User> {
        // Django session keys are 32 characters of [a-z0-9].
        if session_key.is_empty() || session_key.len() > 64 || !session_key.bytes().all(|b| b.is_ascii_alphanumeric()) {
            return None;
        }
        let conn = self.conn.lock().ok()?;
        let data: String = conn
            .query_row(
                "SELECT session_data FROM django_session WHERE session_key = ?1 AND expire_date > ?2",
                rusqlite::params![session_key, sqlite_now()],
                |r| r.get(0),
            )
            .ok()?;
        let session = decode_session(&data, &self.secret)?;
        let backend = session.get("_auth_user_backend")?.as_str()?;
        if backend != MODEL_BACKEND {
            return None;
        }
        let id: i64 = match session.get("_auth_user_id")? {
            serde_json::Value::String(s) => s.parse().ok()?,
            serde_json::Value::Number(n) => n.as_i64()?,
            _ => return None,
        };
        let (username, password, active, staff): (String, String, bool, bool) = conn
            .query_row("SELECT username, password, is_active, is_staff FROM auth_user WHERE id = ?1", [id], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
            })
            .ok()?;
        let hash = session.get("_auth_user_hash")?.as_str()?;
        if !active || !ct_eq(hash.as_bytes(), session_auth_hash(&password, &self.secret).as_bytes()) {
            return None;
        }
        Some(User { id, username, is_staff: staff })
    }
}

impl Sessions for DjangoSqliteSessions {
    fn user(&self, session_key: &str) -> Option<User> {
        let now = Instant::now();
        if let Ok(c) = self.cache.lock()
            && let Some((at, u)) = c.get(session_key)
            && now.duration_since(*at) < self.ttl
        {
            return u.clone();
        }
        let u = self.lookup(session_key);
        if let Ok(mut c) = self.cache.lock() {
            if c.len() > 10_000 {
                c.retain(|_, (at, _)| now.duration_since(*at) < self.ttl);
            }
            c.insert(session_key.to_string(), (now, u.clone()));
        }
        u
    }
}

/// The value of cookie `name` in a `Cookie` header.
pub fn cookie<'a>(header: &'a str, name: &str) -> Option<&'a str> {
    header.split(';').map(str::trim).find_map(|kv| kv.strip_prefix(name)?.strip_prefix('='))
}

#[cfg(test)]
mod tests {
    use super::*;

    // Made with Django 4.2.30: SessionStore().encode(...) under
    // SECRET_KEY = SECRET (see docs/web.md, "Session bridge").
    const SECRET: &str = "av2-web test secret, not a real key";
    const PASSWORD: &str = "pbkdf2_sha256$600000$abcdefghijkl$0123456789abcdefABCDEF0123456789abcdefABCD=";
    const AUTH_HASH: &str = "1c5291d5e556ad77d8c24b554d82f6e365fb4515a3a9deda3bd23865e790c476";
    const SMALL: &str = ".eJxVjDsOwjAQBe_iGln4s_5Q0ucM1tq7wQHkSHFSIe4OkVJA-2bmvUTCba1p67ykicRFeHH63TKWB7cd0B3bbZZlbusyZbkr8qBdDjPx83q4fwcVe_3WqoCOioABHJL3FIq2GcBS0KNj42DMFhSgwUhMaDJpExywj-divRPvD9_oN7g:1xXDI9:uct2xFV7uIwLaWeJdJopB5gNzFnUxjAQ4IXUYrqXSOo";
    const BIG: &str = ".eJztjDsOwjAQRO_iGlnE9toOJT1nsNbeTRxATpSPhIS4O4mUAm5AwZQzb95TBFzmHJaJx9CROAknDp9dxHTjsg10xdL2MvVlHrsoN0Tu6yQvPfH9vLNfgoxTXt9VAlVXBAxgkZwjn5SJAIa8aixrC000UAFqrIkJdSSlvQV29TEZZ1fpgERdaVfZ45-fini9ASe89v0:1xXDI9:Dv2VIqw77NHeRC755dFZ7QkPmbQbxbxK2XUH-H9g30U";

    #[test]
    fn auth_hash_matches_django() {
        assert_eq!(session_auth_hash(PASSWORD, SECRET), AUTH_HASH);
    }

    #[test]
    fn decodes_django_sessions() {
        for data in [SMALL, BIG] {
            let s = decode_session(data, SECRET).expect("valid session");
            assert_eq!(s["_auth_user_id"], "7");
            assert_eq!(s["_auth_user_hash"], AUTH_HASH);
        }
        assert!(decode_session(SMALL, "another secret").is_none());
        let tampered = SMALL.replacen("eJxV", "eJxW", 1);
        assert!(decode_session(&tampered, SECRET).is_none());
    }

    fn db() -> rusqlite::Connection {
        let c = rusqlite::Connection::open_in_memory().unwrap();
        c.execute_batch(
            "CREATE TABLE django_session (session_key varchar(40) PRIMARY KEY, session_data text, expire_date datetime);
             CREATE TABLE auth_user (id integer PRIMARY KEY, username varchar(150), password varchar(128), is_active bool, is_staff bool);",
        )
        .unwrap();
        c.execute("INSERT INTO auth_user VALUES (7, 'belzedar', ?1, 1, 1)", [PASSWORD]).unwrap();
        c.execute("INSERT INTO django_session VALUES ('live0000000000000000000000000000', ?1, '2999-01-01 00:00:00.000000')", [SMALL])
            .unwrap();
        c.execute("INSERT INTO django_session VALUES ('dead0000000000000000000000000000', ?1, '2000-01-01 00:00:00.000000')", [SMALL])
            .unwrap();
        c
    }

    #[test]
    fn signed_in_visitor_is_recognised() {
        let s = DjangoSqliteSessions::with_connection(db(), SECRET.into());
        let u = s.user("live0000000000000000000000000000").expect("signed in");
        assert_eq!((u.id, u.username.as_str(), u.is_staff), (7, "belzedar", true));
        assert!(s.user("dead0000000000000000000000000000").is_none(), "expired session");
        assert!(s.user("nope").is_none());
        assert!(s.user("x' OR 1=1 --").is_none());
    }

    #[test]
    fn password_change_signs_out() {
        let c = db();
        c.execute("UPDATE auth_user SET password = 'pbkdf2_sha256$600000$new$x'", []).unwrap();
        let s = DjangoSqliteSessions::with_connection(c, SECRET.into());
        assert!(s.user("live0000000000000000000000000000").is_none());
    }

    #[test]
    fn cookies_and_time() {
        assert_eq!(cookie("a=1; sessionid=abc; csrftoken=x", "sessionid"), Some("abc"));
        assert_eq!(cookie("xsessionid=abc", "sessionid"), None);
        let now = sqlite_now();
        assert_eq!(now.len(), 26);
        assert!(now.as_str() > "2026-01-01" && now.as_str() < "2100-01-01");
    }
}
