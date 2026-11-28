//! v1 home page (`home.html`): the headline numbers, the running campaign,
//! the root conquest line, proof health, the FEN box, "Now analyzing" and
//! "Up next", the board with the first moves, the top of both contributor
//! boards, the proposed campaigns and the links to the map and milestones.
//!
//! Not ported: the campaign owner's Pause/Close/Activate buttons (staff only;
//! the page is cached for everybody, so they belong in a per-account island
//! or on the campaigns page).

use super::Page;
use crate::format::{human, thousands};
use crate::islands::board::{Board, BoardData};
use crate::pages::contributors::rows_table;
use crate::pages::explore::move_css;
use crate::pages::shell::Meta;
use crate::read::*;
use leptos::prelude::*;

/// Top rows of each contributor board on the home page (v1 shows five).
const BOARD_ROWS: usize = 5;

fn title_case(s: &str) -> String {
    let lower = s.to_lowercase();
    let mut c = lower.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}

fn play_query(play: &str) -> String {
    if play.is_empty() { String::new() } else { format!("?play={}", crate::format::query_value(play)) }
}

fn metric(value: Option<String>, label: &'static str, title: Option<String>, link: Option<&'static str>) -> impl IntoView + use<> {
    let v = value.unwrap_or_else(|| "\u{2014}".into());
    let inner = match link {
        Some(href) => view! { <a href=href>{v}</a> }.into_any(),
        None => v.into_any(),
    };
    view! { <div class="metric" title=title><div class="v">{inner}</div><div class="l">{label}</div></div> }
}

fn feed_row(i: usize, q: &FeedRow, analyzing: bool) -> impl IntoView + use<> {
    let href = format!("/atomicdb/explore/{}/{}", q.key, play_query(&q.play));
    let lead =
        if analyzing { view! { <span class="chip hot"></span> }.into_any() } else { view! { <span class="t">{i + 1}</span> }.into_any() };
    let opening = q.opening.clone().map(|o| view! { " \u{b7} "<span title="Last named Atomic opening on this line">{o}</span> });
    let tail = if analyzing {
        let budget = q.budget.map(|b| format!(" \u{b7} {} nodes", human(b))).unwrap_or_default();
        let machine = q.machine.clone().map(|m| format!(" \u{b7} {m}")).unwrap_or_default();
        Some(
            view! {
                <span class="dim" style="font-size:.8rem">
                    {format!("\u{a0}({})", title_case(q.source.as_deref().unwrap_or("")))}{opening}{budget}{machine}
                </span>
            }
            .into_any(),
        )
    } else if let Some(src) = &q.source {
        Some(view! { <span class="dim" style="font-size:.8rem">{format!("\u{a0}({})", title_case(src))}{opening}</span> }.into_any())
    } else {
        q.opening.clone().map(|o| view! { <span class="dim" style="font-size:.8rem" title="Last named Atomic opening on this line">{format!("\u{a0}{o}")}</span> }.into_any())
    };
    view! { <li>{lead}" "<a class="qline" href=href title=q.full.clone()>{q.san.clone()}</a>{tail.map(|t| view! { " "{t} })}</li> }
}

fn first_move(r: &ChildRow) -> impl IntoView + use<> {
    let css = move_css(r.status, r.score, Status::WhiteWin);
    let enters = r.enters_opening.clone();
    let uci_title = (r.san != r.uci).then(|| r.uci.clone());
    let state = match r.status {
        Status::Unknown => view! { <span class="dim">"unsolved"</span> }.into_any(),
        s => view! { {s.v1()}" "<span class="dim">{r.closure.clone().unwrap_or_default()}</span> }.into_any(),
    };
    let eval = match (r.mate_plies.filter(|_| r.status.is_closed() && r.status != Status::Draw), r.score) {
        (Some(p), _) => view! { <strong>{crate::format::mate_str(p, r.status == Status::WhiteWin)}</strong> }.into_any(),
        (None, Some(s)) => {
            view! { <span class="ev" data-cp=s.to_string() data-stm="w" data-orig=s.to_string()>{s.to_string()}</span> }.into_any()
        }
        (None, None) => view! { <span class="dim">"not analysed"</span> }.into_any(),
    };
    view! {
        <tr>
            <td><span class=format!("chip {css}")></span></td>
            <td>
                <a href=format!("/atomicdb/explore/{}/", r.key) title=enters.as_ref().map(|o| format!("Enters {o}"))>
                    <strong title=uci_title.clone() data-uci=uci_title>{r.san.clone()}</strong>
                </a>
                {enters.map(|o| view! { " "<span class="opening-enters">{o}</span> })}
            </td>
            <td>{state}</td>
            <td>{eval}</td>
            <td class="dim">{r.visits}</td>
        </tr>
    }
}

