//! The explorer: v1 `explore/<key>/` (explore.html) and the home page's
//! board.  Same DOM as v1, same classes, same texts, so `atomicdb.css` and
//! the kept v1 scripts (`explore.js`) apply unchanged.
//!
//! Ported: the line box, the opening name, the value crumb and the
//! transpositions, the two public proposals (name, campaign), the board
//! (island), the request row and live line (island), the verdict and the
//! winning line, the play-a-line row, the FEN box, the stats, the raw
//! engine output and the move table with the off-tree moves.
//! Not yet: the signed-in parts (depth slider, multi-select bar, queue
//! bump/withdraw, receipts, pending suggestion), provenance and the
//! returning-line chips.

use super::{Page, json_script};
use crate::format::*;
use crate::islands::board::{Board, BoardData};
use crate::islands::request::{RequestProps, RequestRow};
use crate::pages::shell::{Meta, play_param};
use crate::read::*;
use leptos::prelude::*;

/// Name-suggestion and campaign form limits (v1 constants).
const SUGGEST_NAME_MAX: u32 = 80;
const SUGGEST_COMMENT_MAX: u32 = 280;
const CAMPAIGN_TITLE_MAX: u32 = 64;
const CAMPAIGN_NAME_MAX: u32 = 64;

fn stm_name(white: bool) -> &'static str {
    if white { "White" } else { "Black" }
}

/// v1 `_move_css`.
pub fn move_css(status: Status, score: Option<i32>, win: Status) -> &'static str {
    match status {
        Status::Draw => "draw",
        Status::Unknown => {
            let e = score.unwrap_or(0).unsigned_abs();
            if e >= 500 {
                "hot"
            } else if e >= 200 {
                "warm"
            } else {
                "cold"
            }
        }
        s if s == win => "won",
        _ => "lost",
    }
}

/// v1 `wdl.row_wdl`.
pub fn row_wdl(r: &ChildRow, win: Status) -> Option<Wdl> {
    let closure = r.closure.clone().unwrap_or_default();
    if r.repetition {
        return Some(Wdl::new(0, 100, 0, true, "repetition draw"));
    }
    match r.status {
        Status::Draw => Some(Wdl::new(0, 100, 0, true, &format!("proven draw ({closure})"))),
        Status::Unknown => r.score.map(Wdl::estimate),
        s if s == win => Some(Wdl::new(100, 0, 0, true, &format!("proven win ({closure})"))),
        _ => Some(Wdl::new(0, 0, 100, true, &format!("proven loss ({closure})"))),
    }
}

fn wdl_view(w: Wdl, stm: &'static str) -> impl IntoView {
    let class = if w.proven { "wdl" } else { "wdl wdl-est" };
    view! {
        <span class=class data-stm=stm role="img" title=w.title.clone() aria-label=w.title>
            {(w.w > 0).then(|| view! { <span class="wdl-w" style=format!("width:{}%", w.w)></span> })}
            {(w.d > 0).then(|| view! { <span class="wdl-d" style=format!("width:{}%", w.d)></span> })}
            {(w.l > 0).then(|| view! { <span class="wdl-l" style=format!("width:{}%", w.l)></span> })}
        </span>
    }
}

fn backed_title(b: Backed, own: Option<(i32, u64)>) -> String {
    let mut t = if b.light {
        "value carried up a line a visitor walked. No engine search supports it yet; verification is queued automatically".to_string()
    } else {
        format!("negamax of what the subtree already knows, {} {} below", b.plies, plies_word(b.plies))
    };
    t.push_str(". Click to jump to the position this value comes from");
    if let Some((cp, nodes)) = own {
        t.push_str(&format!(" \u{b7} own search: {cp:+} @ {} nodes", human(nodes)));
    }
    t
}

