'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const layout = require(path.join(
  __dirname, 'static', 'atomicdb', 'conquest-map.js',
));

{
  const branch = {
    key: 'branch',
    weight: 100,
    hidden_children: 2,
    metrics: {positions: 100, frontier: 100, nodes: 1000},
    children: [{
      key: 'visible',
      weight: 60,
      metrics: {positions: 60, frontier: 60, nodes: 600},
      children: [],
    }],
  };
  const display = layout.withResidualTree(branch, (node) => node.weight);
  const residual = display.children.find((child) => child.residual);

  assert.equal(display.weight, 100);
  assert.equal(display.children[0].weight, 60);
  assert.equal(residual.weight, 40);
  assert.equal(residual.metrics.frontier, 40);
  assert.equal(
    display.children.reduce((total, child) => total + child.weight, 0),
    100,
    'visible leaves plus explicit residual must preserve branch width',
  );

  const allHidden = layout.withResidualTree({
    key: 'all-hidden',
    weight: 25,
    hidden_children: 4,
    truncated: true,
    metrics: {frontier: 25},
    children: [],
  }, (node) => node.weight);
  assert.equal(allHidden.children.length, 1);
  assert.equal(allHidden.children[0].weight, 25);
}

{
  const firstMove = {key: 'first', move: {uci: 'e2e4', san: 'e4'}};
  const payload = {
    snapshot: {start_key: 'start'},
    request: {root: 'deep'},
    root: {
      key: 'deep',
      line_san: '1. e4 e5 2. Nf3',
      line_uci: ['e2e4', 'e7e5', 'g1f3'],
      children: [{key: 'not-a-first-move'}],
    },
    first_moves: [firstMove],
    lineage: {
      start_key: 'start',
      root_key: 'deep',
      line_san: '1. e4 e5 2. Nf3',
      line_uci: ['e2e4', 'e7e5', 'g1f3'],
      positions: [
        {key: 'start', depth: 0, move: null},
        {key: 'first', depth: 1, move: {uci: 'e2e4', san: 'e4'}},
        {key: 'reply', depth: 2, move: {uci: 'e7e5', san: 'e5'}},
        {key: 'deep', depth: 3, move: {uci: 'g1f3', san: 'Nf3'}},
      ],
    },
  };
  const context = layout.payloadContext(payload, []);

  assert.deepEqual(context.firstMoves, [firstMove]);
  assert.notEqual(context.firstMoves[0].key, payload.root.children[0].key);
  assert.deepEqual(
    layout.lineagePrefix(context.lineage, 'deep').map((node) => node.key),
    ['start', 'first', 'reply'],
  );
  assert.equal(layout.lineageParent(context.lineage, 'deep').key, 'reply');
  assert.equal(layout.lineageParent(context.lineage, 'reply').key, 'first');
  assert.equal(layout.lineageParent(context.lineage, 'start'), null);
}

{
  const urlA = '/atomicdb/api/map/v1?root=start&weight=frontier';
  const urlB = '/atomicdb/api/map/v1?root=start&weight=explored';
  const payloadA = {root: {key: 'start'}, request: {weight: 'frontier'}};
  const payloadB = {root: {key: 'start'}, request: {weight: 'explored'}};
  const etags = new Map([
    [urlA, '"etag-a"'],
    [urlB, '"etag-b"'],
  ]);

  assert.equal(
    layout.etagForCurrentPayload(urlA, urlA, payloadA, etags),
    '"etag-a"',
    'refreshing the currently displayed URL may use its ETag',
  );
  assert.equal(
    layout.etagForCurrentPayload(urlA, urlB, payloadB, etags),
    null,
    'A -> B -> A must fetch A again instead of reusing B after an A 304',
  );
  assert.equal(
    layout.canReuseNotModified(urlA, urlA, payloadA),
    true,
    '304 may preserve a body only when that body belongs to the request URL',
  );
  assert.equal(
    layout.canReuseNotModified(urlA, urlB, payloadB),
    false,
    '304 must never preserve a body belonging to another URL',
  );
}

{
  assert.equal(layout.compactText('Nf3', 40), 'Nf3');
  assert.equal(layout.compactText('A very long opening label', 39), 'A ver…');
  assert.equal(layout.compactText('e4', 4), '');

  const shallow = layout.verticalGeometry(496, 5);
  assert.equal(shallow.band, 57);
  assert.equal(shallow.contentHeight, 285);
  assert.equal(shallow.frameHeight, 285);

  const deep = layout.verticalGeometry(496, 95);
  assert.ok(deep.band >= 31, 'deep rows must remain individually legible');
  assert.equal(deep.frameHeight, 496);
  assert.ok(
    deep.contentHeight >= 95 * 31,
    'deep trees must scroll instead of collapsing rows into the viewport',
  );
}