fn shares(rows: &[Share]) -> String {
    rows.iter().map(|r| format!("{} {:.1}% ({})", r.source, r.pct, r.count)).collect::<Vec<_>>().join(" \u{b7} ")
}

pub fn render(e: ExplorerView, h: HomeView, lb: Leaderboard) -> Page {
    let at = h.counters_at.clone().unwrap_or_default();
    let taken = |what: &str| Some(format!("{what}, taken at {at} UTC"));
    let p = e.pos.clone();
    let board_props = BoardData {
        key: p.key.clone(),
        fen: p.fen.clone(),
        turn: (if p.white_to_move() { "white" } else { "black" }).to_string(),
        best_move: p.best_move.clone().unwrap_or_default(),
        play: String::new(),
        legal: e.legal.iter().map(|m| m.uci.clone()).collect(),
    };
    let body = super::html(move || {
        let metrics = view! {
            <div class="metrics">
                {metric(h.total.map(human), "positions", taken("Exact count of every position in the tree"), None)}
                {metric(h.closed.map(human), "practically solved", taken("Exact count of the positions with a decided status"), None)}
                {metric(h.nodes.map(human), "engine nodes", taken("Exact total of the engine nodes invested in the tree"), None)}
                {metric(h.analyses.map(human), "analyses", None, None)}
                {metric(h.requested.map(thousands), "in queue", Some("Visitor analysis requests still waiting for a worker. Up next previews the selector's own priorities instead, so the two numbers can differ".into()), None)}
                {metric(h.workers.map(|w| w.to_string()), "active workers", Some("Workers with a heartbeat in the last 3 minutes".into()), Some("/atomicdb/workers/"))}
                {metric(h.cores.map(|c| c.to_string()), "active cores", Some("Threads reported by active AtomicDB workers".into()), None)}
                {metric(h.nps.map(human), "nodes / sec", Some("Nodes searched in the analyses completed over the last 10 minutes, per second".into()), None)}
                {metric(h.positions_pm.map(|v| v.to_string()), "positions / min", Some("Completed AtomicDB analyses per minute, averaged over 10 minutes".into()), None)}
                {h.root_pn.zip(h.root_dn).map(|(pn, dn)| view! {
                    {metric(Some(thousands(pn)), "proof number", Some("Proof number of the campaign root: the estimated remaining effort to PROVE the conjecture. Not a probability, and not a percentage.".into()), None)}
                    {metric(Some(thousands(dn)), "disproof number", Some("Disproof number of the campaign root: the estimated effort to REFUTE it. A pn that climbs while this one stalls is the earliest honest sign the conjecture may be false.".into()), None)}
                })}
            </div>
        };
        let cards = h
            .campaign_cards
            .iter()
            .map(|c| {
                let pct = format!("{:.1}", c.solved_pct);
                view! {
                    <section class="campaign-card">
                        <div class="campaign-kicker">"Community campaign \u{b7} running now"</div>
                        <h2 class="campaign-title"><a href=format!("/atomicdb/explore/{}/", c.root_key)>{format!("Campaign: {}", c.title)}</a></h2>
                        {(!c.line_shown.is_empty()).then(|| view! { <div class="campaign-line dim">{c.line_shown.clone()}</div> })}
                        <div class="campaign-stats dim">
                            <span>{format!("{} vote{}", c.votes, if c.votes == 1 { "" } else { "s" })}</span>" "
                            <span>{format!("{} of {} positions explored", thousands(c.explored), thousands(c.total))}</span>" "
                            <span>{format!("{} solved", thousands(c.resolved))}</span>" "
                            <span>{format!("{pct}% of the line solved")}</span>
                        </div>
                        <div class="campaign-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow=pct.clone() aria-label="Share of this campaign already solved">
                            <span style=format!("width:{pct}%")></span>
                        </div>
                        <div class="campaign-by dim">{format!("Proposed by {}", c.proposed_by)}</div>
                    </section>
                }
            })
            .collect_view();
        let progress = h.progress.as_ref().map(|pr| {
            view! {
                <div class="progress"><div class="pkpis dim">
                    {format!(
                        "Root conquest: {}/{} first moves solved \u{b7} {:.1}% of tree positions solved \u{b7} last 24h: {} closures, {} nodes searched",
                        pr.solved_first, pr.n_first, pr.solved_pct, human(pr.closed_24h), human(pr.nodes_24h)
                    )}
                </div></div>
            }
        });
        let health = (h.health.is_some() || h.attribution.is_some()).then(|| {
            let hl = h.health.as_ref().map(|x| {
                let close = x.human_close.as_ref().map(|c| view! { " \u{b7} positions a visitor asked for close in "<b>{c.clone()}</b>{format!(" (median of {})", x.human_close_samples)} });
                view! {
                    <div class="pkpis dim">
                        "Proof health \u{b7} median disproof number on the AND frontier: "<b>{x.dn_median}</b>
                        {format!(" over {} open obligations, of which {} ({:.1}%) sit at or below dn {}, one unanswered question from falling over \u{b7} ", x.and_nodes, x.thin, x.thin_pct, x.dn_floor)}
                        <b>{x.saturated}</b>{format!(" open node{} ", if x.saturated == 1 { "" } else { "s" })}
                        <span title="UNSOLVED positions with an impossible pair of proof numbers: a saturated disproof number next to a finite proof number. Sane recurrences cannot produce it (a saturated dn means proved, and proved means pn = 0), so it is the fingerprint of a value that fed back through a repetition cycle. Zero is the healthy reading. Saturation on its own is NOT counted here: it is usually a proved node still owed its closure, or a line honestly refuted.">"on impossible numbers"</span>
                        {close}
                        {format!(" \u{b7} measured at {} UTC", x.at)}
                    </div>
                }
            });
            let at = h.attribution.as_ref().map(|a| {
                view! {
                    <div class="pkpis dim">
                        {format!(
                            "Closures by source, last 24h ({} of {} labelled): {} \u{b7} last 7d ({} of {}): {}. Labelling started at deploy: closures older than that carry no source and are counted in none of the shares above.",
                            a.stamped_24h, a.total_24h, shares(&a.rows_24h), a.stamped_7d, a.total_7d, shares(&a.rows_7d)
                        )}
                    </div>
                }
            });
            view! { <details class="proof-health"><summary class="dim">"Proof health & closures by source"</summary><div class="progress">{hl}{at}</div></details> }
        });
        let analyzing = if h.analyzing.is_empty() {
            view! { <p class="dim">"No active analyses right now."</p> }.into_any()
        } else {
            view! { <ul class="feed">{h.analyzing.iter().enumerate().map(|(i, q)| feed_row(i, q, true)).collect_view()}</ul> }.into_any()
        };
        let upnext = if h.upnext.is_empty() {
            view! { <p class="dim">"Queue is empty."</p> }.into_any()
        } else {
            view! { <ul class="feed">{h.upnext.iter().enumerate().map(|(i, q)| feed_row(i, q, false)).collect_view()}</ul> }.into_any()
        };
        let legal_links = e
            .legal
            .iter()
            .map(|m| view! { <a href=crate::islands::board::goto_url(&p.key, &m.uci, "") data-uci=m.uci.clone() title=m.uci.clone()>{m.san.clone()}</a> })
            .collect_view();
        let first = if e.rows.is_empty() {
            view! { <p class="dim">"The tree does not include the starting position yet. "<a href=format!("/atomicdb/explore/{}/", p.key)>"Explore the root"</a>"."</p> }.into_any()
        } else {
            view! {
                <div class="table-scroll"><table><tbody>
                    <tr><th></th><th>"move"</th><th>"state"</th><th>"eval"</th><th>"visits"</th></tr>
                    {e.rows.iter().map(first_move).collect_view()}
                </tbody></table></div>
            }
            .into_any()
        };
        let boards = [("Last 24 hours", lb.last_24h.clone()), ("All time", lb.all_time.clone())];
        let any_board = boards.iter().any(|(_, r)| !r.is_empty());
        let contrib = if any_board {
            let sections = boards
                .into_iter()
                .map(|(w, rows)| {
                    let fleet: u64 = rows.iter().map(|r| r.nodes).sum();
                    let content = if rows.is_empty() {
                        view! { <p class="dim">"Nothing in this window yet."</p> }.into_any()
                    } else {
                        rows_table(&rows[..rows.len().min(BOARD_ROWS)], 0, fleet).into_any()
                    };
                    view! { <section><h3 class="contrib-sub">{w}</h3>{content}</section> }
                })
                .collect_view();
            view! {
                <h2 id="contributors">"Contributors "<span class="dim">"by engine nodes"</span>" "
                    <a href="/atomicdb/contributors/" style="font-size:.8rem;font-weight:400">"all contributors"</a></h2>
                <div class="lb-grid">{sections}</div>
            }
            .into_any()
        } else {
            view! { <p class="dim home-elsewhere"><a href="/atomicdb/contributors/">"Contributors"</a>" "<span class="dim">"by engine nodes"</span></p> }.into_any()
        };
        let proposed = if h.campaigns_proposed.is_empty() {
            view! { <p class="dim">"No campaigns proposed yet. Open a line in the explorer and put it up from there."</p> }.into_any()
        } else {
            let rows = h
                .campaigns_proposed
                .iter()
                .map(|c| {
                    view! {
                        <li class="campaign-row">
                            <div class="campaign-row-line">
                                <a class="qline" href=format!("/atomicdb/explore/{}/", c.root_key)>{c.title.clone()}</a>
                                {(!c.line_shown.is_empty()).then(|| view! { <div class="campaign-row-san dim">{c.line_shown.clone()}</div> })}
                            </div>
                            <div class="campaign-row-votes dim">{format!("proposed by {} \u{b7} {} vote{}", c.proposed_by, c.votes, if c.votes == 1 { "" } else { "s" })}</div>
                            <div class="campaign-actions">
                                <form method="post" action=format!("/atomicdb/campaign/{}/vote/", c.id)>
                                    <input type="hidden" name="csrfmiddlewaretoken" value=""/>
                                    <input type="hidden" name="back" value="1"/>
                                    <button class="reqbtn">"Vote"</button>
                                </form>
                            </div>
                        </li>
                    }
                })
                .collect_view();
            let more = (h.campaigns_proposed_total > h.campaigns_proposed.len())
                .then(|| view! { <p class="dim campaign-more"><a href="/atomicdb/campaigns/">"see every proposal"</a></p> });
            view! { <ul class="campaign-list">{rows}</ul>{more} }.into_any()
        };
        view! {
            {metrics}
            <p class="support-line">"AtomicDB runs on one rented server, paid for by the community. "
                <a href="https://www.gofundme.com/f/keep-atomicdb-online-solving-atomic-chess" rel="noopener">"Help keep it online"</a>"."</p>
            {cards}
            {progress}
            {health}
            <form class="fenbox" method="post" action="/atomicdb/fen/">
                <input type="hidden" name="csrfmiddlewaretoken" value=""/>
                <input name="fen" placeholder="An opening name, a FEN or a PGN to jump there, or Go for the start" spellcheck="false" autocomplete="off"/>
                <button class="reqbtn">"Go"</button>
            </form>
            <div class="queue">
                <div>
                    <h2 style="margin-top:0" title="Positions currently leased to a worker, up to 5 shown. Active workers counts every machine with a recent heartbeat, including ones between tasks, so the two numbers can differ">
                        "Now analyzing "<a href="/atomicdb/workers/" style="font-size:.8rem;font-weight:400" title="Every active worker, what it is searching and who asked for it">"all workers"</a>
                    </h2>
                    {analyzing}
                </div>
                <div>
                    <h2 style="margin-top:0" title="The unsolved positions the selector ranks highest right now, top 5. The in-queue metric counts visitor requests instead, so the two numbers can differ">"Up next"</h2>
                    {upnext}
                </div>
            </div>
            <div class="cols">
                <div style="width:440px;max-width:100%">
                    <h2 style="margin-top:0">"Explore"</h2>
                    <Board props=board_props/>
                    <noscript><p class="dim">"Enable JavaScript for the interactive board; legal moves remain available below."</p></noscript>
                    <details class="board-legal"><summary>"Keyboard move list"</summary><nav aria-label="Legal moves">{legal_links}</nav></details>
                </div>
                <div>
                    <h2 style="margin-top:0">"First moves"</h2>
                    {first}
                </div>
            </div>
            {contrib}
            <p class="dim home-elsewhere"><a href="/atomicdb/map/">"Move tree"</a>" "<span class="dim">"the explored tree drawn as a map"</span></p>
            <h2 id="campaigns">"Proposed campaigns"</h2>
            {proposed}
            <p class="dim home-elsewhere"><a href="/atomicdb/milestones/">"Milestones"</a>" "<span class="dim">"the latest solved positions"</span></p>
        }
    });
    Page {
        meta: Meta {
            title: "AtomicDB, the Atomic Chess conquest map".into(),
            canonical: Some("/atomicdb/".into()),
            islands: true,
            ..Default::default()
        },
        main_class: "",
        body,
        scripts: vec![],
    }
}
