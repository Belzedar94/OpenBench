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

    def test_switch_is_the_same_bare_icon_openbench_uses(self):
        # One control across the two apps: two sibling SVGs, the one for the
        # theme a click would switch *to* being the visible one. The sliding
        # track/thumb pill it replaced is gone.
        html = self.client.get('/atomicdb/map/').content.decode('utf-8')

        self.assertIn('class="theme-toggle-sun"', html)
        self.assertIn('class="theme-toggle-moon"', html)

        for gone in [
            'theme-toggle-track',
            'theme-toggle-thumb',
            'theme-toggle-icon',
        ]:
            self.assertNotIn(gone, html)

    def test_every_atomicdb_page_takes_the_switch_from_the_base(self):
        # The rendered check above covers one page; every other page inherits
        # the same header, so none can be left holding the old control.
        template_root = (
            Path(settings.BASE_DIR) / 'atomicdb' / 'templates' / 'atomicdb'
        )
        base = template_root / 'base.html'

        self.assertIn('id="theme-toggle"', base.read_text(encoding='utf-8'))

        pages = [
            path for path in template_root.glob('*.html')
            if path.name not in ('base.html', '_board.html')
        ]
        self.assertGreaterEqual(len(pages), 5)

        for path in pages:
            source = path.read_text(encoding='utf-8')
            with self.subTest(template=path.name):
                self.assertIn('{% extends "atomicdb/base.html" %}', source)
                self.assertNotIn('theme-toggle', source)


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

    def test_switch_sits_in_the_corner_and_leaves_the_header_bar_alone(self):
        style = (self.static_root / 'atomicdb.css').read_text(
            encoding='utf-8',
        )

        toggle = style[style.index('.theme-toggle {'):]
        toggle = toggle[:toggle.index('}')]
        for declaration in [
            'position: fixed',
            'right: 16px',
            'top: calc((var(--header-height) - 2rem) / 2)',
        ]:
            self.assertIn(declaration, toggle)

        # Out of flow, so the bar keeps the height the old in-row control gave
        # it. Held from an item rather than as a min-height on the bar: the
        # browser rounds each padding edge separately, and a calc on the bar
        # lands one layout unit off, which shifts hairlines further down the
        # page by a pixel.
        self.assertIn('--header-row: 2.75rem', style)
        self.assertIn('min-height: var(--header-row)', style)

        # Scoped to the page bar. The bare `header h1` rules in this file also
        # match the map page's <header class="tree-hero">, where a min-height
        # would push the whole move tree down.
        self.assertIn('body > header > h1 {', style)

        # On a phone it rejoins the row against the same edge.
        mobile = style[style.index('@media (max-width: 560px)'):]
        self.assertIn('header .theme-toggle', mobile)
        self.assertIn('position: static', mobile)
        self.assertIn('margin-left: auto', mobile)

        # Nothing is left of the pill.
        for gone in [
            '.theme-toggle-track',
            '.theme-toggle-thumb',
            '.theme-toggle-icon',
        ]:
            self.assertNotIn(gone, style)

    def test_switch_is_painted_from_the_atomicdb_palette(self):
        # The control is shared with OpenBench; the colours are not.
        style = (self.static_root / 'atomicdb.css').read_text(
            encoding='utf-8',
        )

        toggle = style[style.index('.theme-toggle {'):
                       style.index('.theme-toggle:focus-visible')]
        self.assertIn('color: var(--ink-muted)', toggle)
        self.assertIn('color: var(--accent)', toggle)

        for openbench_token in [
            '--color-font1', '--color-font2', '--color-font3',
            '--toggle-ink', '--toggle-ink-hover', '--toggle-focus-ring',
        ]:
            self.assertNotIn(openbench_token, style)

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