fn eval_cell(r: &ChildRow, win: Status, stm: &'static str, child_play: &str) -> impl IntoView + use<> {
    let win_here = r.status == win;
    let mate = r.mate_plies.filter(|_| r.status.is_closed() && r.status != Status::Draw).map(|p| mate_str(p, win_here));
    let wdl = row_wdl(r, win).map(|w| wdl_view(w, stm));
    let value = match (&mate, r.score) {
        (Some(m), _) => view! { <strong>{m.clone()}</strong> }.into_any(),
        (None, Some(s)) => {
            let ev = view! { <span class="ev" data-cp=s.to_string() data-stm=stm data-orig=s.to_string()>{s.to_string()}</span> };
            match &r.claim {
                Some(c) => view! { <em class="dim eval-claim" style="font-style:italic" title=c.clone()>{ev}</em> }.into_any(),
                None => ev.into_any(),
            }
        }
        (None, None) => view! { <span class="dim" data-fold="">"not analysed"</span> }.into_any(),
    };
    let has_value = mate.is_some() || r.score.is_some();
    let backed = r.backed.filter(|_| has_value).map(|b| {
        let class = if b.light { "backed-mark light" } else { "backed-mark" };
        let label = format!("{} {}", if b.light { "walked" } else { "backed" }, b.plies);
        let aria = format!("jump to the position this value comes from, {} {} below", b.plies, plies_word(b.plies));
        let href = format!("/atomicdb/backed-source/{}/?play={}", r.key, child_play);
        view! { <a class=class href=href aria-label=aria title=backed_title(b, r.own_search)>{label}</a> }
    });
    let borrowed = (r.borrowed && has_value).then(|| {
        view! { <span class="backed-mark light eval-borrowed" title="this number was written by a search of another parent of this position">"borrowed"</span> }
    });
    let repetition = (r.repetition && has_value).then(|| {
        view! { <span class="backed-mark" title="this move repeats a position of the line: a repetition is a draw">"repetition"</span> }
    });
    view! { <td>{value}{backed}{borrowed}{repetition}{wdl}</td> }
}

fn tree_row(r: ChildRow, win: Status, stm: &'static str, route: &[String]) -> impl IntoView + use<> {
    let css = move_css(r.status, r.score, win);
    let mut child_route = route.to_vec();
    child_route.push(r.uci.clone());
    let child_play = play_param(&child_route);
    let url = format!("/atomicdb/explore/{}/?play={}", r.key, child_play);
    let state = match (r.status, r.closure.clone()) {
        (Status::Unknown, _) => view! { <span class="dim">{if r.unexplored { "unexplored" } else { "unsolved" }}</span> }.into_any(),
        (s, c) => view! { {s.v1()}" "<span class="dim">{c.unwrap_or_default()}</span> }.into_any(),
    };
    let pending = r.pending.clone().map(|p| {
        let style = (p.label == "queued").then_some("border-style:dashed");
        view! { " "<span class="backed-mark pending-mark" data-pending=p.label.clone() title=p.tip style=style>{p.label.clone()}</span> }
    });
    let proof = r.proof.map(|(pn, dn)| format!("{} / {}", proof_number(Some(pn)), proof_number(Some(dn)))).unwrap_or_default();
    let (depth, depth_title) = match r.searched_nodes {
        Some(n) => (budget_label(n), Some(format!("searched to {} nodes \u{b7} visits {}", budget_label(n), r.visits))),
        None => ("-".to_string(), None),
    };
    let enters = r.enters_opening.clone();
    let link_title = enters.as_ref().map(|o| format!("Enters {o}"));
    let san_title = (r.san != r.uci).then(|| r.uci.clone());
    let eval = eval_cell(&r, win, stm, &child_play);
    view! {
        <tr>
            <td><span class=format!("chip {css}")></span></td>
            <td>
                <a href=url title=link_title><strong data-uci=r.uci.clone() title=san_title>{r.san.clone()}</strong></a>
                {enters.map(|o| view! { " "<span class="opening-enters">{o}</span> })}
            </td>
            <td>{state}{pending}</td>
            {eval}
            <td class="dim proofnums">{proof}</td>
            <td class="dim" title=depth_title>{depth}</td>
        </tr>
    }
}

fn offtree_row(m: LegalMove, key: &str, play: &str) -> impl IntoView + use<> {
    let url = crate::islands::board::goto_url(key, &m.uci, play);
    let san_title = (m.san != m.uci).then(|| m.uci.clone());
    view! {
        <tr>
            <td><span class="chip cold"></span></td>
            <td><a href=url><strong data-uci=m.uci.clone() title=san_title>{m.san}</strong></a></td>
            <td><span class="dim">"not in tree"</span></td>
            <td><span class="dim" data-fold="">"-"</span></td>
            <td class="dim proofnums"></td>
            <td class="dim">"-"</td>
        </tr>
    }
}

