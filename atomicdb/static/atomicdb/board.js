import { Chessground } from './vendor/chessground/chessground.min.js';

const board = document.getElementById('board');
const movesNode = document.getElementById('atomicdb-legal-moves');

if (board && movesNode) {
  const legalMoves = JSON.parse(movesNode.textContent);
  const legal = new Set(legalMoves);
  const dests = new Map();
  for (const uci of legalMoves) {
    const from = uci.slice(0, 2);
    const to = uci.slice(2, 4);
    const current = dests.get(from) || [];
    if (!current.includes(to)) current.push(to);
    dests.set(from, current);
  }

  function resolvedUci(from, to) {
    const plain = from + to;
    if (legal.has(plain)) return plain;
    // Queen by default preserves the old UI. The complete keyboard move list
    // still exposes every legal underpromotion.
    for (const suffix of ['q', 'r', 'b', 'n']) {
      if (legal.has(plain + suffix)) return plain + suffix;
    }
    return null;
  }

  let navigating = false;
  let ground;
  const bestMove = board.dataset.bestMove || '';
  const play = board.dataset.play || '';
  const playQuery = play ? `?play=${encodeURIComponent(play)}` : '';
  const autoShapes = bestMove.length >= 4 ? [{
    orig: bestMove.slice(0, 2),
    dest: bestMove.slice(2, 4),
    brush: 'green',
  }] : [];
  ground = Chessground(board, {
    fen: board.dataset.fen,
    orientation: 'white',
    turnColor: board.dataset.turn,
    coordinates: true,
    animation: { enabled: false },
    selectable: { enabled: true },
    draggable: {
      enabled: true,
      distance: 3,
      showGhost: true,
      deleteOnDropOff: false,
    },
    premovable: { enabled: false },
    // Use Chessground's own bounds for the engine arrow. A separate SVG
    // overlay drifts when Chessground rounds its board to a multiple of eight.
    drawable: {
      enabled: false,
      visible: true,
      autoShapes,
    },
    movable: {
      free: false,
      color: board.dataset.turn,
      dests,
      showDests: true,
      events: {
        after(from, to) {
          const uci = resolvedUci(from, to);
          if (!uci || navigating) return;
          navigating = true;
          // Chessground deliberately knows no Atomic rules. Restore the server
          // position before navigating so an Atomic capture is never rendered
          // as a transient orthodox capture.
          ground.set({ fen: board.dataset.fen });
          window.location.assign(
            `/atomicdb/goto/${board.dataset.key}/${uci}/${playQuery}`,
          );
        },
      },
    },
  });
}
