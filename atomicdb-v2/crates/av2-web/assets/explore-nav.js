// In-place navigation in the explorer (v2; owner decision W3, 28-Nov-2026).
//
// Clicking a move (table, line box, transpositions, board) fetches the next
// position's page as JSON from /atomicdb/nav/explore/<key>/?play=..., swaps
// the content of <main> and pushes the URL with history.pushState: no page
// load, no CSS/JS re-parse, the board stays mounted and only changes
// position.  Hovering a link prefetches it, so a click usually finds the
// answer already here.  Back and forward replay the same way.  Anything
// unexpected (an error, the WASM not loaded yet, a modified click) falls back
// to the normal page load, so the page works exactly as v1 without it.
(function () {
  'use strict';
  if (!window.history || !window.fetch || !document.getElementById('board')) { return; }
  const EXPLORE = '/atomicdb/explore/';
  const GOTO = '/atomicdb/goto/';
  const PREFETCH_MAX = 64;
  const HOVER_MS = 60;
  const cache = new Map();          // url -> Promise<data>
  const times = window.__av2NavTimes = [];
  window.__av2Gen = window.__av2Gen || 0;

  function explorerUrl(href) {
    let u;
    try { u = new URL(href, location.href); } catch (e) { return null; }
    if (u.origin !== location.origin) { return null; }
    if (u.pathname.startsWith(EXPLORE) || u.pathname.startsWith(GOTO)) { return u; }
    return null;
  }

  function navUrl(u) {
    return '/atomicdb/nav' + u.pathname.slice('/atomicdb'.length) + u.search;
  }

  function load(u) {
    const key = u.pathname + u.search;
    let p = cache.get(key);
    if (!p) {
      p = fetch(navUrl(u), {headers: {'Accept': 'application/json'}, credentials: 'same-origin'})
        .then(r => { if (!r.ok) { throw new Error('nav ' + r.status); } return r.json(); });
      p.catch(() => cache.delete(key));
      if (cache.size >= PREFETCH_MAX) { cache.delete(cache.keys().next().value); }
      cache.set(key, p);
    }
    return p;
  }

  function ready() {
    return typeof window.__hydrateIsland === 'function' && typeof window.__av2BoardUpdate === 'function';
  }

  function apply(data) {
    const main = document.querySelector('main');
    const tpl = document.createElement('template');
    tpl.innerHTML = data.main;
    const oldBoard = main.querySelector('leptos-island[data-component^="Board_"]');
    const newBoard = tpl.content.querySelector('leptos-island[data-component^="Board_"]');
    if (!oldBoard || !newBoard) { return false; }
    const props = newBoard.dataset.props;
    // Tell what is running on the old position to stop (play-a-line timer,
    // the request row's polls).
    window.__av2Gen += 1;
    window.dispatchEvent(new Event('atomicdb-leave'));
    newBoard.replaceWith(oldBoard);
    main.replaceChildren(tpl.content);
    window.__av2BoardUpdate(props);
    main.querySelectorAll('leptos-island').forEach(el => {
      if (el !== oldBoard) { window.__hydrateIsland(el, el.dataset.component); }
    });
    document.title = data.title;
    const desc = document.querySelector('meta[name="description"]');
    if (desc && data.description) { desc.setAttribute('content', data.description); }
    const canon = document.querySelector('link[rel="canonical"]');
    if (canon && data.canonical) { canon.setAttribute('href', location.origin + data.canonical); }
    const root = document.documentElement;
    if (window.atomicdbNotation) { window.atomicdbNotation(root.dataset.notation === 'uci' ? 'uci' : 'san'); }
    if (window.atomicdbEval && root.dataset.eval === 'white') { window.atomicdbEval('white'); }
    if (window.atomicdbCsrf) { window.atomicdbCsrf(); }
    if (window.atomicdbExploreInit) { window.atomicdbExploreInit(); }
    return true;
  }

  async function go(href, push) {
    let u = explorerUrl(href);
    if (!u || !ready()) { return false; }
    const t0 = performance.now();
    try {
      if (u.pathname.startsWith(GOTO)) {
        // A move into a position the tree does not have yet: the server
        // creates it and redirects; only the final URL is needed.
        const r = await fetch(u.href, {credentials: 'same-origin'});
        u = explorerUrl(r.url);
        if (!r.ok || !u || !u.pathname.startsWith(EXPLORE)) { location.assign(r.url || href); return true; }
      }
      const data = await load(u);
      if (!apply(data)) { location.assign(u.href); return true; }
      if (push) { history.pushState({av2: 1}, '', u.pathname + u.search); }
      times.push(Math.round((performance.now() - t0) * 10) / 10);
      if (times.length > 100) { times.shift(); }
    } catch (e) {
      location.assign(u.href);
    }
    return true;
  }

  // Board moves and play-a-line go through here (islands/board.rs, explore.js).
  window.atomicdbNavigate = function (href) {
    go(href, true).then(done => { if (!done) { location.assign(href); } });
  };

  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) { return; }
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.target || a.hasAttribute('download')) { return; }
    const u = explorerUrl(a.getAttribute('href'));
    if (!u || !ready()) { return; }
    e.preventDefault();
    go(u.href, true);
  });

  let hoverTimer = null;
  function prefetch(e) {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a) { return; }
    const u = explorerUrl(a.getAttribute('href'));
    // Never prefetch a goto: it writes.
    if (!u || !u.pathname.startsWith(EXPLORE)) { return; }
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => load(u), e.type === 'touchstart' ? 0 : HOVER_MS);
  }
  document.addEventListener('mouseover', prefetch, {passive: true});
  document.addEventListener('touchstart', prefetch, {passive: true});
  document.addEventListener('mouseout', () => clearTimeout(hoverTimer), {passive: true});

  history.replaceState({av2: 1}, '');
  window.addEventListener('popstate', function () {
    go(location.href, false).then(done => { if (!done) { location.reload(); } });
  });
}());
