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
  // The arrow's colour can be changed in the settings (settings.js owns the
  // key and announces changes with 'atomicdb-arrow'); its own brush keeps
  // Chessground's green as the default.
  const ARROW_DEFAULT = '#15781b';
  function arrowColour() {
    try {
      const value = window.localStorage.getItem('atomicdb-arrow') || '';
      if (/^#[0-9a-f]{6}$/i.test(value)) return value;
    } catch (_error) {
      // No storage: the default arrow.
    }
    return ARROW_DEFAULT;
  }
  // A fresh brush name for every change: Chessground keys a drawn arrow (and
  // its arrowhead) by the brush's name and keeps what it drew before, so
  // reusing a name, even one from an earlier colour, may not repaint.
  let arrowSerial = 0;
  let arrowName = '';
  function arrowBrushes(colour) {
    arrowSerial += 1;
    arrowName = `arrow${arrowSerial}`;
    return { [arrowName]: { key: arrowName, color: colour, opacity: 1, lineWidth: 10 } };
  }
  function arrowShapes() {
    return bestMove.length >= 4 ? [{
      orig: bestMove.slice(0, 2),
      dest: bestMove.slice(2, 4),
      brush: arrowName,
    }] : [];
  }
  const initialArrow = arrowColour();
  const initialBrushes = arrowBrushes(initialArrow);
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
      brushes: initialBrushes,
      autoShapes: arrowShapes(),
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

  window.addEventListener('atomicdb-arrow', (event) => {
    const colour = /^#[0-9a-f]{6}$/i.test(event.detail || '')
      ? event.detail : ARROW_DEFAULT;
    Object.assign(ground.state.drawable.brushes, arrowBrushes(colour));
    ground.setAutoShapes(arrowShapes());
  });

  // Back from the position a move led to, the browser may restore this page
  // from its back/forward cache exactly as it was left: mid-navigation, with
  // the turn handed over by the move.  The board then took no move until a
  // reload (seaside_cliff, 3-Nov-2026).  Put it back as the server drew it.
  window.addEventListener('pageshow', (event) => {
    if (!event.persisted) return;
    navigating = false;
    ground.set({
      fen: board.dataset.fen,
      turnColor: board.dataset.turn,
      lastMove: undefined,
      movable: { color: board.dataset.turn, dests },
    });
    ground.cancelMove();
  });
}
