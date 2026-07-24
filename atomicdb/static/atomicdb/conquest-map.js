(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AtomicConquestLayout = api;
}(typeof window === 'undefined' ? null : window, function () {
  'use strict';

  function safeWeight(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : 0;
  }

  function metricDifference(parent, children) {
    const result = {};
    const metrics = parent.metrics || {};
    const names = [
      'positions', 'closed', 'unknown', 'frontier', 'historical', 'nodes',
      'seconds', 'active_tasks', 'queued_tasks', 'transpositions',
    ];
    names.forEach((name) => {
      const childTotal = children.reduce(
        (total, child) => total + Number(
          child.metrics && child.metrics[name] || 0,
        ), 0,
      );
      result[name] = Math.max(0, Number(metrics[name] || 0) - childTotal);
    });
    return result;
  }

  function withResidualTree(node, readWeight) {
    const sourceChildren = Array.isArray(node.children) ? node.children : [];
    const children = sourceChildren.map(
      (child) => withResidualTree(child, readWeight),
    );
    const total = safeWeight(readWeight(node));
    const visible = sourceChildren.reduce(
      (sum, child) => sum + safeWeight(readWeight(child)), 0,
    );
    const residual = Math.max(0, total - visible);
    const copy = Object.assign({}, node, {children});
    if (residual > 0 && (
      children.length || node.truncated || Number(node.hidden_children || 0) > 0
    )) {
      const hidden = Math.max(0, Number(node.hidden_children || 0));
      children.push({
        key: `__residual__:${node.key}`,
        residual: true,
        residual_parent: node.key,
        fen: node.fen,
        status: 'UNKNOWN',
        closure: null,
        proof: null,
        eval_cp: null,
        depth: Number(node.depth || 0) + 1,
        move: {uci: null, san: hidden ? `+${hidden} moves` : 'Remainder'},
        line_san: node.line_san || '',
        line_uci: Array.isArray(node.line_uci) ? node.line_uci.slice() : [],
        metrics: metricDifference(node, sourceChildren),
        work: {state: 'idle', active: 0, queued: 0},
        transpositions: {incoming: 0, alternate_parents: 0},
        weight: residual,
        zoomable: Boolean(node.zoomable || node.truncated),
        truncated: true,
        hidden_children: hidden,
        children: [],
      });
    }
    return copy;
  }

  function leafWeight(node, readWeight) {
    return node.children && node.children.length ? 0 : safeWeight(
      readWeight(node),
    );
  }

  function payloadContext(payload, previousFirstMoves) {
    const snapshot = payload.snapshot || {};
    const lineage = payload.lineage || {
      start_key: snapshot.start_key,
      root_key: payload.root && payload.root.key,
      line_san: payload.root && payload.root.line_san || '',
      line_uci: payload.root && payload.root.line_uci || [],
      positions: payload.root ? [payload.root] : [],
    };
    let firstMoves = Array.isArray(payload.first_moves)
      ? payload.first_moves.slice()
      : (Array.isArray(previousFirstMoves) ? previousFirstMoves.slice() : []);
    if (!firstMoves.length && payload.root
        && payload.root.key === snapshot.start_key) {
      firstMoves = Array.isArray(payload.root.children)
        ? payload.root.children.slice() : [];
    }
    return {lineage, firstMoves};
  }

  function lineagePrefix(lineage, localRootKey) {
    const positions = lineage && Array.isArray(lineage.positions)
      ? lineage.positions : [];
    return positions.filter((position) => position.key !== localRootKey);
  }

  function lineageParent(lineage, currentKey) {
    const positions = lineage && Array.isArray(lineage.positions)
      ? lineage.positions : [];
    const index = positions.findIndex((position) => position.key === currentKey);
    return index > 0 ? positions[index - 1] : null;
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

  function compactText(value, pixelWidth, averageCharacterWidth) {
    const text = String(value || '');
    const width = Number(pixelWidth);
    const characterWidth = Number(averageCharacterWidth) || 6.4;
    if (!text || !Number.isFinite(width) || width <= characterWidth) return '';
    const capacity = Math.max(1, Math.floor(width / characterWidth));
    if (text.length <= capacity) return text;
    if (capacity === 1) return '\u2026';
    return `${text.slice(0, capacity - 1).trimEnd()}\u2026`;
  }

  function verticalGeometry(viewportHeight, levels) {
    const viewport = Math.max(368, Number(viewportHeight) || 496);
    const rowCount = Math.max(1, Math.floor(Number(levels) || 1));
    const band = Math.max(31, Math.min(
      57, viewport / Math.max(5, rowCount),
    ));
    const rowsHeight = Math.ceil(rowCount * band);
    const frameHeight = Math.min(
      viewport, Math.max(160, rowsHeight),
    );
    return {
      band,
      contentHeight: Math.max(frameHeight, rowsHeight),
      frameHeight,
      viewportHeight: viewport,
    };
  }

  function isExpandable(entry) {
    if (!entry) return false;
    const data = entry.data || {};
    return Boolean(
      entry.children && entry.children.length
      || data.zoomable
      || data.truncated,
    );
  }

  function keyboardTarget(entry, key, accepts, orderedEntries) {
    if (!entry) return null;
    const allowed = typeof accepts === 'function' ? accepts : () => true;
    const ordered = Array.isArray(orderedEntries)
      ? orderedEntries.filter(allowed) : [];
    if (key === 'Home') return ordered[0] || null;
    if (key === 'End') return ordered[ordered.length - 1] || null;
    if (key === 'ArrowUp') {
      let parent = entry.parent || null;
      while (parent && !allowed(parent)) parent = parent.parent || null;
      return parent;
    }
    if (key === 'ArrowDown') {
      const pending = Array.from(entry.children || []);
      while (pending.length) {
        const descendant = pending.shift();
        if (allowed(descendant)) return descendant;
        pending.push(...(descendant.children || []));
      }
      return null;
    }
    if (key === 'ArrowLeft' || key === 'ArrowRight') {
      const siblings = (entry.parent ? entry.parent.children : [entry])
        .filter(allowed);
      if (!siblings.length) return null;
      const index = siblings.indexOf(entry);
      if (index < 0) return siblings[0];
      const offset = key === 'ArrowLeft' ? -1 : 1;
      return siblings[(index + offset + siblings.length) % siblings.length];
    }
    return null;
  }

  function navigationState(
    entries, selectedKey, currentRovingKey, focusKey,
  ) {
    const candidates = Array.isArray(entries) ? entries : [];
    const keys = new Set(candidates.map((entry) => entry.data.key));
    const logicalSelection = String(selectedKey || '');
    const choices = [
      logicalSelection,
      String(currentRovingKey || ''),
      String(focusKey || ''),
    ];
    const rovingKey = choices.find((key) => key && keys.has(key))
      || (candidates[0] ? candidates[0].data.key : '');
    return {
      selectedKey: logicalSelection,
      rovingKey,
    };
  }

  function densityPlan(entries, readPixelWidth, minimumWidth) {
    const source = Array.isArray(entries) ? entries : [];
    const readWidth = typeof readPixelWidth === 'function'
      ? readPixelWidth : () => 0;
    const threshold = Math.max(0, Number(minimumWidth) || 4);
    const visible = source.filter(
      (entry) => Number(readWidth(entry)) >= threshold,
    );
    const visibleSet = new Set(visible);
    const omittedBranches = new Map();

    source.forEach((entry) => {
      if (visibleSet.has(entry)) return;
      let branch = entry;
      let owner = entry.parent || null;
      while (owner && !visibleSet.has(owner)) {
        branch = owner;
        owner = owner.parent || null;
      }
      if (!owner || !owner.data || !branch.data) return;
      const ownerKey = owner.data.key;
      const branchKey = branch.data.key;
      if (!omittedBranches.has(ownerKey)) {
        omittedBranches.set(ownerKey, new Set());
      }
      omittedBranches.get(ownerKey).add(branchKey);
    });

    const omittedByKey = new Map();
    omittedBranches.forEach((branches, key) => {
      omittedByKey.set(key, branches.size);
    });
    return {visible, omittedByKey};
  }

  function filterFeedback(totalCount, sourceMatchCount, renderedMatchCount) {
    const total = Math.max(0, Number(totalCount) || 0);
    const sourceMatches = Math.max(0, Number(sourceMatchCount) || 0);
    const renderedMatches = Math.max(0, Number(renderedMatchCount) || 0);
    if (renderedMatches > 0) return null;
    if (sourceMatches > 0) {
      return {
        reason: 'too-fine',
        message: `${sourceMatches} of ${total} territories match, but ${
          sourceMatches === 1 ? 'it is' : 'they are'
        } inside branches too narrow to display at this zoom. Zoom into a first move or clear the filters.`,
      };
    }
    return {
      reason: 'no-matches',
      message: `0 of ${total} territories match the current filters.`,
    };
  }

  return {
    withResidualTree,
    leafWeight,
    payloadContext,
    lineagePrefix,
    lineageParent,
    etagForCurrentPayload,
    canReuseNotModified,
    compactText,
    verticalGeometry,
    isExpandable,
    keyboardTarget,
    navigationState,
    densityPlan,
    filterFeedback,
  };
}));

