// v1 explore.html scripts that need no server data, moved verbatim into one
// deferred file (atomicdb/templates/atomicdb/explore.html, lines noted per block).
// The request row, the live line and the board are WASM islands in v2.
//
// v2: the explorer re-renders in place when a move is clicked (explore-nav.js),
// so the per-page blocks run from atomicdbExploreInit(), once per position;
// the keyboard block binds once.  Changes from v1 are marked "v2:".
window.atomicdbExploreInit = function () {

// ---- explore.html 136-165: tabs of the proposal panels
(function () {
  // v2: a deferred file, not an inline script inside the row.
  const row = document.querySelector('.pos-tools');
  if (!row) { return; }
  const panels = Array.from(row.querySelectorAll(':scope > details.suggest-name'));
  if (!panels.length) { return; }
  const buttons = panels.map(function (panel) {
    const summary = panel.querySelector(':scope > summary');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'pos-tab';
    button.textContent = summary.textContent;
    button.title = summary.title;
    panel.classList.add('tabbed');
    panel.before(button);
    return button;
  });
  function show(index) {
    panels.forEach(function (panel, i) {
      const on = i === index;
      panel.open = on;
      buttons[i].setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }
  buttons.forEach(function (button, i) {
    button.addEventListener('click', function () {
      show(panels[i].open ? -1 : i);
    });
  });
  // A panel the server opened (a refused name) keeps its tab pressed.
  show(panels.findIndex(function (panel) { return panel.open; }));
}());

// ---- explore.html 312-331: stale-page reload helper; the cascade line under the FEN box
    // A click refused because the page is out of date (the position was
    // searched, solved or changed since it loaded) reloads the page by
    // itself (cz_eclipsia, 16-Nov-2026).  At most once a minute per page, so
    // a refusal that a reload does not cure cannot loop.
    window.atomicdbReloadStale = function (say) {
      try {
        const k = 'atomicdb-stale-reload:' + location.pathname;
        const last = Number(sessionStorage.getItem(k)) || 0;
        if (Date.now() - last < 60000) { return false; }
        sessionStorage.setItem(k, String(Date.now()));
      } catch (e) { return false; }
      if (say) { say('Page out of date, reloading...'); }
      setTimeout(function () { location.reload(); }, 900);
      return true;
    };
    document.addEventListener('DOMContentLoaded', function () {
      const row = document.querySelector('.fenbox-here');
      const line = document.getElementById('cascade');
      if (row && line) { row.after(line); }
    });

// ---- explore.html 860-998: play a line
    (function () {
      const node = document.getElementById('atomicdb-playline');
      const row = document.getElementById('playrow');
      if (!node || !row) { return; }
      const data = JSON.parse(node.textContent);
      const STORE = 'atomicdb-autoplay';
      const PLAY_MAX_PLIES = 40;
      const PLAY_MARGIN_CP = 30;
      const PLAY_STEP_MS = 900;
      const buttons = {best: document.getElementById('play-best'),
                       random: document.getElementById('play-random')};
      const labels = {best: buttons.best.textContent,
                      random: buttons.random.textContent};
      const note = document.getElementById('playnote');
      let timer = null;
      function load() {
        try { return JSON.parse(sessionStorage.getItem(STORE) || 'null'); }
        catch (e) { return null; }
      }
      function save(state) {
        try {
          if (state) { sessionStorage.setItem(STORE, JSON.stringify(state)); }
          else { sessionStorage.removeItem(STORE); }
          return true;
        } catch (e) { return false; }
      }
      function say(text) {
        note.textContent = text || '';
        note.hidden = !text;
      }
      function lineName(mode) {
        return mode === 'random' ? 'random line' : 'best line';
      }
      function plies(n) { return n + (n === 1 ? ' ply' : ' plies'); }
      // Why nothing can be played from here, or '' when something can.
      function blocked() {
        if (!data.open) { return 'this position is closed'; }
        if (!data.rows.length) { return 'no stored moves here'; }
        if (!data.rows.some(r => r.uci === data.best)) {
          return 'the best move here is not a stored move to follow';
        }
        return '';
      }
      // The next row: the arrow's move, or for the random line any row with
      // a value within the margin of it, the arrow's move included.
      function pick(mode) {
        const best = data.rows.find(r => r.uci === data.best);
        if (!best || mode !== 'random' || best.score === null) { return best; }
        const pool = data.rows.filter(
          r => r.score !== null && r.score >= best.score - PLAY_MARGIN_CP);
        return pool[Math.floor(Math.random() * pool.length)] || best;
      }
      function idle(text) {
        clearTimeout(timer);
        timer = null;
        ['best', 'random'].forEach(mode => {
          buttons[mode].textContent = labels[mode];
          buttons[mode].disabled = false;
          buttons[mode].removeAttribute('aria-pressed');
        });
        say(text);
      }
      function playing(state) {
        ['best', 'random'].forEach(mode => {
          const on = mode === state.mode;
          buttons[mode].textContent = on ? 'Stop' : labels[mode];
          buttons[mode].disabled = !on;
          buttons[mode].setAttribute('aria-pressed', on ? 'true' : 'false');
        });
        say('Playing the ' + lineName(state.mode) + ': ' + plies(state.ply)
            + ' of at most ' + state.max);
      }
      function finish(state, reason) {
        save(null);
        idle(state.ply
          ? 'Played ' + plies(state.ply) + ' of the ' + lineName(state.mode)
            + '; stopped: ' + reason
          : 'Nothing to play: ' + reason);
      }
      function step() {
        timer = null;
        const state = load();
        // Stopped, or this page was reached some other way.
        if (!state || state.at !== data.key) { idle(''); return; }
        const reason = state.ply >= state.max
          ? 'reached the ' + state.max + '-ply limit' : blocked();
        if (reason) { finish(state, reason); return; }
        const next = pick(state.mode);
        state.at = next.key;
        state.ply += 1;
        if (!save(state)) { idle('Playing a line needs browser storage'); return; }
        // v2: through the in-place navigation when it is there.
        if (window.atomicdbNavigate) { window.atomicdbNavigate(next.url); } else { location.assign(next.url); }
      }
      function stop() {
        const state = load();
        if (!state || state.at !== data.key) { return false; }
        save(null);
        idle('Stopped after ' + plies(state.ply) + ' of the '
             + lineName(state.mode));
        return true;
      }
      function start(mode) {
        const state = {mode: mode, at: data.key, ply: 0, max: PLAY_MAX_PLIES};
        if (!save(state)) { idle('Playing a line needs browser storage'); return; }
        playing(state);
        step();
      }
      ['best', 'random'].forEach(mode => {
        buttons[mode].addEventListener('click', function () {
          if (!stop()) { start(mode); }
        });
      });
      // Esc and the history keys end a line in play (the keys script below).
      window.atomicdbStopLine = stop;
      function resume() {
        clearTimeout(timer);
        timer = null;
        const state = load();
        if (state && state.at === data.key) {
          playing(state);
          timer = setTimeout(step, PLAY_STEP_MS);
        } else {
          // A line that was heading somewhere else is over.
          if (state) { save(null); }
          idle('');
        }
      }
      // Back to a page restored from the back/forward cache: its old timer
      // must not walk on from a line that has moved past it.
      window.addEventListener('pageshow', function (event) {
        if (event.persisted) { resume(); }
      });
      window.addEventListener('pagehide', function () {
        clearTimeout(timer);
        timer = null;
      });
      // v2: a position left in place must not walk on.
      window.addEventListener('atomicdb-leave', function () { clearTimeout(timer); timer = null; }, {once: true});
      row.hidden = false;
      resume();
    }());
// ---- explore.html 1071-1114: FEN copy and lichess link
    (function () {
      // Copy the FEN as it stands in the box, edits included, and keep the
      // lichess link on the same FEN.
      var input = document.querySelector('.fenbox-here input[name="fen"]');
      var copy = document.getElementById('fen-copy');
      var lichess = document.getElementById('fen-lichess');
      var timer = null;
      // The icon turns into a tick when the FEN is on the clipboard; when it
      // is not, the FEN is left selected and the tooltip says what to press.
      function said(text) {
        var done = text === 'Copied';
        copy.classList.toggle('done', done);
        copy.title = text;
        copy.setAttribute('aria-label', text);
        clearTimeout(timer);
        timer = setTimeout(function () {
          copy.classList.remove('done');
          copy.title = 'Copy this FEN';
          copy.setAttribute('aria-label', 'Copy this FEN');
        }, 1500);
      }
      function fallback() {
        input.focus();
        input.select();
        try {
          said(document.execCommand('copy') ? 'Copied' : 'Press Ctrl+C');
        } catch (_error) {
          said('Press Ctrl+C');
        }
      }
      copy.addEventListener('click', function () {
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(input.value.trim())
            .then(function () { said('Copied'); }, fallback);
        } else {
          fallback();
        }
      });
      input.addEventListener('input', function () {
        lichess.href = 'https://lichess.org/analysis/atomic/' +
          encodeURIComponent(input.value.trim().replace(/ /g, '_'))
            .replace(/%2F/g, '/');
      });
    }());

// ---- explore.html 1287-1313: fold the moves with no analysis
    (function () {
      const rows = Array.from(document.querySelectorAll('span[data-fold]'), function (s) { return s.closest('tr'); });
      if (rows.length < 6) return;
      const KEY = 'atomicdb.showUnexplored';
      let open = false;
      try { open = localStorage.getItem(KEY) === '1'; } catch (e) {}
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      const btn = document.createElement('button');
      cell.colSpan = 6;
      btn.type = 'button';
      btn.className = 'reqbtn';
      cell.appendChild(btn);
      row.appendChild(cell);
      rows[0].parentNode.insertBefore(row, rows[0]);
      function show(v) {
        open = v;
        rows.forEach(function (r) { r.style.display = v ? '' : 'none'; });
        btn.textContent = (v ? 'Hide the ' : 'Show the ') + rows.length + ' moves with no analysis yet';
        btn.setAttribute('aria-expanded', v ? 'true' : 'false');
      }
      btn.addEventListener('click', function () {
        show(!open);
        try { localStorage.setItem(KEY, open ? '1' : '0'); } catch (e) {}
      });
      show(open);
    })();
};


// ---- explore.html 1001-1040: keyboard shortcuts
    (function () {
      // Keyboard shortcuts, never while typing: arrows walk the browser
      // history, F flips the board (board.js), Q clicks the Request button
      // when the page has one, Esc stops a line in play.
      document.addEventListener('keydown', function (event) {
        if (event.defaultPrevented || event.ctrlKey || event.metaKey
            || event.altKey) { return; }
        const target = event.target;
        if (target && (target.isContentEditable
            || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) { return; }
        const stopLine = window.atomicdbStopLine || function () { return false; };
        switch (event.key) {
          case 'ArrowLeft':
            stopLine();
            history.back();
            break;
          case 'ArrowRight':
            stopLine();
            history.forward();
            break;
          case 'f':
          case 'F':
            window.dispatchEvent(new CustomEvent('atomicdb-flip'));
            break;
          case 'q':
          case 'Q': {
            const request = document.getElementById('reqbtn');
            if (!request || request.disabled) { return; }
            request.click();
            break;
          }
          case 'Escape':
            if (!stopLine()) { return; }
            break;
          default:
            return;
        }
        event.preventDefault();
      });
    }());


window.atomicdbExploreInit();
