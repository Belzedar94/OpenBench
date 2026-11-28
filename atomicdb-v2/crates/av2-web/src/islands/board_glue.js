// Chessground calls for the board island (islands/board.rs).  The island owns
// the decisions (legal moves, promotion, where a move navigates, orientation,
// the arrow's colour); this file only drives Chessground, which is a JS
// library: v1's vendored build, loaded from the same static path.
import { Chessground } from '/static/atomicdb/vendor/chessground/chessground.min.js';

function destsMap(json) {
  return new Map(JSON.parse(json));
}

let serial = 0;
function brush(colour) {
  serial += 1;
  const name = `arrow${serial}`;
  return [name, { [name]: { key: name, color: colour, opacity: 1, lineWidth: 10 } }];
}

function shapes(bestMove, name) {
  return bestMove.length >= 4 ? [{ orig: bestMove.slice(0, 2), dest: bestMove.slice(2, 4), brush: name }] : [];
}

export function mountBoard(el, fen, orientation, turn, destsJson, colour, bestMove, onMove) {
  const [name, brushes] = brush(colour);
  const ground = Chessground(el, {
    fen,
    orientation,
    turnColor: turn,
    coordinates: true,
    animation: { enabled: false },
    selectable: { enabled: true },
    draggable: { enabled: true, distance: 3, showGhost: true, deleteOnDropOff: false },
    premovable: { enabled: false },
    drawable: { enabled: false, visible: true, brushes, autoShapes: shapes(bestMove, name) },
    movable: {
      free: false,
      color: turn,
      dests: destsMap(destsJson),
      showDests: true,
      events: { after(from, to) { onMove(from, to); } },
    },
  });
  return ground;
}

export function resetBoard(ground, fen, turn, destsJson) {
  ground.set({ fen, turnColor: turn, lastMove: undefined, movable: { color: turn, dests: destsMap(destsJson) } });
  ground.cancelMove();
}

export function restoreFen(ground, fen) {
  ground.set({ fen });
}

export function toggleOrientation(ground) {
  ground.toggleOrientation();
  return ground.state.orientation;
}

export function setArrow(ground, colour, bestMove) {
  const [name, brushes] = brush(colour);
  Object.assign(ground.state.drawable.brushes, brushes);
  ground.setAutoShapes(shapes(bestMove, name));
}
