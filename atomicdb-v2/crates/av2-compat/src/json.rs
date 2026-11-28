//! A JSON value that keeps what byte compatibility is made of.
//!
//! `serde_json::Value` forgets the order of object keys (unless the
//! workspace-wide `preserve_order` feature is on, which would change every
//! other crate) and turns numbers into `f64`/`i64`, so `1.0` and `1` look the
//! same.  v1's bodies are compared against v2's field by field *and* in key
//! order, with `1.0` and `1` different, so this module parses into its own
//! value: objects as ordered `(key, value)` lists, numbers as their exact text.

use std::fmt::Write as _;

/// A parsed JSON value.  Numbers keep their source text; objects keep their
/// key order and any duplicate keys.
#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    /// The number exactly as written (`12`, `-3`, `0.5`, `1e-05`).
    Num(String),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

/// What a value is, for shape comparisons and messages.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Null,
    Bool,
    Int,
    Float,
    Str,
    Arr,
    Obj,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Null => "null",
            Kind::Bool => "bool",
            Kind::Int => "int",
            Kind::Float => "float",
            Kind::Str => "string",
            Kind::Arr => "array",
            Kind::Obj => "object",
        }
    }
}

impl Json {
    pub fn kind(&self) -> Kind {
        match self {
            Json::Null => Kind::Null,
            Json::Bool(_) => Kind::Bool,
            Json::Num(text) => {
                if text.contains(['.', 'e', 'E']) {
                    Kind::Float
                } else {
                    Kind::Int
                }
            }
            Json::Str(_) => Kind::Str,
            Json::Arr(_) => Kind::Arr,
            Json::Obj(_) => Kind::Obj,
        }
    }

    /// The value of `key` in an object (the first one, if duplicated).
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(fields) => fields.iter().find(|(name, _)| name == key).map(|(_, value)| value),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(text) => Some(text),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Num(text) => text.parse().ok(),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Json]> {
        match self {
            Json::Arr(items) => Some(items),
            _ => None,
        }
    }

    /// Compact JSON text, for messages and reports (not for byte checks: the
    /// original body text is what those compare).
    pub fn to_compact(&self) -> String {
        let mut out = String::new();
        self.write_compact(&mut out);
        out
    }

    fn write_compact(&self, out: &mut String) {
        match self {
            Json::Null => out.push_str("null"),
            Json::Bool(value) => out.push_str(if *value { "true" } else { "false" }),
            Json::Num(text) => out.push_str(text),
            Json::Str(text) => write_string(text, out),
            Json::Arr(items) => {
                out.push('[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    item.write_compact(out);
                }
                out.push(']');
            }
            Json::Obj(fields) => {
                out.push('{');
                for (index, (key, value)) in fields.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    write_string(key, out);
                    out.push(':');
                    value.write_compact(out);
                }
                out.push('}');
            }
        }
    }

    /// The same value cut to `limit` characters, for a one-line message.
    pub fn preview(&self, limit: usize) -> String {
        let text = self.to_compact();
        if text.chars().count() <= limit {
            text
        } else {
            let cut: String = text.chars().take(limit).collect();
            format!("{cut}...")
        }
    }
}

fn write_string(text: &str, out: &mut String) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Why a text is not JSON, and where.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseError {
    pub offset: usize,
    pub message: String,
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} at byte {}", self.message, self.offset)
    }
}

impl std::error::Error for ParseError {}

/// Parse one JSON document (surrounding whitespace allowed, nothing else).
pub fn parse(text: &str) -> Result<Json, ParseError> {
    let mut parser = Parser { bytes: text.as_bytes(), text, pos: 0, depth: 0 };
    parser.skip_ws();
    let value = parser.value()?;
    parser.skip_ws();
    if parser.pos != parser.bytes.len() {
        return Err(parser.error("trailing characters"));
    }
    Ok(value)
}

const MAX_DEPTH: usize = 256;

struct Parser<'a> {
    bytes: &'a [u8],
    text: &'a str,
    pos: usize,
    depth: usize,
}