fn col_head(label: &'static str, col: &'static str, title: Option<&'static str>) -> impl IntoView {
    let aria = format!("About the {label} column");
    let tip = format!("What the {label} column means");
    view! {
        <th title=title>{label}" "<a class="col-info" href=format!("/atomicdb/docs/reading/#col-{col}") style="text-decoration:none;font-weight:normal" aria-label=aria title=tip>"\u{24d8}"</a></th>
    }
}

fn line_box(e: &ExplorerView) -> impl IntoView + use<> {
    let n = e.line.len();
    let more = (!e.line_from_root).then(|| {
        let top = e.line_top_key.clone().unwrap_or_default();
        view! { <a class="line-more" href=format!("/atomicdb/explore/{top}/") title="Earlier moves: open the position this line starts from" aria-label="Earlier moves">"\u{2026}"</a>" " }
    });
    let steps = e
        .line
        .iter()
        .enumerate()
        .map(|(i, st)| {
            let last = i + 1 == n;
            let num = (!st.num.is_empty()).then(|| view! { <span class="dim">{st.num.clone()}</span>" " });
            view! {
                <a href=st.url.clone() class=last.then_some("cur")>{num}<span data-uci=st.uci.clone()>{st.san.clone()}</span></a>
                {(!last).then_some(" ")}
            }
        })
        .collect_view();
    view! { <div class="linebox">{more}{steps}</div> }
}

fn opening_line(e: &ExplorerView) -> Option<AnyView> {
    const BOX: &str = "padding:.1rem .45rem;display:table;max-width:100%;box-sizing:border-box;background:var(--opening-fill);border-radius:6px;line-height:1.45";
    const H2: &str = "display:inline;margin:0;font-size:.95rem;color:var(--opening-ink)";
    if let Some(o) = &e.opening {
        let margin = if e.line.is_empty() { "0" } else { "-.45rem" };
        let style = format!("margin:{margin} 0 .2rem;{BOX};overflow-wrap:anywhere");
        let name = match &o.position_key {
            Some(k) => {
                let mut title = "This opening on the start position and openings page: its value now, best and worst".to_string();
                if let Some(c) = &o.catalog_name {
                    title.push_str(&format!(" \u{b7} previously \u{201c}{c}\u{201d}"));
                }
                view! { <a href=format!("/atomicdb/roots/#op-{k}") style="color:inherit" title=title>{o.name.clone()}</a> }.into_any()
            }
            None => o.name.clone().into_any(),
        };
        let credit = o.community.then(|| {
            let by = o.named_by.clone().unwrap_or_default();
            view! { <span class="dim opening-credit" style="margin-left:.35rem;font-size:.78rem" title=format!("A community name, proposed by {by}")>{format!("\u{b7} named by {by}")}</span> }
        });
        let reference = o
            .reference_line_san
            .clone()
            .map(|r| view! { <span class="dim" style="margin-left:.35rem;font-size:.78rem">{format!("\u{b7} {r}")}</span> });
        Some(view! { <div class="opening-line" style=style><h2 id="opening-name" style=H2>{name}</h2>{reference}{credit}</div> }.into_any())
    } else if e.line.is_empty() && e.line_from_root {
        Some(view! { <div class="opening-line" style=format!("margin:0 0 .2rem;{BOX}")><h2 id="opening-name" style=H2>"Starting position"</h2></div> }.into_any())
    } else {
        None
    }
}

