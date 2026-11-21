/* TicTacToeDB: tic-tac-toe, solved in the browser.
 *
 * On load every position reachable from the empty board is solved by plain
 * minimax with a memo (5,478 positions).  A result is {r, d}: r is +1, 0 or
 * -1 for the player to move, d the number of moves left until the game ends
 * with best play (the winner ends it as soon as possible, the loser holds out
 * as long as possible).
 *
 * Squares are named like a chessboard: files a-c from left to right, ranks
 * 1-3 from bottom to top.  ?play=b2,a1,... replays a list of moves.
 */
(function () {
  "use strict";

  var LINES = [
    [0, 1, 2], [3, 4, 5], [6, 7, 8],
    [0, 3, 6], [1, 4, 7], [2, 5, 8],
    [0, 4, 8], [2, 4, 6]
  ];
  // Board index 0 is the top-left cell (a3); index 8 is the bottom-right (c1).
  function squareName(i) {
    return "abc".charAt(i % 3) + String(3 - Math.floor(i / 3));
  }
  function squareIndex(name) {
    var m = /^([abc])([123])$/.exec(name);
    if (!m) return -1;
    return (3 - Number(m[2])) * 3 + "abc".indexOf(m[1]);
  }

  function winner(board) {
    for (var k = 0; k < LINES.length; k++) {
      var l = LINES[k];
      var c = board.charAt(l[0]);
      if (c !== "." && c === board.charAt(l[1]) && c === board.charAt(l[2])) {
        return { side: c, line: l };
      }
    }
    return null;
  }
  function toMove(board) {
    var n = 0;
    for (var i = 0; i < 9; i++) if (board.charAt(i) !== ".") n++;
    return n % 2 === 0 ? "X" : "O";
  }
  function play(board, i) {
    return board.slice(0, i) + toMove(board) + board.slice(i + 1);
  }
  function isOver(board) {
    return winner(board) !== null || board.indexOf(".") < 0;
  }

  // Higher is better for the player choosing: quick wins, then draws, then
  // slow losses.
  function rank(res) {
    if (res.r > 0) return 100 - res.d;
    if (res.r < 0) return -100 + res.d;
    return 0;
  }

  var memo = Object.create(null);
  function solve(board) {
    var known = memo[board];
    if (known) return known;
    var res;
    if (winner(board)) {
      res = { r: -1, d: 0 };          // the previous move won
    } else if (board.indexOf(".") < 0) {
      res = { r: 0, d: 0 };
    } else {
      res = null;
      for (var i = 0; i < 9; i++) {
        if (board.charAt(i) !== ".") continue;
        var child = solve(play(board, i));
        var mine = { r: -child.r, d: child.d + 1 };
        if (res === null || rank(mine) > rank(res)) res = mine;
      }
    }
    memo[board] = res;
    return res;
  }

  var EMPTY = ".........";
  solve(EMPTY);
  var positionCount = Object.keys(memo).length;

  function moveResults(board) {
    var out = [];
    if (isOver(board)) return out;
    for (var i = 0; i < 9; i++) {
      if (board.charAt(i) !== ".") continue;
      var child = solve(play(board, i));
      out.push({ index: i, res: { r: -child.r, d: child.d + 1 } });
    }
    out.sort(function (a, b) {
      return rank(b.res) - rank(a.res) || a.index - b.index;
    });
    return out;
  }

  function resultClass(res) {
    return res.r > 0 ? "won" : res.r < 0 ? "lost" : "draw";
  }
  function moves(n) { return n === 1 ? "1 move" : n + " moves"; }
  function resultText(res) {
    if (res.r > 0) return "Win in " + moves(res.d);
    if (res.r < 0) return "Loss in " + moves(res.d);
    return "Draw";
  }
  function resultShort(res) {
    if (res.r > 0) return "W" + res.d;
    if (res.r < 0) return "L" + res.d;
    return "D";
  }

  // -- page ------------------------------------------------------------------

  var app = document.getElementById("ttt-app");
  if (!app) return;
  var boardEl = document.getElementById("ttt-board");
  var statusEl = document.getElementById("ttt-status");
  var listEl = document.getElementById("ttt-list");
  var historyEl = document.getElementById("ttt-history");
  var backBtn = document.getElementById("ttt-back");
  var resetBtn = document.getElementById("ttt-reset");
  var countEl = document.getElementById("ttt-count");
  if (countEl) countEl.textContent = positionCount.toLocaleString("en-US");

  var line = [];   // board indexes played so far

  function boardOf(seq) {
    var b = EMPTY;
    for (var k = 0; k < seq.length; k++) b = play(b, seq[k]);
    return b;
  }

  function parsePlay(raw) {
    var seq = [];
    if (!raw) return seq;
    var parts = raw.split(",");
    var b = EMPTY;
    for (var k = 0; k < parts.length && k < 9; k++) {
      var i = squareIndex(parts[k].trim().toLowerCase());
      if (i < 0 || b.charAt(i) !== "." || isOver(b)) break;
      seq.push(i);
      b = play(b, i);
    }
    return seq;
  }

  function playParam(seq) {
    return seq.map(squareName).join(",");
  }

  function syncUrl(push) {
    var url = window.location.pathname +
      (line.length ? "?play=" + playParam(line) : "");
    try {
      if (push) window.history.pushState({ play: playParam(line) }, "", url);
      else window.history.replaceState({ play: playParam(line) }, "", url);
    } catch (e) { /* file:// or sandboxed: the page still works */ }
  }

  function go(seq, push) {
    line = seq;
    syncUrl(push);
    render();
  }

  function playMove(i) {
    var b = boardOf(line);
    if (isOver(b) || b.charAt(i) !== ".") return;
    go(line.concat([i]), true);
  }

  function render() {
    var b = boardOf(line);
    var win = winner(b);
    var results = moveResults(b);
    var best = results.length ? rank(results[0].res) : null;
    var byIndex = {};
    results.forEach(function (m) { byIndex[m.index] = m; });

    boardEl.textContent = "";
    for (var i = 0; i < 9; i++) {
      var cell = document.createElement("button");
      cell.type = "button";
      cell.className = "ttt-cell";
      cell.setAttribute("role", "gridcell");
      var c = b.charAt(i);
      if (c !== ".") {
        cell.classList.add("ttt-" + c.toLowerCase());
        cell.textContent = c;
        cell.disabled = true;
        cell.setAttribute("aria-label", squareName(i) + ": " + c);
        if (win && win.line.indexOf(i) >= 0) cell.classList.add("ttt-winline");
      } else if (byIndex[i]) {
        var m = byIndex[i];
        cell.classList.add("ttt-open", resultClass(m.res));
        if (rank(m.res) === best) cell.classList.add("ttt-best");
        var tag = document.createElement("span");
        tag.className = "ttt-tag";
        tag.textContent = resultShort(m.res);
        cell.appendChild(tag);
        cell.title = squareName(i) + ": " + resultText(m.res);
        cell.setAttribute("aria-label", squareName(i) + ", " + resultText(m.res));
        cell.addEventListener("click", playMove.bind(null, i));
      } else {
        cell.disabled = true;
        cell.setAttribute("aria-label", squareName(i) + ": empty");
      }
      boardEl.appendChild(cell);
    }

    var side = toMove(b);
    if (win) {
      statusEl.textContent = win.side + " wins.";
      statusEl.className = "ttt-status";
    } else if (b.indexOf(".") < 0) {
      statusEl.textContent = "Draw: the board is full.";
      statusEl.className = "ttt-status draw";
    } else {
      var res = solve(b);
      statusEl.textContent = side + " to move. " + (res.r > 0
        ? side + " wins in " + moves(res.d) + "."
        : res.r < 0
          ? side + " loses in " + moves(res.d) + "."
          : "Draw with best play.");
      statusEl.className = "ttt-status " + resultClass(res);
    }

    listEl.textContent = "";
    results.forEach(function (m) {
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ttt-row";
      if (rank(m.res) === best) btn.classList.add("ttt-best");
      var mv = document.createElement("span");
      mv.className = "ttt-row-move";
      mv.textContent = squareName(m.index);
      var val = document.createElement("span");
      val.className = "ttt-val " + resultClass(m.res);
      val.textContent = resultText(m.res);
      btn.appendChild(mv);
      btn.appendChild(val);
      btn.addEventListener("click", playMove.bind(null, m.index));
      li.appendChild(btn);
      listEl.appendChild(li);
    });
    listEl.hidden = results.length === 0;

    historyEl.textContent = "";
    line.forEach(function (i, k) {
      var li = document.createElement("li");
      var a = document.createElement("button");
      a.type = "button";
      a.className = "ttt-hist";
      a.textContent = (k % 2 === 0 ? "X " : "O ") + squareName(i);
      if (k === line.length - 1) a.setAttribute("aria-current", "step");
      a.addEventListener("click", function () { go(line.slice(0, k + 1), true); });
      li.appendChild(a);
      historyEl.appendChild(li);
    });

    backBtn.disabled = line.length === 0;
    resetBtn.disabled = line.length === 0;
  }

  backBtn.addEventListener("click", function () {
    if (line.length) go(line.slice(0, -1), true);
  });
  resetBtn.addEventListener("click", function () {
    if (line.length) go([], true);
  });
  document.addEventListener("keydown", function (e) {
    if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.key === "ArrowLeft" && line.length) go(line.slice(0, -1), true);
  });
  window.addEventListener("popstate", function () {
    line = parsePlay(new URLSearchParams(window.location.search).get("play"));
    render();
  });

  line = parsePlay(new URLSearchParams(window.location.search).get("play"));
  syncUrl(false);
  render();
})();