impl Parser<'_> {
    fn error(&self, message: &str) -> ParseError {
        ParseError { offset: self.pos, message: message.to_string() }
    }

    fn skip_ws(&mut self) {
        while let Some(b' ' | b'\t' | b'\n' | b'\r') = self.bytes.get(self.pos) {
            self.pos += 1;
        }
    }

    fn eat(&mut self, literal: &str) -> Result<(), ParseError> {
        if self.text[self.pos..].starts_with(literal) {
            self.pos += literal.len();
            Ok(())
        } else {
            Err(self.error(&format!("expected {literal}")))
        }
    }

    fn value(&mut self) -> Result<Json, ParseError> {
        match self.bytes.get(self.pos) {
            None => Err(self.error("unexpected end")),
            Some(b'n') => self.eat("null").map(|_| Json::Null),
            Some(b't') => self.eat("true").map(|_| Json::Bool(true)),
            Some(b'f') => self.eat("false").map(|_| Json::Bool(false)),
            Some(b'"') => self.string().map(Json::Str),
            Some(b'[') => self.array(),
            Some(b'{') => self.object(),
            Some(b'-' | b'0'..=b'9') => self.number(),
            Some(_) => Err(self.error("unexpected character")),
        }
    }

    fn enter(&mut self) -> Result<(), ParseError> {
        self.depth += 1;
        if self.depth > MAX_DEPTH { Err(self.error("nested too deeply")) } else { Ok(()) }
    }

    fn array(&mut self) -> Result<Json, ParseError> {
        self.enter()?;
        self.pos += 1;
        let mut items = Vec::new();
        self.skip_ws();
        if self.bytes.get(self.pos) == Some(&b']') {
            self.pos += 1;
            self.depth -= 1;
            return Ok(Json::Arr(items));
        }
        loop {
            self.skip_ws();
            items.push(self.value()?);
            self.skip_ws();
            match self.bytes.get(self.pos) {
                Some(b',') => self.pos += 1,
                Some(b']') => {
                    self.pos += 1;
                    self.depth -= 1;
                    return Ok(Json::Arr(items));
                }
                _ => return Err(self.error("expected , or ]")),
            }
        }
    }

    fn object(&mut self) -> Result<Json, ParseError> {
        self.enter()?;
        self.pos += 1;
        let mut fields = Vec::new();
        self.skip_ws();
        if self.bytes.get(self.pos) == Some(&b'}') {
            self.pos += 1;
            self.depth -= 1;
            return Ok(Json::Obj(fields));
        }
        loop {
            self.skip_ws();
            if self.bytes.get(self.pos) != Some(&b'"') {
                return Err(self.error("expected a key"));
            }
            let key = self.string()?;
            self.skip_ws();
            if self.bytes.get(self.pos) != Some(&b':') {
                return Err(self.error("expected :"));
            }
            self.pos += 1;
            self.skip_ws();
            let value = self.value()?;
            fields.push((key, value));
            self.skip_ws();
            match self.bytes.get(self.pos) {
                Some(b',') => self.pos += 1,
                Some(b'}') => {
                    self.pos += 1;
                    self.depth -= 1;
                    return Ok(Json::Obj(fields));
                }
                _ => return Err(self.error("expected , or }")),
            }
        }
    }

    fn number(&mut self) -> Result<Json, ParseError> {
        let start = self.pos;
        if self.bytes.get(self.pos) == Some(&b'-') {
            self.pos += 1;
        }
        let digits = |p: &mut Self| {
            let begin = p.pos;
            while let Some(b'0'..=b'9') = p.bytes.get(p.pos) {
                p.pos += 1;
            }
            p.pos > begin
        };
        if !digits(self) {
            return Err(self.error("expected digits"));
        }
        if self.bytes.get(self.pos) == Some(&b'.') {
            self.pos += 1;
            if !digits(self) {
                return Err(self.error("expected fraction digits"));
            }
        }
        if let Some(b'e' | b'E') = self.bytes.get(self.pos) {
            self.pos += 1;
            if let Some(b'+' | b'-') = self.bytes.get(self.pos) {
                self.pos += 1;
            }
            if !digits(self) {
                return Err(self.error("expected exponent digits"));
            }
        }
        Ok(Json::Num(self.text[start..self.pos].to_string()))
    }

    fn hex4(&mut self) -> Result<u32, ParseError> {
        let chunk = self.text.get(self.pos..self.pos + 4).ok_or_else(|| self.error("short \\u escape"))?;
        let value = u32::from_str_radix(chunk, 16).map_err(|_| self.error("bad \\u escape"))?;
        self.pos += 4;
        Ok(value)
    }

    fn string(&mut self) -> Result<String, ParseError> {
        self.pos += 1;
        let mut out = String::new();
        loop {
            let start = self.pos;
            while let Some(&b) = self.bytes.get(self.pos) {
                if b == b'"' || b == b'\\' || b < 0x20 {
                    break;
                }
                self.pos += 1;
            }
            out.push_str(&self.text[start..self.pos]);
            match self.bytes.get(self.pos) {
                None => return Err(self.error("unterminated string")),
                Some(b'"') => {
                    self.pos += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.pos += 1;
                    let escaped = *self.bytes.get(self.pos).ok_or_else(|| self.error("unterminated escape"))?;
                    self.pos += 1;
                    match escaped {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let first = self.hex4()?;
                            let code = if (0xD800..0xDC00).contains(&first) {
                                if self.text[self.pos..].starts_with("\\u") {
                                    self.pos += 2;
                                    let second = self.hex4()?;
                                    if !(0xDC00..0xE000).contains(&second) {
                                        return Err(self.error("bad surrogate pair"));
                                    }
                                    0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00)
                                } else {
                                    return Err(self.error("lone surrogate"));
                                }
                            } else {
                                first
                            };
                            out.push(char::from_u32(code).ok_or_else(|| self.error("bad code point"))?);
                        }
                        _ => return Err(self.error("bad escape")),
                    }
                }
                Some(_) => return Err(self.error("control character in string")),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_key_order_and_number_text() {
        let value = parse(r#"{"b": 1, "a": 1.0, "c": [1e-05, -0, null, true, "x"]}"#).unwrap();
        let Json::Obj(fields) = &value else { panic!() };
        let keys: Vec<&str> = fields.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(keys, ["b", "a", "c"]);
        assert_eq!(value.get("b"), Some(&Json::Num("1".into())));
        assert_eq!(value.get("a").unwrap().kind(), Kind::Float);
        assert_eq!(value.get("b").unwrap().kind(), Kind::Int);
        assert_ne!(value.get("a"), value.get("b"));
        assert_eq!(value.to_compact(), r#"{"b":1,"a":1.0,"c":[1e-05,-0,null,true,"x"]}"#);
    }

    #[test]
    fn decodes_python_ascii_escapes() {
        // Django's JsonResponse writes non-ASCII as \uXXXX, astral as pairs.
        let value = parse(r#""Caf\u00e9 \u2212 \ud83d\ude00 \"q\" \\""#).unwrap();
        assert_eq!(value, Json::Str("Café − 😀 \"q\" \\".into()));
    }

    #[test]
    fn rejects_garbage() {
        for bad in ["", "{", "[1,]", "{\"a\" 1}", "01x", "\"\\x\"", "nul", "1 2", "\"\\ud800\""] {
            assert!(parse(bad).is_err(), "{bad:?} parsed");
        }
    }

    #[test]
    fn depth_is_bounded() {
        let deep = "[".repeat(MAX_DEPTH + 1) + &"]".repeat(MAX_DEPTH + 1);
        assert!(parse(&deep).is_err());
        let ok = "[".repeat(MAX_DEPTH) + &"]".repeat(MAX_DEPTH);
        assert!(parse(&ok).is_ok());
    }
}
