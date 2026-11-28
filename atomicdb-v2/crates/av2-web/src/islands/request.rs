//! The explorer's request row and live request line (v1 explore.html, the
//! request script, lines 333-781).
//!
//! Server side: the buttons, the hidden note and the live line, as v1 draws
//! them.  In the browser: the three POSTs (`request/`, `request-unexplored/`,
//! `pv-verify/`) with v1's texts for every answer, the retry on a busy
//! server, the watch on `api/query` that reloads when the analysis lands,
//! the live line polled from `api/live-request/` and the cascade line from
//! `api/frontier/`.  The depth slider (signed-in contributors only) is not
//! ported yet: without it the POST is v1's default click.

use leptos::prelude::*;
use serde::{Deserialize, Serialize};

/// What the row shows and sends (v1 explore context).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct RequestProps {
    pub key: String,
    pub fen: String,
    pub visits: u64,
    pub status: String,
    /// v1 `frontier_exhausted`.
    pub exhausted: bool,
    /// v1 `request_due_label`.
    pub due_label: String,
    /// v1 `request_due_nodes` (sent as `expect`).
    pub due_nodes: u64,
    pub subtree_cap: u32,
    pub unexplored: u32,
    /// "Verify PV" is offered (v1 `pv_verify_plies`).
    pub pv_verify: bool,
    /// v1 `pv_verify_lines`, shown as a choice only with more than one.
    pub pv_lines: Vec<(u32, String)>,
    /// The live line as the server rendered it: `(text, chip)`.
    pub live: Option<(String, String)>,
}

pub fn idle_label(p: &RequestProps) -> String {
    if p.exhausted { format!("Request {}\u{d7}128M", p.subtree_cap) } else { format!("Request {}", p.due_label) }
}

/// v1 button texts for each answer of `request/<key>/`.
pub fn request_text(status: &str, adopted: bool, ahead: Option<i64>, completed_budget: u64, idle: &str) -> String {
    let inline = ahead.map(|a| format!(" (~{a} ahead)")).unwrap_or_default();
    match status {
        "queued" => format!("Queued, waiting for a worker...{inline}"),
        "already-queued" => {
            if adopted {
                format!("Moved to your queue, waiting for a worker...{inline}")
            } else {
                format!("In the queue, waiting for a worker...{inline}")
            }
        }
        "already-requested" => "Already requested, waiting...".into(),
        "rate-limited" => "Hourly limit reached, try later".into(),
        "queue-full" => "Queue is full, try later".into(),
        "queue-full-account" => "You have too many requests waiting".into(),
        "already-analyzed" => format!("Already searched to {}", human_nodes(completed_budget)),
        "already-solved" => "Already solved".into(),
        "expanded" => "Queued below, waiting for a worker...".into(),
        "saturated" => idle.to_string(),
        _ => "Something went wrong".into(),
    }
}

/// v1 JS `humanNodes`.
pub fn human_nodes(n: u64) -> String {
    let f = n as f64;
    if f >= 1e9 { format!("{:.2}B", f / 1e9) } else { format!("{:.1}M", f / 1e6) }
}

fn plural(n: u64, one: &str, many: &str) -> String {
    format!("{n} {}", if n == 1 { one } else { many })
}

/// v1 JS `descentNote`.
pub fn descent_note(status: &str, subtree: bool, message: &str, plies: u64, queued: u64, exhausted_children: u64) -> String {
    if subtree {
        return message.to_string();
    }
    if status == "expanded" {
        let further = if plies > 0 { format!(" ({} further down)", plural(plies, "ply", "plies")) } else { String::new() };
        return format!(
            "Went wide instead: queued {} below{further}; a deep re-run auto-triggers if they disagree",
            plural(queued, "reply", "replies")
        );
    }
    if plies == 0 && exhausted_children == 0 {
        return "Frontier saturated: nothing left to request below this position".into();
    }
    format!(
        "Frontier saturated: walked {} past {} already at full depth, nothing left to request",
        plural(plies, "ply", "plies"),
        plural(exhausted_children, "reply", "replies")
    )
}

#[cfg_attr(not(feature = "hydrate"), allow(dead_code))]
fn analyses(n: u64) -> String {
    format!("Queued {n} analys{}", if n == 1 { "is" } else { "es" })
}

