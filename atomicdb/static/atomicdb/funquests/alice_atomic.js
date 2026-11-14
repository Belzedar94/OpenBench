/*
 * Alice Atomic rules: Alice chess on two boards, with atomic captures.
 *
 * The same rules run on the server (atomicdb/funquests/alice_atomic.py); the
 * shared test vectors keep the two in step.  Works as a browser global
 * (window.AliceAtomic), inside a Web Worker (importScripts) and in Node
 * (require).
 *
 * Position text: "<board A>|<board B> <side> <castling> <en passant>
 * <halfmove> <fullmove>", each board a FEN piece placement, en passant "-" or
 * board letter plus square ("Be3").  Move text: board letter plus UCI
 * ("Ae2e4", "Bd7d8q", "Ae1g1").  Squares are 0..63, a1 = 0, a8 = 56; an
 * empty square is "".
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AliceAtomic = api;
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var START_AFEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR|' +
    '8/8/8/8/8/8/8/8 w KQkq - 0 1';
  var BOARD_LETTERS = 'AB';
  var FILES = 'abcdefgh';
  var KNIGHT_STEPS = [[1, 2], [2, 1], [-1, 2], [-2, 1],
    [1, -2], [2, -1], [-1, -2], [-2, -1]];
  var KING_STEPS = [[1, 0], [-1, 0], [0, 1], [0, -1],
    [1, 1], [1, -1], [-1, 1], [-1, -1]];
  var ROOK_DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1]];
  var BISHOP_DIRS = [[1, 1], [1, -1], [-1, 1], [-1, -1]];
  var QUEEN_DIRS = ROOK_DIRS.concat(BISHOP_DIRS);
  var PROMOTIONS = 'qrbn';
  var QUIET = 'q', DOUBLE = 'd', CAPTURE = 'c', EN_PASSANT = 'e',
    CASTLE = 'k';

  function squareName(sq) {
    return FILES[sq % 8] + String(Math.floor(sq / 8) + 1);
  }

  function parseSquare(name) {
    if (!/^[a-h][1-8]$/.test(name)) throw new Error('bad square ' + name);
    return FILES.indexOf(name[0]) + 8 * (Number(name[1]) - 1);
  }

  function isUpper(p) { return p >= 'A' && p <= 'Z'; }
  function colorOf(p) { return isUpper(p) ? 'w' : 'b'; }
  function opponent(c) { return c === 'w' ? 'b' : 'w'; }
  function onBoard(f, r) { return f >= 0 && f < 8 && r >= 0 && r < 8; }

  function copyState(s) {
    return {
      boards: [s.boards[0].slice(), s.boards[1].slice()],
      turn: s.turn,
      castling: s.castling,
      ep: s.ep ? [s.ep[0], s.ep[1]] : null,
      halfmove: s.halfmove,
      fullmove: s.fullmove,
    };
  }

  // -- reading and writing positions ---------------------------------------

  function parsePlacement(text) {
    var ranks = text.split('/');
    if (ranks.length !== 8) throw new Error('a board needs 8 ranks');
    var board = new Array(64).fill('');
    for (var i = 0; i < 8; i++) {
      var rank = 7 - i, file = 0, row = ranks[i];
      for (var j = 0; j < row.length; j++) {
        var ch = row[j];
        if (ch >= '0' && ch <= '9') {
          file += Number(ch);
        } else if ('pnbrqkPNBRQK'.indexOf(ch) >= 0) {
          if (file > 7) throw new Error('rank too long');
          board[rank * 8 + file] = ch;
          file += 1;
        } else {
          throw new Error('bad piece ' + ch);
        }
      }
      if (file !== 8) throw new Error('rank of the wrong length');
    }
    return board;
  }

  function placement(board) {
    var rows = [];
    for (var rank = 7; rank >= 0; rank--) {
      var row = '', empty = 0;
      for (var file = 0; file < 8; file++) {
        var p = board[rank * 8 + file];
        if (p) {
          if (empty) { row += String(empty); empty = 0; }
          row += p;
        } else {
          empty += 1;
        }
      }
      if (empty) row += String(empty);
      rows.push(row);
    }
    return rows.join('/');
  }

  function parse(afen) {
    var parts = afen.trim().split(/\s+/);
    if (parts.length !== 6) throw new Error('a position has six fields');
    var halves = parts[0].split('|');
    if (halves.length !== 2) throw new Error('a position has two boards');
    if (parts[1] !== 'w' && parts[1] !== 'b') {
      throw new Error('bad side to move');
    }
    if (parts[2] !== '-' && !/^[KQkq]+$/.test(parts[2])) {
      throw new Error('bad castling field');
    }
    var ep = null;
    if (parts[3] !== '-') {
      if (parts[3].length !== 3 || BOARD_LETTERS.indexOf(parts[3][0]) < 0) {
        throw new Error('bad en passant field');
      }
      ep = [BOARD_LETTERS.indexOf(parts[3][0]), parseSquare(parts[3].slice(1))];
    }
    var rights = '';
    for (var i = 0; i < 4; i++) {
      if (parts[2].indexOf('KQkq'[i]) >= 0) rights += 'KQkq'[i];
    }
    return {
      boards: [parsePlacement(halves[0]), parsePlacement(halves[1])],
      turn: parts[1],
      castling: rights,
      ep: ep,
      halfmove: Number(parts[4]),
      fullmove: Number(parts[5]),
    };
  }

  function epText(s) {
    return s.ep ? BOARD_LETTERS[s.ep[0]] + squareName(s.ep[1]) : '-';
  }

  function toAfen(s) {
    return [placement(s.boards[0]) + '|' + placement(s.boards[1]),
      s.turn, s.castling || '-', epText(s),
      String(s.halfmove), String(s.fullmove)].join(' ');
  }

  function positionKey(s) {
    return toAfen(s).split(' ').slice(0, 4).join(' ');
  }

  function startState() { return parse(START_AFEN); }

  // -- attacks and check ---------------------------------------------------

  function attacked(board, sq, by) {
    var f = sq % 8, r = Math.floor(sq / 8);
    var white = by === 'w';
    var pawnRank = white ? r - 1 : r + 1;
    var pawn = white ? 'P' : 'p';
    var i, df, dr;
    if (pawnRank >= 0 && pawnRank < 8) {
      for (df = -1; df <= 1; df += 2) {
        if (onBoard(f + df, pawnRank) && board[pawnRank * 8 + f + df] === pawn) {
          return true;
        }
      }
    }
    var knight = white ? 'N' : 'n';
    for (i = 0; i < 8; i++) {
      df = KNIGHT_STEPS[i][0]; dr = KNIGHT_STEPS[i][1];
      if (onBoard(f + df, r + dr) && board[(r + dr) * 8 + f + df] === knight) {
        return true;
      }
    }
    var groups = [[ROOK_DIRS, white ? 'RQ' : 'rq'],
      [BISHOP_DIRS, white ? 'BQ' : 'bq']];
    for (var g = 0; g < 2; g++) {
      var dirs = groups[g][0], movers = groups[g][1];
      for (i = 0; i < 4; i++) {
        df = dirs[i][0]; dr = dirs[i][1];
        var ff = f + df, rr = r + dr;
        while (onBoard(ff, rr)) {
          var p = board[rr * 8 + ff];
          if (p) {
            if (movers.indexOf(p) >= 0) return true;
            break;
          }
          ff += df; rr += dr;
        }
      }
    }
    return false;
  }

  function kingLocation(s, color) {
    var king = color === 'w' ? 'K' : 'k';
    for (var b = 0; b < 2; b++) {
      var idx = s.boards[b].indexOf(king);
      if (idx >= 0) return [b, idx];
    }
    return null;
  }

  function kingsAdjacent(board, sq, color) {
    var enemyKing = color === 'w' ? 'k' : 'K';
    var f = sq % 8, r = Math.floor(sq / 8);
    for (var i = 0; i < 8; i++) {
      var df = KING_STEPS[i][0], dr = KING_STEPS[i][1];
      if (onBoard(f + df, r + dr) && board[(r + dr) * 8 + f + df] === enemyKing) {
        return true;
      }
    }
    return false;
  }

  // Whether a king of `color` on `sq` of `board` would be in check there;
  // `other` is the other board.  The capture would land and explode on the
  // other board: impossible if that square is taken there, or if the blast
  // would take the capturer's own king.
  function squareInCheck(board, other, sq, color) {
    if (other[sq] || kingsAdjacent(other, sq, color)) return false;
    return attacked(board, sq, opponent(color));
  }

  function inCheck(s, color) {
    var loc = kingLocation(s, color);
    if (!loc) return false;
    return squareInCheck(s.boards[loc[0]], s.boards[1 - loc[0]], loc[1], color);
  }

  // -- move generation -----------------------------------------------------

  function moveText(m) {
    return BOARD_LETTERS[m.b] + squareName(m.from) + squareName(m.to) +
      (m.promo || '');
  }

  function mv(b, from, to, promo, kind) {
    return { b: b, from: from, to: to, promo: promo, kind: kind };
  }

  function castlingMoves(s, b, sq, out) {
    var white = s.turn === 'w';
    var home = white ? 0 : 56;
    if (b !== 0 || sq !== home + 4) return;
    var board = s.boards[0], other = s.boards[1];
    var rook = white ? 'R' : 'r';
    var sides = [
      [white ? 'K' : 'k', 7, [5, 6], 6, 5, [4, 5, 6]],
      [white ? 'Q' : 'q', 0, [1, 2, 3], 2, 3, [4, 3, 2]],
    ];
    for (var i = 0; i < 2; i++) {
      var side = sides[i];
      if (s.castling.indexOf(side[0]) < 0) continue;
      if (board[home + side[1]] !== rook) continue;
      var blocked = false, j;
      for (j = 0; j < side[2].length; j++) {
        if (board[home + side[2][j]]) blocked = true;
      }
      if (blocked) continue;
      if (other[home + side[3]] || other[home + side[4]]) continue;
      var unsafe = false;
      for (j = 0; j < 3; j++) {
        if (squareInCheck(board, other, home + side[5][j], s.turn)) unsafe = true;
      }
      if (unsafe) continue;
      out.push(mv(0, home + 4, home + side[3], null, CASTLE));
    }
  }

  function pseudoMoves(s) {
    var turn = s.turn, white = turn === 'w';
    var moves = [];
    for (var b = 0; b < 2; b++) {
      var board = s.boards[b], other = s.boards[1 - b];
      for (var sq = 0; sq < 64; sq++) {
        var piece = board[sq];
        if (!piece || colorOf(piece) !== turn) continue;
        var kind = piece.toUpperCase();
        var f = sq % 8, r = Math.floor(sq / 8);
        var i, df, dr, target, victim;
        if (kind === 'P') {
          var step = white ? 8 : -8;
          var startRank = white ? 1 : 6;
          var lastRank = white ? 7 : 0;
          var one = sq + step;
          if (one >= 0 && one < 64 && !board[one]) {
            if (!other[one]) {
              if (Math.floor(one / 8) === lastRank) {
                for (i = 0; i < 4; i++) moves.push(mv(b, sq, one, PROMOTIONS[i], QUIET));
              } else {
                moves.push(mv(b, sq, one, null, QUIET));
              }
            }
            var two = one + step;
            if (r === startRank && !board[two] && !other[two]) {
              moves.push(mv(b, sq, two, null, DOUBLE));
            }
          }
          var rr = r + (white ? 1 : -1);
          for (df = -1; df <= 1; df += 2) {
            if (!onBoard(f + df, rr)) continue;
            target = rr * 8 + f + df;
            if (other[target]) continue;  // a capture passes through too
            victim = board[target];
            if (victim && colorOf(victim) !== turn) {
              if (rr === lastRank) {
                for (i = 0; i < 4; i++) moves.push(mv(b, sq, target, PROMOTIONS[i], CAPTURE));
              } else {
                moves.push(mv(b, sq, target, null, CAPTURE));
              }
            } else if (!victim && s.ep && s.ep[0] === b && s.ep[1] === target &&
                       board[target - step] === (white ? 'p' : 'P')) {
              moves.push(mv(b, sq, target, null, EN_PASSANT));
            }
          }
          continue;
        }
        if (kind === 'N' || kind === 'K') {
          var steps = kind === 'N' ? KNIGHT_STEPS : KING_STEPS;
          for (i = 0; i < 8; i++) {
            df = steps[i][0]; dr = steps[i][1];
            if (!onBoard(f + df, r + dr)) continue;
            target = (r + dr) * 8 + f + df;
            victim = board[target];
            if (!victim) {
              if (!other[target]) moves.push(mv(b, sq, target, null, QUIET));
            } else if (colorOf(victim) !== turn && kind !== 'K' && !other[target]) {
              moves.push(mv(b, sq, target, null, CAPTURE));
            }
          }
          if (kind === 'K') castlingMoves(s, b, sq, moves);
          continue;
        }
        var dirs = kind === 'R' ? ROOK_DIRS : kind === 'B' ? BISHOP_DIRS : QUEEN_DIRS;
        for (i = 0; i < dirs.length; i++) {
          df = dirs[i][0]; dr = dirs[i][1];
          var ff = f + df, r2 = r + dr;
          while (onBoard(ff, r2)) {
            target = r2 * 8 + ff;
            victim = board[target];
            if (!victim) {
              if (!other[target]) moves.push(mv(b, sq, target, null, QUIET));
            } else {
              if (colorOf(victim) !== turn && !other[target]) {
                moves.push(mv(b, sq, target, null, CAPTURE));
              }
              break;
            }
            ff += df; r2 += dr;
          }
        }
      }
    }
    return moves;
  }

  function applyMove(state, m) {
    var s = copyState(state);
    var board = s.boards[m.b], other = s.boards[1 - m.b];
    var piece = board[m.from];
    var white = state.turn === 'w';
    var to = m.to;
    board[m.from] = '';
    if (m.kind === CASTLE) {
      var home = m.from - 4;
      var rookFrom = to === home + 6 ? home + 7 : home;
      var rookTo = to === home + 6 ? home + 5 : home + 3;
      var rook = board[rookFrom];
      board[rookFrom] = '';
      other[to] = piece;
      other[rookTo] = rook;
    } else if (m.kind === CAPTURE || m.kind === EN_PASSANT) {
      if (m.kind === EN_PASSANT) board[white ? to - 8 : to + 8] = '';
      board[to] = '';
      // The capturer passes through to `to` of the other board (empty there:
      // the generator checked) and explodes on that board.
      var f = to % 8, r = Math.floor(to / 8);
      for (var i = 0; i < 8; i++) {
        var df = KING_STEPS[i][0], dr = KING_STEPS[i][1];
        if (onBoard(f + df, r + dr)) {
          var n = (r + dr) * 8 + f + df;
          if (other[n] && other[n] !== 'P' && other[n] !== 'p') other[n] = '';
        }
      }
    } else {
      if (m.promo) piece = white ? m.promo.toUpperCase() : m.promo;
      other[to] = piece;
    }
    s.ep = null;
    if (m.kind === DOUBLE) {
      var middle = (m.from + to) / 2;
      if (!other[middle]) s.ep = [1 - m.b, middle];
    }
    var a = s.boards[0];
    var rights = '';
    var table = [['K', 0, 'K', 'R'], ['Q', 0, 'K', 'R'],
      ['k', 56, 'k', 'r'], ['q', 56, 'k', 'r']];
    for (var t = 0; t < 4; t++) {
      var right = table[t][0];
      if (state.castling.indexOf(right) < 0) continue;
      var rookSq = table[t][1] + (right === 'K' || right === 'k' ? 7 : 0);
      if (a[table[t][1] + 4] === table[t][2] && a[rookSq] === table[t][3]) {
        rights += right;
      }
    }
    s.castling = rights;
    if (piece === 'P' || piece === 'p' || m.kind === CAPTURE || m.kind === EN_PASSANT) {
      s.halfmove = 0;
    } else {
      s.halfmove = state.halfmove + 1;
    }
    if (!white) s.fullmove = state.fullmove + 1;
    s.turn = opponent(state.turn);
    return s;
  }

  function isLegalResult(after, mover) {
    if (!kingLocation(after, mover)) return false;
    if (!kingLocation(after, opponent(mover))) return true;
    return !inCheck(after, mover);
  }

  // Every legal move as {move, text, after}.
  function legalMovesDetailed(s) {
    var out = [];
    var pseudo = pseudoMoves(s);
    for (var i = 0; i < pseudo.length; i++) {
      var after = applyMove(s, pseudo[i]);
      if (isLegalResult(after, s.turn)) {
        out.push({ move: pseudo[i], text: moveText(pseudo[i]), after: after });
      }
    }
    return out;
  }

  function legalMoves(s) {
    return legalMovesDetailed(s).map(function (x) { return x.text; });
  }

  function play(s, text) {
    var legal = legalMovesDetailed(s);
    for (var i = 0; i < legal.length; i++) {
      if (legal[i].text === text) return legal[i].after;
    }
    throw new Error('illegal move ' + text);
  }

  // [result, reason] when the game is over, else null.  `repetitions` is how
  // many times this position has stood on the board, this time included.
  function outcome(s, repetitions) {
    if (!kingLocation(s, 'w')) return ['0-1', 'explosion'];
    if (!kingLocation(s, 'b')) return ['1-0', 'explosion'];
    if (legalMovesDetailed(s).length === 0) {
      if (inCheck(s, s.turn)) return [s.turn === 'w' ? '0-1' : '1-0', 'checkmate'];
      return ['1/2-1/2', 'stalemate'];
    }
    if ((repetitions || 1) >= 3) return ['1/2-1/2', 'repetition'];
    if (s.halfmove >= 100) return ['1/2-1/2', 'fifty-move'];
    return null;
  }

  // Play `moves` from `afen` (default: the start).  Returns
  // {state, outcome, states}; throws on an illegal move.
  function replay(moves, afen) {
    var s = parse(afen || START_AFEN);
    var states = [s];
    var seen = {};
    seen[positionKey(s)] = 1;
    for (var i = 0; i < moves.length; i++) {
      if (outcome(s, seen[positionKey(s)] || 1)) {
        throw new Error('move after the end ' + moves[i]);
      }
      s = play(s, moves[i]);
      if (s.halfmove === 0) seen = {};
      var key = positionKey(s);
      seen[key] = (seen[key] || 0) + 1;
      states.push(s);
    }
    return { state: s, outcome: outcome(s, seen[positionKey(s)] || 1), states: states, seen: seen };
  }

  function perft(s, depth) {
    if (depth === 0) return 1;
    var total = 0;
    var legal = legalMovesDetailed(s);
    for (var i = 0; i < legal.length; i++) {
      if (depth === 1) total += 1;
      else if (!kingLocation(legal[i].after, opponent(s.turn))) continue;
      else total += perft(legal[i].after, depth - 1);
    }
    return total;
  }

  return {
    START_AFEN: START_AFEN,
    BOARD_LETTERS: BOARD_LETTERS,
    QUIET: QUIET, DOUBLE: DOUBLE, CAPTURE: CAPTURE,
    EN_PASSANT: EN_PASSANT, CASTLE: CASTLE,
    squareName: squareName,
    parseSquare: parseSquare,
    colorOf: colorOf,
    opponent: opponent,
    parse: parse,
    toAfen: toAfen,
    positionKey: positionKey,
    startState: startState,
    copyState: copyState,
    attacked: attacked,
    inCheck: inCheck,
    kingLocation: kingLocation,
    pseudoMoves: pseudoMoves,
    applyMove: applyMove,
    legalMovesDetailed: legalMovesDetailed,
    legalMoves: legalMoves,
    moveText: moveText,
    play: play,
    outcome: outcome,
    replay: replay,
    perft: perft,
  };
}));
