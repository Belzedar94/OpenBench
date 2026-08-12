/* Speedometer dials for a gameplay workload: LLR, LOS and Elo.
 *
 * Design after Fishtest's live_elo page (tests.stockfishchess.org). No
 * libraries: the SVG is built here and the colours come from the stylesheet,
 * so the theme switch repaints the dials without this file knowing about it.
 *
 * The page ships the current reading in data-* attributes, and /api/liveElo/
 * answers the same reading later. One shape, two deliveries. */

(function () {

    'use strict';

    var POLL_MS = 30000;
    var SVG_NS  = 'http://www.w3.org/2000/svg';

    /* Dial geometry, in the viewBox's own units. The arc is a half circle
       drawn as a stroked path, so the band is one element per zone. */
    var VIEW_W = 200, VIEW_H = 162;
    var CX = 100, CY = 100, R = 76, BAND = 16;

    function clamp(value, low, high) {
        return Math.min(high, Math.max(low, value));
    }

    /* Fraction of the dial, 0 at the left end and 1 at the right end. */
    function fraction(value, low, high) {
        if (!isFinite(value) || !isFinite(low) || !isFinite(high) || high <= low)
            return 0.5;
        return clamp((value - low) / (high - low), 0, 1);
    }

    function point(radius, t) {
        var angle = (180 - 180 * clamp(t, 0, 1)) * Math.PI / 180;
        return [CX + radius * Math.cos(angle), CY - radius * Math.sin(angle)];
    }

    function arc_path(radius, from, to) {

        var start = point(radius, from), end = point(radius, to);

        /* The whole dial is a half circle, so no piece of it is ever the long
           way round: the large-arc flag stays 0 and the sweep flag carries the
           left-to-right direction. */
        return 'M' + start[0].toFixed(2) + ' ' + start[1].toFixed(2)
             + ' A' + radius + ' ' + radius + ' 0 0 1 '
             + end[0].toFixed(2) + ' ' + end[1].toFixed(2);
    }

    function element(name, attributes) {
        var node = document.createElementNS(SVG_NS, name);
        for (var key in attributes)
            if (Object.prototype.hasOwnProperty.call(attributes, key))
                node.setAttribute(key, attributes[key]);
        return node;
    }

    function band(svg, from, to, kind) {
        if (to - from < 1e-6) return;
        svg.appendChild(element('path', {
            'class'        : 'live-elo-band live-elo-band-' + kind,
            'd'            : arc_path(R, from, to),
            'fill'         : 'none',
            'stroke-width' : BAND,
        }));
    }

    function tick(svg, t, kind) {
        var inner = point(R - BAND / 2 - 3, t), outer = point(R + BAND / 2 + 3, t);
        svg.appendChild(element('line', {
            'class' : 'live-elo-tick live-elo-tick-' + kind,
            'x1'    : inner[0].toFixed(2), 'y1' : inner[1].toFixed(2),
            'x2'    : outer[0].toFixed(2), 'y2' : outer[1].toFixed(2),
        }));
    }

    function text(svg, x, y, content, kind, anchor) {
        var node = element('text', {
            'class'       : 'live-elo-' + kind,
            'x'           : x,
            'y'           : y,
            'text-anchor' : anchor || 'middle',
        });
        node.textContent = content;
        svg.appendChild(node);
        return node;
    }

    function needle(svg, t) {

        var angle  = (180 - 180 * clamp(t, 0, 1)) * Math.PI / 180;
        var along  = [Math.cos(angle), -Math.sin(angle)];
        var across = [-along[1], along[0]];
        var length = R + BAND / 2 - 2;

        var tip   = [CX + along[0] * length, CY + along[1] * length];
        var left  = [CX + across[0] * 5, CY + across[1] * 5];
        var right = [CX - across[0] * 5, CY - across[1] * 5];

        svg.appendChild(element('polygon', {
            'class'  : 'live-elo-needle',
            'points' : [tip, left, right].map(function (p) {
                return p[0].toFixed(2) + ',' + p[1].toFixed(2);
            }).join(' '),
        }));

        svg.appendChild(element('circle', {
            'class' : 'live-elo-hub', 'cx' : CX, 'cy' : CY, 'r' : 7,
        }));
    }

    /* One dial. `zones` is a list of [from, to, kind] in dial fractions. */
    function draw(host, spec) {

        var svg = element('svg', {
            'class'               : 'live-elo-svg',
            'viewBox'             : '0 0 ' + VIEW_W + ' ' + VIEW_H,
            'role'                : 'img',
            'preserveAspectRatio' : 'xMidYMid meet',
            'aria-label'          : spec.label + ' ' + spec.value,
        });

        band(svg, 0, 1, 'track');
        for (var i = 0; i < spec.zones.length; i++)
            band(svg, spec.zones[i][0], spec.zones[i][1], spec.zones[i][2]);

        for (var t = 0; t <= 1.0001; t += 0.25)
            tick(svg, t, 'minor');

        if (spec.mark !== undefined && spec.mark !== null)
            tick(svg, spec.mark, 'mark');

        needle(svg, spec.needle);

        text(svg, 16, CY + 15, spec.low, 'bound', 'start');
        text(svg, VIEW_W - 16, CY + 15, spec.high, 'bound', 'end');
        text(svg, CX, CY + 40, spec.value, 'value');
        text(svg, CX, CY + 57, spec.label, 'label');

        host.textContent = '';
        host.appendChild(svg);
    }

    function llr_spec(data) {

        var low = data.llr_lower, high = data.llr_upper;

        /* GAMES workloads have no hypothesis, and so no LLR to point at. */
        if (!isFinite(data.llr) || !isFinite(low) || !isFinite(high))
            return null;

        return {
            label  : 'LLR',
            value  : data.llr.toFixed(2),
            low    : low.toFixed(2),
            high   : high.toFixed(2),
            needle : fraction(data.llr, low, high),
            /* Red against the rejection bound, green against acceptance. */
            zones  : [[0, 1 / 3, 'bad'], [1 / 3, 2 / 3, 'warn'], [2 / 3, 1, 'good']],
            mark   : fraction(0, low, high),
        };
    }

    function los_spec(data) {
        return {
            label  : 'LOS',
            value  : data.los.toFixed(1) + '%',
            low    : '0%',
            high   : '100%',
            needle : fraction(data.los, 0, 100),
            zones  : [[0, 1 / 3, 'bad'], [1 / 3, 2 / 3, 'warn'], [2 / 3, 1, 'good']],
            /* Even money, where a LOS says nothing at all. */
            mark   : 0.5,
        };
    }

    function elo_spec(data) {
        var low = data.elo_axis_lower, high = data.elo_axis_upper;
        return {
            label  : 'ELO',
            value  : data.elo.toFixed(2),
            low    : low.toFixed(0),
            high   : high.toFixed(0),
            needle : fraction(data.elo, low, high),
            /* The 95% confidence interval, on an axis that adapts to it. */
            zones  : [[
                fraction(data.elo_lower, low, high),
                fraction(data.elo_upper, low, high),
                'good',
            ]],
            mark   : fraction(0, low, high),
        };
    }

    var SPECS = { llr : llr_spec, los : los_spec, elo : elo_spec };

    function render(root, data) {

        var hosts = root.querySelectorAll('[data-gauge]');

        for (var i = 0; i < hosts.length; i++) {
            var name = hosts[i].getAttribute('data-gauge');
            var spec = SPECS[name] ? SPECS[name](data) : null;
            if (spec)
                draw(hosts[i], spec);
        }

        var summary = root.querySelector('.live-elo-summary');
        if (summary && data.summary)
            summary.textContent = data.summary;
    }

    function number(root, name) {
        return parseFloat(root.getAttribute('data-' + name));
    }

    function initial(root) {
        return {
            mode           : root.getAttribute('data-mode'),
            llr            : number(root, 'llr'),
            llr_lower      : number(root, 'llr-lower'),
            llr_upper      : number(root, 'llr-upper'),
            elo            : number(root, 'elo'),
            elo_lower      : number(root, 'elo-lower'),
            elo_upper      : number(root, 'elo-upper'),
            elo_axis_lower : number(root, 'elo-axis-lower'),
            elo_axis_upper : number(root, 'elo-axis-upper'),
            los            : number(root, 'los'),
            finished       : root.getAttribute('data-live') === '0',
        };
    }

    var timer = null;
    var live  = false;
    var last  = 0;

    function schedule(root, delay) {
        window.clearTimeout(timer);
        timer = window.setTimeout(function () { poll(root); },
            delay === undefined ? POLL_MS : delay);
    }

    function stop() {
        window.clearTimeout(timer);
        live = false;
    }

    function poll(root) {

        /* A backgrounded tab does not need a fresh needle; try again later. */
        if (document.hidden)
            return schedule(root);

        /* Returning to the tab asks for a reading, but a window that flips
           between visible and hidden must not turn every flip into a request:
           the interval is a floor, wherever the call came from. */
        var waited = Date.now() - last;
        if (waited < POLL_MS)
            return schedule(root, POLL_MS - waited);

        last = Date.now();

        window.fetch(root.getAttribute('data-endpoint'), {
            credentials : 'same-origin',
            headers     : { 'Accept' : 'application/json' },

        }).then(function (response) {
            return response.ok ? response.json() : null;

        }).then(function (data) {

            if (!data || data.error)
                return schedule(root);

            render(root, data);

            /* A decided test never moves again: stop asking. */
            if (data.finished || data.passed || data.failed)
                return stop();

            schedule(root);

        }).catch(function () {
            schedule(root);
        });
    }

    function start() {

        var root = document.getElementById('live-elo');
        if (!root) return;

        var data = initial(root);
        render(root, data);

        if (data.finished || !window.fetch)
            return;

        live = true;
        last = Date.now();
        schedule(root);

        /* Coming back to a tab that was left open should not mean waiting out
           the rest of a poll on numbers that are already stale. */
        document.addEventListener('visibilitychange', function () {
            if (live && !document.hidden)
                poll(root);
        });
    }

    if (document.readyState === 'loading')
        document.addEventListener('DOMContentLoaded', start);
    else
        start();

}());
