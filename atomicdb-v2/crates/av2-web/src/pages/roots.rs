//! v1 `roots/` (roots.html): the start position and its first moves, the
//! main line, and every named opening with its votes, values from White's
//! side in pawns.
//!
//! Not yet: the best-record chart (`_root_chart.html`, needs the progress
//! snapshots) and the main line by day.

use super::{Page, html};
use crate::format::pawns_white;
use crate::pages::shell::Meta;
use crate::read::*;
use leptos::prelude::*;

/// v1 `value_h`: pawns from White's side, or the verdict.
pub fn value_h(value: Option<i32>, status: Option<Status>) -> String {
    match status {
        Some(Status::WhiteWin) => "White win".into(),
        Some(Status::BlackWin) => "Black win".into(),
        Some(Status::Draw) => "Draw".into(),
        _ => value.map(pawns_white).unwrap_or_else(|| "\u{2014}".into()),
    }
}

/// `Y-m-d H:i` of an ISO time (UTC).
fn minute(iso: &str) -> String {
    iso.get(..16).map(|s| s.replace('T', " ")).unwrap_or_default()
}

/// v1 `roots._daily_history` preview: the first 12 plies, numbered, then
/// ` …` when the line goes on.
pub fn preview(san: &str) -> String {
    const PLIES: usize = 12;
    let mut out = Vec::new();
    let mut plies = 0;
    let mut more = false;
    for tok in san.split_whitespace() {
        let is_number = tok.ends_with('.') && tok.trim_end_matches('.').chars().all(|c| c.is_ascii_digit());
        if !is_number {
            if plies == PLIES {
                more = true;
                break;
            }
            plies += 1;
        } else if plies == PLIES {
            more = true;
            break;
        }
        out.push(tok);
    }
    let mut s = out.join(" ");
    if more {
        s.push_str(" \u{2026}");
    }
    s
}

fn day(iso: &str) -> String {
    iso.get(..10).unwrap_or_default().to_string()
}

fn url(r: &RootRow) -> String {
    if r.route.is_empty() {
        format!("/atomicdb/explore/{}/", r.key)
    } else {
        format!("/atomicdb/explore/{}/?play={}", r.key, r.route.join(","))
    }
}

fn at_cell(v: String, at: &Option<String>) -> impl IntoView + use<> {
    let at = at.as_deref().map(minute);
    view! { <td class="n">{v}{at.map(|a| view! { <div class="dim contrib-spec" style="white-space:normal">{a}</div> })}</td> }
}

fn changes(c: Option<u64>) -> String {
    c.map(|c| c.to_string()).unwrap_or_else(|| "\u{2014}".into())
}

/// Sort keys of the openings table (v1 `?sort=`): a name, `-` for descending.
const SORTS: [&str; 6] = ["name", "disputed", "now", "best", "worst", "changes"];

fn sort_openings(rows: &mut [RootRow], sort: &str) {
    let (desc, key) = match sort.strip_prefix('-') {
        Some(k) => (true, k),
        None => (false, sort),
    };
    let num = |v: Option<i32>| v.map(i64::from).unwrap_or(i64::MIN);
    match key {
        "disputed" => rows.sort_by_key(|r| std::cmp::Reverse(r.votes_down.unwrap_or(0))),
        "now" => rows.sort_by_key(|r| std::cmp::Reverse(num(r.value))),
        "best" => rows.sort_by_key(|r| std::cmp::Reverse(num(r.best_value))),
        "worst" => rows.sort_by_key(|r| num(r.worst_value)),
        "changes" => rows.sort_by_key(|r| std::cmp::Reverse(r.changes.unwrap_or(0))),
        _ => rows.sort_by_key(|r| r.label.to_lowercase()),
    }
    if desc {
        rows.reverse();
    }
}

