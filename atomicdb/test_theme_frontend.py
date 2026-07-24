from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class AtomicDBThemePageTests(SimpleTestCase):

    def test_theme_bootstrap_precedes_styles_and_switch_is_accessible(self):
        response = self.client.get('/atomicdb/map/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="atomicdb-theme-color"')
        self.assertContains(response, 'atomicdb/atomicdb.css')
        self.assertContains(response, 'atomicdb/theme.js')
        self.assertContains(response, 'id="theme-toggle"')
        self.assertContains(response, 'role="switch"')
        self.assertContains(response, 'aria-checked="true"')
        self.assertContains(response, 'aria-labelledby="theme-toggle-label"')
        self.assertContains(response, 'id="theme-toggle-label">Dark mode</span>')

        html = response.content.decode('utf-8')
        self.assertLess(
            html.index('window.localStorage.getItem("atomicdb-theme")'),
            html.index('chessground.base.css'),
        )
        self.assertLess(
            html.index('</nav>'),
            html.index('id="theme-toggle"'),
        )
        self.assertNotIn('<style>', html)


class AtomicDBThemeStaticContractTests(SimpleTestCase):
    static_root = Path(settings.BASE_DIR) / 'atomicdb' / 'static' / 'atomicdb'

    def test_styles_define_both_themes_and_accessibility_fallbacks(self):
        style = (self.static_root / 'atomicdb.css').read_text(
            encoding='utf-8',
        )

        for token in [
            ':root {',
            'color-scheme: dark',
            'html[data-theme="light"]',
            'color-scheme: light',
            '@media (prefers-color-scheme: light)',
            'html:not([data-theme])',
            'html.theme-js .theme-toggle',
            '.theme-toggle:focus-visible',
            '@media (prefers-reduced-motion: reduce)',
            '@media (forced-colors: active)',
            '--fg: var(--ink)',
        ]:
            self.assertIn(token, style)

        # The board palette is deliberately invariant across page themes.
        self.assertEqual(style.count('--sq-l: #f0d9b5'), 1)
        self.assertEqual(style.count('--sq-d: #b58863'), 1)

    def test_controller_persists_syncs_and_follows_system_when_unset(self):
        source = (self.static_root / 'theme.js').read_text(encoding='utf-8')

        for token in [
            "const storageKey = 'atomicdb-theme'",
            "new Set(['light', 'dark'])",
            "window.localStorage.getItem(storageKey)",
            "window.localStorage.setItem(storageKey, theme)",
            "window.addEventListener('storage'",
            "systemPreference.addEventListener('change'",
            "toggle.setAttribute('aria-checked'",
            "root.style.colorScheme = theme",
            "root.classList.add('theme-ready')",
        ]:
            self.assertIn(token, source)