fn pos_tools(e: &ExplorerView, play: &str) -> impl IntoView + use<> {
    let p = &e.pos;
    let white = p.white_to_move();
    let stm_char = if white { "w" } else { "b" };
    let open = p.status == Status::Unknown;
    let eval = p.score.filter(|_| open).map(|s| {
        let s_str = cp_signed(s);
        let backed = p.backed.map(|b| {
            let class = if b.light { "backed-mark light" } else { "backed-mark" };
            let label = format!("{} {}", if b.light { "walked" } else { "backed" }, b.plies);
            let aria = format!("jump to the position this value comes from, {} {} below", b.plies, plies_word(b.plies));
            view! { " "<a class=class href=format!("/atomicdb/backed-source/{}/?play={play}", p.key) aria-label=aria title=backed_title(b, None)>{label}</a> }
        });
        view! {
            <span class="crumb pos-eval" title=format!("the value of this position for the side to move ({})", stm_name(white))>
                "eval "<strong class="ev" data-cp=s.to_string() data-stm=stm_char data-orig=s_str.clone()>{s_str.clone()}</strong>{backed}
            </span>
        }
    });
    let state = open.then(|| {
        if p.score.is_none() {
            Some(view! { <span class="crumb pos-state" title="No engine has searched this position yet. Request analysis to get a value">"unanalysed"</span> }.into_any())
        } else if p.walked && p.backed.is_none() {
            Some(view! { <span class="crumb pos-state walked" title="The value comes from a line that passes through here, not from a search of this position">"walked"</span> }.into_any())
        } else {
            None
        }
    });
    let crumb = e.line.is_empty().then(|| {
        if !e.line_from_root {
            Some(view! { <span class="crumb">"\u{2026} move order unknown"</span> }.into_any())
        } else if e.opening.is_some() {
            Some(view! { <span class="crumb">"root position"</span> }.into_any())
        } else {
            None
        }
    });
    let n = e.parents.len();
    let transpositions = (n > 1).then(|| {
        let links = e
            .parents
            .iter()
            .enumerate()
            .map(|(i, par)| {
                let uci_attr = (par.san != par.uci).then(|| par.uci.clone());
                view! {
                    <a href=format!("/atomicdb/explore/{}/", par.key) title=uci_attr.clone() data-uci=uci_attr>{par.san.clone()}</a>
                    {(i + 1 < n).then_some(" \u{b7} ")}
                }
            })
            .collect_view();
        view! {
            <details class="suggest-name transpositions">
                <summary title="The moves that lead here from other positions">{format!("transpositions ({n})")}</summary>
                <p class="crumb" style="margin:.4rem 0 0">"from: "{links}</p>
            </details>
        }
    });
    let current = e.opening.as_ref().map(|o| o.name.clone());
    let has_name = current.is_some();
    let suggest = view! {
        <details class="suggest-name">
            <summary title=if has_name { "Suggest a correction to this name" } else { "Suggest a name for this position" }>
                {if has_name { "Suggest correction" } else { "Suggest name" }}
            </summary>
            {current.clone().map(|c| view! {
                <p class="suggest-current dim">"Current name: "<strong>{c}</strong>". An approver sees your correction next to it before deciding. To propose that this position have no name, empty the name and say why."</p>
            })}
            <form method="post" action=format!("/atomicdb/suggest/{}/", p.key)>
                <input type="hidden" name="csrfmiddlewaretoken" value=""/>
                <input type="hidden" name="play" value=play.to_string()/>
                <input type="hidden" name="opening" value=""/>
                <label>"Name"<input name="name" maxlength=SUGGEST_NAME_MAX.to_string() required=!has_name value=current.clone().unwrap_or_default()
                    placeholder=if has_name { "empty: this position should have no name" } else { "e.g. Belfast Gambit" } spellcheck="false" autocomplete="off"/></label>
                <label>"Reference or comment "<span class="dim">{if has_name { "(required for an empty name)" } else { "(optional)" }}</span>
                    <input name="comment" maxlength=SUGGEST_COMMENT_MAX.to_string()
                        placeholder=if has_name { "where the name comes from, or why it should go" } else { "where the name comes from" } autocomplete="off"/></label>
                <label>"Move order "<span class="dim">"(optional: the moves from the start position, SAN or UCI)"</span>
                    <input name="line" maxlength="2000" placeholder="e.g. 1. Nf3 f6 2. Nc3" spellcheck="false" autocomplete="off"/></label>
                <button class="reqbtn">"Send for review"</button>
                <span class="dim">"Public, no account. An approver reviews it before it shows up."</span>
            </form>
        </details>
    };
    let campaign = view! {
        <details class="suggest-name">
            <summary title="Propose this line as an exploration campaign">"Propose campaign"</summary>
            <form method="post" action="/atomicdb/campaign/propose/">
                <input type="hidden" name="csrfmiddlewaretoken" value=""/>
                <input type="hidden" name="key" value=p.key.clone()/>
                <input type="hidden" name="back" value="1"/>
                <label>"Title "<span class="dim">"(optional)"</span>
                    <input name="title" maxlength=CAMPAIGN_TITLE_MAX.to_string() placeholder="e.g. Knight tour into e3, or leave blank for the moves" autocomplete="off"/></label>
                <label>"Your name "<span class="dim">"(optional)"</span>
                    <input name="name" maxlength=CAMPAIGN_NAME_MAX.to_string() placeholder="who is putting it up" autocomplete="off"/></label>
                <button class="reqbtn">"Propose this line"</button>
                <span class="dim">"Public, no account. A campaign collects votes on the overview page; the most voted open line is the one the automatic queue works on."</span>
            </form>
        </details>
    };
    view! { <div class="pos-tools">{eval}{state}{crumb}{transpositions}{suggest}{campaign}</div> }
}

