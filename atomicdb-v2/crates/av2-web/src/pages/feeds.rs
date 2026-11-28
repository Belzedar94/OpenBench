//! Three list pages: v1 `notifications/` (an account's analyses, login
//! required), `milestones/` (the event feed) and `campaigns/` (every
//! proposed campaign).  Their POSTs (mark all read, vote) stay on Django.

use super::{Page, html};
use crate::format::thousands;
use crate::pages::shell::Meta;
use crate::read::*;
use crate::time::civil;
use leptos::prelude::*;

const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/// Django `date:"M d H:i"`.
fn md_hm(ts: i64) -> String {
    let (_, m, d, hh, mm, _) = civil(ts);
    format!("{} {d:02} {hh:02}:{mm:02}", MONTHS[(m - 1) as usize])
}

pub fn notifications(n: Notices) -> Page {
    let body = html(move || {
        let list = if n.rows.is_empty() {
            view! { <p class="dim">"Nothing yet. Open a position in the explorer and ask for an analysis: when a worker serves it, it shows up here."</p> }.into_any()
        } else {
            let rows = n
                .rows
                .iter()
                .map(|r| {
                    let play = if r.play.is_empty() { String::new() } else { format!("?play={}", crate::format::query_value(&r.play)) };
                    view! {
                        <li class=if r.seen { "notif-row" } else { "notif-row unseen" }>
                            <a class="notif-link" href=format!("/atomicdb/explore/{}/{play}", r.key) title=r.full.clone()>"Your analysis of "<b>{r.san.clone()}</b>" is ready"</a>" "
                            <span class="notif-when dim">{r.ago.clone()}</span>
                        </li>
                    }
                })
                .collect_view();
            view! {
                <form class="notif-markall" method="post" action="/atomicdb/notifications/">
                    <input type="hidden" name="csrfmiddlewaretoken" value=""/>
                    <input type="hidden" name="back" value="/atomicdb/notifications/"/>
                    <button type="submit" class="reqbtn ghost">"Mark all read"</button>
                </form>
                <ul class="notif-list">{rows}</ul>
            }
            .into_any()
        };
        view! {
            <h2 style="margin-top:0">"Analyses you asked for"</h2>
            <p class="dim notif-lede">"Every analysis you request lands here once a worker has served it. Visiting a position marks its notice as read. If you run workers of your own, they take your requests first. "
                <a href="/atomicdb/notifications/settings/">"Notification settings"</a></p>
            {list}
        }
    });
    Page {
        meta: Meta { title: "Analyses you asked for \u{b7} AtomicDB".into(), ..Default::default() },
        main_class: "",
        body,
        scripts: vec![],
    }
}

pub fn milestones(events: Vec<Milestone>) -> Page {
    let body = html(move || {
        let items = if events.is_empty() {
            view! { <li class="dim">"No events yet."</li> }.into_any()
        } else {
            events
                .iter()
                .map(|e| {
                    let line = e.san.clone().map(|san| {
                        let full = e.full.clone().unwrap_or_default();
                        view! {
                            <details class="milestone-line dim">
                                <summary class="qline" title=full.clone()>{format!("\u{a0}{san}")}</summary>
                                <span class="milestone-full">{full}</span>
                            </details>
                        }
                    });
                    let link = e.key.clone().map(|k| view! { " "<a href=format!("/atomicdb/explore/{k}/")>"view"</a> });
                    view! { <li><span class="t">{md_hm(e.ts)}</span>" "{e.text.clone()}{line}{link}</li> }
                })
                .collect_view()
                .into_any()
        };
        view! { <h2 style="margin-top:0">"Milestones"</h2><ul class="feed">{items}</ul> }
    });
    Page { meta: Meta { title: "Milestones \u{b7} AtomicDB".into(), ..Default::default() }, main_class: "", body, scripts: vec![] }
}

/// How many campaigns the home page shows (v1 `home_limit`).
pub const HOME_LIMIT: usize = 6;

