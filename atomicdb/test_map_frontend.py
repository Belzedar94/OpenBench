import hashlib
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import resolve, reverse

from . import views


class AtomicMoveTreePageTests(SimpleTestCase):

    def test_named_page_route_renders_without_reading_solver_database(self):
        self.assertEqual(reverse('atomicdb-map'), '/atomicdb/map/')
        # La ruta va envuelta en una cache corta de lectura; lo que importa es
        # que por debajo siga estando exactamente esta vista.
        routed = resolve('/atomicdb/map/').func
        self.assertIs(getattr(routed, '__wrapped__', routed),
                      views.conquest_map)

        response = self.client.get('/atomicdb/map/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Atomic move tree')
        self.assertContains(response, 'data-api="/atomicdb/api/map/v1"')
        self.assertContains(response, 'id="map-search"')
        self.assertContains(response, 'type="search"')
        self.assertContains(response, 'name="tree-mode"', count=3)
        self.assertContains(response, 'value="all"')
        self.assertContains(response, 'All branches')
        self.assertContains(response, 'value="unresolved"')
        self.assertContains(response, 'Unresolved')
        self.assertContains(response, 'value="active"')
        self.assertContains(response, 'Active now')
        self.assertContains(response, 'aria-label="Zoom in"')
        self.assertContains(response, 'aria-label="Zoom out"')
        self.assertContains(response, 'id="map-fit"')
        self.assertContains(response, 'Fit tree')
        self.assertContains(response, 'aria-label="How to use the move tree"')
        self.assertContains(response, 'id="map-active-work"')
        self.assertContains(response, 'Analyzing now')
        self.assertContains(response, 'id="map-inspector"')
        self.assertContains(response, 'id="inspector-title"')
        self.assertContains(response, 'id="inspector-line"')
        self.assertContains(response, 'id="inspector-opening"')
        self.assertContains(response, 'id="inspector-status"')
        self.assertContains(response, 'id="inspector-work"')
        self.assertContains(response, 'id="map-svg"')
        self.assertContains(response, 'aria-label="Interactive Atomic chess move tree"')
        self.assertContains(response, 'role="tree"')
        self.assertContains(response, 'aria-live="polite"')
        self.assertContains(response, 'atomicdb/conquest-map.css')
        self.assertContains(response, 'atomicdb/conquest-map.js')
        self.assertContains(response, 'atomicdb/vendor/d3/d3.v7.9.0.min.js')

    def test_page_does_not_render_retired_partition_ui(self):
        response = self.client.get('/atomicdb/map/')

        for retired_text in [
            'Conquest Map',
            'Project root',
            'Rectangle width',
            'Opening compass',
            'Accessible tree table',
            '>Trust<',
        ]:
            self.assertNotContains(response, retired_text, html=False)

        for retired_markup in [
            'id="first-move-strip"',
            'class="territory"',
            'class="map-legend"',
            'class="map-key-set"',
            'id="map-filter-frontier"',
            'id="map-filter-explored"',
            'id="map-filter-compute"',
            'id="map-pattern-',
            'class="eval-inset"',
            'class="map-fallback"',
            '<table',
        ]:
            self.assertNotContains(response, retired_markup, html=False)

    def test_atomicdb_navigation_names_the_product_not_the_old_chart(self):
        response = self.client.get('/atomicdb/map/')

        self.assertContains(response, 'href="/atomicdb/">Overview</a>')
        self.assertContains(
            response,
            'href="/atomicdb/map/">Move tree</a>',
        )


