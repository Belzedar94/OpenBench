import os
import re
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.template import loader
from django.test import SimpleTestCase

from OpenBench import config as openbench_config


class OpenBenchThemeTemplateTests(SimpleTestCase):
    template_root = Path(settings.BASE_DIR) / 'Templates' / 'OpenBench'
    base_path = template_root / 'base.html'

    def test_bootstrap_precedes_styles_and_reuses_atomicdb_controller(self):
        source = self.base_path.read_text(encoding='utf-8')

        for token in [
            'id="atomicdb-theme-color"',
            'window.localStorage.getItem("atomicdb-theme")',
            '(prefers-color-scheme: light)',
            "atomicdb/theme.js",
            'id="theme-toggle"',
            'role="switch"',
            'aria-checked="true"',
            'aria-labelledby="theme-toggle-label"',
            'id="theme-toggle-label">Dark mode</span>',
        ]:
            self.assertIn(token, source)

        self.assertLess(
            source.index('window.localStorage.getItem("atomicdb-theme")'),
            source.index("{% static 'style.css' %}"),
        )

        # The switch lives in the header row and is pinned to the top-right
        # corner from the stylesheet. It follows the title in the markup so
        # the reading and tab order stay title-then-controls.
        self.assertLess(
            source.index('<h2>'),
            source.index('id="theme-toggle"'),
        )
        self.assertLess(
            source.index('id="theme-toggle"'),
            source.index('{% if request.session.error_message %}'),
        )

    def test_browser_chrome_colour_is_declared_by_the_page(self):
        source = self.base_path.read_text(encoding='utf-8')

        # theme.js is shared with AtomicDB, which has a different canvas. Each
        # page states its own pair rather than inheriting AtomicDB's.
        self.assertIn('data-theme-dark="#222222"', source)
        self.assertIn('data-theme-light="#ffffff"', source)
        self.assertIn('meta.dataset.themeLight', source)
        self.assertIn('meta.dataset.themeDark', source)

    def test_every_openbench_base_consumer_compiles(self):
        consumers = []
        for path in self.template_root.glob('*.html'):
            source = path.read_text(encoding='utf-8')
            if '{% extends "OpenBench/base.html" %}' in source:
                consumers.append(path.name)

        self.assertGreaterEqual(len(consumers), 17)
        for name in consumers:
            with self.subTest(template=name):
                loader.get_template('OpenBench/{}'.format(name))

        for name in [
            'index.html',       # /index/ and /greens/
            'workload.html',    # /test/, /tune/, and /datagen/
            'machines.html',
            'machine.html',
            'users.html',
            'regression_index.html',
            'regression_engine.html',
        ]:
            self.assertIn(name, consumers)

    def test_base_consumers_have_no_inline_theme_colours(self):
        colour_pattern = re.compile(
            r'style="[^"]*(?:color|background)|'
            r'color:\s*(?:red|green|yellow|blue|black|white|grey|orange)',
            re.IGNORECASE,
        )

        for path in self.template_root.rglob('*.html'):
            source = path.read_text(encoding='utf-8')
            if (
                'OpenBench/base.html' in source
                or path.parent.name == 'Blocks'
            ):
                with self.subTest(template=str(path.relative_to(
                        self.template_root))):
                    self.assertIsNone(colour_pattern.search(source))


