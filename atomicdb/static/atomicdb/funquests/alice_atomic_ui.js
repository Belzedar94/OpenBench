/*
 * The Alice Atomic page: two boards, the computer opponent and games between
 * two people through private links.
 */
(function () {
  'use strict';

  var R = window.AliceAtomic;
  var app = document.getElementById('alice-app');
  if (!R || !app) return;

  var cfg = {
    token: app.dataset.token || '',
    api: app.dataset.api,
    csrf: app.dataset.csrf || '',
    pieces: app.dataset.pieces,
    rulesUrl: app.dataset.rulesUrl,
    aiUrl: app.dataset.aiUrl,
  };
  var STORE_KEY = 'atomicdb-alice-ai';
  var POLL_MS = 2000;
  var REASONS = {
    explosion: 'the king exploded',
    checkmate: 'checkmate',
    stalemate: 'stalemate',
    repetition: 'threefold repetition',
    'fifty-move': 'the fifty-move rule',
    resignation: 'resignation',
    agreement: 'agreement',
    length: 'the move limit',
  };

  function $(id) { return document.getElementById(id); }

  var els = {
    setup: $('aa-setup'),
    game: $('aa-game'),
    boards: [$('aa-board-a'), $('aa-board-b')],
    status: $('aa-status'),
    banner: $('aa-banner'),
    moves: $('aa-moves'),
    top: $('aa-player-top'),
    bottom: $('aa-player-bottom'),
    flip: $('aa-flip'),
    resign: $('aa-resign'),
    draw: $('aa-draw'),
    drawAccept: $('aa-draw-accept'),
    drawDecline: $('aa-draw-decline'),
    newGame: $('aa-new'),
    invite: $('aa-invite'),
    inviteUrl: $('aa-invite-url'),
    inviteCopy: $('aa-invite-copy'),
    promo: $('aa-promo'),
    aiStart: $('aa-ai-start'),
    aiColor: $('aa-ai-color'),
    friendCreate: $('aa-friend-create'),
    friendColor: $('aa-friend-color'),
    friendResult: $('aa-friend-result'),
    error: $('aa-error'),
  };

  // -- game state ----------------------------------------------------------

  var game = null;   // {mode, you, moves, state, result, reason, ...}
  var view = null;   // replay of game.moves: {state, states, outcome}
  var orientation = 'w';
  var selected = null;   // {b, sq}
  var pending = null;    // promotion choice: list of legal entries
  var worker = null;
  var thinking = false;
  var pollTimer = null;
  var inFlight = false;

  function absoluteUrl(path) {
    return window.location.origin + path;
  }

  function compute() {
    try {
      view = R.replay(game.moves);
    } catch (_error) {
      var s = R.parse(game.afen || R.START_AFEN);
      view = { state: s, states: [s], outcome: R.outcome(s, 1) };
    }
  }

  function finished() {
    if (game.mode === 'friend') return game.state !== 'live' && game.state !== 'waiting';
    return !!view.outcome || !!game.result;
  }

  function resultOf() {
    if (game.mode === 'friend') {
      if (game.state === 'abandoned') return { text: 'Game abandoned', cls: 'draw' };
      if (game.state !== 'finished') return null;
      return describe(game.result, game.reason);
    }
    if (game.result) return describe(game.result, game.reason);
    if (view.outcome) return describe(view.outcome[0], view.outcome[1]);
    return null;
  }

  function describe(result, reason) {
    var why = REASONS[reason] || reason;
    if (result === '1/2-1/2') return { text: 'Draw by ' + why, cls: 'draw' };
    var winner = result === '1-0' ? 'w' : 'b';
    var who = winner === 'w' ? 'White' : 'Black';
    var cls = 'draw';
    if (game.you) cls = winner === game.you ? 'won' : 'lost';
    return { text: who + ' wins by ' + why, cls: cls };
  }

  function myTurn() {
    if (!game || finished() || thinking) return false;
    if (game.mode === 'friend' && game.state !== 'live') return false;
    return view.state.turn === game.you;
  }

  // -- drawing -------------------------------------------------------------

  function lastMoveInfo() {
    var n = game.moves.length;
    if (!n || view.states.length <= n) return null;
    var before = view.states[n - 1];
    var text = game.moves[n - 1];
    var legal = R.legalMovesDetailed(before);
    for (var i = 0; i < legal.length; i++) {
      if (legal[i].text === text) {
        var m = legal[i].move;
        var blast = m.kind === R.CAPTURE || m.kind === R.EN_PASSANT;
        // Every move ends on the other board; a capture explodes there.
        var info = { fromB: m.b, from: m.from, toB: 1 - m.b, to: m.to, blast: blast };
        if (m.kind === R.CASTLE) info.rook = m.to > m.from ? m.to - 1 : m.to + 1;
        return info;
      }
    }
    return null;
  }

  function legalFrom(b, sq) {
    if (!myTurn()) return [];
    return R.legalMovesDetailed(view.state).filter(function (x) {
      return x.move.b === b && x.move.from === sq;
    });
  }

  function squareOrder() {
    var order = [];
    for (var row = 0; row < 8; row++) {
      for (var col = 0; col < 8; col++) {
        var rank = orientation === 'w' ? 7 - row : row;
        var file = orientation === 'w' ? col : 7 - col;
        order.push(rank * 8 + file);
      }
    }
    return order;
  }

  function pieceName(p) {
    var names = { p: 'pawn', n: 'knight', b: 'bishop', r: 'rook', q: 'queen', k: 'king' };
    return (R.colorOf(p) === 'w' ? 'white ' : 'black ') + names[p.toLowerCase()];
  }

  function drawBoards() {
    var s = view.state;
    var last = lastMoveInfo();
    var dests = selected ? legalFrom(selected.b, selected.sq) : [];
    var checkLoc = null;
    if (!finished() && R.inCheck(s, s.turn)) checkLoc = R.kingLocation(s, s.turn);
    var order = squareOrder();
    var interactive = myTurn();
    for (var b = 0; b < 2; b++) {
      var el = els.boards[b];
      el.textContent = '';
      el.classList.toggle('aa-active', interactive);
      for (var i = 0; i < 64; i++) {
        var sq = order[i];
        var cell = document.createElement('button');
        cell.type = 'button';
        cell.className = 'aa-sq ' + (((sq >> 3) + (sq & 7)) % 2 ? 'aa-light' : 'aa-dark');
        cell.dataset.b = String(b);
        cell.dataset.sq = String(sq);
        var piece = s.boards[b][sq];
        var label = R.BOARD_LETTERS[b] + ' ' + R.squareName(sq);
        if (piece) {
          var img = document.createElement('img');
          img.src = cfg.pieces + (R.colorOf(piece) === 'w' ? 'w' : 'b') + piece.toUpperCase() + '.svg';
          img.alt = '';
          img.draggable = false;
          cell.appendChild(img);
          label += ', ' + pieceName(piece);
        }
        cell.setAttribute('aria-label', label);
        if (i % 8 === 0) cell.dataset.rank = String((sq >> 3) + 1);
        if (i >= 56) cell.dataset.file = 'abcdefgh'[sq & 7];
        if (last) {
          if (last.fromB === b && (last.from === sq || (last.blast && last.to === sq))) {
            cell.classList.add('aa-last');
          }
          if (last.toB === b && last.to === sq) cell.classList.add(last.blast ? 'aa-blast' : 'aa-last');
          if (last.rook !== undefined && last.toB === b && last.rook === sq) cell.classList.add('aa-last');
        }
        if (checkLoc && checkLoc[0] === b && checkLoc[1] === sq) cell.classList.add('aa-check');
        if (selected && selected.b === b && selected.sq === sq) cell.classList.add('aa-selected');
        for (var d = 0; d < dests.length; d++) {
          var m = dests[d].move;
          if (m.b === b && m.to === sq) {
            cell.classList.add(piece ? 'aa-dest-capture' : 'aa-dest');
          } else if (m.b !== b && m.to === sq) {
            cell.classList.add('aa-landing');
          }
        }
        el.appendChild(cell);
      }
    }
  }

  function playerLine(color) {
    var label = color === 'w' ? 'White' : 'Black';
    var name = '';
    if (game.mode === 'ai') {
      name = color === game.you ? 'You' : 'Computer';
    } else {
      var info = color === 'w' ? game.white : game.black;
      name = info && info.name ? info.name : 'Anonymous';
      if (color === game.you) name += ' (you)';
      if (info && !info.joined) name += ' · has not joined yet';
    }
    var going = !finished() && (game.mode !== 'friend' || game.state === 'live');
    return { label: label, name: name, turn: going && view.state.turn === color };
  }

  function setPlayer(el, color) {
    var line = playerLine(color);
    el.textContent = '';
    var dot = document.createElement('span');
    dot.className = 'aa-dot aa-dot-' + color;
    var strong = document.createElement('strong');
    strong.textContent = line.label;
    var name = document.createElement('span');
    name.className = 'aa-name';
    name.textContent = line.name;
    el.appendChild(dot);
    el.appendChild(strong);
    el.appendChild(name);
    if (line.turn) {
      var turn = document.createElement('span');
      turn.className = 'aa-turn';
      turn.textContent = 'to move';
      el.appendChild(turn);
    }
  }

  function drawMoves() {
    els.moves.textContent = '';
    var moves = game.moves;
    for (var i = 0; i < moves.length; i += 2) {
      var li = document.createElement('li');
      var white = document.createElement('span');
      white.textContent = moves[i];
      li.appendChild(white);
      if (i + 1 < moves.length) {
        var black = document.createElement('span');
        black.textContent = moves[i + 1];
        li.appendChild(black);
      }
      els.moves.appendChild(li);
    }
    els.moves.scrollTop = els.moves.scrollHeight;
  }

  function statusText() {
    if (game.mode === 'friend') {
      if (game.state === 'waiting') return 'Waiting for your opponent to open their link.';
    }
    if (finished()) return '';
    if (thinking) return 'The computer is thinking…';
    var side = view.state.turn === 'w' ? 'White' : 'Black';
    var check = R.inCheck(view.state, view.state.turn) ? ' Check!' : '';
    if (view.state.turn === game.you) return 'Your move.' + check;
    if (game.mode === 'friend') return side + ' to move: waiting for your opponent.' + check;
    return side + ' to move.' + check;
  }

  function draw() {
    if (!game) return;
    els.setup.hidden = true;
    els.game.hidden = false;
    drawBoards();
    setPlayer(els.top, orientation === 'w' ? 'b' : 'w');
    setPlayer(els.bottom, orientation);
    drawMoves();
    els.status.textContent = statusText();
    var res = resultOf();
    els.banner.hidden = !res;
    if (res) {
      els.banner.textContent = res.text;
      els.banner.className = 'aa-banner aa-' + res.cls;
    }
    var live = !finished();
    var friend = game.mode === 'friend';
    els.resign.hidden = !live || (friend && game.state !== 'live');
    var offer = friend ? game.draw_offer : '';
    var canOffer = friend && live && game.state === 'live';
    els.draw.hidden = !canOffer || !!offer;
    els.drawAccept.hidden = !(canOffer && offer && offer !== game.you);
    els.drawDecline.hidden = els.drawAccept.hidden;
    if (canOffer && offer === game.you) {
      els.status.textContent += ' You offered a draw.';
    } else if (canOffer && offer) {
      els.status.textContent += ' Your opponent offers a draw.';
    }
    els.newGame.hidden = friend && live;
    els.invite.hidden = !(friend && game.invite_url);
    if (friend && game.invite_url) els.inviteUrl.value = absoluteUrl(game.invite_url);
  }

  function showError(text) {
    els.error.textContent = text || '';
    els.error.hidden = !text;
  }

  // -- input ---------------------------------------------------------------

  function onSquare(b, sq) {
    if (!myTurn()) return;
    var piece = view.state.boards[b][sq];
    if (selected) {
      var choices = legalFrom(selected.b, selected.sq).filter(function (x) {
        return x.move.b === b && x.move.to === sq;
      });
      if (choices.length === 1) { selected = null; makeMove(choices[0].text); return; }
      if (choices.length > 1) { askPromotion(choices); return; }
    }
    if (piece && R.colorOf(piece) === game.you && legalFrom(b, sq).length) {
      selected = (selected && selected.b === b && selected.sq === sq) ? null : { b: b, sq: sq };
    } else {
      selected = null;
    }
    drawBoards();
  }

  function askPromotion(choices) {
    pending = choices;
    els.promo.textContent = '';
    var title = document.createElement('p');
    title.textContent = 'Promote to:';
    els.promo.appendChild(title);
    choices.forEach(function (x) {
      var btn = document.createElement('button');
      btn.type = 'button';
      var img = document.createElement('img');
      img.src = cfg.pieces + game.you + x.move.promo.toUpperCase() + '.svg';
      img.alt = { q: 'Queen', r: 'Rook', b: 'Bishop', n: 'Knight' }[x.move.promo];
      btn.appendChild(img);
      btn.addEventListener('click', function () {
        els.promo.hidden = true;
        pending = null;
        selected = null;
        makeMove(x.text);
      });
      els.promo.appendChild(btn);
    });
    var cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'aa-promo-cancel';
    cancel.textContent = 'Cancel';
    cancel.addEventListener('click', function () {
      els.promo.hidden = true;
      pending = null;
      drawBoards();
    });
    els.promo.appendChild(cancel);
    els.promo.hidden = false;
  }

  function makeMove(text) {
    showError('');
    if (game.mode === 'ai') {
      game.moves.push(text);
      compute();
      saveAi();
      draw();
      maybeComputerMove();
    } else {
      sendMove(text);
    }
  }

  els.boards.forEach(function (el) {
    el.addEventListener('click', function (event) {
      var cell = event.target.closest('.aa-sq');
      if (!cell) return;
      onSquare(Number(cell.dataset.b), Number(cell.dataset.sq));
    });
  });

  els.flip.addEventListener('click', function () {
    orientation = orientation === 'w' ? 'b' : 'w';
    draw();
  });

  // -- playing the computer ------------------------------------------------

  function saveAi() {
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify({
        you: game.you, moves: game.moves, result: game.result || '', reason: game.reason || '',
      }));
    } catch (_error) {}
  }

  function clearAi() {
    try { window.localStorage.removeItem(STORE_KEY); } catch (_error) {}
  }

  function startAi(you, moves, result, reason) {
    stopPolling();
    game = { mode: 'ai', you: you, moves: moves || [], result: result || '', reason: reason || '' };
    orientation = you;
    selected = null;
    compute();
    saveAi();
    draw();
    maybeComputerMove();
  }

  function maybeComputerMove() {
    if (game.mode !== 'ai' || finished() || view.state.turn === game.you) return;
    if (!worker) {
      try {
        worker = new Worker(cfg.aiUrl);
      } catch (_error) {
        showError('The computer opponent could not start in this browser.');
        return;
      }
    }
    thinking = true;
    draw();
    var askedFor = game;
    var plies = game.moves.length;
    worker.onmessage = function (event) {
      thinking = false;
      if (game !== askedFor || game.moves.length !== plies) return;
      if (event.data.move) {
        game.moves.push(event.data.move);
        compute();
        saveAi();
      }
      draw();
    };
    worker.onerror = function () {
      thinking = false;
      showError('The computer opponent stopped working. Start a new game to try again.');
      draw();
    };
    worker.postMessage({
      rulesUrl: cfg.rulesUrl,
      afen: R.toAfen(view.state),
      timeMs: 600 + Math.floor(Math.random() * 600),
    });
  }

  function pickColor(value) {
    if (value === 'random') return Math.random() < 0.5 ? 'w' : 'b';
    return value === 'black' ? 'b' : 'w';
  }

  els.aiStart.addEventListener('click', function () {
    startAi(pickColor(els.aiColor.value));
  });

  // -- playing a friend ----------------------------------------------------

  function post(path, data) {
    var body = new URLSearchParams(data || {});
    return fetch(cfg.api + path, {
      method: 'POST',
      body: body,
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': cfg.csrf, 'X-Requested-With': 'fetch' },
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (json) {
        return { ok: response.ok, status: response.status, json: json };
      });
    });
  }

  function adopt(json) {
    if (!json || json.afen === undefined) return;
    var keepOrientation = game && game.mode === 'friend';
    // An answer that left the server before a newer one already shown.
    if (keepOrientation && json.version < game.version) return;
    game = {
      mode: 'friend',
      you: json.you,
      moves: json.moves,
      afen: json.afen,
      state: json.state,
      result: json.result,
      reason: json.reason,
      draw_offer: json.draw_offer,
      white: json.white,
      black: json.black,
      invite_url: json.invite_url || '',
      version: json.version,
    };
    if (!keepOrientation) orientation = json.you;
    selected = null;
    compute();
    draw();
    schedulePoll();
  }

  function sendMove(text) {
    var ply = game.moves.length;
    // Show the move straight away; the server's answer replaces it.
    game.moves = game.moves.concat([text]);
    compute();
    draw();
    post(cfg.token + '/move', { move: text, ply: String(ply) }).then(function (r) {
      if (!r.ok && r.json.error) showError(r.json.error);
      adopt(r.json);
      if (r.json.afen === undefined) poll(true);
    }).catch(function () {
      showError('The move did not reach the server. Check your connection.');
      poll(true);
    });
  }

  function action(path, data) {
    showError('');
    post(cfg.token + '/' + path, data).then(function (r) {
      if (!r.ok && r.json.error) showError(r.json.error);
      adopt(r.json);
    }).catch(function () {
      showError('That did not reach the server. Check your connection.');
    });
  }

  function stopPolling() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function schedulePoll(delay) {
    stopPolling();
    if (!game || game.mode !== 'friend') return;
    if (game.state !== 'live' && game.state !== 'waiting') return;
    if (document.visibilityState === 'hidden') return;
    pollTimer = setTimeout(poll, delay || POLL_MS);
  }

  function poll(full) {
    if (!cfg.token || inFlight) return;
    inFlight = true;
    var since = (!full && game && game.version) ? '?since=' + game.version : '';
    fetch(cfg.api + cfg.token + '/state' + since, { credentials: 'same-origin' })
      .then(function (response) {
        inFlight = false;
        if (response.status === 429 || response.status >= 500) {
          schedulePoll(10000);
          return null;
        }
        return response.json();
      })
      .then(function (json) {
        if (!json) return;
        if (json.changed === false) { schedulePoll(); return; }
        adopt(json);
      })
      .catch(function () {
        inFlight = false;
        schedulePoll(10000);
      });
  }

  document.addEventListener('visibilitychange', function () {
    if (!game || game.mode !== 'friend') return;
    if (document.visibilityState === 'visible') poll();
    else stopPolling();
  });

  els.friendCreate.addEventListener('click', function () {
    els.friendCreate.disabled = true;
    showError('');
    post('create', { color: els.friendColor.value }).then(function (r) {
      els.friendCreate.disabled = false;
      if (!r.ok) {
        showError(r.json.error || 'The game could not be created.');
        return;
      }
      window.location.href = r.json.your_url;
    }).catch(function () {
      els.friendCreate.disabled = false;
      showError('The game could not be created. Check your connection.');
    });
  });

  els.inviteCopy.addEventListener('click', function () {
    els.inviteUrl.select();
    var done = function () { els.inviteCopy.textContent = 'Copied'; };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(els.inviteUrl.value).then(done, function () {
        document.execCommand('copy');
        done();
      });
    } else {
      document.execCommand('copy');
      done();
    }
  });

  // -- buttons shared by both modes ----------------------------------------

  els.resign.addEventListener('click', function () {
    if (!window.confirm('Resign this game?')) return;
    if (game.mode === 'ai') {
      game.result = game.you === 'w' ? '0-1' : '1-0';
      game.reason = 'resignation';
      thinking = false;
      saveAi();
      draw();
    } else {
      action('resign');
    }
  });
  els.draw.addEventListener('click', function () { action('draw', { action: 'offer' }); });
  els.drawAccept.addEventListener('click', function () { action('draw', { action: 'accept' }); });
  els.drawDecline.addEventListener('click', function () { action('draw', { action: 'decline' }); });
  els.newGame.addEventListener('click', function () {
    if (game && game.mode === 'ai' && !finished() && game.moves.length &&
        !window.confirm('Leave this game and start a new one?')) return;
    clearAi();
    stopPolling();
    game = null;
    thinking = false;
    if (cfg.token) {
      window.location.href = app.dataset.home;
      return;
    }
    els.game.hidden = true;
    els.setup.hidden = false;
  });

  // -- start ---------------------------------------------------------------

  if (cfg.token) {
    els.setup.hidden = true;
    post(cfg.token + '/join').then(function (r) {
      if (!r.ok && r.json.error) showError(r.json.error);
      adopt(r.json);
    }).catch(function () {
      showError('Could not reach the server. Reload the page to try again.');
    });
  } else {
    var saved = null;
    try { saved = JSON.parse(window.localStorage.getItem(STORE_KEY) || 'null'); } catch (_error) {}
    if (saved && (saved.you === 'w' || saved.you === 'b') && Array.isArray(saved.moves)) {
      try {
        R.replay(saved.moves);
        startAi(saved.you, saved.moves, saved.result, saved.reason);
      } catch (_error) {
        clearAi();
      }
    }
  }
}());
