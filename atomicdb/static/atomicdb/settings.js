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

  window.addEventListener('storage', function (event) {
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
