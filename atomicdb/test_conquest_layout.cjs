'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const tree = require(path.join(
  __dirname, 'static', 'atomicdb', 'conquest-map.js',
));

function node(key, options = {}, children = []) {
  return {
    key,
    fen: options.fen || '8/8/8/8/8/8/8/8 w - - 0 1',
    status: options.status || 'UNKNOWN',
    eval_cp: options.eval_cp === undefined ? null : options.eval_cp,
    depth: options.depth === undefined ? 1 : options.depth,
    move: options.move || {san: key, uci: `${key}uci`},
    line_san: Object.prototype.hasOwnProperty.call(options, 'line_san')
      ? options.line_san : key,
    opening: options.opening,
    work: options.work || {
      exact_state: 'idle',
      own_active: 0,
      own_queued: 0,
    },
    children,
    zoomable: children.length > 0,
  };
}

{
  const exactIdle = node('idle', {
    work: {
      exact_state: 'idle',
      own_active: 0,
      own_queued: 0,
      state: 'active',
      active: 99,
      queued: 99,
      subtree_active: 99,
    },
  });
  const exactActive = node('active', {
    work: {exact_state: 'active', own_active: 1, own_queued: 0},
  });
  const exactQueued = node('queued', {
    work: {exact_state: 'idle', own_active: 0, own_queued: 2},
  });
  assert.equal(tree.exactWorkState(exactIdle), 'idle');
  assert.equal(tree.exactWorkState(exactActive), 'active');
  assert.equal(tree.exactWorkState(exactQueued), 'queued');
}

{
  const payload = {
    snapshot: {start_key: 'root'},
    root: node('root', {move: null, depth: 0}, [
      node('Nf3', {depth: 1}),
    ]),
    first_moves: [
      node('Nf3', {depth: 1}),
      node('e3', {depth: 1, line_san: ''}),
    ],
  };
  const merged = tree.mergeFirstMoves(payload);
  assert.deepEqual(
    merged.children.map((child) => child.key),
    ['Nf3', 'e3'],
    'bounded root children and complete first_moves must merge by key',
  );
  assert.equal(merged.children[1].line_san, '1. e3');
  assert.equal(merged.children[1].children.length, 0);
  assert.equal(merged.children[1].zoomable, true);
}

{
  const activeLeaf = node('active-leaf', {
    depth: 3,
    work: {exact_state: 'active', own_active: 1, own_queued: 0},
  });
  const activeParent = node('active-parent', {depth: 2}, [activeLeaf]);
  const quietLeaf = node('quiet-leaf', {depth: 2});
  const root = node('root', {move: null, depth: 0}, [
    node('quiet', {depth: 1}, [quietLeaf]),
    node('active', {depth: 1}, [activeParent]),
  ]);
  const projected = tree.projectTree(root);
  assert.deepEqual(
    projected.nodes.map((item) => item.key),
    ['root', 'quiet', 'active', 'active-parent', 'active-leaf'],
    'default projection is the root, its first level, and exact-active paths',
  );
  assert.equal(
    projected.nodes.find((item) => item.key === 'active-leaf')._exactWork,
    'active',
  );
  assert.equal(
    projected.nodes.find((item) => item.key === 'active')._activePath,
    true,
    'active-path metadata is independent of source-order placement',
  );

  const expanded = tree.projectTree(root, {
    expandedKeys: new Set(['root', 'quiet']),
  });
  assert.deepEqual(
    expanded.nodes.map((item) => item.key),
    ['root', 'quiet', 'quiet-leaf', 'active', 'active-parent', 'active-leaf'],
  );

  const collapsed = tree.projectTree(root, {
    collapsedKeys: new Set(['active']),
  });
  assert.deepEqual(
    collapsed.nodes.map((item) => item.key),
    ['root', 'quiet', 'active'],
    'an explicit collapse may hide the default active continuation',
  );
}

{
  const named = node('cowboy', {
    line_san: '1. e3 e6 2. Qh5 g6 3. Nf3',
    opening: {name: 'Cowboy Attack'},
    status: 'WHITE_WIN',
  });
  const unknown = node('unknown', {
    line_san: '1. Nf3 d6 2. Ng5',
    opening: {name: 'Villager Defense'},
    status: 'UNKNOWN',
  });
  const root = node('root', {move: null, depth: 0}, [
    node('branch-a', {depth: 1, status: 'WHITE_WIN'}, [named]),
    node('branch-b', {depth: 1}, [unknown]),
  ]);

  const unresolved = tree.projectTree(root, {mode: 'unresolved'});
  assert.deepEqual(
    unresolved.nodes.map((item) => item.key),
    ['root', 'branch-b', 'unknown'],
    'mode filtering retains every ancestor of a matching node',
  );

  const searchOpening = tree.projectTree(root, {
    query: 'cowboy attack',
  });
  assert.deepEqual(
    searchOpening.nodes.map((item) => item.key),
    ['root', 'branch-a', 'cowboy'],
  );
  const searchLine = tree.projectTree(root, {query: 'Nf3 d6'});
  assert.deepEqual(
    searchLine.nodes.map((item) => item.key),
    ['root', 'branch-b', 'unknown'],
  );
  const searchSan = tree.projectTree(root, {query: 'Qh5'});
  assert.equal(searchSan.nodes.at(-1).key, 'cowboy');
}

