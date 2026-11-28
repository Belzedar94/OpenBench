//! The explorer board (v1 `_board.html` + `board.js`).
//!
//! Server side it renders the board's empty `div` with v1's data attributes;
//! in the browser it mounts Chessground on it (through `board_glue.js`) with
//! the legal moves the server sent, and a move navigates to
//! `/atomicdb/goto/<key>/<uci>/?play=<route>` exactly like v1: the server
//! applies it and redirects, so the board needs no Atomic rules of its own.
//! Promotion is queen by default, underpromotions from the move table (v1).

use leptos::prelude::*;

/// `from -> [to]`, in the order the moves come (v1 board.js `dests`).
pub fn dests(legal: &[String]) -> Vec<(String, Vec<String>)> {
    let mut out: Vec<(String, Vec<String>)> = Vec::new();
    for uci in legal {
        if uci.len() < 4 {
            continue;
        }
        let (from, to) = (&uci[0..2], &uci[2..4]);
        match out.iter_mut().find(|(f, _)| f == from) {
            Some((_, tos)) => {
                if !tos.iter().any(|t| t == to) {
                    tos.push(to.to_string());
                }
            }
            None => out.push((from.to_string(), vec![to.to_string()])),
        }
    }
    out
}

/// v1 board.js `resolvedUci`: the plain move if legal, else the first legal
/// promotion in `q, r, b, n` order.
pub fn resolve(legal: &[String], from: &str, to: &str) -> Option<String> {
    let plain = format!("{from}{to}");
    if legal.contains(&plain) {
        return Some(plain);
    }
    ["q", "r", "b", "n"].iter().map(|s| format!("{plain}{s}")).find(|c| legal.iter().any(|m| m == c))
}

/// Where a move goes (v1 board.js `after`).
pub fn goto_url(key: &str, uci: &str, play: &str) -> String {
    if play.is_empty() {
        format!("/atomicdb/goto/{key}/{uci}/")
    } else {
        format!("/atomicdb/goto/{key}/{uci}/?play={}", crate::format::query_value(play))
    }
}

/// What the board needs; also what an in-place navigation hands to the
/// mounted board (`window.__av2BoardUpdate(<props JSON>)`, explore-nav.js).
#[derive(Clone, Debug, Default, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct BoardData {
    pub key: String,
    pub fen: String,
    /// `white` or `black`.
    pub turn: String,
    pub best_move: String,
    /// The route, comma-joined UCI (v1 `data-play`).
    pub play: String,
    pub legal: Vec<String>,
}

#[island]
pub fn Board(props: BoardData) -> impl IntoView {
    let board_ref = NodeRef::<leptos::html::Div>::new();
    #[cfg(feature = "hydrate")]
    {
        let p = props.clone();
        Effect::new(move |_| {
            if let Some(el) = board_ref.get() {
                client::mount(el.into(), p.clone());
            }
        });
    }
    let BoardData { key, fen, turn, best_move, play, .. } = props;
    view! {
        <div class="boardwrap">
            <div
                id="board"
                class="atomic-board cg-wrap brown cburnett"
                aria-label="Atomic chess board"
                data-key=key
                data-fen=fen
                data-turn=turn
                data-best-move=best_move
                data-play=play
                node_ref=board_ref
            ></div>
        </div>
    }
}

#[cfg(feature = "hydrate")]
mod client {
    use super::BoardData;
    use std::cell::{Cell, RefCell};
    use std::rc::Rc;
    use wasm_bindgen::JsCast;
    use wasm_bindgen::prelude::*;