class OpenBenchThemeStaticContractTests(SimpleTestCase):
    openbench_static = Path(settings.BASE_DIR) / 'OpenBench' / 'static'
    atomicdb_static = (
        Path(settings.BASE_DIR) / 'atomicdb' / 'static' / 'atomicdb'
    )

    # Every value the original OpenBench skin painted with, taken from
    # OpenBench/static/style.css at bbc5783 (the last commit before the theme
    # work). The dark palette is not a design surface: it is that stylesheet,
    # expressed as variables so a second palette can be swapped in. A change
    # here means the dark page stopped looking like OpenBench.
    ORIGINAL_DARK = {
        'color1': '#222222',
        'color2': '#2D2D2D',
        'color3': '#3B3B3B',
        'color-font1': '#DBDEE1',
        'color-font2': '#949BA4',
        'color-font3': '#008F11',
        'link': '#8BBAD6',
        'link-hover': '#8BBAD6',
        'sidebar-hover': '#303030',
        'stripes-th-bg': 'black',
        'table-header-bg': '#171717',
        'row-hover': '#323232',
        'td-border': '#444444',
        'button-border': 'transparent',
        'button-blue': '#1C2D40',
        'button-blue-hover': '#2E3F57',
        'button-start': '#384A5F',
        'button-start-hover': '#2E3F57',
        'button-preset': '#046c52',
        'button-preset-hover': '#11a983',
        'button-yellow': '#7A4E09',
        'button-yellow-hover': '#9B6B0F',
        'button-red': '#74261E',
        'button-red-hover': '#8E3D28',
        'statblock-bg': '#CCCCCC',
        'statblock-ink': 'black',
        'statblock-green': '#76D576',
        'statblock-blue': '#60F0FF',
        'statblock-yellow': '#EFF891',
        'statblock-red': '#FA9696',
        'redlink': '#FF4D4D',
        'error-ink': 'red',
        'warning-ink': 'yellow',
        'success-ink': 'green',
        'was-default-network': '#EFF891',
        # These four replaced inline styles that carried the same literals.
        'marker-thread-odds': 'orange',
        'icon-muted': 'grey',
        'icon-destructive': '#A03030',
        'atomicdb-link-ink': '#e8a33d',
    }

    # Sampled from tests.stockfishchess.org (Bootstrap 5.3 with a green link
    # colour) so a fishtest regular recognises the page.
    FISHTEST_LIGHT = {
        'color1': '#ffffff',            # --bs-body-bg
        'color2': '#f2f2f2',            # --bs-table-striped-bg over white
        'color3': '#e9ecef',            # --bs-gray-200
        'color-font1': '#212529',       # --bs-body-color
        'color-font2': '#343a40',       # --bs-gray-800
        'color-font3': '#008002',       # --bs-link-color
        'link': '#008002',
        'link-hover': '#5f9960',        # --bs-link-hover-color
        'sidebar-bg': '#dee2e6',        # .mainnavbar / --bs-gray-300
        'sidebar-hover': '#f8f9fa',     # .links-link:hover / --bs-light
        'td-border': '#dee2e6',         # --bs-border-color
        'statblock-bg': '#f5f5f5',      # .elo-results
        'statblock-green': '#44EB44',   # fishtest .results-pre passed (measured live)
        'statblock-blue': '#66CCFF',    # fishtest special blue (measured live)
        'statblock-yellow': '#FFFF00',  # fishtest yellow (measured live)
        'statblock-red': '#FF6A6A',     # fishtest .results-pre failed (measured live)
        'card-bg': '#ffffff',
        'card-border': '#dee2e6',
    }

    @staticmethod
    def _variables_for_selector(source, selector):
        start = source.index(selector)
        block_start = source.index('{', start) + 1
        block_end = source.index('}', block_start)
        return dict(re.findall(
            r'--([a-z0-9-]+)\s*:\s*([^;]+);',
            source[block_start:block_end],
            re.IGNORECASE,
        ))

    def _style(self):
        return (self.openbench_static / 'style.css').read_text(
            encoding='utf-8')

    def test_dark_is_the_original_openbench_palette(self):
        variables = self._variables_for_selector(self._style(), ':root {')

        for token, expected in self.ORIGINAL_DARK.items():
            with self.subTest(token=token):
                self.assertEqual(variables[token].strip(), expected)

    def test_light_is_the_fishtest_palette(self):
        variables = self._variables_for_selector(
            self._style(), 'html[data-theme="light"]')

        for token, expected in self.FISHTEST_LIGHT.items():
            with self.subTest(token=token):
                self.assertEqual(variables[token].strip(), expected)

    def test_the_no_script_light_palette_cannot_drift(self):
        style = self._style()

        explicit = self._variables_for_selector(
            style, 'html[data-theme="light"]')
        preferred = self._variables_for_selector(
            style, 'html:not([data-theme])')

        self.assertEqual(explicit, preferred)

    def test_openbench_no_longer_borrows_the_atomicdb_palette(self):
        # OpenBench once redefined AtomicDB's token names with AtomicDB's warm
        # values, which is how the dark page stopped looking like OpenBench.
        # The two apps render from separate base templates and must not share
        # a palette; only the storage key and the controller are shared.
        style = self._style()
        atomicdb = (self.atomicdb_static / 'atomicdb.css').read_text(
            encoding='utf-8')

        openbench_tokens = set(
            self._variables_for_selector(style, ':root {'))
        atomicdb_tokens = set(
            self._variables_for_selector(atomicdb, ':root {'))

        self.assertEqual(openbench_tokens & atomicdb_tokens, set())

        for stylesheet in ['style.css', 'form.css', 'paging.css']:
            source = (self.openbench_static / stylesheet).read_text(
                encoding='utf-8')
            for token in sorted(atomicdb_tokens):
                with self.subTest(stylesheet=stylesheet, token=token):
                    self.assertNotIn('var(--%s)' % token, source)

    def test_the_switch_is_pinned_to_the_top_right_corner(self):
        style = self._style()

        toggle = style[style.index('.theme-toggle {'):]
        toggle = toggle[:toggle.index('}')]
        for declaration in ['position: fixed', 'top: 10px', 'right: 16px']:
            self.assertIn(declaration, toggle)

        # ... and on a phone it joins the header row against the same edge,
        # because a fixed icon would land on top of the title there.
        mobile = style[style.index('@media (max-width: 767px)'):]
        self.assertIn('#content-header .theme-toggle', mobile)
        self.assertIn('margin-left: auto', mobile)

    def test_styles_cover_theme_status_forms_and_accessibility(self):
        style = self._style()
        form = (self.openbench_static / 'form.css').read_text(
            encoding='utf-8')
        summary = (
            Path(settings.BASE_DIR)
            / 'Templates'
            / 'OpenBench'
            / 'Blocks'
            / 'testsummary.html'
        ).read_text(encoding='utf-8')

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
            'background-color: var(--statblock-green)',
            'background-color: var(--statblock-blue)',
            'background-color: var(--statblock-yellow)',
            'background-color: var(--statblock-red)',
        ]:
            self.assertIn(token, style)

        for token in [
            'background-color: var(--input-bg)',
            'border-color: var(--input-focus-border)',
            'box-shadow: var(--input-focus-shadow)',
        ]:
            self.assertIn(token, form)

        for marker in [
            'marker-error',
            'marker-fischer',
            'marker-time-odds',
            'marker-thread-odds',
        ]:
            self.assertIn(marker, summary)
        self.assertNotIn('style="color:', summary)

    def test_unstyled_content_text_is_readable_in_both_themes(self):
        # /regression/ headings carry no colour of their own. Left to the
        # initial value they came out black on #222222.
        style = self._style()

        content = style[style.index('#content {'):]
        content = content[:content.index('}')]
        self.assertIn('color: var(--color-font1)', content)

    def test_every_linked_asset_carries_the_cache_busting_token(self):
        source = (
            Path(settings.BASE_DIR) / 'Templates' / 'OpenBench' / 'base.html'
        ).read_text(encoding='utf-8')

        # A stylesheet or script linked without the token keeps its URL across
        # deploys, so browsers and proxies go on serving the cached copy. That
        # is how the theme switch first shipped with new markup and old CSS.
        for asset in ['style.css', 'base.css', 'form.css', 'paging.css',
                      'atomicdb/theme.js']:
            with self.subTest(asset=asset):
                self.assertIn(
                    "{%% static '%s' %%}?{{ static_version }}" % asset, source)

    def test_shared_controller_persists_and_follows_system(self):
        source = (self.atomicdb_static / 'theme.js').read_text(
            encoding='utf-8',
        )

        for token in [
            "const storageKey = 'atomicdb-theme'",
            "window.localStorage.getItem(storageKey)",
            "window.localStorage.setItem(storageKey, theme)",
            "window.addEventListener('storage'",
            "systemPreference.addEventListener('change'",
            "toggle.setAttribute('aria-checked'",
            "themeColour.dataset.themeLight",
            "themeColour.dataset.themeDark",
        ]:
            self.assertIn(token, source)


