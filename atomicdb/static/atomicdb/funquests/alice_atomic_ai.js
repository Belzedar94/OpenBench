/*
 * The computer opponent of Alice Atomic, run in a Web Worker so the page
 * stays responsive.  Iterative deepening alpha-beta over the shared rules,
 * with a short capture search at the leaves; the evaluation is material plus
 * a little mobility and pressure around the enemy king.
 *
 * Message in:  {rulesUrl, afen, timeMs}
 * Message out: {move, depth, score} (move is null when there is none)
 */
'use strict';

var rules = null;
var VALUE = { P: 100, N: 300, B: 320, R: 500, Q: 900, K: 0 };
var MATE = 100000;
var nodes = 0;
var deadline = 0;

function Stop() {}

function kingNeighbourhood(s, color) {
  var loc = rules.kingLocation(s, color);
  if (!loc) return 0;
  var board = s.boards[loc[0]];
  // A capture made on the other board explodes on the king's board, so
  // enemy pieces around the king's square over there are a danger too.
  var other = s.boards[1 - loc[0]];
  var f = loc[1] % 8, r = Math.floor(loc[1] / 8);
  var pressure = 0;
  for (var df = -2; df <= 2; df++) {
    for (var dr = -2; dr <= 2; dr++) {
      var ff = f + df, rr = r + dr;
      if (ff < 0 || ff > 7 || rr < 0 || rr > 7 || (!df && !dr)) continue;
      var p = board[rr * 8 + ff];
      if (p && p.toUpperCase() !== 'K' && rules.colorOf(p) !== color) {
        pressure += 1;
      }
      var q = other[rr * 8 + ff];
      if (q && q.toUpperCase() !== 'K' && rules.colorOf(q) !== color) {
        pressure += 1;
      }
    }
  }
  return pressure;
}

// Score from the side to move's point of view.
function evaluate(s) {
  var score = 0;
  for (var b = 0; b < 2; b++) {
    var board = s.boards[b];
    for (var sq = 0; sq < 64; sq++) {
      var p = board[sq];
      if (!p) continue;
      var v = VALUE[p.toUpperCase()];
      if (p === 'P') v += Math.floor(sq / 8) * 4;
      else if (p === 'p') v += (7 - Math.floor(sq / 8)) * 4;
      score += rules.colorOf(p) === 'w' ? v : -v;
    }
  }
  score += 12 * (kingNeighbourhood(s, 'b') - kingNeighbourhood(s, 'w'));
  var mine = s.turn === 'w' ? score : -score;
  return mine + 2 * rules.pseudoMoves(s).length;
}

function tick() {
  nodes += 1;
  if ((nodes & 255) === 0 && Date.now() > deadline) throw new Stop();
}

function isCapture(entry) {
  return entry.move.kind === rules.CAPTURE;
}

function ordered(s, legal) {
  var them = rules.opponent(s.turn);
  return legal.map(function (entry) {
    var key = 0;
    if (!rules.kingLocation(entry.after, them)) key = 1e6;
    else if (isCapture(entry)) key = 1000 - evaluate(entry.after);
    else if (entry.move.promo === 'q') key = 900;
    return { entry: entry, key: key + Math.random() };
  }).sort(function (a, b) { return b.key - a.key; })
    .map(function (x) { return x.entry; });
}

function quiesce(s, alpha, beta, ply, left) {
  tick();
  if (!rules.kingLocation(s, s.turn)) return -MATE + ply;
  var stand = evaluate(s);
  if (left <= 0) return stand;
  if (stand >= beta) return stand;
  if (stand > alpha) alpha = stand;
  var legal = rules.legalMovesDetailed(s).filter(isCapture);
  legal = ordered(s, legal);
  for (var i = 0; i < legal.length; i++) {
    var score = -quiesce(legal[i].after, -beta, -alpha, ply + 1, left - 1);
    if (score >= beta) return score;
    if (score > alpha) alpha = score;
  }
  return alpha;
}

function search(s, depth, alpha, beta, ply) {
  tick();
  if (!rules.kingLocation(s, s.turn)) return -MATE + ply;
  var legal = rules.legalMovesDetailed(s);
  if (!legal.length) return rules.inCheck(s, s.turn) ? -MATE + ply : 0;
  if (s.halfmove >= 100) return 0;
  if (depth <= 0) return quiesce(s, alpha, beta, ply, 4);
  legal = ordered(s, legal);
  var best = -Infinity;
  for (var i = 0; i < legal.length; i++) {
    var score = -search(legal[i].after, depth - 1, -beta, -alpha, ply + 1);
    if (score > best) best = score;
    if (score > alpha) alpha = score;
    if (alpha >= beta) break;
  }
  return best;
}

function think(afen, timeMs) {
  var root = rules.parse(afen);
  var legal = ordered(root, rules.legalMovesDetailed(root));
  if (!legal.length) return { move: null, depth: 0, score: 0 };
  nodes = 0;
  deadline = Date.now() + timeMs;
  var bestMove = legal[0].text, bestScore = 0, reached = 0;
  for (var depth = 1; depth <= 12; depth++) {
    var alpha = -Infinity, iterBest = null, iterScore = -Infinity;
    try {
      for (var i = 0; i < legal.length; i++) {
        var score = -search(legal[i].after, depth - 1, -Infinity, -alpha, 1);
        if (score > iterScore) {
          iterScore = score;
          iterBest = legal[i];
        }
        if (score > alpha) alpha = score;
      }
    } catch (error) {
      if (!(error instanceof Stop)) throw error;
      // A partial iteration still counts if it found something better.
      if (iterBest && iterScore > bestScore) bestMove = iterBest.text;
      break;
    }
    bestMove = iterBest.text;
    bestScore = iterScore;
    reached = depth;
    // Search the best move first next time.
    legal.splice(legal.indexOf(iterBest), 1);
    legal.unshift(iterBest);
    if (Math.abs(iterScore) > MATE - 100) break;
    if (Date.now() > deadline - timeMs / 2) break;
  }
  return { move: bestMove, depth: reached, score: bestScore };
}

self.onmessage = function (event) {
  var data = event.data;
  if (!rules) {
    importScripts(data.rulesUrl);
    rules = self.AliceAtomic;
  }
  self.postMessage(think(data.afen, data.timeMs || 1000));
};