    #[wasm_bindgen(module = "/src/islands/board_glue.js")]
    extern "C" {
        #[wasm_bindgen(js_name = mountBoard)]
        fn mount_board(
            el: &web_sys::Element,
            fen: &str,
            orientation: &str,
            turn: &str,
            dests: &str,
            colour: &str,
            best_move: &str,
            on_move: &Closure<dyn FnMut(String, String)>,
        ) -> JsValue;
        #[wasm_bindgen(js_name = resetBoard)]
        fn reset_board(ground: &JsValue, fen: &str, turn: &str, dests: &str);
        #[wasm_bindgen(js_name = restoreFen)]
        fn restore_fen(ground: &JsValue, fen: &str);
        #[wasm_bindgen(js_name = toggleOrientation)]
        fn toggle_orientation(ground: &JsValue) -> String;
        #[wasm_bindgen(js_name = setArrow)]
        fn set_arrow(ground: &JsValue, colour: &str, best_move: &str);
    }

    const ARROW_DEFAULT: &str = "#15781b";
    const ORIENTATION_KEY: &str = "atomicdb-orientation";

    fn is_colour(s: &str) -> bool {
        s.len() == 7 && s.starts_with('#') && s[1..].bytes().all(|b| b.is_ascii_hexdigit())
    }

    fn arrow_colour(w: &web_sys::Window) -> String {
        w.local_storage()
            .ok()
            .flatten()
            .and_then(|s| s.get_item("atomicdb-arrow").ok().flatten())
            .filter(|c| is_colour(c))
            .unwrap_or_else(|| ARROW_DEFAULT.to_string())
    }

