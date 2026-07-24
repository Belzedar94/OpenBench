(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AtomicConquestTree = api;
}(typeof window === 'undefined' ? null : window, function () {
  'use strict';

  const MAX_TREEITEMS = 300;
  const DEFAULT_FOCAL_ITEMS = 24;
  const DEFAULT_ROOT_BRANCHES = 5;
  const FILTERED_DESKTOP_ITEMS = 40;
  const FILTERED_NARROW_ITEMS = 24;
  const FILTERED_DESKTOP_TARGETS = 10;
  const FILTERED_NARROW_TARGETS = 6;
  const VALID_MODES = new Set(['all', 'unresolved', 'active']);

  function safeNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function statusOf(node) {
    const status = String(node && node.status || 'UNKNOWN').toUpperCase();
    return ['WHITE_WIN', 'BLACK_WIN', 'DRAW'].includes(status)
      ? status : 'UNKNOWN';
  }

  function exactWorkState(node) {
    const work = node && node.work || {};
    const stated = String(work.exact_state || '').toLowerCase();
    if (stated === 'active' || safeNumber(work.own_active) > 0) {
      return 'active';
    }
    if (stated === 'queued' || safeNumber(work.own_queued) > 0) {
      return 'queued';
    }
    return 'idle';
  }

  function statusGlyph(node) {
    return {
      WHITE_WIN: 'W',
      BLACK_WIN: 'B',
      DRAW: '=',
      UNKNOWN: '?',
    }[statusOf(node)];
  }

  function statusLabel(node) {
    return {
      WHITE_WIN: 'White win',
      BLACK_WIN: 'Black win',
      DRAW: 'Draw',
      UNKNOWN: 'Unresolved',
    }[statusOf(node)];
  }

  function evaluationLabel(node) {
    const status = statusOf(node);
    if (status !== 'UNKNOWN') return statusLabel(node);
    if (node && node.eval_cp !== null && node.eval_cp !== undefined
        && Number.isFinite(Number(node.eval_cp))) {
      const pawns = Number(node.eval_cp) / 100;
      const sign = pawns > 0 ? '+' : (pawns < 0 ? '\u2212' : '');
      return `White ${sign}${Math.abs(pawns).toFixed(2)}`;
    }
    return 'No evaluation';
  }

  function moveToken(node) {
    if (!node || !node.move) return 'Start';
    return String(node.move.san || node.move.uci || 'Move');
  }

  function numberedMove(node) {
    if (!node || !node.move) return 'Start';
    const depth = Math.max(1, Math.floor(safeNumber(node.depth) || 1));
    const moveNumber = Math.ceil(depth / 2);
    const separator = depth % 2 ? '.' : '...';
    return `${moveNumber}${separator} ${moveToken(node)}`;
  }

  function ellipsize(value, limit) {
    const text = String(value || '');
    const size = Math.max(2, Math.floor(safeNumber(limit) || 2));
    return text.length > size
      ? `${text.slice(0, size - 1).trimEnd()}\u2026`
      : text;
  }

  function openingName(node) {
    return node && node.opening && node.opening.name
      ? String(node.opening.name) : '';
  }

  function searchText(node) {
    return [
      moveToken(node),
      node && node.line_san,
      node && Array.isArray(node.line_uci) ? node.line_uci.join(' ') : '',
      openingName(node),
    ].filter(Boolean).join(' ').toLocaleLowerCase();
  }

  function modeMatches(node, mode) {
    const selected = VALID_MODES.has(mode) ? mode : 'all';
    if (selected === 'unresolved') return statusOf(node) === 'UNKNOWN';
    if (selected === 'active') return exactWorkState(node) === 'active';
    return true;
  }

  function searchMatches(node, query) {
    const needle = String(query || '').trim().toLocaleLowerCase();
    return !needle || searchText(node).includes(needle);
  }

  function firstMoveLine(node) {
    if (node && node.line_san) return node.line_san;
    return numberedMove(Object.assign({depth: 1}, node || {}));
  }

  function mergeFirstMoves(payload) {
    if (!payload || !payload.root) return null;
    const root = Object.assign({}, payload.root);
    const existing = Array.isArray(root.children) ? root.children.slice() : [];
    if (!payload.snapshot || root.key !== payload.snapshot.start_key) {
      root.children = existing;
      return root;
    }
    const seen = new Set(existing.map((child) => String(child.key)));
    (Array.isArray(payload.first_moves) ? payload.first_moves : [])
      .forEach((firstMove) => {
        const key = String(firstMove && firstMove.key || '');
        if (!key || seen.has(key)) return;
        seen.add(key);
        const copy = Object.assign({}, firstMove, {
          children: [],
          line_san: firstMoveLine(firstMove),
          line_uci: Array.isArray(firstMove.line_uci)
            ? firstMove.line_uci.slice()
            : (firstMove.move && firstMove.move.uci
              ? [firstMove.move.uci] : []),
          // A first move omitted by the bounded display tree must still be
          // directly focusable, even when its compact summary has no children.
          zoomable: true,
          _mergedFirstMove: true,
        });
        existing.push(copy);
      });
    root.children = existing;
    return root;
  }

  function sourceRecords(root) {
    const records = [];
    const byKey = new Map();
    function visit(node, parent, order) {
      if (!node || node.key === null || node.key === undefined) return;
      const key = String(node.key);
      if (byKey.has(key)) return;
      const record = {
        key,
        data: node,
        parent,
        order,
        depth: parent ? parent.depth + 1 : 0,
        children: [],
      };
      records.push(record);
      byKey.set(key, record);
      (Array.isArray(node.children) ? node.children : [])
        .forEach((child, index) => {
          visit(child, record, index);
          const childRecord = byKey.get(String(child && child.key));
          if (childRecord && childRecord.parent === record) {
            record.children.push(childRecord);
          }
        });
    }
    visit(root, null, 0);
    return {records, byKey};
  }

  function addPath(record, destination) {
    let cursor = record;
    while (cursor) {
      destination.add(cursor.key);
      cursor = cursor.parent;
    }
  }

  function projectTree(root, options) {
    if (!root) {
      return {
        root: null, nodes: [], totalMatches: 0, truncated: false,
      };
    }
    const settings = options || {};
    const mode = VALID_MODES.has(settings.mode) ? settings.mode : 'all';
    const query = String(settings.query || '').trim();
    const expanded = new Set(
      Array.from(settings.expandedKeys || []).map(String),
    );
    const collapsed = new Set(
      Array.from(settings.collapsedKeys || []).map(String),
    );
    const budget = Math.max(1, Math.min(
      MAX_TREEITEMS,
      Math.floor(safeNumber(settings.maxItems) || MAX_TREEITEMS),
    ));
    const source = sourceRecords(root);
    const rootRecord = source.records[0];
    const targets = new Set();
    const matchingRecords = [];
    const activePaths = new Set();
    const targetPaths = new Set();
    const selectedPaths = new Set();
    const focusKey = String(settings.focusKey || '');
    const focalBudget = Math.max(1, Math.min(
      25,
      Math.floor(
        safeNumber(settings.focalItems) || DEFAULT_FOCAL_ITEMS,
      ),
    ));
    const rootBranchBudget = Math.max(1, Math.min(
      7,
      Math.floor(
        safeNumber(settings.rootBranches) || DEFAULT_ROOT_BRANCHES,
      ),
    ));
    const siblingRadius = Math.max(0, Math.min(
      3,
      Math.floor(
        settings.siblingRadius === undefined
          ? 3 : safeNumber(settings.siblingRadius),
      ),
    ));
    const rootPreviewFanout = Math.max(1, Math.min(
      3,
      Math.floor(safeNumber(settings.previewReplies) || 3),
    ));

    source.records.forEach((record) => {
      const work = exactWorkState(record.data);
      if (mode === 'all' && !query && work === 'active') {
        addPath(record, activePaths);
      }
      const isFiltered = mode !== 'all' || Boolean(query);
      if (isFiltered
          && modeMatches(record.data, mode)
          && searchMatches(record.data, query)) {
        matchingRecords.push(record);
      }
    });
    const totalMatches = matchingRecords.length;
    const targetLimit = Math.max(1, Math.min(
      budget,
      Math.floor(safeNumber(settings.targetItems) || budget),
    ));
    if (totalMatches) {
      // A broad filter should look like a compact, representative tree rather
      // than a source-order wall. Sample round-robin across first-move
      // branches, and admit a target only when its complete lineage fits the
      // visible budget. A direct search normally asks for one target and thus
      // keeps its full deep path.
      let candidates = matchingRecords.slice();
      if (mode === 'unresolved' && !query
          && candidates.some((record) => record !== rootRecord)) {
        candidates = candidates.filter((record) => record !== rootRecord);
      }
      const groups = new Map();
      candidates.forEach((record) => {
        let branch = record;
        while (branch.parent && branch.parent !== rootRecord) {
          branch = branch.parent;
        }
        const branchKey = record === rootRecord
          ? rootRecord.key : branch.key;
        if (!groups.has(branchKey)) groups.set(branchKey, []);
        groups.get(branchKey).push(record);
      });
      const queues = Array.from(groups.values());
      let progressed = true;
      while (targets.size < targetLimit && progressed) {
        progressed = false;
        queues.forEach((queue) => {
          if (!queue.length || targets.size >= targetLimit) return;
          while (queue.length) {
            const candidate = queue.shift();
            const lineage = [];
            let cursor = candidate;
            while (cursor) {
              lineage.push(cursor);
              cursor = cursor.parent;
            }
            const additions = lineage.filter(
              (record) => !targetPaths.has(record.key),
            );
            if (targetPaths.size + additions.length > budget) continue;
            targets.add(candidate.key);
            lineage.forEach((record) => targetPaths.add(record.key));
            progressed = true;
            break;
          }
        });
      }
    }
    if (focusKey && source.byKey.has(focusKey)) {
      addPath(source.byKey.get(focusKey), selectedPaths);
    }

    const forced = new Set([rootRecord.key]);
    activePaths.forEach((key) => forced.add(key));
    targetPaths.forEach((key) => forced.add(key));
    selectedPaths.forEach((key) => forced.add(key));
    const focalKeys = new Set(forced);

    function addFocal(record) {
      if (!record || focalKeys.size >= focalBudget) return;
      focalKeys.add(record.key);
    }

    function addSiblingContext(pathKeys) {
      source.records.forEach((record) => {
        if (!pathKeys.has(record.key) || !record.parent) return;
        const siblings = record.parent.children;
        const index = siblings.indexOf(record);
        for (let distance = 1; distance <= siblingRadius; distance += 1) {
          addFocal(siblings[index - distance]);
          addFocal(siblings[index + distance]);
        }
      });
    }

    addSiblingContext(activePaths);
    addSiblingContext(selectedPaths);
    // A quiet start position needs a compact sampling, not a wall of every
    // legal move. Exact live/queued work and the selected route win, then the
    // stable source order fills a compact five-card first-move fan.
    function previewPriority(record) {
      if (selectedPaths.has(record.key)) return 0;
      const work = exactWorkState(record.data);
      if (work === 'active') return 1;
      if (work === 'queued') return 2;
      return 3;
    }

    function previewChildren(record) {
      return record.children.slice().sort((left, right) => (
        previewPriority(left) - previewPriority(right)
        || left.order - right.order
      ));
    }

    const rootPriority = rootRecord.children.slice().sort((left, right) => (
      previewPriority(left) - previewPriority(right)
      || left.order - right.order
    ));
    let visibleRootChildren = rootRecord.children.filter(
      (record) => focalKeys.has(record.key),
    ).length;
    rootPriority.forEach((record) => {
      if (visibleRootChildren >= rootBranchBudget
          || focalKeys.has(record.key)) return;
      addFocal(record);
      if (focalKeys.has(record.key)) visibleRootChildren += 1;
    });
    const focusRecord = source.byKey.get(focusKey);
    if (focusRecord && focusRecord !== rootRecord) {
      previewChildren(focusRecord).slice(0, 5).forEach(addFocal);
    }

    // Use the remainder of the focal budget to reveal actual continuations,
    // not more unrelated first moves. Three replies per visible first move
    // establish branching; one child per deeper node preserves the shape
    // without turning the initial view into another unreadable wall.
    let previewLayer = focusRecord ? rootPriority.filter((record) => (
      focalKeys.has(record.key) && record.children.length
    )) : [];
    const previewDepthLimit = focusRecord === rootRecord
      ? 4
      : safeNumber(focusRecord && focusRecord.depth) + 3;
    while (previewLayer.length && focalKeys.size < focalBudget) {
      const nextLayer = [];
      previewLayer.forEach((record) => {
        const perParent = record.parent === rootRecord
          ? rootPreviewFanout : 1;
        previewChildren(record)
          .filter((child) => child.depth <= previewDepthLimit)
          .slice(0, perParent)
          .forEach((child) => {
          const before = focalKeys.size;
          addFocal(child);
          if (focalKeys.size > before
              && child.depth < previewDepthLimit
              && child.children.length) {
            nextLayer.push(child);
          }
        });
      });
      previewLayer = nextLayer;
    }

    function hasCollapsedAncestor(record) {
      if (query) return false;
      let cursor = record.parent;
      while (cursor) {
        if (collapsed.has(cursor.key)) return true;
        cursor = cursor.parent;
      }
      return false;
    }

    function eligible(record) {
      if (record === rootRecord) return true;
      if (hasCollapsedAncestor(record)) return false;
      if (forced.has(record.key)) return true;
      if (mode === 'all' && !query && focalKeys.has(record.key)) {
        return true;
      }
      if (mode !== 'all' || query) return false;
      return Boolean(record.parent && expanded.has(record.parent.key));
    }

    let count = 0;
    let omitted = 0;
    const nodes = [];

    function clone(record) {
      if (!eligible(record)) return null;
      if (count >= budget) {
        omitted += 1;
        return null;
      }
      count += 1;
      const copy = Object.assign({}, record.data, {
        children: [],
        _sourceChildCount: record.children.length
          + Math.max(0, safeNumber(record.data.hidden_children)),
        _exactWork: exactWorkState(record.data),
        _activePath: activePaths.has(record.key),
        _match: targets.has(record.key),
        _selectedPath: selectedPaths.has(record.key),
        _sourceDepth: record.depth,
      });
      nodes.push(copy);
      const candidates = record.children
        .filter(eligible)
        .sort((left, right) => left.order - right.order);
      candidates.forEach((child) => {
        const childCopy = clone(child);
        if (childCopy) copy.children.push(childCopy);
      });
      copy._hiddenChildren = Math.max(
        0,
        copy._sourceChildCount - copy.children.length,
      );
      copy._partialChildren = copy.children.length > 0
        && copy._hiddenChildren > 0
        && !expanded.has(record.key);
      return copy;
    }

    const projectedRoot = clone(rootRecord);
    return {
      root: projectedRoot,
      nodes,
      totalMatches,
      truncated: omitted > 0
        || nodes.length < forced.size
        || totalMatches > targets.size,
      omitted,
      sourceCount: source.records.length,
      mode,
      query,
    };
  }

  function orientationFor(width) {
    return safeNumber(width) < 720 ? 'vertical' : 'horizontal';
  }

  function cardGeometry(orientation) {
    if (orientation === 'vertical') {
      return {
        cardWidth: 164,
        cardHeight: 66,
        nodeSize: [130, 116],
        direction: 'vertical',
      };
    }
    return {
      cardWidth: 180,
      cardHeight: 68,
      nodeSize: [78, 226],
      direction: 'horizontal',
    };
  }

  function keyboardTarget(
    entry, key, orientation, accepts, orderedEntries,
  ) {
    if (!entry) return null;
    const allowed = typeof accepts === 'function' ? accepts : () => true;
    const ordered = (Array.isArray(orderedEntries) ? orderedEntries : [])
      .filter(allowed);
    if (key === 'Home') return ordered[0] || null;
    if (key === 'End') return ordered[ordered.length - 1] || null;

    if (key === 'ArrowLeft') {
      let parent = entry.parent || null;
      while (parent && !allowed(parent)) parent = parent.parent || null;
      return parent;
    }
    if (key === 'ArrowRight') {
      const pending = Array.from(entry.children || []);
      while (pending.length) {
        const descendant = pending.shift();
        if (allowed(descendant)) return descendant;
        pending.push(...(descendant.children || []));
      }
      return null;
    }
    if (key === 'ArrowUp' || key === 'ArrowDown') {
      const index = ordered.indexOf(entry);
      if (index < 0) return ordered[0] || null;
      return ordered[index + (key === 'ArrowUp' ? -1 : 1)] || null;
    }
    return null;
  }

  function navigationState(entries, selectedKey, rovingKey) {
    const source = Array.isArray(entries) ? entries : [];
    const keys = new Set(source.map((entry) => String(entry.data.key)));
    const selected = String(selectedKey || '');
    const roving = keys.has(String(rovingKey || ''))
      ? String(rovingKey)
      : (keys.has(selected) ? selected
        : (source[0] ? String(source[0].data.key) : ''));
    return {selectedKey: selected, rovingKey: roving};
  }

  function fitTransform(bounds, viewport, padding, scaleExtent) {
    const box = bounds || {};
    const frame = viewport || {};
    const inset = Math.max(0, safeNumber(padding) || 24);
    const width = Math.max(1, safeNumber(box.maxX) - safeNumber(box.minX));
    const height = Math.max(1, safeNumber(box.maxY) - safeNumber(box.minY));
    const availableWidth = Math.max(1, safeNumber(frame.width) - inset * 2);
    const availableHeight = Math.max(1, safeNumber(frame.height) - inset * 2);
    const extent = Array.isArray(scaleExtent) ? scaleExtent : [0.35, 2.2];
    const scale = Math.max(
      safeNumber(extent[0]) || 0.35,
      Math.min(
        safeNumber(extent[1]) || 2.2,
        availableWidth / width,
        availableHeight / height,
      ),
    );
    return {
      x: (safeNumber(frame.width) - (
        safeNumber(box.minX) + safeNumber(box.maxX)
      ) * scale) / 2,
      y: (safeNumber(frame.height) - (
        safeNumber(box.minY) + safeNumber(box.maxY)
      ) * scale) / 2,
      k: scale,
    };
  }

  function etagForCurrentPayload(
    requestUrl, currentPayloadUrl, payload, etags,
  ) {
    if (!payload || requestUrl !== currentPayloadUrl || !etags) return null;
    return etags.get(requestUrl) || null;
  }

  function canReuseNotModified(requestUrl, currentPayloadUrl, payload) {
    return Boolean(payload && requestUrl === currentPayloadUrl);
  }

  return {
    MAX_TREEITEMS,
    DEFAULT_FOCAL_ITEMS,
    DEFAULT_ROOT_BRANCHES,
    FILTERED_DESKTOP_ITEMS,
    FILTERED_NARROW_ITEMS,
    FILTERED_DESKTOP_TARGETS,
    FILTERED_NARROW_TARGETS,
    safeNumber,
    statusOf,
    exactWorkState,
    statusGlyph,
    statusLabel,
    evaluationLabel,
    moveToken,
    numberedMove,
    ellipsize,
    openingName,
    searchText,
    modeMatches,
    searchMatches,
    mergeFirstMoves,
    sourceRecords,
    projectTree,
    orientationFor,
    cardGeometry,
    keyboardTarget,
    navigationState,
    fitTransform,
    etagForCurrentPayload,
    canReuseNotModified,
  };
}));