#[island]
pub fn RequestRow(props: RequestProps) -> impl IntoView {
    let idle = idle_label(&props);
    let req_text = RwSignal::new(idle.clone());
    let req_disabled = RwSignal::new(false);
    let all_text = RwSignal::new(format!("Request {} unanalysed", props.unexplored));
    let all_disabled = RwSignal::new(false);
    let pv_text = RwSignal::new("Verify PV".to_string());
    let pv_disabled = RwSignal::new(false);
    let note = RwSignal::new(String::new());
    let note_spent = RwSignal::new(false);
    let (live_text0, live_chip0) = props.live.clone().unwrap_or_default();
    let live_shown = RwSignal::new(props.live.is_some());
    let live_text = RwSignal::new(live_text0);
    let live_chip = RwSignal::new(live_chip0);
    let pv_line = RwSignal::new("0".to_string());
    let then_verify = RwSignal::new(false);

    #[cfg(feature = "hydrate")]
    let ctl = client::Controls {
        p: std::rc::Rc::new(props.clone()),
        idle: idle.clone(),
        req_text,
        req_disabled,
        all_text,
        all_disabled,
        pv_text,
        pv_disabled,
        note,
        note_spent,
        live_shown,
        live_text,
        live_chip,
        pv_line,
        then_verify,
        state: Default::default(),
    };
    #[cfg(feature = "hydrate")]
    {
        let c = ctl.clone();
        Effect::new(move |_| c.start());
    }
    #[cfg(feature = "hydrate")]
    let (on_req, on_all, on_pv, on_verify) = {
        let (a, b, c, d) = (ctl.clone(), ctl.clone(), ctl.clone(), ctl.clone());
        (
            move |_| a.click_request(),
            move |_| b.click_all(),
            move |_| c.click_pv(),
            move |ev: leptos::ev::Event| d.toggle_verify(event_target_checked(&ev)),
        )
    };
    #[cfg(not(feature = "hydrate"))]
    let (on_req, on_all, on_pv, on_verify) = (|_| {}, |_| {}, |_| {}, |_: leptos::ev::Event| {});

    let exhausted = props.exhausted;
    let req_title = exhausted.then(|| {
        format!("Searched to the top of the ladder already: this queues up to {} searches of 128M on the lines below it", props.subtree_cap)
    });
    let show_all = props.unexplored > 0;
    let show_pv = props.pv_verify;
    let lines = props.pv_lines.clone();
    let show_lines = lines.len() > 1;
    view! {
        <p class="reqrow">
            <button class="reqbtn" id="reqbtn" title=req_title disabled=move || req_disabled.get() on:click=on_req>
                {move || req_text.get()}
            </button>
            {(!exhausted).then(|| view! {
                <label class="then-verify" title="When this search lands, queue analysis along the line it finds, so the whole line gets checked">
                    <input type="checkbox" id="thenverify" autocomplete="off" prop:checked=move || then_verify.get() on:change=on_verify/>
                    " then verify its line"
                </label>
            })}
            {show_all.then(|| view! {
                <button class="reqbtn" id="allbtn" title="Queues one engine search for each move in the table that no engine has searched yet"
                    disabled=move || all_disabled.get() on:click=on_all>{move || all_text.get()}</button>
            })}
            {show_pv.then(|| view! {
                <button class="reqbtn" id="pvbtn" title="Walks down a line and queues engine analysis where it is still missing, skipping what is already verified. With no line picked it moves on to the next one when a line is covered end to end; pick a line and it walks that one only"
                    disabled=move || pv_disabled.get() on:click=on_pv>{move || pv_text.get()}</button>
            })}
            {show_lines.then(|| view! {
                <span class="pvlinechoice">
                    <label class="pvlinelab" for="pvline">"line"</label>
                    <select id="pvline" class="pvline" title="Which line of the engine output to walk. The default buys the nearest gap, moving on when a line is covered end to end; pick a line to walk that one and no other"
                        on:change=move |ev| pv_line.set(event_target_value(&ev))>
                        <option value="0">"nearest gap"</option>
                        {lines.into_iter().map(|(i, label)| view! { <option value=i.to_string()>{format!("{i}. {label}")}</option> }).collect_view()}
                    </select>
                </span>
            })}
            " "
            <span class="reqnote" id="reqnote" class:spent=move || note_spent.get() hidden=move || note.get().is_empty()>{move || note.get()}</span>
        </p>
        <p class="livereq" id="livereq" hidden=move || !live_shown.get()>
            <span class=move || format!("chip {}", live_chip.get()) id="livereq-chip"></span>
            <span id="livereq-text">{move || live_text.get()}</span>
        </p>
    }
}