fn verdict(e: &ExplorerView) -> Option<impl IntoView + use<>> {
    let p = &e.pos;
    if !p.status.is_closed() {
        return None;
    }
    let win = Status::win_for(p.white_to_move());
    let css = move_css(p.status, p.score, win);
    let closure = p.closure.clone().unwrap_or_default();
    let who = match p.status {
        Status::WhiteWin => "White wins",
        Status::BlackWin => "Black wins",
        _ => "Draw",
    };
    let mate = p.mate_in.map(|m| view! { " \u{b7} "<strong>{verdict_mate(m)}</strong> });
    Some(view! {
        <div class=format!("verdict {css}")>
            <span title=format!("practical verdict, closed via {closure}")>{format!("{who} \u{b7} {closure}")}</span>{mate}
        </div>
    })
}

fn won_line(e: &ExplorerView, play: &str) -> Option<AnyView> {
    let p = &e.pos;
    if !p.won_line.is_empty() {
        let uci = p.won_line.join(" ");
        let n = p.won_line.len() as u32;
        let shown = if p.won_line_san.is_empty() { uci.clone() } else { p.won_line_san.clone() };
        let attrs = (!p.won_line_san.is_empty()).then(|| uci.clone());
        return Some(
            view! {
                <p style="font-size:.85rem"><span class="dim">"winning line:"</span>" "
                    <strong title=attrs.clone() data-uci=attrs>{shown}</strong>" "
                    <a class="backed-mark" href=format!("/atomicdb/proven-line-end/{}/?play={play}", p.key)
                        aria-label=format!("jump to the last position of this winning line, {n} {} below", plies_word(n))
                        title=format!("Follow this line to its end and land on the position it finishes at, {n} {} below", plies_word(n))>{format!("end of line \u{b7}{n}")}</a>
                </p>
            }
            .into_any(),
        );
    }
    if p.status.is_closed() && p.closure.as_deref() == Some("MATE_PV") {
        return Some(
            view! { <p class="dim" style="font-size:.8rem">"closed before line recording was added; the mate was verified move by move at closure time"</p> }.into_any(),
        );
    }
    None
}

#[derive(serde::Serialize)]
struct PlayRow<'a> {
    uci: &'a str,
    key: &'a str,
    url: String,
    score: Option<i32>,
}

#[derive(serde::Serialize)]
struct PlayLine<'a> {
    key: &'a str,
    open: bool,
    best: Option<&'a str>,
    rows: Vec<PlayRow<'a>>,
}

fn fen_box(fen: &str) -> impl IntoView + use<> {
    view! {
        <form class="fenbox fenbox-here" method="post" action="/atomicdb/fen/">
            <input type="hidden" name="csrfmiddlewaretoken" value=""/>
            <input name="fen" value=fen.to_string() spellcheck="false" autocomplete="off"
                aria-label="FEN of this position. Edit it, or paste another FEN or a PGN, to go there"
                title="Edit this FEN, or paste another FEN or a PGN, to go there"/>
            <button class="reqbtn">"Go"</button>
            <button type="button" class="reqbtn fen-icon fen-copy" id="fen-copy" aria-label="Copy this FEN" title="Copy this FEN"
                inner_html=r#"<svg aria-hidden="true" class="ico-copy" height="16" viewBox="0 0 24 24" width="16"><rect height="11" rx="2" width="11" x="9" y="9"></rect><path d="M5 15V5a2 2 0 0 1 2-2h10"></path></svg><svg aria-hidden="true" class="ico-done" height="16" viewBox="0 0 24 24" width="16"><path d="M5 12.5l4.5 4.5L19 7.5"></path></svg>"#>
            </button>
            <a class="reqbtn fen-icon fen-lichess" id="fen-lichess" href=lichess_analysis(fen) target="_blank" rel="noopener"
                aria-label="Open this position on lichess" title="Open this position on the lichess atomic analysis board"
                inner_html=r#"<svg aria-hidden="true" height="16" viewBox="0 0 24 24" width="16"><path d="M14 4h6v6"></path><path d="M20 4l-9 9"></path><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"></path></svg><span class="fen-icon-word">lichess</span>"#>
            </a>
        </form>
    }
}