    fn orientation(w: &web_sys::Window) -> &'static str {
        match w.session_storage().ok().flatten().and_then(|s| s.get_item(ORIENTATION_KEY).ok().flatten()) {
            Some(o) if o == "black" => "black",
            _ => "white",
        }
    }

    fn dests_json(legal: &[String]) -> String {
        serde_json::to_string(&super::dests(legal)).unwrap_or_else(|_| "[]".into())
    }

    /// Go to `url`: in place when explore-nav.js is loaded, else a page load.
    fn navigate(w: &web_sys::Window, url: &str) {
        let nav = js_sys::Reflect::get(w, &JsValue::from_str("atomicdbNavigate")).ok().and_then(|f| f.dyn_into::<js_sys::Function>().ok());
        match nav {
            Some(f) => {
                let _ = f.call1(&JsValue::NULL, &JsValue::from_str(url));
            }
            None => {
                let _ = w.location().assign(url);
            }
        }
    }

    pub fn mount(el: web_sys::Element, props: BoardData) {
        let Some(w) = web_sys::window() else { return };
        let state = Rc::new(RefCell::new(props));
        let ground: Rc<RefCell<JsValue>> = Rc::new(RefCell::new(JsValue::NULL));
        let navigating = Rc::new(Cell::new(false));
        let on_move = {
            let (ground, navigating, state, w) = (ground.clone(), navigating.clone(), state.clone(), w.clone());
            Closure::<dyn FnMut(String, String)>::new(move |from: String, to: String| {
                let s = state.borrow().clone();
                let Some(uci) = super::resolve(&s.legal, &from, &to) else { return };
                if navigating.get() {
                    return;
                }
                navigating.set(true);
                // Chessground knows no Atomic rules: put the server's position
                // back before leaving so a capture never shows as orthodox.
                restore_fen(&ground.borrow(), &s.fen);
                navigate(&w, &super::goto_url(&s.key, &uci, &s.play));
            })
        };
        let p = state.borrow().clone();
        let g = mount_board(&el, &p.fen, orientation(&w), &p.turn, &dests_json(&p.legal), &arrow_colour(&w), &p.best_move, &on_move);
        *ground.borrow_mut() = g;
        on_move.forget();

        // In-place navigation: the board stays mounted and takes the next
        // position (explore-nav.js calls this with the new island's props).
        let update = {
            let (ground, navigating, state, w, el) = (ground.clone(), navigating.clone(), state.clone(), w.clone(), el.clone());
            Closure::<dyn FnMut(String)>::new(move |json: String| {
                // The island's `data-props` wraps its one argument: `{"props": {...}}`.
                let Ok(mut v) = serde_json::from_str::<serde_json::Value>(&json) else { return };
                if let Some(inner) = v.get_mut("props") {
                    v = inner.take();
                }
                let Ok(p) = serde_json::from_value::<BoardData>(v) else { return };
                let g = ground.borrow();
                reset_board(&g, &p.fen, &p.turn, &dests_json(&p.legal));
                set_arrow(&g, &arrow_colour(&w), &p.best_move);
                for (k, v) in [
                    ("data-key", &p.key),
                    ("data-fen", &p.fen),
                    ("data-turn", &p.turn),
                    ("data-best-move", &p.best_move),
                    ("data-play", &p.play),
                ] {
                    let _ = el.set_attribute(k, v);
                }
                *state.borrow_mut() = p;
                navigating.set(false);
            })
        };
        let _ = js_sys::Reflect::set(&w, &JsValue::from_str("__av2BoardUpdate"), update.as_ref());
        update.forget();

        let flip = {
            let (ground, w) = (ground.clone(), w.clone());
            Closure::<dyn FnMut()>::new(move || {
                let o = toggle_orientation(&ground.borrow());
                if let Ok(Some(s)) = w.session_storage() {
                    let _ = s.set_item(ORIENTATION_KEY, &o);
                }
            })
        };
        let _ = w.add_event_listener_with_callback("atomicdb-flip", flip.as_ref().unchecked_ref());
        flip.forget();

        let arrow = {
            let (ground, state) = (ground.clone(), state.clone());
            Closure::<dyn FnMut(web_sys::CustomEvent)>::new(move |e: web_sys::CustomEvent| {
                let c = e.detail().as_string().filter(|c| is_colour(c)).unwrap_or_else(|| ARROW_DEFAULT.to_string());
                set_arrow(&ground.borrow(), &c, &state.borrow().best_move);
            })
        };
        let _ = w.add_event_listener_with_callback("atomicdb-arrow", arrow.as_ref().unchecked_ref());
        arrow.forget();

        // Back from the position a move led to, the browser may restore this
        // page from its back/forward cache mid-navigation: put the board back
        // as the server drew it (v1, seaside_cliff 3-Nov-2026).
        let pageshow = {
            let ground = ground.clone();
            Closure::<dyn FnMut(web_sys::PageTransitionEvent)>::new(move |e: web_sys::PageTransitionEvent| {
                if !e.persisted() {
                    return;
                }
                navigating.set(false);
                let s = state.borrow();
                reset_board(&ground.borrow(), &s.fen, &s.turn, &dests_json(&s.legal));
            })
        };
        let _ = w.add_event_listener_with_callback("pageshow", pageshow.as_ref().unchecked_ref());
        pageshow.forget();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(xs: &[&str]) -> Vec<String> {
        xs.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn dests_group_by_origin() {
        let d = dests(&v(&["e2e3", "e2e4", "g1f3", "a7a8q", "a7a8n"]));
        assert_eq!(d[0], ("e2".to_string(), v(&["e3", "e4"])));
        assert_eq!(d[2], ("a7".to_string(), v(&["a8"])));
    }

    #[test]
    fn promotion_defaults_to_queen() {
        let legal = v(&["a7a8n", "a7a8q", "e2e4"]);
        assert_eq!(resolve(&legal, "a7", "a8").as_deref(), Some("a7a8q"));
        assert_eq!(resolve(&legal, "e2", "e4").as_deref(), Some("e2e4"));
        assert_eq!(resolve(&legal, "e2", "e5"), None);
    }

    #[test]
    fn goto_keeps_route() {
        assert_eq!(goto_url("k", "g1f3", ""), "/atomicdb/goto/k/g1f3/");
        assert_eq!(goto_url("k", "f7f6", "g1f3"), "/atomicdb/goto/k/f7f6/?play=g1f3");
        assert_eq!(goto_url("k", "f7f6", "g1f3,e2e3"), "/atomicdb/goto/k/f7f6/?play=g1f3%2Ce2e3");
    }
}
