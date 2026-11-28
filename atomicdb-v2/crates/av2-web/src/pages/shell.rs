//! The document around every page: v1 `atomicdb/base.html`, byte for byte
//! where it can be (same head, header, nav, settings panel, footer and
//! inline scripts), so `atomicdb.css`, `theme.js`, `settings.js` and
//! `notifications.js` work unchanged.
//!
//! A page is rendered in three pieces: everything before the identity zone,
//! the identity zone, and everything after it.  The identity zone is the only
//! part that depends on who is looking (sign-in links or the account's name
//! and bell), so the page cache keeps the two shared pieces and splices the
//! zone in per request (see `server.rs`).

use crate::format::query_value;

/// `&`, `<`, `>`, `"` and `'` escaped: safe in text and in quoted attributes.
pub fn esc(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            _ => out.push(c),
        }
    }
    out
}

/// What the head of a page says about it.
#[derive(Clone, Debug, Default)]
pub struct Meta {
    pub title: String,
    pub description: Option<String>,
    /// Absolute path of the canonical URL (`/atomicdb/explore/<key>/`).
    pub canonical: Option<String>,
    /// Extra tags for the head (page stylesheets).
    pub extra_head: String,
    /// Scripts after the base scripts at the end of the body.
    pub extra_scripts: String,
    /// The page has islands: load the WASM bundle.
    pub islands: bool,
}

/// A signed-in account, as the Django session says (see `auth.rs`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct User {
    pub id: i64,
    pub username: String,
    pub is_staff: bool,
}

/// Who is looking, and where the sign-in links come back to.
#[derive(Clone, Debug, Default)]
pub struct Identity {
    pub user: Option<User>,
    /// The account's notices (bell), when signed in.
    pub notices: Option<crate::read::Notices>,
    /// v1 `identity_next`: the request path, quoted.
    pub next: String,
}

impl Identity {
    pub fn anonymous(path_and_query: &str) -> Identity {
        Identity { user: None, notices: None, next: quote_path(path_and_query) }
    }
}