{
  const root = {data: {key: 'root'}, parent: null, children: []};
  const first = {data: {key: 'first'}, parent: root, children: []};
  const residual = {
    data: {key: 'residual', residual: true}, parent: root, children: [],
  };
  const last = {data: {key: 'last'}, parent: root, children: []};
  root.children = [first, residual, last];
  const ordered = [root, first, last];
  const allowed = (entry) => !entry.data.residual;

  assert.equal(
    layout.keyboardTarget(first, 'ArrowRight', allowed, ordered),
    last,
    'sibling navigation must skip non-interactive residual marks',
  );
  assert.equal(
    layout.keyboardTarget(last, 'ArrowRight', allowed, ordered),
    first,
    'sibling navigation wraps among interactive marks only',
  );
  assert.equal(
    layout.keyboardTarget(first, 'ArrowUp', allowed, ordered),
    root,
  );
  assert.equal(
    layout.keyboardTarget(root, 'ArrowDown', allowed, ordered),
    first,
  );
  assert.equal(layout.keyboardTarget(last, 'Home', allowed, ordered), root);
  assert.equal(layout.keyboardTarget(first, 'End', allowed, ordered), last);
  assert.equal(layout.isExpandable(root), true);
  assert.equal(layout.isExpandable(first), false);
  assert.equal(layout.isExpandable({data: {truncated: true}}), true);
}

{
  const root = {data: {key: 'root'}, parent: null, children: []};
  const filteredBridge = {
    data: {key: 'filtered-bridge'}, parent: root, children: [],
  };
  const visibleLeaf = {
    data: {key: 'visible-leaf'},
    parent: filteredBridge,
    children: [],
  };
  filteredBridge.children = [visibleLeaf];
  root.children = [filteredBridge];
  const allowed = (entry) => (
    entry.data.key === 'root' || entry.data.key === 'visible-leaf'
  );

  assert.equal(
    layout.keyboardTarget(root, 'ArrowDown', allowed, [root, visibleLeaf]),
    visibleLeaf,
    'down navigation must cross a filtered parent to its nearest visible descendant',
  );
  assert.equal(
    layout.keyboardTarget(
      visibleLeaf, 'ArrowUp', allowed, [root, visibleLeaf],
    ),
    root,
    'up navigation must cross filtered ancestors to the nearest visible one',
  );
}

{
  const root = {
    data: {key: 'root'}, parent: null, children: [], pixelWidth: 300,
  };
  const narrow = {
    data: {key: 'narrow'}, parent: root, children: [], pixelWidth: 3.9,
  };
  const narrowLeaf = {
    data: {key: 'narrow-leaf'},
    parent: narrow,
    children: [],
    pixelWidth: 1,
  };
  const broad = {
    data: {key: 'broad'}, parent: root, children: [], pixelWidth: 40,
  };
  narrow.children = [narrowLeaf];
  root.children = [narrow, broad];

  const density = layout.densityPlan(
    [root, narrow, narrowLeaf, broad],
    (entry) => entry.pixelWidth,
    4,
  );
  assert.deepEqual(
    density.visible.map((entry) => entry.data.key),
    ['root', 'broad'],
    'sub-four-pixel visual slivers should not consume DOM marks',
  );
  assert.equal(
    density.omittedByKey.get('root'),
    1,
    'an omitted subtree counts once at its nearest rendered ancestor',
  );
  assert.equal(
    density.omittedByKey.has('narrow'),
    false,
    'omission context belongs only to an ancestor that remains visible',
  );
}

{
  const root = {data: {key: 'root'}};
  const broad = {data: {key: 'broad'}};
  const navigation = layout.navigationState(
    [root, broad],
    'narrow-selected',
    'broad',
    'root',
  );
  assert.deepEqual(
    navigation,
    {selectedKey: 'narrow-selected', rovingKey: 'broad'},
    'an omitted logical selection must survive while focus roves elsewhere',
  );
  assert.deepEqual(
    layout.navigationState([root], 'narrow-selected', '', 'root'),
    {selectedKey: 'narrow-selected', rovingKey: 'root'},
    'URL-restored sub-pixel selections must not be replaced by the focus mark',
  );
}

{
  assert.equal(
    layout.filterFeedback(20, 3, 2),
    null,
    'a rendered filter match needs no empty-state guidance',
  );
  assert.deepEqual(
    layout.filterFeedback(20, 0, 0),
    {
      reason: 'no-matches',
      message: '0 of 20 territories match the current filters.',
    },
  );
  assert.deepEqual(
    layout.filterFeedback(20, 3, 0),
    {
      reason: 'too-fine',
      message: '3 of 20 territories match, but they are inside branches too narrow to display at this zoom. Zoom into a first move or clear the filters.',
    },
    'source matches omitted by the pixel-density plan need visible guidance',
  );
}

console.log('Conquest Map frontend contracts: PASS');