/// Render the explorer of one position reached by `route`.
pub fn render(e: ExplorerView, route: &[String]) -> Page {
    Owner::new().with(|| render_inner(e, route))
}

fn render_inner(e: ExplorerView, route: &[String]) -> Page {
    let p = e.pos.clone();
    let white = p.white_to_move();
    let stm: &'static str = if white { "w" } else { "b" };
    let win = Status::win_for(white);
    let open = p.status == Status::Unknown;
    let play = play_param(route);
    let route_str = route.join(",");
    let in_tree: std::collections::HashSet<&str> = e.rows.iter().map(|r| r.uci.as_str()).collect();
    let offtree: Vec<LegalMove> = e.legal.iter().filter(|m| !in_tree.contains(m.uci.as_str())).cloned().collect();
    let terminal = p.closure.as_deref() == Some("TERMINAL");

    let title = match &e.opening {
        Some(o) => format!("{} \u{b7} AtomicDB explorer", o.name),
        None => "AtomicDB explorer".to_string(),
    };
    let mut description = match &e.opening {
        Some(o) => format!("{}, an Atomic Chess opening", o.name),
        None => "An Atomic Chess position".to_string(),
    };
    match p.status {
        Status::WhiteWin => description.push_str(": White wins"),
        Status::BlackWin => description.push_str(": Black wins"),
        Status::Draw => description.push_str(": a draw"),
        Status::Unknown => {
            if let Some(s) = p.score {
                description.push_str(&format!(", eval {} for {}", cp_signed(s), stm_name(white)));
            }
        }
    }
    description.push_str(", in AtomicDB's opening tree, with engine analysis and every move from here.");

    let playline = PlayLine {
        key: &p.key,
        open,
        best: p.best_move.as_deref(),
        rows: e
            .rows
            .iter()
            .map(|r| {
                let mut rr = route.to_vec();
                rr.push(r.uci.clone());
                PlayRow { uci: &r.uci, key: &r.key, url: format!("/atomicdb/explore/{}/?play={}", r.key, play_param(&rr)), score: r.score }
            })
            .collect(),
    };
    let playline_json = serde_json::to_string(&playline).unwrap_or_default();

    let request = open.then(|| {
        let props = RequestProps {
            key: p.key.clone(),
            fen: p.fen.clone(),
            visits: p.visits,
            status: p.status.v1().to_string(),
            exhausted: e.request.frontier_exhausted,
            due_label: e.request.due_label.clone(),
            due_nodes: 0,
            subtree_cap: e.request.subtree_cap,
            unexplored: e.request.unexplored_count,
            pv_verify: !e.request.pv_verify_lines.is_empty(),
            pv_lines: e.request.pv_verify_lines.clone(),
            live: e.live.as_ref().map(|l| (l.text.clone(), l.chip.clone())),
        };
        view! { <RequestRow props=props/> }
    });

    let stats_title = "searched = an engine looked at that move itself \u{b7} from lines only = the value was seeded from an engine line through here, and every pass seeds the top moves it reports, so they pile up over passes \u{b7} queued = analysis already requested and waiting";
    let summary = (!p.moves_summary.is_empty()).then(|| {
        view! { " \u{b7} "<span class="moves-summary" title=stats_title>{format!("moves {}", p.moves_summary)}</span> }
    });
    let mut divider_done = false;
    let raw_lines = p
        .analysis
        .iter()
        .map(|l| {
            let divider = (l.prior && !std::mem::replace(&mut divider_done, true))
                .then(|| view! { <div class="rawline rawdivider">"\u{b7} earlier wider pass \u{b7}"</div> });
            let class = if l.prior { "rawline rawprior" } else { "rawline" };
            view! { {divider}<div class=class>{l.raw.clone()}</div> }
        })
        .collect_view();
    let raw = (!p.analysis.is_empty()).then(|| {
        view! { <details><summary>"raw engine output (last analysis)"</summary><div class="raw">{raw_lines}</div></details> }
    });

    let rows_view = if terminal {
        view! { <tr><td colspan="6" class="dim">"Terminal position: the game ended here."</td></tr> }.into_any()
    } else {
        let tree = e.rows.iter().cloned().map(|r| tree_row(r, win, stm, route)).collect_view();
        let off = offtree.into_iter().map(|m| offtree_row(m, &p.key, &route_str)).collect_view();
        view! { {tree}{off} }.into_any()
    };

    let board_props = BoardData {
        key: p.key.clone(),
        fen: p.fen.clone(),
        turn: (if white { "white" } else { "black" }).to_string(),
        best_move: p.best_move.clone().unwrap_or_default(),
        play: route_str.clone(),
        legal: e.legal.iter().map(|m| m.uci.clone()).collect(),
    };
    let body = view! {
        {(!e.line.is_empty()).then(|| line_box(&e))}
        {opening_line(&e)}
        {pos_tools(&e, &play)}
        <div class="cols" style="column-gap:1.1rem">
            <div class="board-col">
                <Board props=board_props/>
                <noscript><p class="dim">"Enable JavaScript for the interactive board; legal moves remain available in the move table."</p></noscript>
                {request}
                {open.then(|| view! { <p class="dim" id="cascade" style="font-size:.8rem;margin:.45rem 0 0" hidden=true></p> })}
                {verdict(&e)}
                {won_line(&e, &play)}
                <p class="reqrow playrow" id="playrow" hidden=true>
                    <button type="button" class="reqbtn" id="play-best" title="Step forward along the best stored move at each position, the move the arrow shows">"Play best line"</button>
                    <button type="button" class="reqbtn" id="play-random" title="Step forward along a random stored move at each position, among those within 30cp of the best">"Play random line"</button>
                    <span class="reqnote" id="playnote" hidden=true></span>" "
                    <span class="dim keys-hint" style="font-size:.75rem" title="Keyboard shortcuts, off while typing in a box">
                        "keys: "<kbd>"\u{2190}"</kbd>" "<kbd>"\u{2192}"</kbd>" back / forward \u{b7} "<kbd>"F"</kbd>" flip board"
                        {open.then(|| view! { " \u{b7} "<kbd>"Q"</kbd>" request analysis" })}
                        " \u{b7} "<kbd>"Esc"</kbd>" stop line"
                    </span>
                </p>
                <script type="application/json" id="atomicdb-playline" inner_html=json_script(&playline_json)></script>
                {fen_box(&p.fen)}
                <p class="dim" style="font-size:.8rem"><a href=format!("/atomicdb/lite/?key={}", p.key)>"Lite view"</a>": this position without JavaScript, for e-readers"</p>
                <p class="dim" style="font-size:.8rem">
                    {format!("visits {} \u{b7} nodes {} \u{b7} {}", p.visits, human(p.nodes_invested), seconds(p.time_invested))}{summary}
                </p>
                {raw}
            </div>
            <div>
                <h2 class="visually-hidden" id="moves-heading">"Moves"</h2>
                <div class="table-scroll movecols">
                    <table aria-labelledby="moves-heading">
                        <tbody>
                            <tr>
                                <th></th>
                                <th>"move"</th>
                                {col_head("state", "state", None)}
                                {col_head("eval", "eval", None)}
                                {col_head("pn / dn", "pndn", Some("Estimated proof / disproof effort for the active campaign. Not a probability."))}
                                {col_head("depth", "depth", Some("The node budget of the deepest search each move's position has had, a dash where none has yet. Hover a cell for its visits."))}
                            </tr>
                            {rows_view}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    };
    Page {
        meta: Meta {
            title,
            description: Some(description),
            canonical: Some(format!("/atomicdb/explore/{}/", p.key)),
            islands: true,
            ..Default::default()
        },
        main_class: "",
        body: body.to_html(),
        scripts: vec!["explore.js", "explore-nav.js"],
    }
}