(function () {
  'use strict';

  if (typeof document === 'undefined') return;
  const host = document.getElementById('conquest-map');
  if (!host) return;

  const d3 = window.d3;
  const layout = window.AtomicConquestLayout;
  const elements = {
    svg: document.getElementById('map-svg'),
    svgWrap: document.getElementById('map-svg-wrap'),
    stage: document.getElementById('map-stage'),
    status: document.getElementById('map-status'),
    stamp: document.getElementById('map-snapshot-stamp'),
    firstMoves: document.getElementById('first-move-strip'),
    breadcrumbs: document.getElementById('map-breadcrumbs'),
    refresh: document.getElementById('map-refresh'),
    statusFilter: document.getElementById('map-filter-status'),
    closureFilter: document.getElementById('map-filter-closure'),
    workFilter: document.getElementById('map-filter-work'),
    trustFilter: document.getElementById('map-filter-trust'),
    textRows: document.getElementById('map-text-rows'),
    inspectorTitle: document.getElementById('inspector-title'),
    inspectorKicker: document.getElementById('inspector-kicker'),
    inspectorLine: document.getElementById('inspector-line'),
    inspectorOpening: document.getElementById('inspector-opening'),
    inspectorOpeningName: document.getElementById('inspector-opening-name'),
    inspectorOpeningState: document.getElementById('inspector-opening-state'),
    inspectorStatus: document.getElementById('inspector-status'),
    inspectorTrust: document.getElementById('inspector-trust'),
    inspectorEval: document.getElementById('inspector-eval'),
    inspectorFrontier: document.getElementById('inspector-frontier'),
    inspectorPositions: document.getElementById('inspector-positions'),
    inspectorNodes: document.getElementById('inspector-nodes'),
    inspectorTime: document.getElementById('inspector-time'),
    inspectorTranspositions: document.getElementById('inspector-transpositions'),
    inspectorWork: document.getElementById('inspector-work'),
    zoomSelected: document.getElementById('map-zoom-selected'),
    explorer: document.getElementById('map-open-explorer'),
    board: document.getElementById('map-board'),
  };

  const params = new URLSearchParams(window.location.search);
  const allowedWeights = new Set(['frontier', 'explored', 'compute']);
  const allowedClosures = new Set([
    'all', 'TERMINAL', 'TB', 'MATE_PV', 'MINIMAX', 'NONE',
  ]);
  const reducedMotion = window.matchMedia(
    '(prefers-reduced-motion: reduce)',
  ).matches;
  const state = {
    apiRoot: params.get('root') || host.dataset.rootKey,
    requestedBranch: params.get('branch') || '',
    requestedSelection: params.get('selected') || '',
    weight: allowedWeights.has(params.get('weight'))
      ? params.get('weight') : 'frontier',
    filters: {
      status: params.get('status') || 'all',
      closure: allowedClosures.has(params.get('closure'))
        ? params.get('closure') : 'all',
      work: params.get('work') || 'all',
      trust: params.get('trust') || 'all',
    },
    data: null,
    dataUrl: '',
    hierarchy: null,
    index: new Map(),
    keyboardKeys: new Set(),
    densityOmitted: new Map(),
    rovingKey: '',
    focusKey: '',
    selectedKey: '',
    firstMoves: [],
    lineage: [],
    etags: new Map(),
    abortController: null,
    loading: false,
    resizeTimer: null,
    lastWidth: 0,
    lastContainerWidth: 0,
    viewportCeiling: 0,
  };

  const statusNames = {
    UNKNOWN: 'Unknown',
    WHITE_WIN: 'White win',
    BLACK_WIN: 'Black win',
    DRAW: 'Draw',
  };
  const statusClasses = {
    UNKNOWN: 'unknown',
    WHITE_WIN: 'white-win',
    BLACK_WIN: 'black-win',
    DRAW: 'draw',
  };
  const statusColours = {
    UNKNOWN: '#67615a',
    WHITE_WIN: '#6da63b',
    BLACK_WIN: '#d84b4b',
    DRAW: '#4d91d9',
  };
  const metricFields = {
    frontier: 'frontier',
    explored: 'positions',
    compute: 'nodes',
  };
  const pieceNames = {
    p: 'pawn', n: 'knight', b: 'bishop', r: 'rook', q: 'queen', k: 'king',
  };

  function safeNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function human(value) {
    const number = safeNumber(value);
    const absolute = Math.abs(number);
    if (absolute >= 1e12) return `${(number / 1e12).toFixed(2)}T`;
    if (absolute >= 1e9) return `${(number / 1e9).toFixed(2)}B`;
    if (absolute >= 1e6) return `${(number / 1e6).toFixed(1)}M`;
    if (absolute >= 1e3) return `${(number / 1e3).toFixed(1)}k`;
    return Math.round(number).toLocaleString();
  }

  function duration(seconds) {
    const value = safeNumber(seconds);
    if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)}s`;
    if (value < 3600) return `${(value / 60).toFixed(1)}m`;
    if (value < 86400) return `${(value / 3600).toFixed(1)}h`;
    return `${(value / 86400).toFixed(1)}d`;
  }

  function evaluation(value) {
    if (value === null || value === undefined) return '—';
    const cp = safeNumber(value);
    const pawns = cp / 100;
    return `${pawns >= 0 ? '+' : ''}${pawns.toFixed(2)} White POV`;
  }

  function statusOf(node) {
    const status = String(node.status || 'UNKNOWN').toUpperCase();
    return Object.prototype.hasOwnProperty.call(statusNames, status)
      ? status : 'UNKNOWN';
  }

  function workOf(node) {
    const work = node.work || {};
    if (work.active || safeNumber(work.active_tasks) > 0
        || safeNumber(node.metrics && node.metrics.active_tasks) > 0
        || String(work.state || '').toLowerCase() === 'active') {
      return 'active';
    }
    if (work.queued || safeNumber(work.queued_tasks) > 0
        || safeNumber(node.metrics && node.metrics.queued_tasks) > 0
        || String(work.state || '').toLowerCase() === 'queued') {
      return 'queued';
    }
    return 'idle';
  }

  function trustOf(node) {
    const proof = String(node.proof || '').toUpperCase();
    const closure = String(node.closure || '').toUpperCase();
    if (proof.includes('DISPUT') || proof.includes('CONFLICT')) {
      return 'disputed';
    }
    if (proof.includes('AND') || proof.includes('CERTIF')
        || proof.includes('PROVEN')) {
      return 'proven';
    }
    if (proof.includes('VERIF') || closure === 'TERMINAL' || closure === 'TB') {
      return 'verified';
    }
    if (proof.includes('ENGINE') || node.eval_cp !== null
        || safeNumber(node.metrics && node.metrics.nodes) > 0) {
      return 'engine';
    }
    return 'unproven';
  }

  function closureOf(node) {
    const closure = String(node.closure || '').toUpperCase();
    return allowedClosures.has(closure) && closure !== 'all'
      ? closure : 'NONE';
  }

  function trustName(trust) {
    return {
      verified: 'Verified',
      proven: 'AND/OR proof',
      engine: 'Engine evidence',
      disputed: 'Disputed',
      unproven: 'Unproven',
    }[trust] || 'Unproven';
  }

  function iconFor(node) {
    const trust = trustOf(node);
    const incoming = safeNumber(
      node.transpositions && node.transpositions.incoming,
    );
    let icon = '';
    if (trust === 'verified' || trust === 'proven') icon += '◆';
    if (trust === 'engine') icon += '⚙';
    if (trust === 'disputed') icon += '⚠';
    if (incoming > 1) icon += ` ↗${incoming - 1}`;
    return icon.trim();
  }

  function moveLabel(node) {
    if (!node.move) return 'Start';
    return node.move.san || node.move.uci || '…';
  }

  function lineFor(hierarchyNode) {
    if (!hierarchyNode) return 'Start position';
    const declared = hierarchyNode.data.line_san;
    if (typeof declared === 'string' && declared.trim()) return declared.trim();
    if (Array.isArray(hierarchyNode.data.line_uci)
        && hierarchyNode.data.line_uci.length) {
      return hierarchyNode.data.line_uci.join(' ');
    }
    const moves = hierarchyNode.ancestors().reverse()
      .map((entry) => entry.data.move && (
        entry.data.move.san || entry.data.move.uci
      ))
      .filter(Boolean);
    return moves.length ? moves.join(' ') : 'Start position';
  }

  function weightFor(node) {
    const direct = safeNumber(node.weight);
    if (direct > 0) return direct;
    const metrics = node.metrics || {};
    const metric = safeNumber(metrics[metricFields[state.weight]]);
    return metric > 0 ? metric : 1;
  }

  function matchesFilters(node) {
    if (state.filters.status !== 'all'
        && statusOf(node) !== state.filters.status) return false;
    if (state.filters.closure !== 'all'
        && closureOf(node) !== state.filters.closure) return false;
    if (state.filters.work !== 'all'
        && workOf(node) !== state.filters.work) return false;
    if (state.filters.trust !== 'all'
        && trustOf(node) !== state.filters.trust) return false;
    return true;
  }

  function showStatus(message, kind) {
    elements.status.hidden = false;
    elements.status.className = `map-status ${kind || ''}`.trim();
    elements.status.dataset.mode = kind || 'loading';
    delete elements.status.dataset.reason;
    elements.status.replaceChildren();
    if (!kind) {
      const spinner = document.createElement('span');
      spinner.className = 'map-spinner';
      spinner.setAttribute('aria-hidden', 'true');
      elements.status.appendChild(spinner);
    }
    elements.status.appendChild(document.createTextNode(message));
  }

  function hideStatus() {
    elements.status.hidden = true;
    delete elements.status.dataset.mode;
    delete elements.status.dataset.reason;
  }

  function showFilterFeedback(force) {
    if (!state.data || (state.loading && !force)) return;
    const territories = visibleNodes().filter(
      (entry) => !entry.data.residual,
    );
    const matches = territories.filter(
      (entry) => matchesFilters(entry.data),
    );
    const feedback = layout.filterFeedback(
      territories.length,
      matches.length,
      state.keyboardKeys.size,
    );
    if (!feedback) {
      if (elements.status.dataset.mode === 'filters') hideStatus();
      return;
    }

    elements.status.hidden = false;
    elements.status.className = 'map-status empty';
    elements.status.dataset.mode = 'filters';
    elements.status.dataset.reason = feedback.reason;
    elements.status.replaceChildren();
    const message = document.createElement('span');
    message.textContent = feedback.message;
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'map-action';
    clear.textContent = 'Clear filters';
    clear.addEventListener('click', () => {
      Object.keys(state.filters).forEach((key) => {
        state.filters[key] = 'all';
      });
      setInputsFromState();
      handleFilter();
      window.requestAnimationFrame(() => elements.statusFilter.focus());
    });
    elements.status.append(message, clear);
  }

  function enhanceInteractionHelp() {
    const help = document.querySelector('.map-help');
    if (!help || help.querySelector('[data-pointer-help]')) return;
    help.setAttribute('aria-label', 'Map interaction help');
    [
      ['Click / tap', 'select'],
      ['Double-click', 'zoom'],
    ].forEach(([gesture, action]) => {
      const command = document.createElement('span');
      command.className = 'map-key-command';
      command.dataset.pointerHelp = '';
      const keySet = document.createElement('span');
      keySet.className = 'map-key-set';
      const key = document.createElement('kbd');
      key.textContent = gesture;
      keySet.appendChild(key);
      const label = document.createElement('span');
      label.textContent = action;
      command.append(keySet, label);
      help.appendChild(command);
    });
  }

  function setInputsFromState() {
    const radio = document.querySelector(
      `input[name="weight"][value="${state.weight}"]`,
    );
    if (radio) radio.checked = true;
    elements.statusFilter.value = state.filters.status;
    elements.closureFilter.value = state.filters.closure;
    elements.workFilter.value = state.filters.work;
    elements.trustFilter.value = state.filters.trust;
  }

  function updateUrl() {
    const query = new URLSearchParams();
    if (state.apiRoot && state.apiRoot !== host.dataset.rootKey) {
      query.set('root', state.apiRoot);
    }
    if (state.weight !== 'frontier') query.set('weight', state.weight);
    const snapshotStart = state.data && state.data.snapshot
      ? state.data.snapshot.start_key : host.dataset.rootKey;
    if (state.focusKey && state.focusKey !== snapshotStart) {
      query.set('branch', state.focusKey);
    }
    if (state.selectedKey && state.selectedKey !== state.focusKey) {
      query.set('selected', state.selectedKey);
    }
    Object.entries(state.filters).forEach(([key, value]) => {
      if (value !== 'all') query.set(key, value);
    });
    const suffix = query.toString();
    const next = `${window.location.pathname}${suffix ? `?${suffix}` : ''}`;
    window.history.replaceState(null, '', next);
  }

  function renderBoard(fen, label) {
    const boardFen = String(fen || host.dataset.rootFen).split(' ')[0];
    const ranks = boardFen.split('/');
    elements.board.replaceChildren();
    elements.board.setAttribute(
      'aria-label', `Atomic chess position after ${label || 'start position'}`,
    );
    if (ranks.length !== 8) return;
    ranks.forEach((rank, rankIndex) => {
      let fileIndex = 0;
      Array.from(rank).forEach((symbol) => {
        const empty = Number.parseInt(symbol, 10);
        const amount = Number.isNaN(empty) ? 1 : empty;
        for (let index = 0; index < amount && fileIndex < 8; index += 1) {
          const square = document.createElement('span');
          square.className = 'map-square';
          if ((rankIndex + fileIndex) % 2) square.classList.add('dark');
          square.setAttribute('aria-hidden', 'true');
          if (Number.isNaN(empty)) {
            const colour = symbol === symbol.toUpperCase() ? 'w' : 'b';
            const kind = symbol.toUpperCase();
            const image = document.createElement('img');
            image.src = `${host.dataset.pieceBase}${colour}${kind}.svg`;
            image.alt = '';
            image.title = `${colour === 'w' ? 'White' : 'Black'} ${
              pieceNames[symbol.toLowerCase()] || 'piece'
            }`;
            square.appendChild(image);
          }
          elements.board.appendChild(square);
          fileIndex += 1;
        }
      });
    });
  }

  function updateInspector(hierarchyNode, persistent) {
    if (!hierarchyNode) return;
    const node = hierarchyNode.data;
    const metrics = node.metrics || {};
    const status = statusOf(node);
    const trust = trustOf(node);
    const work = workOf(node);
    const line = lineFor(hierarchyNode);
    elements.inspectorKicker.textContent = node.move
      ? `Ply ${safeNumber(node.depth)}` : 'Project root';
    elements.inspectorTitle.textContent = moveLabel(node);
    elements.inspectorTitle.title = line;
    elements.inspectorLine.textContent = line;
    if (node.opening && node.opening.name) {
      elements.inspectorOpening.hidden = false;
      elements.inspectorOpeningName.textContent = node.opening.name;
      elements.inspectorOpeningState.textContent = 'exact position';
    } else {
      elements.inspectorOpening.hidden = true;
      elements.inspectorOpeningName.textContent = '';
      elements.inspectorOpeningState.textContent = '';
    }
    elements.inspectorStatus.className = `verdict-pill ${
      statusClasses[status]
    }`;
    elements.inspectorStatus.textContent = statusNames[status];
    elements.inspectorTrust.textContent = trustName(trust);
    elements.inspectorEval.textContent = evaluation(node.eval_cp);
    elements.inspectorFrontier.textContent = human(metrics.frontier);
    elements.inspectorPositions.textContent = human(metrics.positions);
    elements.inspectorNodes.textContent = human(metrics.nodes);
    elements.inspectorTime.textContent = duration(metrics.seconds);
    const incoming = safeNumber(
      node.transpositions && node.transpositions.incoming,
    );
    elements.inspectorTranspositions.textContent = incoming > 1
      ? `${incoming - 1} alternate` : 'None';
    elements.inspectorWork.className = `work-summary ${work}`;
    if (work === 'active') {
      elements.inspectorWork.textContent = `${
        human(metrics.active_tasks || 1)
      } analysis task${safeNumber(metrics.active_tasks) === 1 ? '' : 's'} active`;
    } else if (work === 'queued') {
      elements.inspectorWork.textContent = `${
        human(metrics.queued_tasks || 1)
      } task${safeNumber(metrics.queued_tasks) === 1 ? '' : 's'} queued`;
    } else {
      elements.inspectorWork.textContent = 'No active work';
    }
    const explorerUrl = new URL(
      `/atomicdb/explore/${encodeURIComponent(node.key)}/`,
      window.location.origin,
    );
    if (Array.isArray(node.line_uci) && node.line_uci.length) {
      explorerUrl.searchParams.set('play', node.line_uci.join(','));
    }
    elements.explorer.href = `${explorerUrl.pathname}${explorerUrl.search}`;
    elements.zoomSelected.disabled = !(
      hierarchyNode.children
      || node.zoomable
      || node.truncated
    );
    renderBoard(node.fen, line);
    if (persistent) {
      state.selectedKey = node.key;
      updateUrl();
      renderChart();
    }
  }

  function selectNode(hierarchyNode, focusElement) {
    if (!hierarchyNode) return;
    if (hierarchyNode.data.residual) hierarchyNode = hierarchyNode.parent;
    updateInspector(hierarchyNode, true);
    renderTextFallback();
    if (focusElement) focusMark(hierarchyNode.data.key);
  }

  function markForKey(key) {
    return Array.from(elements.svg.querySelectorAll('[data-key]')).find(
      (mark) => mark.getAttribute('data-key') === key,
    ) || null;
  }

  function focusMark(key) {
    window.requestAnimationFrame(() => {
      const mark = markForKey(key) || markForKey(state.rovingKey);
      if (!mark) return;
      mark.focus({preventScroll: true});
      if (typeof mark.scrollIntoView === 'function') {
        mark.scrollIntoView({
          block: 'nearest',
          inline: 'nearest',
          behavior: 'auto',
        });
      }
    });
  }

  function plainHierarchy(data, parent, depth) {
    const entry = {
      data,
      parent: parent || null,
      depth: depth || 0,
      children: null,
      ancestors() {
        const result = [];
        let current = this;
        while (current) {
          result.push(current);
          current = current.parent;
        }
        return result;
      },
      descendants() {
        const result = [];
        const pending = [this];
        while (pending.length) {
          const current = pending.shift();
          result.push(current);
          if (current.children) pending.unshift(...current.children);
        }
        return result;
      },
      each(callback) {
        this.descendants().forEach(callback);
        return this;
      },
    };
    const children = Array.isArray(data.children) ? data.children : [];
    entry.children = children.length
      ? children.map((child) => plainHierarchy(child, entry, entry.depth + 1))
      : null;
    return entry;
  }

  function hierarchyFor(dataRoot) {
    const displayRoot = layout.withResidualTree(dataRoot, weightFor);
    const hierarchy = d3
      ? d3.hierarchy(displayRoot)
      : plainHierarchy(displayRoot);
    if (!d3) {
      state.index = new Map();
      hierarchy.each((entry) => {
        if (!entry.data.residual) state.index.set(entry.data.key, entry);
      });
      return hierarchy;
    }
    hierarchy.sum((node) => layout.leafWeight(node, weightFor));
    hierarchy.sort((left, right) => (
      (right.value || 0) - (left.value || 0)
      || String(left.data.key).localeCompare(String(right.data.key))
    ));
    const width = Math.max(320, elements.svgWrap.clientWidth || 800);
    d3.partition().size([width, hierarchy.height + 1])(hierarchy);
    state.index = new Map();
    hierarchy.each((entry) => {
      if (!entry.data.residual) state.index.set(entry.data.key, entry);
    });
    return hierarchy;
  }

  function focusNode() {
    return state.index.get(state.focusKey) || state.hierarchy;
  }

  function chartLabel(entry, pixelWidth) {
    const hidden = safeNumber(entry.data.hidden_children)
      + safeNumber(state.densityOmitted.get(entry.data.key));
    if (hidden > 0 && pixelWidth > 80) {
      return layout.compactText(
        `${moveLabel(entry.data)} · +${human(hidden)}`,
        Math.max(0, pixelWidth - 12),
      );
    }
    return layout.compactText(
      moveLabel(entry.data), Math.max(0, pixelWidth - 12),
    );
  }

  function ariaLabel(entry) {
    const metrics = entry.data.metrics || {};
    if (entry.data.residual) {
      return `${
        moveLabel(entry.data)
      }, ${human(entry.data.weight)} hidden ${state.weight}; zoom to expand`;
    }
    const icon = iconFor(entry.data);
    return [
      lineFor(entry),
      statusNames[statusOf(entry.data)],
      `${human(metrics[metricFields[state.weight]])} ${state.weight}`,
      workOf(entry.data) === 'idle' ? '' : workOf(entry.data),
      trustName(trustOf(entry.data)),
      icon,
      entry.data.truncated ? 'More descendants available' : '',
      state.densityOmitted.get(entry.data.key)
        ? `${state.densityOmitted.get(entry.data.key)} fine branch${
          state.densityOmitted.get(entry.data.key) === 1 ? '' : 'es'
        } hidden at this zoom`
        : '',
    ].filter(Boolean).join(', ');
  }

  function visibleNodes() {
    const focus = focusNode();
    if (!focus) return [];
    const nodes = focus.descendants().filter((entry) => {
      // Keep a node's own weight as an intentional gap below it, but only
      // draw a residual mark when it also represents omitted children.
      if (entry.data.residual
          && safeNumber(entry.data.hidden_children) === 0) return false;
      let current = entry;
      while (current && current !== focus) current = current.parent;
      return current === focus;
    });
    return nodes.slice(0, 600);
  }

  function interactiveNodes() {
    return visibleNodes().filter((entry) => (
      !entry.data.residual && matchesFilters(entry.data)
    ));
  }

  function rovingFocusKey(nodes) {
    const entries = nodes || interactiveNodes();
    return layout.navigationState(
      entries,
      state.selectedKey,
      state.rovingKey,
      state.focusKey,
    ).rovingKey;
  }

  function reconcileSelection() {
    const nodes = interactiveNodes();
    state.rovingKey = rovingFocusKey(nodes);
    return state.index.get(state.selectedKey) || focusNode();
  }

  function resetChartFrame() {
    elements.svgWrap.style.height = '';
    elements.svgWrap.style.minHeight = '';
    elements.svgWrap.style.maxHeight = '';
    elements.svgWrap.style.overflowY = '';
    elements.svg.style.height = '';
    elements.svg.style.minHeight = '';
    elements.svg.style.maxHeight = '';
    state.viewportCeiling = 0;
  }

  function renderChart() {
    if (!d3 || !state.hierarchy) return;
    const focus = focusNode();
    const width = Math.max(320, elements.svgWrap.clientWidth || 800);
    const sourceNodes = visibleNodes();
    const xScale = d3.scaleLinear()
      .domain([focus.x0, focus.x1])
      .range([0, width]);
    const pixelWidth = (entry) => Math.max(
      0, xScale(entry.x1) - xScale(entry.x0) - 1,
    );
    const density = layout.densityPlan(sourceNodes, pixelWidth, 4);
    const nodes = density.visible;
    state.densityOmitted = density.omittedByKey;
    const levels = nodes.reduce(
      (maximum, entry) => Math.max(maximum, entry.depth - focus.depth + 1),
      1,
    );
    if (!state.viewportCeiling) {
      state.viewportCeiling = Math.max(
        368, elements.svgWrap.clientHeight || 496,
      );
    }
    const geometry = layout.verticalGeometry(state.viewportCeiling, levels);
    const height = geometry.contentHeight;
    const band = geometry.band;
    const isVisibleMark = (entry) => matchesFilters(entry.data);
    const isKeyboardMark = (entry) => (
      !entry.data.residual && isVisibleMark(entry)
    );
    const keyboardNodes = nodes.filter(isKeyboardMark);
    state.keyboardKeys = new Set(
      keyboardNodes.map((entry) => entry.data.key),
    );
    const tabKey = rovingFocusKey(keyboardNodes);
    state.rovingKey = tabKey;
    elements.svgWrap.style.height = `${geometry.frameHeight}px`;
    elements.svgWrap.style.minHeight = `${geometry.frameHeight}px`;
    elements.svgWrap.style.maxHeight = `${geometry.frameHeight}px`;
    elements.svgWrap.style.overflowY = (
      height > geometry.frameHeight ? 'auto' : 'hidden'
    );
    // The responsive stylesheet declares #map-svg with ID specificity.
    // Mark the runtime content height important so the SVG and its adaptive
    // frame cannot diverge at narrow breakpoints.
    elements.svg.style.setProperty('height', `${height}px`, 'important');
    elements.svg.style.setProperty('min-height', `${height}px`, 'important');
    elements.svg.style.setProperty('max-height', `${height}px`, 'important');
    const svg = d3.select(elements.svg)
      .attr('viewBox', `0 0 ${width} ${height}`)
      .attr('preserveAspectRatio', 'none')
      .attr('aria-busy', 'false');
    svg.interrupt();
    const join = svg.selectAll('g.map-node')
      .data(nodes, (entry) => entry.data.key);
    // Exiting marks stop being interactive immediately. Keeping them around
    // for a fade made the advertised 600-node budget temporarily untrue and
    // exposed stale treeitems to assistive technology after every zoom.
    join.exit().interrupt().remove();
    const enter = join.enter().append('g')
      .attr('class', 'map-node')
      .attr('opacity', 0)
      .attr('role', 'treeitem')
      .attr('tabindex', -1);
    enter.append('rect').attr('class', 'territory').attr('rx', 3);
    enter.append('line').attr('class', 'eval-inset');
    enter.append('text').attr('class', 'map-label');
    enter.append('text').attr('class', 'map-meta');
    enter.append('title');
    const merged = enter.merge(join)
      .attr('data-key', (entry) => entry.data.key)
      .attr('role', (entry) => (
        isKeyboardMark(entry) ? 'treeitem' : 'presentation'
      ))
      .attr('aria-level', (entry) => (
        isKeyboardMark(entry) ? entry.depth + 1 : null
      ))
      .attr('aria-expanded', (entry) => (
        isKeyboardMark(entry) && layout.isExpandable(entry) ? 'true' : null
      ))
      .attr('aria-selected', (entry) => (
        isKeyboardMark(entry)
          ? (entry.data.key === state.selectedKey ? 'true' : 'false')
          : null
      ))
      .attr('aria-hidden', (entry) => (
        isKeyboardMark(entry) ? null : 'true'
      ))
      .attr('aria-label', (entry) => (
        isKeyboardMark(entry) ? ariaLabel(entry) : null
      ))
      .attr('pointer-events', (entry) => (
        isVisibleMark(entry) ? null : 'none'
      ))
      .attr('tabindex', (entry) => (
        entry.data.key === tabKey ? 0 : -1
      ))
      .attr('class', (entry) => {
        const classes = [
          'map-node', `status-${statusClasses[statusOf(entry.data)]}`,
        ];
        if (entry.data.residual) classes.push('is-residual');
        const work = workOf(entry.data);
        if (work === 'active') classes.push('is-active');
        if (work === 'queued') classes.push('is-queued');
        if (entry.data.key === state.selectedKey) classes.push('is-selected');
        if (!matchesFilters(entry.data)) classes.push('is-filtered');
        return classes.join(' ');
      })
      .on('click', function (event, entry) {
        event.stopPropagation();
        selectNode(entry, true);
      })
      .on('dblclick', function (event, entry) {
        event.preventDefault();
        event.stopPropagation();
        zoomTo(entry);
      })
      .on('mouseenter', function (event, entry) {
        updateInspector(entry, false);
      })
      .on('mouseleave', function () {
        updateInspector(
          state.index.get(state.selectedKey) || focusNode(), false,
        );
      })
      .on('focus', function (event, entry) {
        selectNode(entry, false);
      })
      .on('keydown', handleNodeKeydown);
    const transition = merged.transition().duration(reducedMotion ? 0 : 360)
      .ease(d3.easeCubicOut)
      .attr('opacity', 1)
      .attr('transform', (entry) => (
        `translate(${xScale(entry.x0)},${(entry.depth - focus.depth) * band})`
      ));
    transition.select('rect.territory')
      .attr('width', (entry) => Math.max(0, xScale(entry.x1) - xScale(entry.x0) - 1))
      .attr('height', Math.max(4, band - 2));
    merged.select('line.eval-inset')
      .attr('display', (entry) => (
        statusOf(entry.data) === 'UNKNOWN' && entry.data.eval_cp !== null
          ? null : 'none'
      ))
      .attr('x1', 3)
      .attr('x2', (entry) => {
        const rectWidth = Math.max(0, xScale(entry.x1) - xScale(entry.x0) - 1);
        const normalized = Math.max(-1, Math.min(1, safeNumber(entry.data.eval_cp) / 800));
        return Math.max(3, Math.min(rectWidth - 3, rectWidth * (normalized + 1) / 2));
      })
      .attr('y1', Math.max(4, band - 6))
      .attr('y2', Math.max(4, band - 6));
    merged.select('text.map-label')
      .attr('x', 6)
      .attr('y', Math.min(18, band * .43))
      .attr('display', (entry) => (
        xScale(entry.x1) - xScale(entry.x0) > 37 ? null : 'none'
      ))
      .text((entry) => chartLabel(
        entry, xScale(entry.x1) - xScale(entry.x0),
      ));
    merged.select('text.map-meta')
      .attr('x', 6)
      .attr('y', Math.min(34, band * .75))
      .attr('display', (entry) => (
        xScale(entry.x1) - xScale(entry.x0) > 62 && band > 39
          ? null : 'none'
      ))
      .text((entry) => layout.compactText(
        iconFor(entry.data),
        Math.max(0, xScale(entry.x1) - xScale(entry.x0) - 12),
        5.2,
      ));
    merged.select('title').text(ariaLabel);
    state.lastWidth = width;
    state.lastContainerWidth = Math.max(
      320, elements.svgWrap.parentElement.clientWidth || width,
    );
  }

  function handleNodeKeydown(event, entry) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (event.key === 'Enter' && layout.isExpandable(entry)) {
        zoomTo(entry, true);
      } else {
        selectNode(entry, true);
      }
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      zoomOut(true);
      return;
    }
    const ordered = interactiveNodes().filter(
      (candidate) => state.keyboardKeys.has(candidate.data.key),
    );
    const allowed = (candidate) => ordered.includes(candidate);
    const target = layout.keyboardTarget(entry, event.key, allowed, ordered);
    if (target) {
      event.preventDefault();
      selectNode(target, true);
    }
  }

  function breadcrumbsFor(entry) {
    if (!entry) return [];
    const local = entry.ancestors().reverse();
    const prefixPositions = layout.lineagePrefix(
      state.lineage, local[0] && local[0].data.key,
    );
    if (!prefixPositions.length) return local;
    const localRootKey = local[0] && local[0].data.key;
    const prefix = prefixPositions
      .filter((position) => position.key !== localRootKey)
      .map((position) => ({data: position, external: true}));
    return prefix.concat(local);
  }

  function activateBreadcrumb(entry) {
    if (!entry.external) {
      setFocus(entry);
      return;
    }
    state.requestedBranch = entry.data.key;
    state.requestedSelection = entry.data.key;
    loadMap(entry.data.key, 'breadcrumb');
  }

  function renderBreadcrumbs() {
    elements.breadcrumbs.replaceChildren();
    const crumbs = breadcrumbsFor(focusNode());
    crumbs.forEach((entry, index) => {
      if (index) {
        const separator = document.createElement('span');
        separator.className = 'separator';
        separator.setAttribute('aria-hidden', 'true');
        separator.textContent = '›';
        elements.breadcrumbs.appendChild(separator);
      }
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = moveLabel(entry.data);
      if (index === crumbs.length - 1) {
        button.className = 'current';
        button.setAttribute('aria-current', 'page');
      }
      button.title = entry.external ? moveLabel(entry.data) : lineFor(entry);
      button.addEventListener('click', () => activateBreadcrumb(entry));
      elements.breadcrumbs.appendChild(button);
    });
  }

  function isWithinBranch(node, branch) {
    let current = node;
    while (current) {
      if (current === branch) return true;
      current = current.parent;
    }
    return false;
  }

  function setFocus(entry, restoreKeyboardFocus) {
    if (!entry) return;
    state.focusKey = entry.data.key;
    const selected = state.index.get(state.selectedKey);
    if (!selected || !isWithinBranch(selected, entry)) {
      state.selectedKey = entry.data.key;
    }
    reconcileSelection();
    renderBreadcrumbs();
    renderFirstMoves();
    renderChart();
    renderTextFallback();
    updateInspector(state.index.get(state.selectedKey) || entry, false);
    updateUrl();
    elements.svgWrap.scrollTop = 0;
    if (restoreKeyboardFocus) focusMark(state.selectedKey || state.focusKey);
  }

  async function zoomTo(entry, restoreKeyboardFocus) {
    if (!entry) return;
    if (entry.data.residual) entry = entry.parent;
    if ((entry.data.truncated || (!entry.children && entry.data.zoomable))
        && entry.data.key !== state.apiRoot) {
      state.requestedBranch = entry.data.key;
      state.requestedSelection = entry.data.key;
      await loadMap(entry.data.key, 'zoom');
      if (restoreKeyboardFocus) {
        focusMark(state.selectedKey || state.focusKey);
      }
      return;
    }
    if (layout.isExpandable(entry)) {
      setFocus(entry, restoreKeyboardFocus);
    } else {
      selectNode(entry, restoreKeyboardFocus);
    }
  }

  function zoomOut(restoreKeyboardFocus) {
    const focus = focusNode();
    if (focus && focus.parent) {
      setFocus(focus.parent, restoreKeyboardFocus);
      return;
    }
    const parent = layout.lineageParent(state.lineage, state.apiRoot);
    if (parent) {
      state.requestedBranch = parent.key;
      state.requestedSelection = parent.key;
      loadMap(parent.key, 'zoom-out').then(() => {
        if (restoreKeyboardFocus) {
          focusMark(state.selectedKey || state.focusKey);
        }
      });
      return;
    }
    if (state.apiRoot !== host.dataset.rootKey) {
      state.requestedBranch = '';
      state.requestedSelection = '';
      loadMap(host.dataset.rootKey, 'zoom-out').then(() => {
        if (restoreKeyboardFocus) {
          focusMark(state.selectedKey || state.focusKey);
        }
      });
    }
  }

  function renderFirstMoves() {
    elements.firstMoves.replaceChildren();
    elements.firstMoves.setAttribute('role', 'group');
    elements.firstMoves.setAttribute('aria-label', 'Selectable first moves');
    if (!state.firstMoves.length) {
      const empty = document.createElement('span');
      empty.className = 'strip-placeholder';
      empty.textContent = 'No attributed first moves in this snapshot.';
      elements.firstMoves.appendChild(empty);
      return;
    }
    state.firstMoves.forEach((node) => {
      const metrics = node.metrics || {};
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'first-move-button';
      if (state.focusKey === node.key) button.classList.add('is-focus');
      button.style.setProperty('--status-colour', statusColours[statusOf(node)]);
      button.setAttribute(
        'aria-pressed', state.focusKey === node.key ? 'true' : 'false',
      );
      button.setAttribute('aria-label', [
        moveLabel(node),
        `${human(metrics[metricFields[state.weight]])} ${state.weight}`,
        statusNames[statusOf(node)],
      ].join(', '));
      button.title = node.line_san || moveLabel(node);
      const move = document.createElement('span');
      move.className = 'move';
      move.textContent = moveLabel(node);
      const amount = document.createElement('span');
      amount.className = 'amount';
      amount.textContent = `${human(metrics[metricFields[state.weight]])}`;
      const status = document.createElement('span');
      status.className = 'state';
      status.textContent = statusNames[statusOf(node)];
      button.append(move, amount, status);
      button.addEventListener('click', async () => {
        const current = state.index.get(node.key);
        if (current) setFocus(current);
        else {
          state.requestedBranch = node.key;
          state.requestedSelection = node.key;
          await loadMap(node.key, 'first-move');
        }
      });
      elements.firstMoves.appendChild(button);
    });
  }

  function renderTextFallback() {
    elements.textRows.replaceChildren();
    const nodes = visibleNodes().filter((entry) => matchesFilters(entry.data));
    if (!nodes.length) {
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = 5;
      cell.textContent = 'No visible positions match these filters.';
      row.appendChild(cell);
      elements.textRows.appendChild(row);
      return;
    }
    nodes.slice(0, 120).forEach((entry) => {
      const node = entry.data;
      const metrics = node.metrics || {};
      const row = document.createElement('tr');
      const lineCell = document.createElement('td');
      const link = document.createElement('a');
      const target = node.residual && entry.parent ? entry.parent : entry;
      link.href = `/atomicdb/explore/${
        encodeURIComponent(target.data.key)
      }/`;
      if (Array.isArray(target.data.line_uci)
          && target.data.line_uci.length) {
        link.href += `?play=${encodeURIComponent(
          target.data.line_uci.join(','),
        )}`;
      }
      link.textContent = node.residual
        ? `${lineFor(target)} — ${moveLabel(node)}`
        : lineFor(entry);
      if (!node.residual && node.opening && node.opening.name) {
        link.textContent += ` · ${node.opening.name}`;
      }
      lineCell.appendChild(link);
      const statusCell = document.createElement('td');
      statusCell.className = 'table-state';
      statusCell.textContent = statusNames[statusOf(node)];
      const frontierCell = document.createElement('td');
      frontierCell.textContent = human(metrics.frontier);
      const workCell = document.createElement('td');
      workCell.textContent = workOf(node);
      const trustCell = document.createElement('td');
      trustCell.textContent = trustName(trustOf(node));
      row.append(lineCell, statusCell, frontierCell, workCell, trustCell);
      elements.textRows.appendChild(row);
    });
  }

  function applyPayload(payload, requestedRoot, payloadUrl) {
    if (!payload || payload.schema !== 'atomicdb.map.v1' || !payload.root) {
      throw new Error('The server returned an unsupported map schema.');
    }
    state.data = payload;
    state.dataUrl = payloadUrl;
    state.apiRoot = requestedRoot || payload.request.root;
    const context = layout.payloadContext(payload, state.firstMoves);
    state.lineage = context.lineage;
    state.firstMoves = context.firstMoves;
    state.hierarchy = hierarchyFor(payload.root);
    const requestedFocus = state.requestedBranch || state.focusKey;
    state.focusKey = state.index.has(requestedFocus)
      ? requestedFocus : payload.root.key;
    const requestedSelection = state.requestedSelection || state.selectedKey;
    state.selectedKey = state.index.has(requestedSelection)
      ? requestedSelection : state.focusKey;
    reconcileSelection();
    state.requestedBranch = '';
    state.requestedSelection = '';
    renderFirstMoves();
    renderBreadcrumbs();
    renderChart();
    renderTextFallback();
    updateInspector(
      state.index.get(state.selectedKey) || state.hierarchy, false,
    );
    const generated = new Date(payload.snapshot.generated_at);
    elements.stamp.classList.add('live');
    const stampText = Number.isNaN(generated.getTime())
      ? 'Snapshot ready'
      : `Snapshot ${generated.toLocaleString()}`;
    elements.stamp.lastElementChild.textContent = stampText;
    if (d3) {
      hideStatus();
      showFilterFeedback(true);
    } else {
      showStatus(
        'The visual renderer is unavailable; the complete accessible map table is loaded below.',
        'error',
      );
    }
    updateUrl();
  }

  function errorMessage(response, body) {
    const serverMessage = body && body.error && (
      typeof body.error === 'string' ? body.error : body.error.message
    );
    if (response.status === 503) {
      return 'The first map snapshot is not ready yet. The solver and position explorer remain available.';
    }
    if (response.status === 404) {
      return 'That branch is not reachable from the current Atomic start position.';
    }
    if (response.status === 400) {
      return serverMessage || 'The requested map view is invalid.';
    }
    return serverMessage || `Map service unavailable (${response.status}).`;
  }

  async function loadMap(rootKey, reason) {
    if (state.loading) {
      if (state.abortController) state.abortController.abort();
    }
    state.loading = true;
    elements.stage.setAttribute('aria-busy', 'true');
    elements.svg.setAttribute('aria-busy', 'true');
    const controller = new AbortController();
    state.abortController = controller;
    const query = new URLSearchParams({
      root: rootKey || host.dataset.rootKey,
      weight: state.weight,
      limit: '600',
    });
    const url = `${host.dataset.api}?${query.toString()}`;
    const headers = {'Accept': 'application/json'};
    const etag = layout.etagForCurrentPayload(
      url, state.dataUrl, state.data, state.etags,
    );
    if (etag) headers['If-None-Match'] = etag;
    if (!state.data || reason !== 'refresh') {
      showStatus('Loading the latest solver snapshot…');
    }
    elements.refresh.disabled = true;
    try {
      const response = await window.fetch(url, {
        headers,
        credentials: 'same-origin',
        signal: controller.signal,
      });
      if (state.abortController !== controller) return;
      if (response.status === 304) {
        if (!layout.canReuseNotModified(url, state.dataUrl, state.data)) {
          throw new Error(
            'The map service returned an invalid cache response; the previous snapshot remains visible.',
          );
        }
        if (d3) {
          hideStatus();
          showFilterFeedback(true);
        } else {
          showStatus(
            'The visual renderer is unavailable; the complete accessible map table is loaded below.',
            'error',
          );
        }
        return;
      }
      const body = await response.json().catch(() => null);
      if (state.abortController !== controller) return;
      if (!response.ok) throw new Error(errorMessage(response, body));
      const responseEtag = response.headers.get('ETag');
      if (responseEtag) state.etags.set(url, responseEtag);
      applyPayload(body, rootKey, url);
    } catch (error) {
      if (state.abortController !== controller) return;
      if (error.name === 'AbortError') return;
      if (state.data) {
        showStatus(
          `Could not refresh the map; showing the previous snapshot. ${error.message}`,
          'error',
        );
      } else {
        showStatus(error.message || 'Unable to load the map.', 'error');
        elements.stage.setAttribute('aria-busy', 'false');
        elements.textRows.replaceChildren();
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 5;
        cell.textContent = 'No map snapshot is available. Try Refresh or use the AtomicDB overview.';
        row.appendChild(cell);
        elements.textRows.appendChild(row);
      }
    } finally {
      if (state.abortController === controller) {
        state.loading = false;
        state.abortController = null;
        elements.refresh.disabled = false;
        elements.stage.setAttribute('aria-busy', 'false');
        elements.svg.setAttribute('aria-busy', 'false');
      }
    }
  }

  function handleFilter() {
    state.filters.status = elements.statusFilter.value;
    state.filters.closure = elements.closureFilter.value;
    state.filters.work = elements.workFilter.value;
    state.filters.trust = elements.trustFilter.value;
    const selected = reconcileSelection();
    renderChart();
    renderTextFallback();
    if (selected) updateInspector(selected, false);
    updateUrl();
    showFilterFeedback();
  }

  document.querySelectorAll('input[name="weight"]').forEach((radio) => {
    radio.addEventListener('change', () => {
      if (!radio.checked || !allowedWeights.has(radio.value)) return;
      state.weight = radio.value;
      state.requestedBranch = state.focusKey;
      state.requestedSelection = state.selectedKey;
      loadMap(state.apiRoot, 'weight');
    });
  });
  elements.statusFilter.addEventListener('change', handleFilter);
  elements.closureFilter.addEventListener('change', handleFilter);
  elements.workFilter.addEventListener('change', handleFilter);
  elements.trustFilter.addEventListener('change', handleFilter);
  elements.refresh.addEventListener('click', () => loadMap(state.apiRoot, 'refresh'));
  elements.zoomSelected.addEventListener('click', () => {
    zoomTo(state.index.get(state.selectedKey));
  });
  elements.svg.addEventListener('click', () => {
    selectNode(focusNode(), false);
  });

  window.addEventListener('popstate', () => {
    const current = new URLSearchParams(window.location.search);
    const nextWeight = current.get('weight') || 'frontier';
    state.weight = allowedWeights.has(nextWeight) ? nextWeight : 'frontier';
    state.filters.status = current.get('status') || 'all';
    const nextClosure = current.get('closure') || 'all';
    state.filters.closure = allowedClosures.has(nextClosure)
      ? nextClosure : 'all';
    state.filters.work = current.get('work') || 'all';
    state.filters.trust = current.get('trust') || 'all';
    state.requestedBranch = current.get('branch') || '';
    state.requestedSelection = current.get('selected') || '';
    setInputsFromState();
    loadMap(current.get('root') || host.dataset.rootKey, 'history');
  });

  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(() => {
      window.clearTimeout(state.resizeTimer);
      state.resizeTimer = window.setTimeout(() => {
        if (state.hierarchy) {
          const containerWidth = Math.max(
            320,
            elements.svgWrap.parentElement.clientWidth
              || elements.svgWrap.clientWidth
              || 800,
          );
          if (containerWidth === state.lastContainerWidth) return;
          resetChartFrame();
          state.hierarchy = hierarchyFor(state.data.root);
          renderChart();
        }
      }, 90);
    });
    observer.observe(elements.svgWrap);
  } else {
    window.addEventListener('resize', () => {
      window.clearTimeout(state.resizeTimer);
      state.resizeTimer = window.setTimeout(() => {
        if (!state.hierarchy) return;
        resetChartFrame();
        state.hierarchy = hierarchyFor(state.data.root);
        renderChart();
      }, 120);
    });
  }

  window.setInterval(() => {
    if (!document.hidden && state.data && !state.loading) {
      loadMap(state.apiRoot, 'refresh');
    }
  }, 60000);

  setInputsFromState();
  enhanceInteractionHelp();
  renderBoard(host.dataset.rootFen, 'start position');
  if (!d3) {
    showStatus(
      'The visual renderer did not load; fetching the accessible map table…',
      'error',
    );
  }
  loadMap(state.apiRoot, 'initial');
}());