fn reach_view(r: &Reach) -> impl IntoView + use<> {
    let spans: Vec<String> = if r.capped {
        vec![
            format!("{}+ positions", thousands(r.total)),
            format!("{}+ explored", thousands(r.explored)),
            format!("{}+ solved", thousands(r.resolved)),
            format!("{:.1}% of the positions counted solved", r.solved_pct),
        ]
    } else {
        vec![
            format!("{} of {} positions explored", thousands(r.explored), thousands(r.total)),
            format!("{} solved", thousands(r.resolved)),
            format!("{:.1}% of the line solved", r.solved_pct),
        ]
    };
    let mut spans = spans;
    if let Some(a) = &r.measured_ago {
        spans.push(format!("measured {a} ago"));
    }
    let n = spans.len();
    let items = spans.into_iter().enumerate().map(|(i, s)| view! { <span>{s}</span>{(i + 1 < n).then_some(" ")} }).collect_view();
    view! { <div class="campaign-stats dim">{items}</div> }
}

fn proof_view(p: &CampaignProof) -> impl IntoView + use<> {
    let inner = if p.proven {
        view! { <span>"Proven win for White"</span> }.into_any()
    } else if p.refuted {
        view! { <span>"Shown White cannot win"</span> }.into_any()
    } else {
        view! {
            <span title="The fewest unsolved positions whose solving would prove a White win, along the cheapest lines">{format!("{} positions to prove a White win", p.pn.map(thousands).unwrap_or_default())}</span>" "
            <span title="The fewest unsolved positions whose solving would show White cannot win">{format!("{} to show it is not", p.dn.map(thousands).unwrap_or_default())}</span>
        }
        .into_any()
    };
    view! { <div class="campaign-stats dim campaign-proof">{inner}</div> }
}

pub fn campaigns(rows: Vec<CampaignRow>) -> Page {
    let body = html(move || {
        let list = if rows.is_empty() {
            view! { <p class="dim">"No campaigns proposed yet. Open a line in the explorer and put it up from there."</p> }.into_any()
        } else {
            let items = rows
                .iter()
                .map(|c| {
                    view! {
                        <li class="campaign-row">
                            <div class="campaign-row-line">
                                <a class="qline" href=format!("/atomicdb/explore/{}/", c.root_key)>{c.title.clone()}</a>
                                {(!c.line_shown.is_empty()).then(|| view! { <div class="campaign-row-san dim">{c.line_shown.clone()}</div> })}
                                {c.reach.as_ref().map(reach_view)}
                                {c.proof.as_ref().map(proof_view)}
                            </div>
                            <div class="campaign-row-votes dim">{format!("proposed by {} \u{b7} {} vote{}", c.proposed_by, c.votes, if c.votes == 1 { "" } else { "s" })}</div>
                            <div class="campaign-actions">
                                <form method="post" action=format!("/atomicdb/campaign/{}/vote/", c.id)>
                                    <input type="hidden" name="csrfmiddlewaretoken" value=""/>
                                    <input type="hidden" name="back" value="campaigns"/>
                                    <button class="reqbtn">"Vote"</button>
                                </form>
                            </div>
                        </li>
                    }
                })
                .collect_view();
            view! { <ul class="campaign-list">{items}</ul> }.into_any()
        };
        view! {
            <h2 id="campaigns">"Proposed campaigns"</h2>
            <p class="dim campaign-lede">"Every line anyone has put up, most voted first. The "<a href="/atomicdb/#campaigns">"overview"</a>
                {format!(" shows only the {HOME_LIMIT} most voted; the most voted open line is the one the automatic queue works on.")}</p>
            {list}
        }
    });
    Page { meta: Meta { title: "AtomicDB proposed campaigns".into(), ..Default::default() }, main_class: "", body, scripts: vec![] }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn milestone_time() {
        assert_eq!(md_hm(1_795_852_260), "Nov 28 07:51");
    }
}