#[cfg(feature = "hydrate")]
mod client {
    use super::*;
    use std::cell::{Cell, RefCell};
    use std::rc::Rc;
    use std::time::Duration;
    use wasm_bindgen::JsCast;
    use wasm_bindgen::prelude::*;
    use wasm_bindgen_futures::{JsFuture, spawn_local};

    const LIVE_EVERY: Duration = Duration::from_millis(20_000);
    const LIVE_MAX_ASKS: u32 = 200;
    const POLL_EVERY: Duration = Duration::from_millis(10_000);

    #[derive(Default)]
    pub struct State {
        below: RefCell<Option<String>>,
        live_asks: Cell<u32>,
        live_watching: Cell<bool>,
        kids0: Cell<Option<i64>>,
        /// `window.__av2Gen` when this row was hydrated: an in-place
        /// navigation bumps it, and this row's timers stop (explore-nav.js).
        generation: Cell<f64>,
    }

    #[derive(Clone)]
    pub struct Controls {
        pub p: Rc<RequestProps>,
        pub idle: String,
        pub req_text: RwSignal<String>,
        pub req_disabled: RwSignal<bool>,
        pub all_text: RwSignal<String>,
        pub all_disabled: RwSignal<bool>,
        pub pv_text: RwSignal<String>,
        pub pv_disabled: RwSignal<bool>,
        pub note: RwSignal<String>,
        pub note_spent: RwSignal<bool>,
        pub live_shown: RwSignal<bool>,
        pub live_text: RwSignal<String>,
        pub live_chip: RwSignal<String>,
        pub pv_line: RwSignal<String>,
        pub then_verify: RwSignal<bool>,
        pub state: Rc<State>,
    }

    fn window() -> web_sys::Window {
        web_sys::window().expect("window")
    }

    fn get(o: &JsValue, k: &str) -> JsValue {
        js_sys::Reflect::get(o, &JsValue::from_str(k)).unwrap_or(JsValue::UNDEFINED)
    }
    fn get_str(o: &JsValue, k: &str) -> String {
        get(o, k).as_string().unwrap_or_default()
    }
    fn get_num(o: &JsValue, k: &str) -> f64 {
        get(o, k).as_f64().unwrap_or(0.0)
    }

    /// The token Django checks: the form inputs carry it (filled from the
    /// csrftoken cookie by the base script).
    fn csrf() -> String {
        window()
            .document()
            .and_then(|d| d.query_selector("[name=csrfmiddlewaretoken]").ok().flatten())
            .and_then(|e| e.dyn_into::<web_sys::HtmlInputElement>().ok())
            .map(|i| i.value())
            .unwrap_or_default()
    }

    fn play_route() -> String {
        let search = window().location().search().unwrap_or_default();
        web_sys::UrlSearchParams::new_with_str(&search).ok().and_then(|p| p.get("play")).unwrap_or_default()
    }

    fn form_body(pairs: &[(&str, String)]) -> String {
        pairs.iter().map(|(k, v)| format!("{k}={}", js_sys::encode_uri_component(v))).collect::<Vec<_>>().join("&")
    }

    struct Answer {
        status: u16,
        retry_after: Option<f64>,
        data: Option<JsValue>,
    }

    async fn fetch(url: &str, post: Option<String>) -> Result<Answer, JsValue> {
        let init = web_sys::RequestInit::new();
        if let Some(body) = post {
            init.set_method("POST");
            let h = web_sys::Headers::new()?;
            h.set("Content-Type", "application/x-www-form-urlencoded")?;
            h.set("X-CSRFToken", &csrf())?;
            init.set_headers(&h);
            init.set_body(&JsValue::from_str(&body));
        }
        let resp: web_sys::Response = JsFuture::from(window().fetch_with_str_and_init(url, &init)).await?.dyn_into()?;
        let retry_after = resp.headers().get("Retry-After").ok().flatten().and_then(|s| s.parse::<f64>().ok());
        let data = match resp.json() {
            Ok(p) => JsFuture::from(p).await.ok(),
            Err(_) => None,
        };
        Ok(Answer { status: resp.status(), retry_after, data })
    }

    fn sleep_then(d: Duration, f: impl FnOnce() + 'static) {
        set_timeout(f, d);
    }