class AtomicMoveTreeStaticContractTests(SimpleTestCase):
    static_root = Path(settings.BASE_DIR) / 'atomicdb' / 'static' / 'atomicdb'

    def test_frontend_is_a_searchable_zoomable_accessible_node_link_tree(self):
        source = (self.static_root / 'conquest-map.js').read_text(
            encoding='utf-8',
        )

        for token in [
            "schema !== 'atomicdb.map.v1'",
            "'If-None-Match'",
            "response.status === 304",
            'd3.tree(',
            'd3.zoom(',
            'ResizeObserver',
            "'ArrowUp'",
            "'ArrowDown'",
            "'ArrowLeft'",
            "'ArrowRight'",
            "'Enter'",
            'aria-expanded',
            'treeitem',
            'history.replaceState',
            'document.hidden',
            'ensureBoard',
            'line_san',
            'line_uci',
            'node.opening',
            'work_items',
            'exact_state',
            'own_active',
            'own_queued',
            'FILTERED_DESKTOP_ITEMS',
            'FILTERED_NARROW_ITEMS',
            'FILTERED_DESKTOP_TARGETS',
            'FILTERED_NARROW_TARGETS',
            'lastViewportWidth',
            'is-selected-path',
            "loadMap(state.apiRoot, 'initial')",
        ]:
            self.assertIn(token, source)
        self.assertRegex(
            source,
            r'(?:event\.key\s*===\s*["\'] ["\']|case\s+["\'] ["\']|'
            r'Spacebar|event\.code\s*===\s*["\']Space["\'])',
        )
        self.assertRegex(
            source,
            r'(?:MAX_TREEITEMS|MAX_VISIBLE_NODES|MAX_RENDERED_NODES)\s*=\s*300',
        )
        self.assertNotIn("setAttribute('role', 'listitem')", source)
        self.assertNotIn("setAttribute('aria-pressed'", source)

    def test_frontend_has_explicit_modes_search_work_rail_and_inspector(self):
        source = (self.static_root / 'conquest-map.js').read_text(
            encoding='utf-8',
        )

        for token in [
            'map-search',
            'map-mode',
            'map-active-work',
            'map-inspector',
            'inspector-title',
            'inspector-line',
            'inspector-opening',
            'inspector-status',
            'inspector-work',
            'map-zoom-in',
            'map-zoom-out',
            'map-fit',
            'map-help-open',
            'map-help-dialog',
        ]:
            self.assertIn(token, source)

    def test_frontend_does_not_reintroduce_partition_encodings_or_duplicate_ui(self):
        source = (self.static_root / 'conquest-map.js').read_text(
            encoding='utf-8',
        )
        style = (self.static_root / 'conquest-map.css').read_text(
            encoding='utf-8',
        )
        combined = source + '\n' + style

        for retired_token in [
            'd3.partition',
            'withResidualTree',
            'densityPlan',
            'rect.territory',
            '.territory',
            'eval-inset',
            'map-eval-bar',
            'map-pattern-',
            'url(#map-pattern',
            'firstMoveStrip',
            'first-move-strip',
            'renderTextFallback',
            'map-fallback',
            'map-key-set',
            'map-key-command',
            'Accessible tree table',
            'Rectangle width',
            'Opening compass',
        ]:
            self.assertNotIn(retired_token, combined)

    def test_css_supports_themes_responsive_layout_and_accessibility(self):
        style = (self.static_root / 'conquest-map.css').read_text(
            encoding='utf-8',
        )

        for token in [
            'var(--',
            '.work-rail',
            '.tree-inspector',
            '.tree-node',
            '.tree-link',
        ]:
            self.assertIn(token, style)
        compact_style = ''.join(style.split())
        for token in [
            'text-overflow:ellipsis',
            'overflow-wrap:anywhere',
            '@media(prefers-reduced-motion:reduce)',
            '@media(forced-colors:active)',
        ]:
            self.assertIn(token, compact_style)
        self.assertRegex(compact_style, r'max-width:(?:1199|1200)px')
        self.assertRegex(compact_style, r'max-width:(?:719|720)px')

    def test_d3_is_pinned_vendored_and_licensed(self):
        vendor = self.static_root / 'vendor' / 'd3'
        script = vendor / 'd3.v7.9.0.min.js'

        self.assertTrue(script.is_file())
        self.assertTrue((vendor / 'LICENSE').is_file())
        self.assertTrue((vendor / 'UPSTREAM.md').is_file())
        # Git's Windows checkout may apply core.autocrlf to the two upstream
        # newlines. Pin the vendored content, not the platform checkout
        # convention; Linux production still receives the exact LF bytes.
        canonical_bytes = script.read_bytes().replace(b'\r\n', b'\n')
        self.assertEqual(
            hashlib.sha256(canonical_bytes).hexdigest(),
            'f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539',
        )
        self.assertIn(
            'https://d3js.org v7.9.0',
            script.read_text(encoding='utf-8')[:100],
        )
