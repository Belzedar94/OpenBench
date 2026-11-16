// Settings panel behind the cog in the header.
//
// Theme and move notation live in this browser (localStorage) and work for
// everybody; the queue option and the link to the notification settings are
// server-rendered for signed-in accounts only. The panel opens and closes
// like the bell's: click, Escape, or a click anywhere else.
//
// The theme goes through the corner switch (theme.js), so storage, the
// browser-chrome colour and the other tabs keep following one mechanism.
// The notation swap itself is the inline script at the end of base.html,
// which runs before this file and is what keeps a UCI page from showing SAN
// first.
(function () {
  'use strict';

  const root = document.documentElement;
  const wrap = document.getElementById('settings-wrap');
  const cog = document.getElementById('settings-cog');
  const panel = document.getElementById('settings-panel');
  if (!wrap || !cog || !panel) return;

  const notationKey = 'atomicdb-notation';
  const themeInputs = panel.querySelectorAll('input[name="settings-theme"]');
  const notationInputs = panel.querySelectorAll(
    'input[name="settings-notation"]');
  const evalKey = 'atomicdb-eval';
  const evalInputs = panel.querySelectorAll('input[name="settings-eval"]');

  function check(inputs, value) {
    inputs.forEach(function (input) {
      input.checked = input.value === value;
    });
  }

  function currentNotation() {
    return root.dataset.notation === 'uci' ? 'uci' : 'san';
  }

  function sync() {
    check(themeInputs, root.dataset.theme === 'light' ? 'light' : 'dark');
    check(notationInputs, currentNotation());
    check(evalInputs, root.dataset.eval === 'white' ? 'white' : 'stm');
  }

  function open() {
    return !panel.hidden;
  }

  function show(visible) {
    if (visible) sync();
    panel.hidden = !visible;
    cog.setAttribute('aria-expanded', visible ? 'true' : 'false');
  }

  cog.addEventListener('click', function () {
    show(!open());
  });

  themeInputs.forEach(function (input) {
    input.addEventListener('change', function () {
      if (!input.checked || root.dataset.theme === input.value) return;
      const toggle = document.getElementById('theme-toggle');
      if (toggle) {
        toggle.click();
        return;
      }
      root.dataset.theme = input.value;
      root.style.colorScheme = input.value;
      try {
        window.localStorage.setItem('atomicdb-theme', input.value);
      } catch (_error) {
        // Storage can be disabled. The current page still changes theme.
      }
    });
  });

  function applyNotation(notation) {
    if (window.atomicdbNotation) {
      window.atomicdbNotation(notation);
    } else {
      root.dataset.notation = notation;
    }
  }

  notationInputs.forEach(function (input) {
    input.addEventListener('change', function () {
      if (!input.checked) return;
      applyNotation(input.value);
      try {
        if (input.value === 'uci') {
          window.localStorage.setItem(notationKey, 'uci');
        } else {
          window.localStorage.removeItem(notationKey);
        }
      } catch (_error) {
        // Without storage the choice holds for this page only.
      }
    });
  });

  function applyEval(mode) {
    if (window.atomicdbEval) window.atomicdbEval(mode);
  }

  evalInputs.forEach(function (input) {
    input.addEventListener('change', function () {
      if (!input.checked) return;
      applyEval(input.value);
      try {
        if (input.value === 'white') {
          window.localStorage.setItem(evalKey, 'white');
        } else {
          window.localStorage.removeItem(evalKey);
        }
      } catch (_error) {
        // Without storage the choice holds for this page only.
      }
    });
  });

  // Board colours: two pickers, kept in this browser.  The inline script in
  // base.html applies the stored pair before the first paint; this keeps the
  // page and the stored value in step while the pickers move.
  const boardKey = 'atomicdb-board';
  const boardLight = document.getElementById('settings-board-l');
  const boardDark = document.getElementById('settings-board-d');
  const boardReset = document.getElementById('settings-board-reset');
  const BOARD_DEFAULT = {l: '#f0d9b5', d: '#b58863'};

  function applyBoard(colours) {
    if (colours) {
      root.style.setProperty('--sq-l', colours.l);
      root.style.setProperty('--sq-d', colours.d);
      root.dataset.board = 'custom';
    } else {
      root.style.removeProperty('--sq-l');
      root.style.removeProperty('--sq-d');
      delete root.dataset.board;
    }
  }

  function storedBoard() {
    try {
      const value = JSON.parse(window.localStorage.getItem(boardKey) || 'null');
      const hex = /^#[0-9a-f]{6}$/i;
      if (value && hex.test(value.l) && hex.test(value.d)) return value;
    } catch (_error) {
      // Unreadable or no storage: the default board.
    }
    return null;
  }

  function syncBoard() {
    const colours = storedBoard() || BOARD_DEFAULT;
    if (boardLight) boardLight.value = colours.l;
    if (boardDark) boardDark.value = colours.d;
  }

  function saveBoard() {
    const colours = {l: boardLight.value, d: boardDark.value};
    applyBoard(colours);
    try {
      window.localStorage.setItem(boardKey, JSON.stringify(colours));
    } catch (_error) {
      // Without storage the colours hold for this page only.
    }
  }

  if (boardLight && boardDark) {
    syncBoard();
    boardLight.addEventListener('input', saveBoard);
    boardDark.addEventListener('input', saveBoard);
  }

  // The best-move arrow's colour (cz_eclipsia, 16-Nov-2026), kept in this
  // browser.  board.js reads the same key when it draws the board and
  // listens for 'atomicdb-arrow' to repaint the arrow while the picker moves.
  const arrowKey = 'atomicdb-arrow';
  const arrowPicker = document.getElementById('settings-board-arrow');
  const ARROW_DEFAULT = '#15781b';

  function storedArrow() {
    try {
      const value = window.localStorage.getItem(arrowKey) || '';
      if (/^#[0-9a-f]{6}$/i.test(value)) return value;
    } catch (_error) {
      // Unreadable or no storage: the default arrow.
    }
    return null;
  }

  function announceArrow(colour) {
    window.dispatchEvent(new CustomEvent('atomicdb-arrow',
      {detail: colour || ARROW_DEFAULT}));
  }

  if (arrowPicker) {
    arrowPicker.value = storedArrow() || ARROW_DEFAULT;
    arrowPicker.addEventListener('input', function () {
      try {
        window.localStorage.setItem(arrowKey, arrowPicker.value);
      } catch (_error) {
        // Without storage the colour holds for this page only.
      }
      announceArrow(arrowPicker.value);
    });
  }

  if (boardReset) {
    boardReset.addEventListener('click', function () {
      applyBoard(null);
      try {
        window.localStorage.removeItem(boardKey);
        window.localStorage.removeItem(arrowKey);
      } catch (_error) {
        // Nothing stored to forget.
      }
      syncBoard();
      if (arrowPicker) arrowPicker.value = ARROW_DEFAULT;
      announceArrow(null);
    });
  }

  window.addEventListener('storage', function (event) {
    if (event.key === boardKey) {
      applyBoard(storedBoard());
      syncBoard();
      return;
    }
    if (event.key === arrowKey) {
      const colour = storedArrow();
      if (arrowPicker) arrowPicker.value = colour || ARROW_DEFAULT;
      announceArrow(colour);
      return;
    }
    if (event.key === evalKey) {
      applyEval(event.newValue === 'white' ? 'white' : 'stm');
      if (open()) sync();
      return;
    }
    if (event.key !== notationKey && event.key !== null) return;
    applyNotation(event.newValue === 'uci' ? 'uci' : 'san');
    if (open()) sync();
  });

  document.addEventListener('click', function (event) {
    if (open() && !wrap.contains(event.target)) show(false);
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && open()) {
      show(false);
      cog.focus();
    }
  });
}());