    fn kidsum(d: &JsValue) -> i64 {
        let moves = get(d, "moves");
        let arr: js_sys::Array = match moves.dyn_into() {
            Ok(a) => a,
            Err(_) => return 0,
        };
        arr.iter()
            .map(|m| {
                let visits = get_num(&m, "visits") as i64;
                let score = get(&m, "score");
                let scored = if score.is_null() || score.is_undefined() { 0 } else { 1 };
                let closed = if get_str(&m, "status") != "UNKNOWN" { 1 } else { 0 };
                visits + scored + closed
            })
            .sum()
    }

    fn generation() -> f64 {
        get(&window(), "__av2Gen").as_f64().unwrap_or(0.0)
    }

    impl Controls {
        /// False once the page moved on to another position in place.
        fn alive(&self) -> bool {
            generation() == self.state.generation.get()
        }

        pub fn start(&self) {
            self.state.generation.set(generation());
            if self.p.exhausted {
                *self.state.below.borrow_mut() = Some(self.p.key.clone());
            }
            if let Some(s) = window().local_storage().ok().flatten() {
                self.then_verify.set(s.get_item("atomicdb-then-verify").ok().flatten().as_deref() == Some("1"));
            }
            if self.live_shown.get_untracked() {
                self.live_watch(false);
            }
            let c = self.clone();
            spawn_local(async move {
                if let Ok(a) = fetch(&c.query_url(), None).await
                    && let Some(d) = a.data
                {
                    c.state.kids0.set(Some(kidsum(&d)));
                }
            });
            self.show_cascade();
        }

        fn query_url(&self) -> String {
            format!("/atomicdb/api/query?fen={}", String::from(js_sys::encode_uri_component(&self.p.fen)))
        }

        fn show_note(&self, text: &str, spent: bool) {
            self.note.set(text.to_string());
            self.note_spent.set(spent);
        }

        pub fn toggle_verify(&self, on: bool) {
            self.then_verify.set(on);
            if let Some(s) = window().local_storage().ok().flatten() {
                let _ = s.set_item("atomicdb-then-verify", if on { "1" } else { "0" });
            }
        }

        fn show_cascade(&self) {
            let Some(below) = self.state.below.borrow().clone() else { return };
            spawn_local(async move {
                let Ok(a) = fetch(&format!("/atomicdb/api/frontier/{below}/"), None).await else { return };
                let Some(d) = a.data else { return };
                if !get(&d, "error").is_undefined() && !get(&d, "error").is_null() {
                    return;
                }
                let doc = window().document();
                if let Some(el) = doc.and_then(|d| d.get_element_by_id("cascade")) {
                    el.set_text_content(Some(&format!(
                        "Analyzing beneath this position: {} running, {} queued, {} done ({} children solved)",
                        get_num(&d, "running"),
                        get_num(&d, "queued"),
                        get_num(&d, "done"),
                        get_num(&d, "children_solved")
                    )));
                    let _ = el.remove_attribute("hidden");
                }
            });
        }

        fn live_say(&self, text: &str, chip: &str) {
            self.live_text.set(text.to_string());
            self.live_chip.set(chip.to_string());
            self.live_shown.set(true);
        }

        fn live_later(&self) {
            let c = self.clone();
            sleep_then(LIVE_EVERY, move || c.live_now());
        }

        fn live_now(&self) {
            if !self.alive() {
                return;
            }
            let hidden = window().document().map(|d| d.hidden()).unwrap_or(false);
            if hidden {
                self.live_later();
                return;
            }
            if self.state.live_asks.get() >= LIVE_MAX_ASKS {
                self.live_say("Reload for the latest on this request", "cold");
                return;
            }
            self.state.live_asks.set(self.state.live_asks.get() + 1);
            let c = self.clone();
            spawn_local(async move {
                match fetch(&format!("/atomicdb/api/live-request/{}/", c.p.key), None).await {
                    Ok(Answer { data: Some(d), .. }) => {
                        if !get(&d, "error").is_undefined() && !get(&d, "error").is_null() {
                            return;
                        }
                        if get(&d, "live").is_truthy() {
                            c.live_say(&get_str(&d, "text"), &get_str(&d, "chip"));
                            c.live_later();
                        } else {
                            c.live_say("Analysis landed here \u{b7} reload to see it", "won");
                        }
                    }
                    _ => c.live_later(),
                }
            });
        }

        fn live_watch(&self, ask_now: bool) {
            if self.state.live_watching.get() {
                return;
            }
            self.state.live_watching.set(true);
            if ask_now { self.live_now() } else { self.live_later() }
        }