{
  const children = [];
  for (let index = 0; index < 40; index += 1) {
    children.push(node(`candidate-${index}`, {depth: 1}));
  }
  const root = node('root', {move: null, depth: 0}, children);
  const focal = tree.projectTree(root, {focusKey: 'root'});
  assert.equal(focal.nodes.length, 6);
  assert.equal(focal.root._partialChildren, true);
  assert.equal(focal.root._hiddenChildren, 35);

  const compact = tree.projectTree(root, {
    focusKey: 'root',
    focalItems: 14,
    rootBranches: 3,
    siblingRadius: 1,
  });
  assert.equal(compact.root.children.length, 3);
  assert.equal(compact.root._hiddenChildren, 37);

  const disclosed = tree.projectTree(root, {
    focusKey: 'root',
    expandedKeys: new Set(['root']),
  });
  assert.equal(disclosed.nodes.length, 41);
  assert.equal(disclosed.root._partialChildren, false);
  assert.equal(disclosed.root._hiddenChildren, 0);

  const selected = tree.projectTree(root, {focusKey: 'candidate-39'});
  assert.ok(
    selected.nodes.some((item) => item.key === 'candidate-39'),
    'a selected path outranks generic sibling context',
  );
  assert.ok(
    selected.nodes.length <= 6,
    'the initial focal scene remains within the readable card budget',
  );
}

{
  const deepBranch = (prefix) => node(`${prefix}-first`, {depth: 1}, [
    node(`${prefix}-reply-a`, {depth: 2}, [
      node(`${prefix}-continuation-a`, {depth: 3}, [
        node(`${prefix}-continuation-b`, {depth: 4}),
      ]),
    ]),
    node(`${prefix}-reply-b`, {depth: 2}),
    node(`${prefix}-reply-c`, {depth: 2}),
    node(`${prefix}-reply-hidden`, {depth: 2}),
  ]);
  const root = node('root', {move: null, depth: 0}, [
    deepBranch('a'),
    deepBranch('b'),
    node('quiet-c', {depth: 1}),
    node('quiet-d', {depth: 1}),
    node('quiet-e', {depth: 1}),
    node('quiet-f', {depth: 1}),
    node('quiet-g', {depth: 1}),
    node('quiet-hidden', {depth: 1}),
  ]);
  const focal = tree.projectTree(root, {focusKey: 'root'});
  assert.ok(
    focal.nodes.length > 8 && focal.nodes.length <= tree.DEFAULT_FOCAL_ITEMS,
    'the initial scene spends its remaining budget on real continuations',
  );
  assert.ok(
    focal.nodes.some((item) => item.key === 'a-reply-a')
      && focal.nodes.some((item) => item.key === 'a-continuation-a'),
    'the focal preview reveals both branching and useful depth',
  );
  assert.equal(
    focal.root.children.length,
    tree.DEFAULT_ROOT_BRANCHES,
    'deep context never reintroduces a wall of first moves',
  );
  const narrow = tree.projectTree(root, {
    focusKey: 'root',
    focalItems: 14,
    rootBranches: 3,
    siblingRadius: 1,
    previewReplies: 1,
  });
  assert.equal(narrow.root.children.length, 3);
  assert.ok(
    narrow.root.children.every((child) => child.children.length <= 1),
    'the narrow-screen preview exposes depth without a horizontal reply wall',
  );
}

{
  const children = [];
  for (let index = 0; index < 450; index += 1) {
    children.push(node(`move-${String(index).padStart(3, '0')}`, {
      status: 'UNKNOWN',
      depth: 1,
    }));
  }
  const projected = tree.projectTree(
    node('root', {move: null, depth: 0}, children),
    {mode: 'unresolved', maxItems: 9999},
  );
  assert.equal(projected.nodes.length, tree.MAX_TREEITEMS);
  assert.equal(projected.truncated, true);
  assert.equal(projected.nodes[0].key, 'root');
}

{
  assert.equal(tree.FILTERED_DESKTOP_ITEMS, 40);
  assert.equal(tree.FILTERED_NARROW_ITEMS, 24);
  assert.equal(tree.FILTERED_DESKTOP_TARGETS, 10);
  assert.equal(tree.FILTERED_NARROW_TARGETS, 6);
  assert.ok(
    tree.FILTERED_NARROW_ITEMS < tree.FILTERED_DESKTOP_ITEMS
      && tree.FILTERED_DESKTOP_ITEMS < tree.MAX_TREEITEMS,
    'filtered views use a readable budget below the hard safety cap',
  );
  assert.ok(
    tree.FILTERED_NARROW_TARGETS < tree.FILTERED_DESKTOP_TARGETS,
    'narrow filtered views sample fewer endpoints',
  );
}