class OpenBenchStaticVersionTests(SimpleTestCase):

    def test_token_tracks_the_assets_instead_of_a_manual_constant(self):

        with tempfile.TemporaryDirectory() as folder:
            asset = os.path.join(folder, 'style.css')

            with open(asset, 'w', encoding='utf-8') as handle:
                handle.write('body { color: red; }')

            original = openbench_config.OPENBENCH_STATIC_ASSETS
            openbench_config.OPENBENCH_STATIC_ASSETS = [folder]

            try:
                before = openbench_config.compute_static_version()
                self.assertEqual(
                    before, openbench_config.compute_static_version())

                # mtime has one-second resolution in the digest
                time.sleep(1.05)
                with open(asset, 'w', encoding='utf-8') as handle:
                    handle.write('body { color: blue; }\n/* longer now */')

                after = openbench_config.compute_static_version()

            finally:
                openbench_config.OPENBENCH_STATIC_ASSETS = original

        self.assertNotEqual(before, after)
        self.assertTrue(before.startswith('v'))
        self.assertTrue(after.startswith('v'))

    def test_token_falls_back_when_assets_are_unreadable(self):

        original = openbench_config.OPENBENCH_STATIC_ASSETS
        openbench_config.OPENBENCH_STATIC_ASSETS = [
            os.path.join('there', 'is', 'no', 'such', 'asset.css')]

        try:
            self.assertEqual(
                openbench_config.compute_static_version(base='v9'), 'v9')
        finally:
            openbench_config.OPENBENCH_STATIC_ASSETS = original