pub fn render(v: RootsView, sort: &str) -> Page {
    let sort = if SORTS.iter().any(|s| *s == sort.trim_start_matches('-')) { sort.to_string() } else { "name".to_string() };
    let mut openings = v.openings.clone();
    sort_openings(&mut openings, &sort);
    let since = v.since.as_deref().map(day);
    let link = |k: &str| if sort == k { format!("-{k}") } else { k.to_string() };
    let arrow = |k: &str| {
        if sort == k {
            " \u{25b4}"
        } else if sort == format!("-{k}") {
            " \u{25be}"
        } else {
            ""
        }
    };
    let head = SORTS.map(|k| (link(k), arrow(k)));
    let n_openings = openings.len();
    let body = html(move || {
        let rows = v
            .positions
            .iter()
            .enumerate()
            .map(|(i, r)| {
                let indent = (r.route.len() >= 2).then_some("padding-left:1.6rem");
                let label = if i == 0 { view! { <strong>{r.label.clone()}</strong> }.into_any() } else { r.label.clone().into_any() };
                view! {
                    <tr>
                        <td style=indent><a href=url(r)>{label}</a></td>
                        <td class="n">{value_h(r.value, r.status)}</td>
                        {at_cell(value_h(r.best_value, r.best_status), &r.best_at)}
                        {at_cell(value_h(r.worst_value, r.worst_status), &r.worst_at)}
                        <td class="n dim">{changes(r.changes)}</td>
                    </tr>
                }
            })
            .collect_view();
        let main_line = match &v.main_line {
            Some(l) => view! {
                <p>
                    <strong>{value_h(l.value, l.status)}</strong>
                    {format!(" \u{b7} {} {}", l.plies, crate::format::plies_word(l.plies))}
                    {l.end_text.clone().map(|t| format!(" \u{b7} {t}"))}
                    " "<span class="dim">{format!("\u{b7} recorded {} UTC", minute(&l.at))}</span>
                </p>
                {(!l.san.is_empty()).then(|| view! { <p style="margin-top:.5rem">{l.san.clone()}</p> })}
                {l.end_key.clone().map(|k| view! {
                    <p class="dim lb-foot"><a href=format!("/atomicdb/explore/{k}/")>"Open the last position of the line"</a></p>
                })}
            }
            .into_any(),
            None => view! { <p class="dim">"Nothing recorded yet."</p> }.into_any(),
        };
        let chart = v.chart.as_ref().and_then(super::root_chart::measure).map(super::root_chart::view);
        let history = if v.history.is_empty() {
            view! { <p class="dim">"Nothing recorded yet."</p> }.into_any()
        } else {
            let rows = v
                .history
                .iter()
                .map(|h| {
                    view! {
                        <tr>
                            <td style="white-space:nowrap">{h.day.clone()}</td>
                            <td class="n">{value_h(h.value, h.status)}</td>
                            <td class="n">{h.plies}</td>
                            <td title=h.san.clone()>{preview(&h.san)}</td>
                        </tr>
                    }
                })
                .collect_view();
            view! {
                <div class="contrib-scroll"><table class="lb-table"><tbody>
                    <tr><th>"Day"</th><th class="n">"Value"</th><th class="n">"Plies"</th><th>"Line"</th></tr>
                    {rows}
                </tbody></table></div>
            }
            .into_any()
        };
        let open_rows = openings
            .iter()
            .map(|r| {
                let spec_text = r.route_san.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| r.line_san.clone());
                let mut spec = spec_text.clone();
                if r.community {
                    if !spec.is_empty() {
                        spec.push_str(" \u{b7} ");
                    }
                    spec.push_str(&format!("community name \u{b7} named by {}", r.named_by.clone().unwrap_or_default()));
                }
                if !r.present {
                    if !spec.is_empty() {
                        spec.push_str(" \u{b7} ");
                    }
                    spec.push_str("not in the tree yet");
                }
                let votable = r.votes_up.is_some();
                let marks = votable.then(|| {
                    view! { <div class="namevote-marks"><span class="namevote-flag" hidden=!r.under_review.unwrap_or(false)>"under review"</span></div> }
                });
                let vote = votable.then(|| {
                    let label = r.label.clone();
                    view! {
                        <form class="namevote" method="post" action=format!("/atomicdb/names/vote/{}/", r.key) data-key=r.key.clone()>
                            <input type="hidden" name="csrfmiddlewaretoken" value=""/>
                            <input type="hidden" name="sort" value=sort.clone()/>
                            <input type="hidden" name="toggle" value="1"/>
                            <button class="namevote-btn" name="vote" value="up" aria-pressed="false" title="Good name" aria-label=format!("Thumbs up for {label}")>
                                "\u{1f44d}"<span class="namevote-n">{r.votes_up.unwrap_or(0)}</span>
                            </button>
                            <button class="namevote-btn" name="vote" value="down" aria-pressed="false" title="Not a good name" aria-label=format!("Thumbs down for {label}")>
                                "\u{1f44e}"<span class="namevote-n">{r.votes_down.unwrap_or(0)}</span>
                            </button>
                        </form>
                    }
                });
                view! {
                    <tr id=format!("op-{}", r.key)>
                        <td style="overflow-wrap:anywhere"><a href=url(r)>{r.label.clone()}</a><div class="dim contrib-spec">{spec}</div>{marks}</td>
                        <td class="namevote-cell">{vote}</td>
                        <td class="n">{value_h(r.value, r.status)}</td>
                        {at_cell(value_h(r.best_value, r.best_status), &r.best_at)}
                        {at_cell(value_h(r.worst_value, r.worst_status), &r.worst_at)}
                        <td class="n dim">{changes(r.changes)}</td>
                    </tr>
                }
            })
            .collect_view();
        let h = head.clone();
        let th = move |i: usize, label: &'static str, title: Option<&'static str>| {
            view! { <a href=format!("?sort={}#openings", h[i].0) title=title>{label}</a>{h[i].1} }
        };
        view! {
            <h2 style="margin-top:0">"Start position and first moves"</h2>
            <p class="dim campaign-lede">
                "Each value is the one the explorer shows for that position, in pawns and from White's side. A solved position shows as White win, Black win or Draw. "
                {match since {
                    Some(d) => format!("Best and worst are the highest and lowest value each position has had since {d}, with the time it first got there (UTC)."),
                    None => "Best and worst start with the first values recorded.".to_string(),
                }}
                " The same for every "<a href="#openings">"named opening"</a>" further down."
            </p>
            <div class="contrib-scroll">
                <table class="lb-table"><tbody>
                    <tr><th>"Position"</th><th class="n">"Now"</th><th class="n">"Best"</th><th class="n">"Worst"</th><th class="n" title="How many times the value has changed">"Changes"</th></tr>
                    {rows}
                </tbody></table>
            </div>
            <h2>"Main line"</h2>
            <p class="dim campaign-lede">{format!("From the start position, the best known move at each step, until a solved position or {} plies. Recorded every hour.", v.max_plies)}</p>
            {main_line}
            {chart}
            <details id="main-line-by-day">
                <summary style="cursor:pointer"><h2 style="display:inline-block">"Main line by day"</h2></summary>
                <p class="dim campaign-lede">{format!("The last recording of each day (UTC), for the last {} days.", v.history_days)}</p>
                {history}
            </details>
            <h2 id="openings">"Named openings"</h2>
            <p class="dim campaign-lede">
                {format!("Every position the opening catalogue names, and every position a community name has been approved for, {n_openings} in all, followed the same way: the value the explorer shows, from White's side, and the highest and lowest it has had since it was first recorded. A name approved here joins the list as soon as it is approved. Click a column to sort.")}
            </p>
            <p class="dim campaign-lede" id="name-votes">
                {format!("Do you like a name? Give it a thumbs up or a thumbs down: one vote per person and name, and pressing your thumb again takes the vote back. A name with at least {} votes, three quarters of them down, goes under review: it is put to a poll in the ", v.name_min_votes)}
                <strong>"#opening-names"</strong>
                " channel of our Discord, and a moderator decides whether to keep it, go back to the catalogue name, remove it or rename it. Sort by "
                <a href=format!("?sort={}#openings", head[1].0)>"most disputed"</a>
                " to see the names with the most thumbs down."
            </p>
            <div class="contrib-scroll">
                <table class="lb-table namevote-table" data-votes-url="/atomicdb/names/votes/"><tbody>
                    <tr>
                        <th style="width:40%">{th(0, "Opening", None)}</th>
                        <th class="namevote-head">{th(1, "Votes", Some("Most thumbs down first"))}</th>
                        <th class="n">{th(2, "Now", None)}</th>
                        <th class="n">{th(3, "Best", None)}</th>
                        <th class="n">{th(4, "Worst", None)}</th>
                        <th class="n">{th(5, "Changes", Some("How many times the value has changed"))}</th>
                    </tr>
                    {open_rows}
                </tbody></table>
            </div>
        }
    });
    Page {
        meta: Meta { title: "AtomicDB, start position and openings".into(), extra_scripts: String::new(), ..Default::default() },
        main_class: "",
        body,
        // v1 loads name-votes.js on this page.
        scripts: vec!["/static/atomicdb/name-votes.js"],
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn values() {
        assert_eq!(value_h(Some(1184), Some(Status::Unknown)), "+11.84");
        assert_eq!(value_h(None, Some(Status::Draw)), "Draw");
        assert_eq!(value_h(None, None), "\u{2014}");
        assert_eq!(minute("2026-11-18T14:39:15.027802+00:00"), "2026-11-18 14:39");
    }
}
