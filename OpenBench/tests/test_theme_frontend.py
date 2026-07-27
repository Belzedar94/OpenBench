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
        self.assertLess(
            source.index('<h2>'),
            source.index('id="theme-toggle"'),
        )

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

    def test_openbench_uses_atomicdb_palette_in_both_themes(self):
        openbench = (self.openbench_static / 'style.css').read_text(
            encoding='utf-8',
        )
        atomicdb = (self.atomicdb_static / 'atomicdb.css').read_text(
            encoding='utf-8',
        )
        shared_tokens = [
            'canvas',
            'surface-1',
            'surface-2',
            'surface-hover',
            'ink',
            'ink-strong',
            'ink-muted',
            'border',
            'border-strong',
            'accent',
            'accent-hover',
            'accent-contrast',
            'focus-ring',
            'won',
            'lost',
            'draw',
            'hot',
            'warm',
            'cold',
        ]

        for selector in [':root {', 'html[data-theme="light"]']:
            expected = self._variables_for_selector(atomicdb, selector)
            actual = self._variables_for_selector(openbench, selector)
            for token in shared_tokens:
                with self.subTest(selector=selector, token=token):
                    self.assertEqual(actual[token], expected[token])

    def test_styles_cover_theme_status_forms_code_and_accessibility(self):
        style = (self.openbench_static / 'style.css').read_text(
            encoding='utf-8',
        )
        form = (self.openbench_static / 'form.css').read_text(
            encoding='utf-8',
        )
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
            '--status-green-surface',
            '--status-blue-surface',
            '--status-yellow-surface',
            '--status-red-surface',
            'background-color: var(--status-green-surface)',
            'background-color: var(--status-blue-surface)',
            'background-color: var(--status-yellow-surface)',
            'background-color: var(--status-red-surface)',
            '#content code',
        ]:
            self.assertIn(token, style)

        for token in [
            'background-color: var(--surface-1)',
            'border-color: var(--focus-ring)',
            'box-shadow: 0 0 0 2px var(--focus-shadow)',
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