(function () {
  'use strict';

  if (typeof document === 'undefined') return;
  const host = document.getElementById('conquest-map');
  if (!host) return;

  const d3 = window.d3;
  const tree = window.AtomicConquestTree;
  const byId = (id) => document.getElementById(id);
  const elements = {
    svg: byId('map-svg'),
    svgWrap: byId('map-svg-wrap'),
    scene: byId('map-scene'),
    links: byId('map-links'),
    nodes: byId('map-nodes'),
    stage: byId('map-stage'),
    status: byId('map-status'),
    stamp: byId('map-snapshot-stamp'),
    search: byId('map-search'),
    mode: Array.from(document.querySelectorAll(
      'input[name="tree-mode"], input[name="map-mode"], [data-map-mode]',
    )),
    refresh: byId('map-refresh'),
    zoomIn: byId('map-zoom-in'),
    zoomOut: byId('map-zoom-out'),
    fit: byId('map-fit'),
    clear: byId('map-clear-view'),
    empty: byId('map-empty-state'),
    breadcrumbs: byId('map-breadcrumbs'),
    branchTitle: byId('tree-branch-title'),
    resultKey: byId('tree-result-key'),
    workList: byId('map-active-work') || byId('map-work-items'),
    workEmpty: byId('map-active-work-empty'),
    inspector: byId('map-inspector'),
    inspectorClose: byId('map-inspector-close'),
    inspectorTitle: byId('inspector-title'),
    inspectorOpening: byId('inspector-opening'),
    inspectorOpeningName: byId('inspector-opening-name'),
    inspectorLine: byId('inspector-line'),
    inspectorResult: byId('inspector-result'),
    inspectorResultSymbol: byId('inspector-result-symbol'),
    inspectorStatus: byId('inspector-status'),
    inspectorEval: byId('inspector-eval'),
    inspectorPositions: byId('inspector-positions'),
    inspectorNodes: byId('inspector-nodes'),
    inspectorTime: byId('inspector-time'),
    inspectorVisits: byId('inspector-visits'),
    inspectorTranspositions: byId('inspector-transpositions'),
    inspectorWork: byId('inspector-work'),
    focusBranch: byId('map-focus-branch'),
    explorer: byId('map-open-explorer'),
    board: byId('map-board'),
    helpOpen: byId('map-help-open') || byId('map-help-toggle'),
    helpClose: byId('map-help-close'),
    helpDialog: byId('map-help-dialog') || byId('map-help-panel'),
  };

  if (!elements.svg || !elements.scene || !elements.links
      || !elements.nodes) return;

  const params = new URLSearchParams(window.location.search);
  const reducedMotion = window.matchMedia(
    '(prefers-reduced-motion: reduce)',
  ).matches;
  const state = {
    apiRoot: params.get('root') || host.dataset.rootKey,
    payload: null,
    payloadUrl: '',
    sourceRoot: null,
    projection: null,
    hierarchy: null,
    index: new Map(),
    selectedKey: params.get('selected') || '',
    rovingKey: '',
    mode: ['all', 'unresolved', 'active'].includes(params.get('mode'))
      ? params.get('mode') : 'all',
    query: params.get('q') || '',
    expanded: new Set(),
    collapsed: new Set(),
    orientation: '',
    transform: null,
    needsFit: true,
    zoom: null,
    loading: false,
    controller: null,
    etags: new Map(),
    resizeTimer: null,
    searchTimer: null,
    lastLoadedAt: 0,
    board: null,
    boardPromise: null,
    lastViewportWidth: 0,
  };

  function human(value) {
    const number = tree.safeNumber(value);
    const absolute = Math.abs(number);
    if (absolute >= 1e12) return `${(number / 1e12).toFixed(2)}T`;
    if (absolute >= 1e9) return `${(number / 1e9).toFixed(2)}B`;
    if (absolute >= 1e6) return `${(number / 1e6).toFixed(1)}M`;
    if (absolute >= 1e3) return `${(number / 1e3).toFixed(1)}k`;
    return Math.round(number).toLocaleString();
  }

  function duration(seconds) {
    const value = tree.safeNumber(seconds);
    if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)}s`;
    if (value < 3600) return `${(value / 60).toFixed(1)}m`;
    if (value < 86400) return `${(value / 3600).toFixed(1)}h`;
    return `${(value / 86400).toFixed(1)}d`;
  }

  function setText(element, value) {
    if (element) element.textContent = value;
  }

  function statusMessage(message, kind) {
    if (!elements.status) return;
    elements.status.hidden = false;
    elements.status.dataset.state = kind || 'loading';
    elements.status.className = `tree-status is-${
      kind || 'loading'
    }`;
    const text = elements.status.querySelector('span:last-child');
    setText(text || elements.status, message);
  }

  function hideStatus() {
    if (elements.status) elements.status.hidden = true;
  }

  function updateUrl() {
    const url = new URL(window.location.href);
    if (state.apiRoot && state.apiRoot !== host.dataset.rootKey) {
      url.searchParams.set('root', state.apiRoot);
    } else {
      url.searchParams.delete('root');
    }
    if (state.selectedKey && state.selectedKey !== state.apiRoot) {
      url.searchParams.set('selected', state.selectedKey);
    } else {
      url.searchParams.delete('selected');
    }
    if (state.mode !== 'all') url.searchParams.set('mode', state.mode);
    else url.searchParams.delete('mode');
    if (state.query) url.searchParams.set('q', state.query);
    else url.searchParams.delete('q');
    window.history.replaceState({}, '', url);
  }

  function workLabel(node) {
    const work = tree.exactWorkState(node);
    if (work === 'active') return 'Analyzing this exact position now';
    if (work === 'queued') return 'This exact position is queued';
    const details = node && node.work || {};
    const descendantActive = tree.safeNumber(details.descendant_active);
    const descendantQueued = tree.safeNumber(details.descendant_queued);
    if (descendantActive > 0) {
      return `${human(descendantActive)} active ${
        descendantActive === 1 ? 'continuation' : 'continuations'
      } below this position`;
    }
    if (descendantQueued > 0) {
      return `${human(descendantQueued)} queued ${
        descendantQueued === 1 ? 'continuation' : 'continuations'
      } below this position`;
    }
    return 'No active work on this position';
  }

  function findSourceNode(key) {
    if (!state.sourceRoot) return null;
    return tree.sourceRecords(state.sourceRoot).byKey.get(String(key))?.data
      || null;
  }

  function renderFallbackBoard(fen) {
    if (!elements.board || state.board) return;
    const position = String(fen || host.dataset.rootFen || 'start')
      .split(/\s+/)[0];
    const start = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR';
    const placement = position === 'start' ? start : position;
    const glyphs = {
      K: '\u2654', Q: '\u2655', R: '\u2656',
      B: '\u2657', N: '\u2658', P: '\u2659',
      k: '\u265A', q: '\u265B', r: '\u265C',
      b: '\u265D', n: '\u265E', p: '\u265F',
    };
    elements.board.classList.add('map-board-fallback');
    elements.board.replaceChildren();
    const squares = [];
    placement.split('/').forEach((rank) => {
      Array.from(rank).forEach((token) => {
        const empty = Number(token);
        if (Number.isInteger(empty)) {
          for (let index = 0; index < empty; index += 1) squares.push('');
        } else {
          squares.push(glyphs[token] || '');
        }
      });
    });
    squares.slice(0, 64).forEach((piece, index) => {
      const square = document.createElement('span');
      square.className = `map-board-square ${
        (Math.floor(index / 8) + index) % 2 ? 'dark' : 'light'
      }`;
      square.textContent = piece;
      elements.board.appendChild(square);
    });
  }

  function boardModuleUrl() {
    return host.dataset.chessgroundModule
      || (elements.board && elements.board.dataset.chessgroundModule)
      || '';
  }

  async function ensureBoard(fen) {
    if (!elements.board) return null;
    if (state.board) {
      state.board.set({fen: fen || host.dataset.rootFen || 'start'});
      return state.board;
    }
    if (state.boardPromise) {
      const board = await state.boardPromise;
      if (board) board.set({fen: fen || host.dataset.rootFen || 'start'});
      return board;
    }
    const url = boardModuleUrl();
    if (!url) {
      renderFallbackBoard(fen);
      return null;
    }
    state.boardPromise = import(url).then((module) => {
      const create = module.Chessground
        || (module.default && module.default.Chessground)
        || module.default;
      if (typeof create !== 'function') {
        throw new Error('Chessground export is unavailable');
      }
      elements.board.classList.remove('map-board-fallback');
      elements.board.replaceChildren();
      state.board = create(elements.board, {
        fen: fen || host.dataset.rootFen || 'start',
        orientation: 'white',
        coordinates: true,
        viewOnly: true,
        animation: {enabled: !reducedMotion, duration: 180},
        movable: {color: undefined, free: false, showDests: false},
        draggable: {enabled: false},
        selectable: {enabled: false},
        drawable: {enabled: false, visible: false},
        highlight: {lastMove: false, check: false},
      });
      return state.board;
    }).catch(() => {
      state.boardPromise = null;
      renderFallbackBoard(fen);
      return null;
    });
    return state.boardPromise;
  }

  function updateInspector(node) {
    if (!node) return;
    setText(elements.inspectorTitle, tree.numberedMove(node));
    setText(
      elements.inspectorLine,
      node.line_san || (node.move ? tree.numberedMove(node) : 'Start position'),
    );
    const opening = tree.openingName(node);
    if (elements.inspectorOpening) {
      elements.inspectorOpening.hidden = !opening;
    }
    setText(elements.inspectorOpeningName, opening);
    if (elements.inspectorResult) {
      elements.inspectorResult.dataset.status = tree.statusOf(node);
    }
    setText(elements.inspectorResultSymbol, tree.statusGlyph(node));
    setText(elements.inspectorStatus, tree.statusLabel(node));
    const evaluation = tree.evaluationLabel(node);
    setText(
      elements.inspectorEval,
      tree.statusOf(node) === 'UNKNOWN' && node.eval_cp !== null
        && node.eval_cp !== undefined
        ? `${evaluation} \u00b7 estimate, not proof`
        : evaluation,
    );
    const metrics = node.metrics || {};
    setText(elements.inspectorPositions, human(metrics.positions));
    setText(elements.inspectorNodes, human(metrics.nodes));
    setText(elements.inspectorTime, duration(metrics.seconds));
    setText(elements.inspectorVisits, human(node.visits));
    setText(
      elements.inspectorTranspositions,
      human(node.transpositions && node.transpositions.incoming),
    );
    if (elements.inspectorWork) {
      const exactState = tree.exactWorkState(node);
      const work = node.work || {};
      const descendant = exactState === 'idle' && (
        tree.safeNumber(work.descendant_active) > 0
        || tree.safeNumber(work.descendant_queued) > 0
      );
      elements.inspectorWork.dataset.state = exactState;
      elements.inspectorWork.dataset.scope = descendant
        ? 'descendant' : 'exact';
      elements.inspectorWork.classList.toggle(
        'is-active',
        exactState === 'active',
      );
      elements.inspectorWork.classList.toggle(
        'has-descendant-work',
        descendant,
      );
      const label = elements.inspectorWork.querySelector('span:last-child');
      setText(label || elements.inspectorWork, workLabel(node));
    }
    if (elements.explorer) {
      elements.explorer.href = `/atomicdb/explore/${encodeURIComponent(
        node.key,
      )}/`;
      if (Array.isArray(node.line_uci) && node.line_uci.length) {
        elements.explorer.href += `?play=${encodeURIComponent(
          node.line_uci.join(','),
        )}`;
      }
    }
    ensureBoard(node.fen || host.dataset.rootFen);
  }

  function renderBreadcrumbs() {
    if (!elements.breadcrumbs || !state.payload) return;
    elements.breadcrumbs.replaceChildren();
    const lineage = state.payload.lineage
      && Array.isArray(state.payload.lineage.positions)
      ? state.payload.lineage.positions : [];
    const positions = lineage.length ? lineage : [{
      key: state.sourceRoot.key, move: null, depth: state.sourceRoot.depth,
    }];
    positions.forEach((position, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.key = position.key;
      button.textContent = position.move
        ? tree.numberedMove(position) : 'Start';
      if (index === positions.length - 1) {
        button.className = 'current';
        button.setAttribute('aria-current', 'page');
      }
      button.addEventListener('click', () => {
        if (position.key === state.apiRoot) {
          selectKey(position.key, true, true);
        } else {
          loadMap(position.key, 'breadcrumb');
        }
      });
      elements.breadcrumbs.appendChild(button);
    });
  }

  function renderWorkRail() {
    if (!elements.workList || !state.payload) return;
    const oldItems = Array.from(
      elements.workList.querySelectorAll('[data-work-key]'),
    );
    oldItems.forEach((item) => item.remove());
    const items = (Array.isArray(state.payload.work_items)
      ? state.payload.work_items : [])
      .filter((item) => tree.exactWorkState(item) !== 'idle');
    if (elements.workEmpty) elements.workEmpty.hidden = items.length > 0;
    items.forEach((item) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'work-rail-item';
      button.dataset.workKey = item.key;
      button.dataset.state = tree.exactWorkState(item);
      const stateMark = document.createElement('span');
      stateMark.className = 'work-rail-state';
      stateMark.setAttribute('aria-hidden', 'true');
      const copy = document.createElement('span');
      copy.className = 'work-rail-copy';
      const line = document.createElement('strong');
      line.textContent = item.line_san || tree.numberedMove(item);
      const meta = document.createElement('span');
      meta.textContent = [
        tree.exactWorkState(item) === 'active' ? 'Analyzing now' : 'Queued',
        tree.openingName(item),
      ].filter(Boolean).join(' \u00b7 ');
      copy.append(line, meta);
      button.append(stateMark, copy);
      button.addEventListener('click', () => {
        if (state.index.has(String(item.key))) {
          selectKey(item.key, true, true);
        } else {
          loadMap(item.key, 'work');
        }
      });
      elements.workList.appendChild(button);
    });
  }

  function setModeControls() {
    elements.mode.forEach((control) => {
      const value = control.value || control.dataset.mapMode;
      if ('checked' in control) control.checked = value === state.mode;
      control.removeAttribute('aria-pressed');
    });
    if (elements.search && elements.search.value !== state.query) {
      elements.search.value = state.query;
    }
  }

  function sourceHasChildren(node) {
    return tree.safeNumber(node._sourceChildCount) > 0
      || Boolean(node.zoomable || node.truncated);
  }

  function toggleNode(node) {
    const key = String(node.key);
    if (tree.safeNumber(node._hiddenChildren) > 0
        && !state.expanded.has(key)) {
      state.collapsed.delete(key);
      state.expanded.add(key);
    } else if (state.expanded.has(key)
        || (state.index.get(key) && state.index.get(key).children)) {
      state.expanded.delete(key);
      state.collapsed.add(key);
    } else if (tree.safeNumber(node._sourceChildCount) > 0) {
      state.collapsed.delete(key);
      state.expanded.add(key);
    } else if (node.zoomable || node.truncated) {
      loadMap(key, 'focus');
      return;
    }
    renderTree(false);
  }

  function selectKey(key, focus, reproject) {
    const normalized = String(key);
    const entry = state.index.get(normalized);
    const sourceNode = entry ? entry.data : findSourceNode(normalized);
    if (!sourceNode) return;
    const changed = state.selectedKey !== normalized;
    state.selectedKey = normalized;
    if (entry) state.rovingKey = normalized;
    if (changed && reproject) renderTree(false);
    updateInspector(sourceNode);
    updateUrl();
    elements.nodes.querySelectorAll('[role="treeitem"]').forEach((element) => {
      const selected = element.dataset.key === normalized;
      element.classList.toggle('is-selected', selected);
      element.setAttribute('aria-selected', String(selected));
      element.setAttribute(
        'tabindex',
        element.dataset.key === state.rovingKey ? '0' : '-1',
      );
    });
    if (focus) {
      const target = elements.nodes.querySelector(
        `[role="treeitem"][data-key="${CSS.escape(normalized)}"]`,
      );
      if (target) target.focus({preventScroll: true});
    }
  }

  function nodePosition(entry, orientation) {
    return orientation === 'vertical'
      ? [entry.x, entry.y] : [entry.y, entry.x];
  }

  function treeBounds(entries, geometry, orientation) {
    const points = entries.map((entry) => nodePosition(entry, orientation));
    return {
      minX: Math.min(...points.map((point) => point[0]))
        - geometry.cardWidth / 2,
      maxX: Math.max(...points.map((point) => point[0]))
        + geometry.cardWidth / 2 + 32,
      minY: Math.min(...points.map((point) => point[1]))
        - geometry.cardHeight / 2,
      maxY: Math.max(...points.map((point) => point[1]))
        + geometry.cardHeight / 2,
    };
  }

  function applyTransform(transform, animate) {
    if (!state.zoom || !transform) return;
    const d3Transform = d3.zoomIdentity
      .translate(transform.x, transform.y)
      .scale(transform.k);
    const selection = d3.select(elements.svg);
    if (animate && !reducedMotion) {
      selection.transition().duration(240)
        .call(state.zoom.transform, d3Transform);
    } else {
      selection.call(state.zoom.transform, d3Transform);
    }
  }

  function fitTree(animate) {
    if (!state.hierarchy) return;
    const entries = state.hierarchy.descendants();
    if (!entries.length) return;
    const geometry = tree.cardGeometry(state.orientation);
    const rect = elements.svgWrap.getBoundingClientRect();
    const frame = {
      width: Math.max(1, rect.width || 800),
      height: Math.max(1, rect.height || 560),
    };
    const transform = tree.fitTransform(
      treeBounds(entries, geometry, state.orientation),
      frame,
      32,
      [0.35, 2.2],
    );
    state.transform = transform;
    applyTransform(transform, animate);
  }

  function focusTree(animate) {
    if (!state.hierarchy) return;
    const entries = state.hierarchy.descendants();
    if (!entries.length) return;
    if (state.mode === 'unresolved' && !state.query) {
      fitTree(animate);
      return;
    }
    const rootEntry = entries[0];
    const selected = state.index.get(String(state.selectedKey));
    const active = entries
      .filter((entry) => entry.data._exactWork === 'active')
      .sort((left, right) => right.depth - left.depth)[0];
    const match = entries.find((entry) => entry.data._match);
    // A deliberate search/filter must surface its result, while the ordinary
    // overview continues to respect the user's selection (including root).
    // This keeps a deep match on-screen instead of centring an unrelated root.
    let target = selected || active || match || rootEntry;
    if (state.query || state.mode === 'unresolved') {
      target = match || selected || active || rootEntry;
    } else if (state.mode === 'active') {
      target = active || match || selected || rootEntry;
    }
    const rect = elements.svgWrap.getBoundingClientRect();
    const width = Math.max(1, rect.width || 800);
    const height = Math.max(1, rect.height || 560);
    const point = nodePosition(target, state.orientation);
    const geometry = tree.cardGeometry(state.orientation);
    const bounds = treeBounds(entries, geometry, state.orientation);
    const spanX = Math.max(1, bounds.maxX - bounds.minX);
    const spanY = Math.max(1, bounds.maxY - bounds.minY);
    const minimumRootScale = state.orientation === 'vertical' ? 0.55 : 0.64;
    const rootScale = Math.max(minimumRootScale, Math.min(
      1,
      (width - 44) / spanX,
      (height - 44) / spanY,
    ));
    const scale = target === rootEntry
      ? rootScale
      : (state.orientation === 'vertical' ? 0.95 : 1);
    const boundsCenter = [
      (bounds.minX + bounds.maxX) / 2,
      (bounds.minY + bounds.maxY) / 2,
    ];
    const anchor = state.orientation === 'vertical'
      ? [width / 2, target === rootEntry ? height * 0.14 : height * 0.38]
      : [width * (target === rootEntry ? 0.14 : 0.42), height / 2];
    const transform = {
      x: target === rootEntry && state.orientation === 'horizontal'
        ? 22 - bounds.minX * scale
        : anchor[0] - point[0] * scale,
      y: target === rootEntry && state.orientation === 'horizontal'
        ? height / 2 - boundsCenter[1] * scale
        : (target === rootEntry && state.orientation === 'vertical'
          ? Math.max(32, (height - spanY * scale) / 2)
            - bounds.minY * scale
          : anchor[1] - point[1] * scale),
      k: scale,
    };
    state.transform = transform;
    applyTransform(transform, animate);
  }

  function zoomBy(factor) {
    if (!state.zoom) return;
    d3.select(elements.svg)
      .transition().duration(reducedMotion ? 0 : 180)
      .call(state.zoom.scaleBy, factor);
  }

  function focusEntry(entry) {
    if (!entry) return;
    state.rovingKey = String(entry.data.key);
    const target = elements.nodes.querySelector(
      `[role="treeitem"][data-key="${CSS.escape(state.rovingKey)}"]`,
    );
    if (target) {
      elements.nodes.querySelectorAll('[role="treeitem"]').forEach((item) => {
        item.setAttribute('tabindex', item === target ? '0' : '-1');
      });
      target.focus({preventScroll: true});
    }
  }

  function handleNodeKeydown(event, entry, ordered) {
    if (event.key === 'Enter') {
      event.preventDefault();
      selectKey(entry.data.key, true, true);
      if (sourceHasChildren(entry.data)) toggleNode(entry.data);
      return;
    }
    if (event.key === ' ') {
      event.preventDefault();
      selectKey(entry.data.key, true, true);
      return;
    }
    if (event.key === 'ArrowRight' && sourceHasChildren(entry.data)
        && !(entry.children && entry.children.length)) {
      event.preventDefault();
      toggleNode(entry.data);
      return;
    }
    if (event.key === 'ArrowLeft' && entry.children
        && entry.children.length) {
      event.preventDefault();
      toggleNode(entry.data);
      return;
    }
    const next = tree.keyboardTarget(
      entry,
      event.key,
      state.orientation,
      () => true,
      ordered,
    );
    if (next) {
      event.preventDefault();
      focusEntry(next);
    }
  }

  function renderTree(requestFit) {
    if (!state.sourceRoot || !d3) return;
    const width = Math.max(
      1,
      elements.svgWrap.getBoundingClientRect().width || 800,
    );
    if (state.lastViewportWidth
        && Math.abs(width - state.lastViewportWidth) > 80) {
      state.needsFit = true;
    }
    state.lastViewportWidth = width;
    const nextOrientation = tree.orientationFor(width);
    if (state.orientation && state.orientation !== nextOrientation) {
      state.needsFit = true;
    }
    state.orientation = nextOrientation;
    host.dataset.orientation = state.orientation;
    const geometry = tree.cardGeometry(state.orientation);
    state.projection = tree.projectTree(state.sourceRoot, {
      mode: state.mode,
      query: state.query,
      focusKey: state.selectedKey,
      focalItems: state.orientation === 'vertical'
        ? 14 : tree.DEFAULT_FOCAL_ITEMS,
      rootBranches: state.orientation === 'vertical'
        ? 2 : tree.DEFAULT_ROOT_BRANCHES,
      siblingRadius: state.orientation === 'vertical' ? 1 : 3,
      previewReplies: state.orientation === 'vertical' ? 1 : 3,
      expandedKeys: state.expanded,
      collapsedKeys: state.collapsed,
      targetItems: state.query
        ? 1
        : (state.orientation === 'vertical'
          ? tree.FILTERED_NARROW_TARGETS
          : tree.FILTERED_DESKTOP_TARGETS),
      maxItems: state.mode !== 'all' || state.query
        ? (state.query
          ? tree.MAX_TREEITEMS
          : (state.orientation === 'vertical'
          ? tree.FILTERED_NARROW_ITEMS
          : tree.FILTERED_DESKTOP_ITEMS))
        : tree.MAX_TREEITEMS,
    });
    if (!state.projection.root) return;
    state.hierarchy = d3.hierarchy(state.projection.root);
    d3.tree().nodeSize(geometry.nodeSize)(state.hierarchy);
    const entries = state.hierarchy.descendants();
    const links = state.hierarchy.links();
    if (elements.resultKey) {
      const shownMatches = entries.filter((entry) => (
        entry.data._match
      )).length;
      setText(
        elements.resultKey,
        state.mode !== 'all' || state.query
          ? `Showing ${shownMatches} of ${state.projection.totalMatches} matching positions.`
          : 'Select a node to inspect its position.',
      );
    }
    state.index = new Map(
      entries.map((entry) => [String(entry.data.key), entry]),
    );
    const navigation = tree.navigationState(
      entries,
      state.selectedKey,
      state.rovingKey,
    );
    state.rovingKey = navigation.rovingKey;
    if (!state.selectedKey || !findSourceNode(state.selectedKey)) {
      state.selectedKey = String(state.hierarchy.data.key);
    }

    const lineGenerator = state.orientation === 'vertical'
      ? d3.linkVertical().x((entry) => entry.x).y((entry) => entry.y)
      : d3.linkHorizontal().x((entry) => entry.y).y((entry) => entry.x);
    d3.select(elements.links)
      .selectAll('path.tree-link')
      .data(links, (link) => String(link.target.data.key))
      .join(
        (enter) => enter.append('path')
          .attr('class', 'tree-link'),
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr('d', lineGenerator)
      .classed('is-active-path', (link) => Boolean(
        link.target.data._activePath,
      ))
      .classed('is-selected-path', (link) => Boolean(
        link.target.data._selectedPath,
      ));

    const nodeSelection = d3.select(elements.nodes)
      .selectAll('g.tree-node')
      .data(entries, (entry) => String(entry.data.key))
      .join(
        (enter) => {
          const node = enter.append('g').attr('class', 'tree-node');
          node.append('rect')
            .attr('class', 'tree-node-card');
          node.append('circle').attr('class', 'node-work-ring');
          node.append('text').attr('class', 'tree-node-result');
          node.append('text').attr('class', 'tree-node-move');
          node.append('text').attr('class', 'tree-node-eval');
          node.append('text').attr('class', 'tree-node-opening');
          const toggle = node.append('g').attr('class', 'tree-node-toggle');
          toggle.append('circle')
            .attr('class', 'tree-node-toggle-hitbox')
            .attr('r', 22);
          toggle.append('circle')
            .attr('class', 'tree-node-toggle-surface');
          toggle.append('path')
            .attr('class', 'tree-node-toggle-icon');
          toggle.append('text')
            .attr('class', 'tree-node-more-count')
            .attr('y', 21)
            .attr('text-anchor', 'middle')
            .attr('font-size', 9)
            .attr('font-weight', 700)
            .attr('fill', 'currentColor')
            .attr('pointer-events', 'none');
          node.append('title');
          return node;
        },
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr('transform', (entry) => {
        const position = nodePosition(entry, state.orientation);
        return `translate(${position[0]},${position[1]})`;
      })
      .attr('role', 'treeitem')
      .attr('data-key', (entry) => String(entry.data.key))
      .attr('tabindex', (entry) => (
        String(entry.data.key) === state.rovingKey ? 0 : -1
      ))
      .attr('aria-level', (entry) => entry.depth + 1)
      .attr('aria-posinset', (entry) => (
        entry.parent ? entry.parent.children.indexOf(entry) + 1 : 1
      ))
      .attr('aria-setsize', (entry) => (
        entry.parent ? entry.parent.children.length : 1
      ))
      .attr('aria-selected', (entry) => String(
        String(entry.data.key) === state.selectedKey,
      ))
      .attr('aria-label', (entry) => [
        tree.numberedMove(entry.data),
        tree.statusLabel(entry.data),
        tree.evaluationLabel(entry.data),
        tree.openingName(entry.data)
          ? `Opening ${tree.openingName(entry.data)}` : '',
        workLabel(entry.data),
      ].filter(Boolean).join('. '))
      .attr('aria-expanded', (entry) => (
        sourceHasChildren(entry.data)
          ? String(Boolean(entry.children && entry.children.length))
          : null
      ))
      .classed('is-selected', (entry) => (
        String(entry.data.key) === state.selectedKey
      ))
      .classed('is-active', (entry) => (
        entry.data._exactWork === 'active'
      ))
      .classed('is-queued', (entry) => (
        entry.data._exactWork === 'queued'
      ))
      .classed('is-active-path', (entry) => Boolean(
        entry.data._activePath,
      ))
      .classed('is-selected-path', (entry) => Boolean(
        entry.data._selectedPath,
      ))
      .classed('is-search-match', (entry) => Boolean(entry.data._match))
      .classed('is-root', (entry) => entry.depth === 0)
      .classed('has-more-branches', (entry) => Boolean(
        entry.data._partialChildren,
      ))
      .classed('status-white-win', (entry) => (
        tree.statusOf(entry.data) === 'WHITE_WIN'
      ))
      .classed('status-black-win', (entry) => (
        tree.statusOf(entry.data) === 'BLACK_WIN'
      ))
      .classed('status-draw', (entry) => (
        tree.statusOf(entry.data) === 'DRAW'
      ))
      .classed('status-unknown', (entry) => (
        tree.statusOf(entry.data) === 'UNKNOWN'
      ))
      .on('click', (event, entry) => {
        event.stopPropagation();
        selectKey(entry.data.key, false, true);
      })
      .on('keydown', (event, entry) => {
        handleNodeKeydown(event, entry, entries);
      });

    nodeSelection.select('rect.tree-node-card')
      .attr('x', -geometry.cardWidth / 2)
      .attr('y', -geometry.cardHeight / 2)
      .attr('width', geometry.cardWidth)
      .attr('height', geometry.cardHeight)
      .attr('rx', 12);
    nodeSelection.select('circle.node-work-ring')
      .attr('cx', -geometry.cardWidth / 2 + 17)
      .attr('cy', -geometry.cardHeight / 2 + 17)
      .attr('r', 14);
    nodeSelection.select('text.tree-node-result')
      .attr('x', -geometry.cardWidth / 2 + 14)
      .attr('y', -geometry.cardHeight / 2 + 22)
      .text((entry) => tree.statusGlyph(entry.data));
    nodeSelection.select('text.tree-node-move')
      .attr('x', -geometry.cardWidth / 2 + 38)
      .attr('y', -geometry.cardHeight / 2 + 22)
      .text((entry) => tree.ellipsize(
        tree.numberedMove(entry.data),
        18,
      ));
    nodeSelection.select('text.tree-node-eval')
      .attr('x', -geometry.cardWidth / 2 + 14)
      .attr('y', -geometry.cardHeight / 2 + 44)
      .text((entry) => tree.ellipsize(
        tree.evaluationLabel(entry.data),
        20,
      ));
    nodeSelection.select('text.tree-node-opening')
      .attr('x', -geometry.cardWidth / 2 + 14)
      .attr('y', geometry.cardHeight / 2 - 8)
      .text((entry) => tree.ellipsize(
        tree.openingName(entry.data),
        28,
      ));
    nodeSelection.select('g.tree-node-toggle')
      .attr(
        'transform',
        `translate(${geometry.cardWidth / 2 + 14},0)`,
      )
      .attr('role', 'button')
      .attr('aria-label', (entry) => {
        if (entry.data._partialChildren
            && !state.expanded.has(String(entry.data.key))) {
          return `Show ${entry.data._hiddenChildren} more branches after ${
            tree.numberedMove(entry.data)
          }`;
        }
        return entry.children && entry.children.length
          ? `Collapse ${tree.numberedMove(entry.data)}`
          : `Expand ${tree.numberedMove(entry.data)}`;
      })
      .style('display', (entry) => (
        sourceHasChildren(entry.data) ? null : 'none'
      ))
      .on('click', (event, entry) => {
        event.stopPropagation();
        toggleNode(entry.data);
      });
    nodeSelection.select('circle.tree-node-toggle-surface').attr('r', 9);
    nodeSelection.select('path.tree-node-toggle-icon')
      .attr('d', (entry) => (
        entry.children && entry.children.length
          && !entry.data._partialChildren
          ? 'M -4 0 H 4'
          : 'M -4 0 H 4 M 0 -4 V 4'
      ));
    nodeSelection.select('text.tree-node-more-count')
      .text('');
    nodeSelection.select('title').text((entry) => [
      entry.data.line_san || 'Start position',
      tree.openingName(entry.data),
      tree.statusLabel(entry.data),
      tree.evaluationLabel(entry.data),
      entry.data._hiddenChildren
        ? `${entry.data._hiddenChildren} more branches` : '',
    ].filter(Boolean).join(' \u00b7 '));

    const noMatches = (state.mode !== 'all' || state.query)
      && state.projection.totalMatches === 0;
    if (elements.empty) elements.empty.hidden = !noMatches;
    elements.svg.hidden = noMatches;
    if (elements.branchTitle) {
      setText(
        elements.branchTitle,
        state.sourceRoot.line_san || 'Start position',
      );
    }
    if (requestFit || state.needsFit) {
      state.needsFit = false;
      window.requestAnimationFrame(() => focusTree(false));
    } else if (state.transform) {
      applyTransform(state.transform, false);
    }
    selectKey(state.selectedKey, false);
  }

  function initialiseZoom() {
    if (!d3) return;
    state.zoom = d3.zoom()
      .scaleExtent([0.35, 2.2])
      .filter((event) => event.type !== 'dblclick')
      .on('zoom', (event) => {
        state.transform = {
          x: event.transform.x,
          y: event.transform.y,
          k: event.transform.k,
        };
        d3.select(elements.scene).attr('transform', event.transform);
      });
    d3.select(elements.svg).call(state.zoom).on('dblclick.zoom', null);
  }

  function applyPayload(payload, requestedRoot, url) {
    if (!payload || payload.schema !== 'atomicdb.map.v1' || !payload.root) {
      throw new Error('The server returned an unsupported move-tree schema.');
    }
    const nextRoot = requestedRoot || payload.request.root;
    const sameRoot = Boolean(
      state.payload && String(state.apiRoot) === String(nextRoot),
    );
    state.payload = payload;
    state.payloadUrl = url;
    state.apiRoot = nextRoot;
    state.sourceRoot = tree.mergeFirstMoves(payload);
    if (!sameRoot) {
      state.expanded.clear();
      state.collapsed.clear();
      state.needsFit = true;
    }
    const source = tree.sourceRecords(state.sourceRoot);
    if (!source.byKey.has(String(state.selectedKey))) {
      state.selectedKey = String(state.sourceRoot.key);
    }
    if (!source.byKey.has(String(state.rovingKey))) {
      state.rovingKey = state.selectedKey;
    }
    renderBreadcrumbs();
    renderWorkRail();
    renderTree(!sameRoot);
    updateInspector(
      source.byKey.get(String(state.selectedKey))?.data || state.sourceRoot,
    );
    const generated = new Date(payload.snapshot.generated_at);
    if (elements.stamp) {
      const text = elements.stamp.querySelector('span:last-child');
      setText(
        text || elements.stamp,
        Number.isNaN(generated.getTime())
          ? 'Snapshot ready'
          : `Updated ${generated.toLocaleString()}`,
      );
      elements.stamp.classList.add('live');
    }
    hideStatus();
    updateUrl();
  }

  function responseError(response, body) {
    const message = body && body.error && (
      typeof body.error === 'string' ? body.error : body.error.message
    );
    if (response.status === 503) {
      return 'The first solver snapshot is not ready yet.';
    }
    if (response.status === 404) {
      return 'That branch is not in the current Atomic move tree.';
    }
    return message || `Move tree unavailable (${response.status}).`;
  }

  async function loadMap(rootKey, reason) {
    if (document.hidden && reason === 'poll') return;
    if (state.controller) state.controller.abort();
    const controller = new AbortController();
    state.controller = controller;
    state.loading = true;
    if (elements.refresh) {
      elements.refresh.disabled = true;
      elements.refresh.classList.add('is-loading');
    }
    elements.stage?.setAttribute('aria-busy', 'true');
    elements.svg.setAttribute('aria-busy', 'true');
    statusMessage(
      state.payload ? 'Refreshing the move tree\u2026'
        : 'Loading the latest move tree\u2026',
      'loading',
    );
    const query = new URLSearchParams({
      root: rootKey || host.dataset.rootKey,
      weight: 'frontier',
      limit: '600',
    });
    const url = `${host.dataset.api}?${query.toString()}`;
    const headers = {Accept: 'application/json'};
    const etag = tree.etagForCurrentPayload(
      url,
      state.payloadUrl,
      state.payload,
      state.etags,
    );
    if (etag) headers['If-None-Match'] = etag;
    try {
      const response = await window.fetch(url, {
        headers,
        credentials: 'same-origin',
        signal: controller.signal,
      });
      if (state.controller !== controller) return;
      if (response.status === 304) {
        if (!tree.canReuseNotModified(
          url, state.payloadUrl, state.payload,
        )) {
          throw new Error('Invalid cached move-tree response.');
        }
        state.lastLoadedAt = Date.now();
        hideStatus();
        return;
      }
      const body = await response.json().catch(() => null);
      if (!response.ok) throw new Error(responseError(response, body));
      const responseEtag = response.headers.get('ETag');
      if (responseEtag) state.etags.set(url, responseEtag);
      applyPayload(body, rootKey, url);
      state.lastLoadedAt = Date.now();
    } catch (error) {
      if (error.name === 'AbortError') return;
      statusMessage(
        state.payload
          ? `Refresh failed; the previous tree is still shown. ${error.message}`
          : (error.message || 'Unable to load the move tree.'),
        'error',
      );
    } finally {
      if (state.controller === controller) {
        state.controller = null;
        state.loading = false;
        if (elements.refresh) {
          elements.refresh.disabled = false;
          elements.refresh.classList.remove('is-loading');
        }
        elements.stage?.setAttribute('aria-busy', 'false');
        elements.svg.setAttribute('aria-busy', 'false');
      }
    }
  }

  function clearView() {
    state.mode = 'all';
    state.query = '';
    state.expanded.clear();
    state.collapsed.clear();
    setModeControls();
    renderTree(true);
    updateUrl();
  }

  function openHelp() {
    if (!elements.helpDialog) return;
    if (typeof elements.helpDialog.showModal === 'function') {
      elements.helpDialog.showModal();
    } else {
      elements.helpDialog.hidden = false;
    }
  }

  function closeHelp() {
    if (!elements.helpDialog) return;
    if (typeof elements.helpDialog.close === 'function') {
      elements.helpDialog.close();
    } else {
      elements.helpDialog.hidden = true;
    }
  }

  elements.mode.forEach((control) => {
    control.addEventListener('change', () => {
      const value = control.value || control.dataset.mapMode;
      if (!['all', 'unresolved', 'active'].includes(value)) return;
      state.mode = value;
      state.collapsed.clear();
      setModeControls();
      renderTree(true);
      updateUrl();
    });
  });
  elements.search?.addEventListener('input', () => {
    window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(() => {
      state.query = elements.search.value.trim();
      renderTree(true);
      updateUrl();
    }, 120);
  });
  elements.zoomIn?.addEventListener('click', () => zoomBy(1.25));
  elements.zoomOut?.addEventListener('click', () => zoomBy(0.8));
  elements.fit?.addEventListener('click', () => fitTree(true));
  elements.clear?.addEventListener('click', clearView);
  elements.refresh?.addEventListener('click', () => (
    loadMap(state.apiRoot, 'refresh')
  ));
  elements.focusBranch?.addEventListener('click', () => (
    loadMap(state.selectedKey, 'focus')
  ));
  elements.helpOpen?.addEventListener('click', openHelp);
  elements.helpClose?.addEventListener('click', closeHelp);
  elements.inspectorClose?.addEventListener('click', () => {
    elements.inspector?.classList.toggle('is-collapsed');
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === '/' && document.activeElement !== elements.search) {
      event.preventDefault();
      elements.search?.focus();
    }
    if (event.key === '?' && !event.ctrlKey && !event.metaKey) {
      openHelp();
    }
  });

  window.addEventListener('popstate', () => {
    const current = new URLSearchParams(window.location.search);
    state.mode = ['all', 'unresolved', 'active'].includes(current.get('mode'))
      ? current.get('mode') : 'all';
    state.query = current.get('q') || '';
    state.selectedKey = current.get('selected') || '';
    setModeControls();
    loadMap(current.get('root') || host.dataset.rootKey, 'history');
  });

  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(() => {
      window.clearTimeout(state.resizeTimer);
      state.resizeTimer = window.setTimeout(() => {
        if (!state.sourceRoot) return;
        renderTree(false);
      }, 90);
    });
    observer.observe(elements.svgWrap);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (state.controller) state.controller.abort();
      return;
    }
    if (!state.loading && Date.now() - state.lastLoadedAt > 60000) {
      loadMap(state.apiRoot, 'visible');
    }
  });
  window.setInterval(() => {
    if (!document.hidden && !state.loading && state.payload) {
      loadMap(state.apiRoot, 'poll');
    }
  }, 60000);

  if (!d3) {
    statusMessage(
      'The visual renderer could not load. Refresh the page to try again.',
      'error',
    );
    return;
  }
  initialiseZoom();
  setModeControls();
  ensureBoard(host.dataset.rootFen);
  loadMap(state.apiRoot, 'initial');
}());
