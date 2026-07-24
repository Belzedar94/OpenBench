import hashlib
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import resolve, reverse

from . import views


class ConquestMapPageTests(SimpleTestCase):

    def test_named_page_route_renders_without_reading_solver_database(self):
        self.assertEqual(reverse('atomicdb-map'), '/atomicdb/map/')
        self.assertIs(resolve('/atomicdb/map/').func, views.conquest_map)

        response = self.client.get('/atomicdb/map/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Conquest Map')
        self.assertContains(response, 'data-api="/atomicdb/api/map/v1"')
        self.assertContains(response, 'id="first-move-strip"')
        self.assertContains(response, 'id="map-filter-closure"')
        self.assertContains(response, '<option value="MATE_PV">Mate PV</option>')
        self.assertContains(response, '<option value="NONE">No closure</option>')
        self.assertContains(response, 'id="map-svg"')
        self.assertContains(response, 'id="inspector-opening"')
        self.assertContains(response, 'id="inspector-opening-name"')
        self.assertContains(response, 'role="tree"')
        self.assertContains(response, 'Accessible tree table')
        self.assertContains(response, 'aria-label="Keyboard navigation"')
        self.assertContains(response, 'class="map-key-command"', count=5)
        self.assertContains(response, 'class="map-table-line"')
        self.assertContains(response, 'class="map-table-fact"')
        self.assertContains(response, 'class="strip-scroll-cue"')
        self.assertContains(response, 'id="map-pattern-unknown"')
        self.assertContains(response, 'id="map-pattern-white-win"')
        self.assertContains(response, 'id="map-pattern-black-win"')
        self.assertContains(response, 'id="map-pattern-draw"')
        self.assertContains(response, 'atomicdb/conquest-map.css')
        self.assertContains(response, 'atomicdb/conquest-map.js')
        self.assertContains(response, 'atomicdb/vendor/d3/d3.v7.9.0.min.js')

    def test_atomicdb_navigation_distinguishes_overview_and_map(self):
        response = self.client.get('/atomicdb/map/')

        self.assertContains(response, 'href="/atomicdb/">Overview</a>')
        self.assertContains(
            response, 'href="/atomicdb/map/">Conquest Map</a>',
        )


class ConquestMapStaticContractTests(SimpleTestCase):
    static_root = Path(settings.BASE_DIR) / 'atomicdb' / 'static' / 'atomicdb'

    def test_frontend_has_interaction_accessibility_and_fallback_contracts(self):
        source = (self.static_root / 'conquest-map.js').read_text(
            encoding='utf-8',
        )
        style = (self.static_root / 'conquest-map.css').read_text(
            encoding='utf-8',
        )

        for token in [
            "schema !== 'atomicdb.map.v1'",
            "weight: state.weight",
            "'If-None-Match'",
            "response.status === 304",
            "'ArrowUp'",
            "'ArrowDown'",
            "'ArrowLeft'",
            "'ArrowRight'",
            "'Escape'",
            "'Enter'",
            'line_san',
            'line_uci',
            'node.opening',
            'inspectorOpeningName',
            "searchParams.set('play'",
            'document.hidden',
            'ResizeObserver',
            'history.replaceState',
            'renderTextFallback',
            'renderBoard',
            'isWithinBranch',
            'withResidualTree',
            'payloadContext',
            'lineagePrefix',
            'lineageParent',
            'closureOf(node)',
            'state.filters.closure',
            'elements.closureFilter',
            'etagForCurrentPayload',
            'canReuseNotModified',
            'state.abortController === controller',
            "loadMap(state.apiRoot, 'initial')",
        ]:
            self.assertIn(token, source)
        # The initial fetch is unconditional: without D3 the accessible table
        # must still receive the versioned API payload.
        self.assertNotIn(
            "} else {\n    loadMap(state.apiRoot, 'initial');",
            source,
        )
        self.assertIn('@media(prefers-reduced-motion:reduce)', style)
        self.assertIn('@media(forced-colors:active)', style)
        self.assertIn('@media(max-width:520px)', style)

    def test_frontend_css_guards_dense_and_long_content_at_all_breakpoints(self):
        style = (self.static_root / 'conquest-map.css').read_text(
            encoding='utf-8',
        )

        for token in [
            '.snapshot-stamp > span:last-child',
            '.strip-scroll-cue',
            '.map-stage{',
            'align-items:start',
            'fill:url(#map-pattern-white-win)',
            'text-overflow:ellipsis',
            '.map-key-command',
            '.map-key-set',
            'overflow-wrap:anywhere',
            '.opening-line',
            '.inspector-opening',
            'max-height:5.05rem',
            '.map-fallback table',
            'min-width:43rem',
            '@media(max-width:960px)',
            '@media(max-width:640px)',
            '@media(max-width:360px)',
            '.inspector-metrics{grid-template-columns:1fr}',
            '.inspector-actions{grid-template-columns:1fr}',
        ]:
            self.assertIn(token, style)

    def test_d3_is_pinned_vendored_and_licensed(self):
        vendor = self.static_root / 'vendor' / 'd3'
        script = vendor / 'd3.v7.9.0.min.js'

        self.assertTrue(script.is_file())
        self.assertTrue((vendor / 'LICENSE').is_file())
        self.assertTrue((vendor / 'UPSTREAM.md').is_file())
        # Git's Windows checkout may apply core.autocrlf to the two upstream
        # newlines.  Pin the vendored content, not the platform checkout
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