        fn poll(&self, tries: u32) {
            if !self.alive() {
                return;
            }
            if tries == 0 {
                self.req_text.set("Still queued, check back in a while".into());
                return;
            }
            self.show_cascade();
            let c = self.clone();
            spawn_local(async move {
                let again = {
                    let c = c.clone();
                    move || sleep_then(POLL_EVERY, move || c.poll(tries - 1))
                };
                match fetch(&c.query_url(), None).await {
                    Ok(Answer { data: Some(d), .. }) => {
                        let kids = c.state.kids0.get();
                        let kids_changed = kids.is_some_and(|k| kidsum(&d) != k);
                        if get_num(&d, "visits") as u64 > c.p.visits || get_str(&d, "status") != c.p.status || kids_changed {
                            c.req_text.set("Analysis arrived, reloading...".into());
                            let _ = window().location().reload();
                        } else {
                            again();
                        }
                    }
                    _ => again(),
                }
            });
        }

        /// v1 `window.atomicdbReloadStale` (explore.js): reload once a minute
        /// at most, saying so on the button.
        fn reload_stale(&self, set: impl Fn(String) + 'static) -> bool {
            let f = get(&window(), "atomicdbReloadStale");
            let Ok(f) = f.dyn_into::<js_sys::Function>() else { return false };
            let cb = Closure::<dyn Fn(String)>::new(set);
            let r = f.call1(&JsValue::NULL, cb.as_ref()).map(|v| v.is_truthy()).unwrap_or(false);
            cb.forget();
            r
        }

        pub fn click_request(&self) {
            self.req_disabled.set(true);
            self.show_note("", false);
            self.request(0);
        }

        fn request(&self, attempt: u32) {
            if !self.alive() {
                return;
            }
            let mut body = vec![("route", play_route())];
            if self.then_verify.get_untracked() {
                body.push(("then_verify", "1".into()));
            }
            if !self.p.exhausted {
                body.push(("expect", self.p.due_nodes.to_string()));
            }
            let body = form_body(&body);
            let c = self.clone();
            spawn_local(async move {
                let r = fetch(&format!("/atomicdb/request/{}/", c.p.key), Some(body)).await;
                match r {
                    Ok(a) if a.status == 503 && a.data.as_ref().is_some_and(|d| get_str(d, "status") == "busy") => {
                        c.retry(attempt, a.retry_after.unwrap_or(2.0), false);
                    }
                    Ok(Answer { data: Some(d), .. }) => c.answered(&d),
                    Ok(Answer { data: None, .. }) => {
                        c.req_text.set("Server error, try again".into());
                        c.req_disabled.set(false);
                    }
                    Err(_) => c.retry(attempt, 0.0, true),
                }
            });
        }

        fn retry(&self, attempt: u32, retry_after: f64, network: bool) {
            if !network && attempt < 9 {
                let delay = (retry_after * 1000.0).max(f64::from((1000 + attempt * 1000).min(5000)));
                self.req_text.set("Server busy, retrying...".into());
                let c = self.clone();
                sleep_then(Duration::from_millis(delay as u64), move || c.request(attempt + 1));
            } else {
                self.req_text.set("Network error, try again".into());
                self.req_disabled.set(false);
            }
        }

        fn answered(&self, d: &JsValue) {
            let status = get_str(d, "status");
            let ahead = get(d, "ahead").as_f64().map(|a| a as i64);
            let text = request_text(&status, get(d, "adopted").is_truthy(), ahead, get_num(d, "completed_budget") as u64, &self.idle);
            self.req_text.set(text);
            if ["already-analyzed", "already-solved", "bad-budget"].contains(&status.as_str()) {
                let t = self.req_text;
                if self.reload_stale(move |s| t.set(s)) {
                    return;
                }
            }
            let descent = status == "expanded" || status == "saturated";
            let note = if descent {
                descent_note(
                    &status,
                    get(d, "subtree").is_truthy(),
                    &get_str(d, "message"),
                    get_num(d, "descent_plies") as u64,
                    get_num(d, "children_queued") as u64,
                    get_num(d, "children_exhausted") as u64,
                )
            } else {
                String::new()
            };
            self.show_note(&note, status == "saturated");
            let dk = get_str(d, "descent_key");
            if !dk.is_empty() {
                *self.state.below.borrow_mut() = Some(dk);
            }
            if status == "saturated" {
                self.show_cascade();
            }
            if ["queued", "already-queued", "already-requested"].contains(&status.as_str()) {
                self.live_watch(true);
            }
            if ["queued", "already-queued", "already-requested", "expanded"].contains(&status.as_str()) {
                self.poll(36);
            } else if status != "already-analyzed" {
                self.req_disabled.set(false);
            }
        }

