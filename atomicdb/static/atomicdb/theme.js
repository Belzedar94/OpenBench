(function () {
  'use strict';

  const root = document.documentElement;
  const toggle = document.getElementById('theme-toggle');
  if (!toggle) return;

  const storageKey = 'atomicdb-theme';
  const themes = new Set(['light', 'dark']);
  const systemPreference = window.matchMedia
    ? window.matchMedia('(prefers-color-scheme: light)')
    : null;
  const themeColour = document.getElementById('atomicdb-theme-color');

  // The browser-chrome colour follows whichever palette the page ships. The
  // AtomicDB pages state nothing and keep their own canvas colours; OpenBench
  // declares its two on the meta tag.
  const chrome = {
    light: (themeColour && themeColour.dataset.themeLight) || '#f5f2ea',
    dark: (themeColour && themeColour.dataset.themeDark) || '#161512',
  };

  function readStoredTheme() {
    try {
      const value = window.localStorage.getItem(storageKey);
      return themes.has(value) ? value : null;
    } catch (_error) {
      return null;
    }
  }

  function systemTheme() {
    return systemPreference && systemPreference.matches ? 'light' : 'dark';
  }

  let explicitPreference = readStoredTheme() !== null;

  function render(theme) {
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    toggle.setAttribute('aria-checked', theme === 'dark' ? 'true' : 'false');
    toggle.title = theme === 'dark'
      ? 'Switch to light mode'
      : 'Switch to dark mode';
    if (themeColour) {
      themeColour.content = theme === 'light' ? chrome.light : chrome.dark;
    }
  }

  function persist(theme) {
    explicitPreference = true;
    try {
      window.localStorage.setItem(storageKey, theme);
    } catch (_error) {
      // Storage can be disabled. The current page still changes theme.
    }
  }

  const initialTheme = themes.has(root.dataset.theme)
    ? root.dataset.theme
    : (readStoredTheme() || systemTheme());
  render(initialTheme);

  toggle.addEventListener('click', function () {
    const nextTheme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    persist(nextTheme);
    render(nextTheme);
  });

  window.addEventListener('storage', function (event) {
    if (event.key !== storageKey && event.key !== null) return;
    if (themes.has(event.newValue)) {
      explicitPreference = true;
      render(event.newValue);
    } else if (event.newValue === null) {
      explicitPreference = false;
      render(systemTheme());
    }
  });

  function followSystemPreference() {
    if (!explicitPreference) render(systemTheme());
  }

  if (systemPreference) {
    if (systemPreference.addEventListener) {
      systemPreference.addEventListener('change', followSystemPreference);
    } else if (systemPreference.addListener) {
      systemPreference.addListener(followSystemPreference);
    }
  }

  window.requestAnimationFrame(function () {
    window.requestAnimationFrame(function () {
      root.classList.add('theme-ready');
    });
  });
}());
