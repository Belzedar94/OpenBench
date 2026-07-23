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

console.log('Conquest Map frontend contracts: PASS');