        pub fn click_all(&self) {
            self.all_disabled.set(true);
            self.all_text.set("Queueing...".into());
            let body = form_body(&[("route", play_route())]);
            let c = self.clone();
            spawn_local(async move {
                match fetch(&format!("/atomicdb/request-unexplored/{}/", c.p.key), Some(body)).await {
                    Ok(Answer { data: Some(d), .. }) => {
                        let status = get_str(&d, "status");
                        if status == "queued" {
                            let n = get_num(&d, "queued") as u64;
                            let candidates = get_num(&d, "candidates") as u64;
                            c.all_text.set(if n == 0 && candidates > 0 {
                                format!("All {candidates} already queued; results pending")
                            } else {
                                analyses(n)
                            });
                            c.poll(36);
                        } else {
                            c.all_text.set(
                                match status.as_str() {
                                    "rate-limited" => "Daily limit reached, try tomorrow",
                                    "nothing-to-do" => "Nothing left unexplored here",
                                    "already-solved" => "Already solved",
                                    _ => "Something went wrong",
                                }
                                .into(),
                            );
                        }
                    }
                    _ => {
                        c.all_text.set("Network error, try again".into());
                        c.all_disabled.set(false);
                    }
                }
            });
        }

        pub fn click_pv(&self) {
            self.pv_disabled.set(true);
            self.pv_text.set("Queueing...".into());
            self.show_note("", false);
            let has_choice = self.p.pv_lines.len() > 1;
            let line = if has_choice { self.pv_line.get_untracked() } else { "0".into() };
            let body = form_body(&[("route", play_route()), ("line", line)]);
            let c = self.clone();
            spawn_local(async move {
                match fetch(&format!("/atomicdb/pv-verify/{}/", c.p.key), Some(body)).await {
                    Ok(Answer { data: Some(d), .. }) => {
                        let status = get_str(&d, "status");
                        c.show_note(&get_str(&d, "message"), status == "nothing-to-do" || status == "unknown-line");
                        if status == "queued" {
                            c.pv_text.set(analyses(get_num(&d, "queued") as u64));
                            c.poll(36);
                        } else {
                            c.pv_text.set(
                                match status.as_str() {
                                    "nothing-to-do" => "Nothing left to verify here",
                                    "queue-full" => "Queue is full, try later",
                                    "queue-full-account" => "You have too many requests waiting",
                                    "already-solved" => "Already solved",
                                    "unknown-line" => "That line is gone, reload",
                                    _ => "Something went wrong",
                                }
                                .into(),
                            );
                            if status == "already-solved" || status == "unknown-line" {
                                let t = c.pv_text;
                                if c.reload_stale(move |s| t.set(s)) {
                                    return;
                                }
                            }
                        }
                        if has_choice {
                            c.pv_disabled.set(false);
                        }
                    }
                    _ => {
                        c.pv_text.set("Network error, try again".into());
                        c.pv_disabled.set(false);
                    }
                }
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn texts_match_v1() {
        assert_eq!(request_text("queued", false, Some(3), 0, "x"), "Queued, waiting for a worker... (~3 ahead)");
        assert_eq!(request_text("already-queued", true, None, 0, "x"), "Moved to your queue, waiting for a worker...");
        assert_eq!(request_text("already-analyzed", false, None, 512_000_000, "x"), "Already searched to 512.0M");
        assert_eq!(request_text("saturated", false, None, 0, "Request 78\u{d7}128M"), "Request 78\u{d7}128M");
        assert_eq!(request_text("nonsense", false, None, 0, "x"), "Something went wrong");
        assert_eq!(human_nodes(2_000_000_000), "2.00B");
    }

    #[test]
    fn descent_notes() {
        assert_eq!(
            descent_note("expanded", false, "", 2, 1, 0),
            "Went wide instead: queued 1 reply below (2 plies further down); a deep re-run auto-triggers if they disagree"
        );
        assert_eq!(descent_note("saturated", false, "", 0, 0, 0), "Frontier saturated: nothing left to request below this position");
        assert_eq!(descent_note("saturated", true, "server says", 0, 0, 0), "server says");
    }

    #[test]
    fn idle_labels() {
        let mut p = RequestProps { due_label: "128M".into(), subtree_cap: 78, ..Default::default() };
        assert_eq!(idle_label(&p), "Request 128M");
        p.exhausted = true;
        assert_eq!(idle_label(&p), "Request 78\u{d7}128M");
    }
}