{
  function unresolvedBranch(prefix) {
    let child = node(`${prefix}-5`, {depth: 5});
    for (let depth = 4; depth >= 1; depth -= 1) {
      child = node(`${prefix}-${depth}`, {depth}, [child]);
    }
    return child;
  }
  const root = node('root', {move: null, depth: 0}, [
    unresolvedBranch('a'),
    unresolvedBranch('b'),
    unresolvedBranch('c'),
    unresolvedBranch('d'),
    unresolvedBranch('e'),
  ]);
  const projected = tree.projectTree(root, {
    mode: 'unresolved',
    maxItems: 16,
    targetItems: 6,
  });
  assert.equal(projected.totalMatches, 26);
  assert.equal(
    projected.nodes.filter((item) => item._match).length,
    6,
  );
  assert.equal(projected.root.children.length, 5);
  assert.ok(projected.nodes.length <= 16);
  assert.equal(projected.truncated, true);
  assert.deepEqual(
    projected.root.children.map((item) => item.key),
    ['a-1', 'b-1', 'c-1', 'd-1', 'e-1'],
    'broad filters sample round-robin across first-move branches',
  );
}

{
  assert.equal(tree.orientationFor(719), 'vertical');
  assert.equal(tree.orientationFor(720), 'horizontal');
  assert.deepEqual(tree.cardGeometry('horizontal').nodeSize, [78, 226]);
  assert.deepEqual(tree.cardGeometry('vertical').nodeSize, [130, 116]);
}

{
  const root = {data: {key: 'root'}, parent: null, children: []};
  const first = {data: {key: 'first'}, parent: root, children: []};
  const second = {data: {key: 'second'}, parent: root, children: []};
  root.children = [first, second];
  const ordered = [root, first, second];

  assert.equal(
    tree.keyboardTarget(root, 'ArrowRight', 'horizontal', null, ordered),
    first,
  );
  assert.equal(
    tree.keyboardTarget(first, 'ArrowLeft', 'horizontal', null, ordered),
    root,
  );
  assert.equal(
    tree.keyboardTarget(first, 'ArrowDown', 'horizontal', null, ordered),
    second,
  );
  assert.equal(
    tree.keyboardTarget(root, 'ArrowDown', 'vertical', null, ordered),
    first,
  );
  assert.equal(
    tree.keyboardTarget(first, 'ArrowUp', 'vertical', null, ordered),
    root,
  );
  assert.equal(
    tree.keyboardTarget(first, 'ArrowRight', 'vertical', null, ordered),
    null,
  );
  assert.equal(
    tree.keyboardTarget(first, 'ArrowDown', 'vertical', null, ordered),
    second,
  );
  assert.equal(
    tree.keyboardTarget(first, 'Escape', 'horizontal', null, ordered),
    null,
    'Escape is not a tree zoom or navigation command',
  );
  assert.equal(
    tree.navigationState(ordered, 'second', 'missing').rovingKey,
    'second',
  );
}

{
  assert.equal(tree.statusGlyph(node('w', {status: 'WHITE_WIN'})), 'W');
  assert.equal(tree.statusGlyph(node('b', {status: 'BLACK_WIN'})), 'B');
  assert.equal(tree.statusGlyph(node('d', {status: 'DRAW'})), '=');
  assert.equal(tree.statusGlyph(node('u')), '?');
  assert.equal(tree.evaluationLabel(node('p', {eval_cp: 17})), 'White +0.17');
  assert.equal(
    tree.evaluationLabel(node('n', {eval_cp: -22})),
    'White \u22120.22',
  );
  assert.equal(
    tree.evaluationLabel(node('mate', {status: 'WHITE_WIN', eval_cp: 9999})),
    'White win',
  );
  assert.equal(tree.numberedMove(node('Nf3', {depth: 1})), '1. Nf3');
  assert.equal(tree.numberedMove(node('d6', {depth: 2})), '1... d6');
  assert.equal(tree.ellipsize('Villager Defense', 10), 'Villager\u2026');
}

{
  const transform = tree.fitTransform(
    {minX: 0, maxX: 1000, minY: 0, maxY: 500},
    {width: 500, height: 500},
    25,
    [0.35, 2.2],
  );
  assert.equal(transform.k, 0.45);
  assert.equal(transform.x, 25);
  assert.equal(transform.y, 137.5);
}

{
  const urlA = '/atomicdb/api/map/v1?root=start';
  const urlB = '/atomicdb/api/map/v1?root=branch';
  const payloadA = {root: {key: 'start'}};
  const payloadB = {root: {key: 'branch'}};
  const etags = new Map([[urlA, '"a"'], [urlB, '"b"']]);
  assert.equal(
    tree.etagForCurrentPayload(urlA, urlA, payloadA, etags),
    '"a"',
  );
  assert.equal(
    tree.etagForCurrentPayload(urlA, urlB, payloadB, etags),
    null,
  );
  assert.equal(tree.canReuseNotModified(urlA, urlA, payloadA), true);
  assert.equal(tree.canReuseNotModified(urlA, urlB, payloadB), false);
}

console.log('Atomic move-tree frontend contracts: PASS');