/// v1 quotes `request.get_full_path()` for the `?next=` links.
pub fn quote_path(p: &str) -> String {
    let mut out = String::with_capacity(p.len());
    for b in p.bytes() {
        match b {
            b'a'..=b'z' | b'A'..=b'Z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' | b'/' => out.push(b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Asset URLs shared by every page.
#[derive(Clone, Debug, Default)]
pub struct Assets {
    /// v1 `static_version`: first 10 hex characters of a digest of the
    /// static tree, appended as `?v=` so a deploy busts browser caches.
    pub version: String,
    /// The `<link>`/`<script>` tags that load and hydrate the islands.
    pub hydration: String,
    /// `scheme://host` of canonical links (v1 uses the request's own).
    pub origin: String,
}

impl Assets {
    pub fn v(&self) -> String {
        if self.version.is_empty() { String::new() } else { format!("?v={}", self.version) }
    }
}

/// The three pieces of a rendered page.
#[derive(Clone, Debug)]
pub struct Rendered {
    pub head: String,
    pub tail: String,
}

impl Rendered {
    pub fn with_identity(&self, identity: &Identity) -> String {
        let zone = identity_zone(identity);
        let mut s = String::with_capacity(self.head.len() + zone.len() + self.tail.len());
        s.push_str(&self.head);
        s.push_str(&zone);
        s.push_str(&self.tail);
        s
    }
}

const PREPAINT: &str = r##"<script>
(function () {
  var root = document.documentElement;
  var theme = "dark";
  root.classList.add("theme-js");
  try {
    var stored = window.localStorage.getItem("atomicdb-theme");
    if (stored === "light" || stored === "dark") {
      theme = stored;
    } else if (window.matchMedia &&
               window.matchMedia("(prefers-color-scheme: light)").matches) {
      theme = "light";
    }
  } catch (_error) {
    if (window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: light)").matches) {
      theme = "light";
    }
  }
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  document.getElementById("atomicdb-theme-color").content =
    theme === "light" ? "#f5f2ea" : "#161512";
  try {
    if (window.localStorage.getItem("atomicdb-notation") === "uci") {
      root.dataset.notation = "uci";
    }
  } catch (_error) {}
  try {
    if (window.localStorage.getItem("atomicdb-wdl-hidden") === "1") {
      var was = (window.localStorage.getItem("atomicdb-hidecols") || "")
        .split(" ").filter(Boolean);
      if (was.indexOf("wdl") < 0) was.push("wdl");
      window.localStorage.setItem("atomicdb-hidecols", was.join(" "));
    }
    window.localStorage.removeItem("atomicdb-wdl-hidden");
  } catch (_error) {}
  try {
    var hide = (window.localStorage.getItem("atomicdb-hidecols") || "")
      .split(" ").filter(function (c) { return /^(state|pn|depth|wdl)$/.test(c); });
    if (hide.length) root.dataset.hidecols = hide.join(" ");
  } catch (_error) {}
  try {
    var board = JSON.parse(window.localStorage.getItem("atomicdb-board") || "null");
    var hex = /^#[0-9a-f]{6}$/i;
    if (board && hex.test(board.l) && hex.test(board.d)) {
      root.style.setProperty("--sq-l", board.l);
      root.style.setProperty("--sq-d", board.d);
      root.dataset.board = "custom";
    }
  } catch (_error) {}
}());
</script>"##;

const HEADER_START: &str = r##"<header>
  <h1><a class="atomicdb-brand" href="/atomicdb/">Atomic<span>DB</span></a></h1>
  <button type="button" class="nav-toggle" id="nav-toggle" aria-expanded="false" aria-controls="site-nav"><svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><span>Menu</span></button>
  <nav id="site-nav">
    <span class="navgroup">
    <a href="/atomicdb/">Analyze</a>
    <a href="/atomicdb/roots/">Openings</a>
    </span><span class="navsep" aria-hidden="true"></span><span class="navgroup">
    <a href="/atomicdb/queue/">Queue</a>
    <a href="/atomicdb/contributors/">Contributors</a>
    <a href="/atomicdb/docs/" title="How AtomicDB works: the queue, the method, the API and running a worker">Site Guide</a>
    <a href="/atomicdb/fun/" title="Fun side quests: atomic chess games to play in the browser">Fun</a>
    </span><span class="navsep" aria-hidden="true"></span><span class="navgroup">
    <a class="nav-ext nav-support" href="https://www.gofundme.com/f/keep-atomicdb-online-solving-atomic-chess" rel="noopener" title="AtomicDB runs on one rented server; the fundraiser keeps it online"><svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M12 21s-7.5-4.6-9.6-9.3C.9 8.3 3 4.5 6.7 4.5c2.1 0 3.6 1.1 4.6 2.6.3.4.9.4 1.2 0 1-1.5 2.5-2.6 4.6-2.6 3.7 0 5.8 3.8 4.3 7.2C19.5 16.4 12 21 12 21z"/></svg>Donate</a>
    <a class="nav-ext nav-openbench" href="/index/"><img src="/static/logo.svg" alt="" width="14" height="14">OpenBench</a>
    <a class="nav-ext nav-discord" href="https://discord.gg/YK4wZ3Xtpv" rel="noopener"><svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z"/></svg>Discord</a>
    </span>
  </nav>
  <div class="identity">
"##;

const SETTINGS_START: &str = r##"    <div class="settings-wrap" id="settings-wrap">
      <button type="button" class="settings-cog" id="settings-cog"
        aria-haspopup="true" aria-expanded="false" aria-controls="settings-panel"
        title="Settings">
        <svg class="settings-icon" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="3"></circle>
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
        </svg>
        <span class="visually-hidden">Settings</span>
      </button>
      <div class="settings-panel" id="settings-panel" hidden>
        <div class="settings-panel-head">Settings</div>
        <fieldset class="settings-row">
          <legend>Theme</legend>
          <div class="settings-choice">
            <label><input type="radio" name="settings-theme" value="dark"><span>Dark</span></label>
            <label><input type="radio" name="settings-theme" value="light"><span>Light</span></label>
          </div>
        </fieldset>
        <fieldset class="settings-row">
          <legend>Move notation</legend>
          <div class="settings-choice">
            <label><input type="radio" name="settings-notation" value="san" checked><span>SAN</span></label>
            <label><input type="radio" name="settings-notation" value="uci"><span>UCI</span></label>
          </div>
        </fieldset>
        <fieldset class="settings-row" title="Centipawns from the side to move, or pawns from White's side">
          <legend>Eval</legend>
          <div class="settings-choice">
            <label><input type="radio" name="settings-eval" value="stm" checked><span>Centipawns</span></label>
            <label><input type="radio" name="settings-eval" value="white"><span>Pawns</span></label>
          </div>
        </fieldset>
        <fieldset class="settings-row" title="Columns shown in the explorer's move table">
          <legend>Explorer columns</legend>
          <div class="settings-cols">
            <label><input type="checkbox" class="settings-col" value="state" checked><span>State</span></label>
            <label><input type="checkbox" class="settings-col" value="pn" checked><span>pn / dn</span></label>
            <label><input type="checkbox" class="settings-col" value="depth" checked><span>Depth</span></label>
            <label title="The win/draw/loss bar under each move's eval"><input type="checkbox" class="settings-col" value="wdl" checked><span>W/D/L bars</span></label>
          </div>
        </fieldset>
        <fieldset class="settings-row settings-board">
          <legend>Board</legend>
          <div class="settings-colours">
            <label><input type="color" id="settings-board-l" value="#f0d9b5"><span>Light</span></label>
            <label><input type="color" id="settings-board-d" value="#b58863"><span>Dark</span></label>
            <label title="Colour of the engine's best-move arrow"><input type="color" id="settings-board-arrow" value="#15781b"><span>Arrow</span></label>
            <button type="button" class="settings-reset" id="settings-board-reset">Reset</button>
          </div>
        </fieldset>
"##;

const HEADER_END: &str = r##"      </div>
    </div>
  </div>
  <button type="button" class="theme-toggle" id="theme-toggle" hidden
    role="switch" aria-checked="true" aria-labelledby="theme-toggle-label"
    title="Switch to light mode">
    <svg class="theme-toggle-sun" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="4.2"></circle>
      <path d="M12 1.9v2.5M12 19.6v2.5M4.4 4.4l1.8 1.8M17.8 17.8l1.8 1.8M1.9 12h2.5M19.6 12h2.5M4.4 19.6l1.8-1.8M17.8 6.2l1.8-1.8"></path>
    </svg>
    <svg class="theme-toggle-moon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M20.4 15.5A8.6 8.6 0 0 1 8.5 3.6 8.9 8.9 0 1 0 20.4 15.5z"></path>
    </svg>
    <span class="visually-hidden" id="theme-toggle-label">Dark mode</span>
  </button>
</header>
"##;

const FOOTER: &str = r##"<footer>AtomicDB, a practical solving effort for Atomic Chess. Results are
  labeled "practical", not game-theoretic certificates. Piece set: cburnett
  (lichess), CC BY-SA.</footer>
<script>
(function () {
  var root = document.documentElement;
  window.atomicdbNotation = function (notation) {
    var uci = notation === "uci";
    var nodes = document.querySelectorAll(uci ? "[data-uci]" : "[data-san]");
    Array.prototype.forEach.call(nodes, function (node) {
      if (!node.hasAttribute("data-san")) {
        node.setAttribute("data-san", node.textContent);
      }
      var san = node.getAttribute("data-san");
      var code = node.getAttribute("data-uci");
      var shown = uci ? code : san;
      var other = uci ? san : code;
      if (node.textContent !== shown) node.textContent = shown;
      if (node.title && node.title !== other) node.title = other;
    });
    if (uci) {
      root.dataset.notation = "uci";
    } else {
      delete root.dataset.notation;
    }
    root.classList.add("notation-ready");
  };
  window.atomicdbNotation(root.dataset.notation === "uci" ? "uci" : "san");
  var navToggle = document.getElementById("nav-toggle");
  if (navToggle) {
    navToggle.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  window.atomicdbEval = function (mode) {
    var white = mode === "white";
    var nodes = document.querySelectorAll(".ev[data-cp]");
    Array.prototype.forEach.call(nodes, function (node) {
      var cp = parseInt(node.getAttribute("data-cp"), 10);
      var shown = node.getAttribute("data-orig");
      if (white && !isNaN(cp)) {
        var v = node.getAttribute("data-stm") === "b" ? -cp : cp;
        shown = (v > 0 ? "+" : v < 0 ? "−" : "") + (Math.abs(v) / 100).toFixed(2);
      }
      if (node.textContent !== shown) node.textContent = shown;
    });
    if (white) {
      root.dataset.eval = "white";
    } else {
      delete root.dataset.eval;
    }
  };
  try {
    if (window.localStorage.getItem("atomicdb-eval") === "white") {
      window.atomicdbEval("white");
    }
  } catch (_error) {}
  // v2: forms carry no per-visitor token in the cached HTML.  Django (4.1+)
  // accepts the raw secret of the csrftoken cookie as the form token, so it
  // is filled in here; a visitor without the cookie gets one minted the way
  // Django mints it (32 characters of [A-Za-z0-9]).
  // Also run again after an in-place navigation (explore-nav.js).
  window.atomicdbCsrf = function () {
    try {
      var m = document.cookie.match(/(?:^|; )csrftoken=([A-Za-z0-9]{32}|[A-Za-z0-9]{64})(?:;|$)/);
      var token = m ? m[1] : "";
      if (!token) {
        var abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
        var bytes = new Uint8Array(32);
        window.crypto.getRandomValues(bytes);
        for (var i = 0; i < 32; i++) token += abc[bytes[i] % 62];
        document.cookie = "csrftoken=" + token + "; path=/; max-age=31449600; samesite=lax";
      }
      Array.prototype.forEach.call(document.querySelectorAll("input[name=csrfmiddlewaretoken]"), function (f) { f.value = token; });
    } catch (_error) {}
  };
  window.atomicdbCsrf();
}());
</script>
"##;

const COLUMNS_SCRIPT: &str = r##"<script>
(function () {
  var root = document.documentElement;
  var boxes = document.querySelectorAll(".settings-col");
  function hidden() {
    return (root.dataset.hidecols || "").split(" ").filter(Boolean);
  }
  function apply(list) {
    if (list.length) root.dataset.hidecols = list.join(" ");
    else delete root.dataset.hidecols;
    Array.prototype.forEach.call(boxes, function (box) {
      box.checked = list.indexOf(box.value) < 0;
    });
  }
  apply(hidden());
  Array.prototype.forEach.call(boxes, function (box) {
    box.addEventListener("change", function () {
      var list = [];
      Array.prototype.forEach.call(boxes, function (b) {
        if (!b.checked) list.push(b.value);
      });
      apply(list);
      try {
        if (list.length) window.localStorage.setItem("atomicdb-hidecols", list.join(" "));
        else window.localStorage.removeItem("atomicdb-hidecols");
      } catch (_error) {}
    });
  });
}());
</script>
</body>
</html>
"##;

/// The identity zone: sign-in links, or the account's name, bell and sign
/// out, then the settings cog (whose "Account settings" link is for
/// accounts only).
pub fn identity_zone(id: &Identity) -> String {
    let mut s = String::with_capacity(SETTINGS_START.len() + 2048);
    match &id.user {
        Some(u) => {
            // The bell (v1 base.html): the account's latest notices, read or
            // not, and the unseen count on the badge (99+ above 99).
            let notices = id.notices.clone().unwrap_or_default();
            let unseen = notices.unseen;
            let quiet = if unseen > 0 { format!("Analyses you asked for, {unseen} new") } else { "Analyses you asked for".into() };
            let badge = if unseen > 99 { "99+".to_string() } else { unseen.to_string() };
            s.push_str(&format!(
                r#"    <div class="bell-wrap" id="bell-wrap">
      <a class="bell" id="bell" href="/atomicdb/notifications/"
        aria-haspopup="true" aria-expanded="false" aria-controls="bell-panel"
        title="Analyses you asked for">
        <svg class="bell-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M18 15.4V10a6 6 0 1 0-12 0v5.4l-1.5 2.1h15z"></path>
          <path d="M9.8 20.4a2.4 2.4 0 0 0 4.4 0"></path>
        </svg>
        <span class="visually-hidden" data-quiet="Analyses you asked for">{quiet}</span>
"#
            ));
            if unseen > 0 {
                s.push_str(&format!(
                    "        <span class=\"bell-badge\" aria-hidden=\"true\" title=\"{unseen} unread result{}\">{badge}</span>\n",
                    if unseen == 1 { "" } else { "s" }
                ));
            }
            s.push_str("      </a>\n      <div class=\"bell-panel\" id=\"bell-panel\" hidden>\n        <div class=\"bell-panel-head\"><span title=\"The latest results, read or not. The badge on the bell counts only the unread ones, so the two numbers can differ\">Analyses you asked for</span>\n");
            if unseen > 0 {
                s.push_str(&format!(
                    "          <form class=\"bell-markall\" method=\"post\" action=\"/atomicdb/notifications/\"><input type=\"hidden\" name=\"csrfmiddlewaretoken\" value=\"\">\n            <input type=\"hidden\" name=\"back\" value=\"{}\">\n            <button type=\"submit\">Mark all read</button>\n          </form>\n",
                    esc(&id.next)
                ));
            }
            s.push_str("        </div>\n        <ul class=\"bell-list\">\n");
            if notices.rows.is_empty() {
                s.push_str("          <li class=\"bell-empty\">Nothing yet. Ask for an analysis from any\n            position and it lands here when a worker serves it.</li>\n");
            }
            for n in &notices.rows {
                let play = if n.play.is_empty() { String::new() } else { format!("?play={}", query_value(&n.play)) };
                s.push_str(&format!(
                    "          <li class=\"bell-item{}\">\n            <a href=\"/atomicdb/explore/{}/{play}\" title=\"{}\">\n              <span class=\"bell-line\">Your analysis of <b>{}</b> is ready</span>\n              <span class=\"bell-when\">{}</span>\n            </a>\n          </li>\n",
                    if n.seen { "" } else { " unseen" },
                    esc(&n.key),
                    esc(&n.full),
                    esc(&n.san),
                    esc(&n.ago)
                ));
            }
            s.push_str(
                "        </ul>\n        <a class=\"bell-all\" href=\"/atomicdb/notifications/\">See all</a>\n      </div>\n    </div>\n",
            );
            s.push_str(&format!(
                "    <a class=\"identity-user\" href=\"/atomicdb/me/\"\n      title=\"Your AtomicDB contributions\">{}</a>\n    <a class=\"identity-link\" href=\"/logout/?next={}\">Sign out</a>\n",
                esc(&u.username),
                esc(&id.next)
            ));
        }
        None => {
            s.push_str(&format!(
                "    <a class=\"identity-link\" href=\"/login/?next={n}\">Sign in</a>\n    <a class=\"identity-link identity-cta\" href=\"/register/?next={n}\">Register</a>\n",
                n = esc(&id.next)
            ));
        }
    }
    s.push_str(SETTINGS_START);
    if id.user.is_some() {
        s.push_str("        <a class=\"settings-link\" href=\"/atomicdb/notifications/settings/\">Account settings</a>\n");
    }
    s
}

/// Render the document around `main_html` (the content of `<main>`).
pub fn document(meta: &Meta, assets: &Assets, main_class: &str, main_html: &str) -> Rendered {
    let v = assets.v();
    let mut head = String::with_capacity(16 * 1024 + main_html.len());
    head.push_str("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<meta name=\"color-scheme\" content=\"dark light\">\n<meta name=\"theme-color\" id=\"atomicdb-theme-color\" content=\"#161512\">\n");
    head.push_str(&format!("<title>{}</title>\n", esc(&meta.title)));
    let description = meta.description.as_deref().unwrap_or(
        "AtomicDB, a community effort to practically solve Atomic Chess: explore the opening tree, engine evaluations, proven lines and named openings.",
    );
    head.push_str(&format!("<meta name=\"description\" content=\"{}\">\n", esc(description)));
    if let Some(c) = &meta.canonical {
        head.push_str(&format!("<link rel=\"canonical\" href=\"{}{}\">\n", esc(&assets.origin), esc(c)));
    }
    head.push_str(&format!("<link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/atomicdb/favicon.svg{v}\">\n"));
    head.push_str(PREPAINT);
    head.push('\n');
    head.push_str(&format!(
        "<link rel=\"stylesheet\" href=\"/static/atomicdb/vendor/chessground/chessground.base.css{v}\">\n<link rel=\"stylesheet\" href=\"/static/atomicdb/vendor/chessground/chessground.brown.css{v}\">\n<link rel=\"stylesheet\" href=\"/static/atomicdb/vendor/chessground/chessground.cburnett.css{v}\">\n<link rel=\"stylesheet\" href=\"/static/atomicdb/atomicdb.css{v}\">\n<script src=\"/static/atomicdb/theme.js{v}\" defer></script>\n<script src=\"/static/atomicdb/notifications.js{v}\" defer></script>\n<script src=\"/static/atomicdb/settings.js{v}\" defer></script>\n"
    ));
    head.push_str(&meta.extra_head);
    if meta.islands {
        head.push_str(&assets.hydration);
    }
    head.push_str("</head>\n<body>\n");
    head.push_str(HEADER_START);
    let mut tail = String::with_capacity(main_html.len() + 8 * 1024);
    tail.push_str(HEADER_END);
    tail.push_str(&format!("<main class=\"{}\">", esc(main_class)));
    tail.push_str(main_html);
    tail.push_str("</main>\n");
    tail.push_str(FOOTER);
    tail.push_str(&meta.extra_scripts);
    tail.push_str(COLUMNS_SCRIPT);
    Rendered { head, tail }
}

/// `?play=` value for a route.
pub fn play_param(route: &[String]) -> String {
    query_value(&route.join(","))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn escapes() {
        assert_eq!(esc(r#"a<b>&"c'"#), "a&lt;b&gt;&amp;&quot;c&#x27;");
    }

    #[test]
    fn identity_zone_switches() {
        let anon = identity_zone(&Identity::anonymous("/atomicdb/roots/?sort=name"));
        assert!(anon.contains("/login/?next=/atomicdb/roots/%3Fsort%3Dname"));
        assert!(!anon.contains("Account settings"));
        let user = Identity { user: Some(User { id: 1, username: "a<b".into(), is_staff: false }), notices: None, next: "/x/".into() };
        let z = identity_zone(&user);
        assert!(z.contains("a&lt;b") && z.contains("Account settings") && z.contains("Sign out"));
    }

    #[test]
    fn document_pieces() {
        let r = document(&Meta { title: "T".into(), ..Default::default() }, &Assets::default(), "", "<p>x</p>");
        let page = r.with_identity(&Identity::anonymous("/atomicdb/"));
        assert!(page.starts_with("<!DOCTYPE html>"));
        assert!(page.contains("<main class=\"\"><p>x</p></main>"));
        assert!(page.ends_with("</html>\n"));
    }
}
