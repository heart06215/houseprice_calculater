/* Node vm test: metro artifact loader wiring + version-keyed cross-city distance + shared-state helpers.
 * Run from repo root:  D:/nodejs/node.exe data_processing/test_metro_wiring.cjs
 * Node built-ins only (fs, vm, assert). No network, no deps.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const JS = __dirname + '/frontend/realestate/assets/js';
const ROOT = __dirname + '/frontend/realestate';

let pass = 0;
const failures = [];
function check(label, fn) {
    try {
        fn();
        pass++;
        console.log('PASS: ' + label);
    } catch (e) {
        const msg = (e && e.message) || String(e);
        failures.push({ label: label, message: msg });
        console.log('FAIL: ' + label + ' -> ' + msg);
    }
}
function jEq(a, b, label) {
    assert.strictEqual(JSON.stringify(a), JSON.stringify(b), label || 'json-equal');
}
function read(file) { return fs.readFileSync(file, 'utf8'); }

function newSandbox() {
    const sandbox = {};
    sandbox.window = sandbox;
    sandbox.console = console;
    return sandbox;
}
function loadCommunity(sandbox) { vm.runInNewContext(read(JS + '/community.js'), sandbox); }
function loadLoader(sandbox) { vm.runInNewContext(read(JS + '/loader.js'), sandbox); }
function loadCommunityMap(sandbox) { vm.runInNewContext(read(JS + '/community-map.js'), sandbox); }

// fake fetch keyed by exact URL substring; spec = {ok?:bool, body:any}
function fakeFetch(bodies) {
    return function (url) {
        const keys = Object.keys(bodies);
        for (let i = 0; i < keys.length; i++) {
            if (url.indexOf(keys[i]) >= 0) {
                const spec = bodies[keys[i]];
                const ok = spec.ok !== false;
                return Promise.resolve({
                    ok: ok,
                    status: ok ? 200 : 404,
                    json: function () { return Promise.resolve(spec.body); }
                });
            }
        }
        return Promise.resolve({ ok: false, status: 404, json: function () { return Promise.resolve(null); } });
    };
}

// ---------------- fixtures ----------------
const ARTIFACT = {
    schema_version: '1',
    generated_at: '2026-09-20T10:01:57.006415+00:00',
    versions: { station_set: 'test:v1', coord_conversion: 'gcj02->wgs84:v1', distance_caliber: 'straight_line_wgs84' },
    coverage: { cities: 2, stations: 3, lines: 3 },
    note: '未逐站按运营方核对',
    cities: {
        'A市': [
            ['a1', 'A站1', 100.002, 30.002, ['L1', 'L2'], 'operational', 'exact'],
            ['a2', 'A站2', 100.010, 30.010, ['L1'], 'operational', 'exact']
        ],
        'B市': [
            ['b1', 'B站1', 101.0, 31.0, ['L9'], 'operational', 'exact']
        ]
    },
    cols: ['id', 'name', 'lng', 'lat', 'lines', 'op', 'prec'],
    excluded_schematic: 0
};
const CORE_OPERATIONAL = {
    'data/communities.json': { body: { communities: [], updated_at: '2026-09-20T00:00:00Z' } },
    'data/metro.json': { body: { cities: [], updated_at: '2020-01-01T00:00:00Z' } },
    'data/highspeed.json': { body: { cities: [] } },
    'data/cities.json': { body: { cities: [] } },
    'data/water.json': { body: { cities: {} } },
    'data/metro_operational.min.json': { body: ARTIFACT }
};
const LEGACY_METRO = {
    cities: [{ name: 'A市', lines: [{ name: 'L1', stations: [['站1', 10.0, 20.0]] }] }],
    updated_at: '2026-01-02T00:00:00Z'
};
const CORE_LEGACY = {
    'data/communities.json': { body: { communities: [], updated_at: '2026-09-20T00:00:00Z' } },
    'data/metro.json': { body: LEGACY_METRO },
    'data/highspeed.json': { body: { cities: [] } },
    'data/cities.json': { body: { cities: [] } },
    'data/water.json': { body: { cities: {} } },
    'data/metro_operational.min.json': { ok: false, body: null }
};

const rowNearA = { name: 'nearA', lng: 100.003, lat: 30.003, city: 'A市' };
const rowFar = { name: 'farC', lng: 120.0, lat: 40.0, city: 'C市' };

(async function main() {
    // ================= A. artifact path =================
    const sbA = newSandbox();
    loadCommunity(sbA);
    loadLoader(sbA);
    sbA.fetch = fakeFetch(CORE_OPERATIONAL);
    const data = await sbA.HP.RE.loadData();

    check('A1 metroVersions.station_set === test:v1', function () {
        assert.strictEqual(data.metroVersions.station_set, 'test:v1');
    });
    check('A2 metroOperational is the artifact', function () {
        assert.strictEqual(data.metroOperational, ARTIFACT);
    });
    check('A3 stations.length === totalRows (3)', function () {
        assert.strictEqual(data.stations.length, 3);
    });
    check('A4 every station has name/lng/lat/city/operational_status', function () {
        data.stations.forEach(function (s, i) {
            assert.strictEqual(typeof s.name, 'string', 'name#' + i);
            assert.strictEqual(typeof s.lng, 'number', 'lng#' + i);
            assert.strictEqual(typeof s.lat, 'number', 'lat#' + i);
            assert.strictEqual(typeof s.city, 'string', 'city#' + i);
            assert.strictEqual(typeof s.operational_status, 'string', 'operational_status#' + i);
        });
    });
    check('A5 lines array preserved, line = first line', function () {
        const s = data.stations.filter(function (x) { return x.name === 'A站1'; })[0];
        assert.ok(s, 'A站1 missing');
        jEq(s.lines, ['L1', 'L2'], 'lines');
        assert.strictEqual(s.line, 'L1', 'line');
    });
    check('A6 city assigned per city bucket', function () {
        const s = data.stations.filter(function (x) { return x.name === 'B站1'; })[0];
        assert.ok(s, 'B站1 missing');
        assert.strictEqual(s.city, 'B市');
    });

    // ================= B. cross-city distance via query =================
    sbA.HP.RE.data = data;

    check('B1 query: rowNearA supported, rowFar excluded (stationsOf fallback, cross-city)', function () {
        const q = sbA.HP.RE.community.query([rowNearA, rowFar], { metroMaxM: 1000, versions: data.metroVersions });
        assert.ok(q.supported.indexOf(rowNearA) >= 0, 'rowNearA not in supported');
        assert.ok(q.excluded.indexOf(rowFar) >= 0, 'rowFar not in excluded');
        assert.strictEqual(q.counts.total, 2);
    });
    check('B2 rowFar exclusion reason = metro > max', function () {
        const ex = sbA.HP.RE.community.explain(rowFar, { metroMaxM: 1000, versions: data.metroVersions });
        const m = ex.filter(function (c) { return c.condition === '地铁距离'; })[0];
        assert.ok(m, 'no metro condition');
        assert.strictEqual(m.status, 'excluded');
        assert.ok(/>/.test(m.reason), 'reason=' + m.reason);
    });

    // ================= C. version keying =================
    check('C1 cacheKey differs when station_set differs', function () {
        const a = sbA.HP.RE.community.cacheKey(data.metroVersions);
        const b = sbA.HP.RE.community.cacheKey(Object.assign({}, data.metroVersions, { station_set: 'other:v9' }));
        assert.notStrictEqual(a, b);
    });
    check('C2 distance known with meters <= 1000', function () {
        const d = sbA.HP.RE.community.distance(rowNearA, data.stations, data.metroVersions);
        assert.strictEqual(d.status, 'known');
        assert.ok(d.meters <= 1000, 'meters=' + d.meters);
    });

    // ================= D. legacy fallback =================
    const sbD = newSandbox();
    loadCommunity(sbD);
    loadLoader(sbD);
    sbD.fetch = fakeFetch(CORE_LEGACY);
    const dataD = await sbD.HP.RE.loadData();

    check('D1 legacy station operational_status === operational', function () {
        assert.strictEqual(dataD.stations[0].operational_status, 'operational');
    });
    check('D2 legacy verification_state === unverified_legacy', function () {
        assert.strictEqual(dataD.stations[0].verification_state, 'unverified_legacy');
    });
    check('D3 station_set === legacy updated_at', function () {
        assert.strictEqual(dataD.metroVersions.station_set, '2026-01-02T00:00:00Z');
    });
    check('D4 metroOperational.fallback === true', function () {
        assert.strictEqual(dataD.metroOperational.fallback, true);
    });
    check('D5 legacy name/coord/line/city', function () {
        const s = dataD.stations[0];
        assert.strictEqual(s.name, '站1');
        assert.strictEqual(s.lng, 10);
        assert.strictEqual(s.lat, 20);
        assert.strictEqual(s.line, 'L1');
        assert.strictEqual(s.city, 'A市');
    });

    // ================= E. shared-state helpers (community-map.js) =================
    const sbM = newSandbox();
    sbM.document = { getElementById: function () { return null; } };
    sbM.L = {};
    loadCommunity(sbM);
    vm.runInNewContext(read(JS + '/community-map.js'), sbM);
    const qs = sbM.HP.RE.qs;

    check('E1 HP.RE.qs + HP.RE.sortRows exist as functions', function () {
        assert.strictEqual(typeof qs, 'object');
        ['pull', 'push', 'yearOf', 'formsOf', 'reset'].forEach(function (k) {
            assert.strictEqual(typeof qs[k], 'function', 'qs.' + k);
        });
        assert.strictEqual(typeof sbM.HP.RE.sortRows, 'function');
    });
    check('E2 yearOf 2010s', function () {
        qs.reset();
        qs.state.yearPreset = '2010s';
        jEq(qs.yearOf(), { yearFrom: 2010, yearTo: 2019, status: null });
    });
    check('E3 yearOf custom 2005/null', function () {
        qs.reset();
        qs.state.yearFrom = 2005;
        qs.state.yearTo = null;
        jEq(qs.yearOf(), { yearFrom: 2005, yearTo: null, status: null });
    });
    check('E4 yearOf known', function () {
        qs.reset();
        qs.state.yearPreset = 'known';
        jEq(qs.yearOf(), { yearFrom: null, yearTo: null, status: 'known' });
    });
    check('E5 formsOf 别墅 -> one merged villa form', function () {
        qs.reset();
        qs.state.type = '别墅';
        jEq(qs.formsOf(), { forms: ['别墅'], requireNoForm: false });
    });
    check('E5b formsOf 别墅+楼房 -> both forms', function () {
        qs.reset();
        qs.state.type = '别墅+楼房';
        jEq(qs.formsOf(), { forms: ['别墅', '楼房'], requireNoForm: false });
    });
    check('E6 formsOf 形态待核', function () {
        qs.reset();
        qs.state.type = '形态待核';
        jEq(qs.formsOf(), { forms: [], requireNoForm: true });
    });

    // ================= F. city marker groups =================
    const html = read(ROOT + '/map.html');
    ['map-search','map-district','map-type','map-year','map-year-from','map-year-to','map-metro','map-sort','map-hints','map-viewport'].forEach(function (id) {
        check('F-wire ' + id + ' -> HP.RE.city.applyFilter()', function () {
            const tag = html.split('id="' + id + '"')[1].split('>')[0];
            assert.ok(/on(input|change)="[^"]*HP\.RE\.city\.applyFilter\(\)/.test(tag), id + ' tag=' + tag);
        });
    });
    check('F-wire map-city-select -> HP.RE.openCityView', function () {
        const tag = html.split('id="map-city-select"')[1].split('>')[0];
        assert.ok(tag.indexOf('HP.RE.openCityView(') >= 0, 'tag=' + tag);
    });

    function makeDom() {
        const els = {};
        function cls() {
            const set = new Set();
            return { add: function (c) { set.add(c); }, remove: function (c) { set.delete(c); },
                contains: function (c) { return set.has(c); },
                toggle: function (c, force) { const on = (force === undefined) ? !set.has(c) : !!force; if (on) set.add(c); else set.delete(c); return on; } };
        }
        function makeEl(id) {
            return { id: id, value: '', checked: false, innerHTML: '', textContent: '', hidden: false,
                className: '', scrollTop: 0, dataset: {}, style: {}, classList: cls(),
                setAttribute: function () {}, querySelector: function () { return null; },
                querySelectorAll: function () { return []; }, appendChild: function () {},
                insertAdjacentHTML: function () {}, onclick: null };
        }
        return {
            get: function (id) { if (!els[id]) els[id] = makeEl(id); return els[id]; },
            getElementById: function (id) { return this.get(id); },
            querySelector: function (sel) { return makeEl(sel); },
            querySelectorAll: function () { return []; },
            createElement: function (tag) { return makeEl(tag); },
            addEventListener: function () {}
        };
    }

    function markerSandbox() {
        const log = { markerCount: 0, layerGroupCount: 0, markers: [] };
        const bounds = { pad: function () { return this; }, contains: function () { return true; } };
        function layerGroup(items) {
            log.layerGroupCount++;
            const g = { __items: items || [] };
            g.addTo = function (map) { map._layers.add(g); return g; };
            return g;
        }
        function marker(latlng, opts) {
            log.markerCount++;
            const m = { __latlng: latlng, __opts: opts || {}, __tooltip: '' };
            m.bindTooltip = function (t) { m.__tooltip = t; return m; };
            m.on = function () { return m; }; m.bindPopup = function () { return m; }; m.openPopup = function () { return m; };
            log.markers.push(m); return m;
        }
        function circleMarker(latlng, opts) {
            log.markerCount++;
            const m = { __latlng: latlng, __opts: opts || {}, __tooltip: '' };
            m.bindTooltip = function (t) { m.__tooltip = t; return m; };
            m.on = function () { return m; };
            log.markers.push(m); return m;
        }
        const L = {
            divIcon: function (o) { return o || {}; },
            marker: marker, circleMarker: circleMarker, layerGroup: layerGroup,
            latLngBounds: function () { return { getCenter: function () { return [0, 0]; }, pad: function () { return this; } }; },
            tileLayer: function () { const t = { on: function () { return t; }, addTo: function () { return t; } }; return t; },
            control: { zoom: function () { return { addTo: function () { return this; } }; } },
            geoJSON: function () { return { addTo: function () { return this; } }; }
        };
        const dom = makeDom();
        const sb = newSandbox();
        sb.document = dom;
        sb.L = L;
        sb.matchMedia = function () { return { matches: false }; };
        loadCommunity(sb);
        sb.HP.RE.data = { properties: [], stations: [], highspeed: [], metroVersions: {}, metroOperational: {}, coverage: { cities: {} }, districts: {}, cities: {} };
        loadCommunityMap(sb);
        const map = {
            _layers: new Set(), remove: function () {}, on: function () {}, getZoom: function () { return 11; },
            project: function (ll) { return { x: ll[1] * 1000, y: ll[0] * 1000 }; },
            getBounds: function () { return bounds; },
            createPane: function () { return { style: {} }; }, getPane: function () { return { style: {} }; },
            setView: function () {}, fitBounds: function () {},
            removeLayer: function (l) { map._layers.delete(l); },
            invalidateSize: function () {}, closePopup: function () {}
        };
        return { sb: sb, dom: dom, log: log, map: map };
    }

    const rows = [
        { id: 's1', name: 'sup1', city: '甲市', district: 'A区', lat: 30, lng: 120, completion_year: { status: 'recorded', precision: 'year', start: 2003, end: 2003 }, residential_forms: ['楼房'] },
        { id: 's2', name: 'sup2', city: '甲市', district: 'A区', lat: 31, lng: 121, completion_year: { status: 'recorded', precision: 'year', start: 2004, end: 2004 }, residential_forms: ['楼房'] },
        { id: 'p1', name: 'pend1', city: '甲市', district: 'A区', lat: 32, lng: 122, completion_year: { status: 'unknown' } },
        { id: 'p2', name: 'pend2', city: '甲市', district: 'A区', lat: 33, lng: 123 },
        { id: 'e1', name: 'excl1', city: '甲市', district: 'A区', lat: 34, lng: 124, completion_year: { status: 'recorded', precision: 'year', start: 2010, end: 2010 }, residential_forms: ['楼房'] }
    ];
    const H = markerSandbox();
    const city = H.sb.HP.RE.city;
    city.state.map = H.map;
    city.state.currentCity = '甲市';
    city.state.props = rows;
    H.dom.get('map-sort').value = 'relevance';
    H.dom.get('map-hints').checked = true;
    H.dom.get('map-viewport').checked = false;
    H.dom.get('map-year-from').value = '2000';
    H.dom.get('map-year-to').value = '2005';
    H.dom.get('btn-props').classList.add('on');
    city.applyFilter();
    function names(layer) { return (layer.__items || []).map(function (m) { return m.__tooltip; }).sort(); }

    check('F1 restrictive filter: props layer = groups.supported only', function () {
        jEq(names(city.state.layers.props), city.state.groups.supported.map(function (p) { return p.name; }).sort());
        assert.strictEqual(names(city.state.layers.props).join(','), 'sup1,sup2');
        assert.strictEqual(names(city.state.layers.props).indexOf('pend1'), -1);
    });
    check('F2 pending layer built but hidden by default', function () {
        jEq(names(city.state.layers.pending), ['pend1', 'pend2']);
        assert.ok(!H.map._layers.has(city.state.layers.pending), 'pending must be hidden by default');
        assert.ok(H.map._layers.has(city.state.layers.props), 'supported must be visible by default');
    });
    check('F3 excluded rows never in default layers', function () {
        const seen = names(city.state.layers.props).concat(names(city.state.layers.pending));
        assert.strictEqual(seen.indexOf('excl1'), -1);
        assert.strictEqual(city.state.groups.counts.excluded, 1);
        jEq(names(city.state.layers.excluded), ['excl1']);
        assert.ok(!H.map._layers.has(city.state.layers.excluded));
    });
    check('F4 map-pending toggle adds exactly pending rows and survives filter change', function () {
        H.dom.get('map-pending').checked = true;
        city.applyFilter();
        assert.ok(H.map._layers.has(city.state.layers.pending), 'pending layer must be added');
        jEq(names(city.state.layers.pending), ['pend1', 'pend2']);
        jEq(names(city.state.layers.props), ['sup1', 'sup2']);
        H.dom.get('map-search').value = 'sup';
        city.applyFilter();
        assert.ok(H.map._layers.has(city.state.layers.pending), 'pending preference must survive filter change');
    });
    check('F5 resetFilter restores supported-only default', function () {
        city.resetFilter();
        assert.strictEqual(H.dom.get('map-pending').checked, false);
        assert.ok(!H.map._layers.has(city.state.layers.pending));
        assert.ok(H.map._layers.has(city.state.layers.props));
    });
    check('F6 each filter control triggers a marker rebuild', function () {
        const cases = [['map-search', 'x'], ['map-district', 'A区'], ['map-type', '楼房'], ['map-year', '2010s'], ['map-year-from', '1990'], ['map-year-to', '2020'], ['map-metro', '1000'], ['map-sort', 'name'], ['map-hints', false], ['map-viewport', false]];
        cases.forEach(function (c) {
            H.dom.get('map-year-from').value = ''; H.dom.get('map-year-to').value = '';
            const before = H.log.layerGroupCount;
            if (typeof c[1] === 'boolean') H.dom.get(c[0]).checked = c[1]; else H.dom.get(c[0]).value = c[1];
            city.applyFilter();
            assert.ok(H.log.layerGroupCount > before, c[0] + ' did not rebuild markers');
        });
    });
    check('F7 #filter-count names all four groups with counts', function () {
        city.resetFilter();
        const txt = H.dom.get('filter-count').textContent;
        ['资料支持符合', '待核候选', '已有证据不符', '未覆盖'].forEach(function (k) {
            assert.ok(txt.indexOf(k) >= 0, 'missing ' + k + ' in ' + txt);
        });
        assert.ok(txt.indexOf('未覆盖 0') >= 0, 'uncovered count missing: ' + txt);
    });

    // ================= summary =================
    console.log('PASS=' + pass + ' FAIL=' + failures.length);
    if (failures.length) {
        console.log('FAILURES:');
        failures.forEach(function (f) { console.log('- ' + f.label + ': ' + f.message); });
        process.exit(1);
    }
    console.log('ALL PASS');
})().catch(function (e) {
    console.error('UNEXPECTED ERROR:', e && e.stack || e);
    process.exit(1);
});
