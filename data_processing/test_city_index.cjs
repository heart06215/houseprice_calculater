/* Node vm test: per-city columnar index + lazy city loader.
 * Run from repo root:  D:/nodejs/node.exe data_processing/test_city_index.cjs
 * Node built-ins only (fs, vm, assert). No network, no deps.
 */
'use strict';
const fs = require('fs');
const crypto = require('crypto');
const vm = require('vm');
const assert = require('assert');

const ROOT = __dirname + '/frontend/realestate';
const DATA = ROOT + '/data';
const JS = ROOT + '/assets/js';
const COMMUNITIES = DATA + '/communities';
const FULL_FILE = DATA + '/communities.json';
const MANIFEST_FILE = COMMUNITIES + '/index.json';

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
function readJson(file) { return JSON.parse(read(file)); }

// Canonical column allow-list (order fixed by manifest).
const CANON_COLS = ['id', 'name', 'city', 'district', 'address', 'type', 'housing_types', 'residential_forms', 'villa_subtypes', 'pure_villa', 'type_hints', 'completion_year', 'type_evidence', 'lat', 'lng', 'coordinate_system', 'precision', 'source', 'source_url', 'fetched_at', 'location_note', 'fang_profile', 'fang_reference', 'catalog_only', 'year_status', 'year_start', 'year_end', 'year_precision', 'year_meaning', 'year_scope', 'evidence'];
const CANON_SET = new Set(CANON_COLS);
const REQUIRED_COLS = ['id', 'name', 'city', 'district', 'type', 'housing_types', 'completion_year', 'type_evidence', 'lat', 'lng', 'coordinate_system', 'source', 'source_url'];
const CITY_NAMES = ['北京', '上海', '广州'];

function cityFile(city) { return COMMUNITIES + '/' + city + '.json'; }
function cityUrl(city) { return 'data/communities/' + encodeURIComponent(city) + '.json'; }

// Columnar rows -> object rows (mirrors loader.decodeIndex).
function decode(json) {
    const cols = json.cols || [], rows = json.rows || [], out = [];
    for (let i = 0; i < rows.length; i++) {
        const row = rows[i] || {}, o = {};
        for (let j = 0; j < cols.length; j++) o[cols[j]] = row[j];
        out.push(o);
    }
    return out;
}
// Project object onto cols, missing key -> null.
function project(row, cols) {
    return cols.map(function (c) { return c in row ? row[c] : null; });
}
function byStringId(a, b) {
    const x = String(a.id), y = String(b.id);
    return x < y ? -1 : x > y ? 1 : 0;
}
function uniqueCount(rows) {
    const seen = new Set();
    for (let i = 0; i < rows.length; i++) seen.add(rows[i].city + '|' + rows[i].id);
    return seen.size;
}

// Fake fetch keyed by substring (insertion order); records every URL.
function recordingFetch(bodies, requested) {
    return function (url) {
        requested.push(url);
        const keys = Object.keys(bodies);
        for (let i = 0; i < keys.length; i++) {
            if (url.indexOf(keys[i]) >= 0) {
                const spec = bodies[keys[i]];
                const ok = spec.ok !== false;
                return Promise.resolve({ ok: ok, status: ok ? 200 : 404, json: function () { return Promise.resolve(spec.body); } });
            }
        }
        return Promise.resolve({ ok: false, status: 404, json: function () { return Promise.resolve(null); } });
    };
}
function newSandbox() {
    const sandbox = {};
    sandbox.window = sandbox;
    sandbox.console = console;
    return sandbox;
}
function loadCommunity(sandbox) { vm.runInNewContext(read(JS + '/community.js'), sandbox); }
function loadLoader(sandbox) { vm.runInNewContext(read(JS + '/loader.js'), sandbox); }
function countUrl(requested, needle) {
    let n = 0;
    for (let i = 0; i < requested.length; i++) if (requested[i].indexOf(needle) >= 0) n++;
    return n;
}
const CORE = {
    'data/metro.json': { body: { cities: [] } },
    'data/highspeed.json': { body: { cities: [] } },
    'data/cities.json': { body: {} },
    'data/water.json': { body: { cities: {} } },
    'data/metro_operational.min.json': { ok: false, body: null }
};

(async function main() {
    const fullBytes = fs.statSync(FULL_FILE).size;
    const manifest = readJson(MANIFEST_FILE);

    // ================= A. manifest =================
    check('A1 per-city rows present & positive', function () {
        CITY_NAMES.forEach(function (c) {
            assert.ok(manifest.cities[c], 'missing city ' + c);
            assert.ok(Number.isInteger(manifest.cities[c].rows) && manifest.cities[c].rows > 0, c + ' rows=' + manifest.cities[c].rows);
        });
    });
    check('A2 per-city rows sum to manifest.total_rows', function () {
        assert.ok(Number.isInteger(manifest.total_rows) && manifest.total_rows > 0);
        const sum = CITY_NAMES.reduce(function (n, c) { return n + manifest.cities[c].rows; }, 0);
        assert.strictEqual(sum, manifest.total_rows);
    });
    check('A3 default_city === 上海', function () {
        assert.strictEqual(manifest.default_city, '上海');
    });
    check('A4 source_bytes/source_sha256 match full file', function () {
        assert.strictEqual(manifest.source_bytes, fullBytes, 'manifest=' + manifest.source_bytes + ' stat=' + fullBytes);
        assert.strictEqual(manifest.source_sha256, crypto.createHash('sha256').update(fs.readFileSync(FULL_FILE)).digest('hex'));
    });

    // ================= B. per-city file shape =================
    const cityJson = {}, cityObj = {};
    CITY_NAMES.forEach(function (city) {
        const json = readJson(cityFile(city));
        cityJson[city] = json;
        cityObj[city] = decode(json);

        check('B' + city + '1 cols subset of canonical', function () {
            json.cols.forEach(function (c) { assert.ok(CANON_SET.has(c), 'unknown col: ' + c); });
        });
        check('B' + city + '2 required cols present', function () {
            REQUIRED_COLS.forEach(function (c) { assert.ok(json.cols.indexOf(c) >= 0, 'missing col: ' + c); });
        });
        check('B' + city + '3 rows.length === manifest.rows === row_count', function () {
            assert.strictEqual(json.rows.length, manifest.cities[city].rows);
            assert.strictEqual(json.row_count, manifest.cities[city].rows);
        });
        check('B' + city + '4 every row length === cols length', function () {
            json.rows.forEach(function (r, i) {
                assert.ok(Array.isArray(r), 'row not array #' + i);
                assert.strictEqual(r.length, json.cols.length, 'row #' + i + ' len=' + r.length);
            });
        });
        const colSet = new Set(json.cols);
        check('B' + city + '5 decoded city/lat/lng finite, no key outside cols', function () {
            cityObj[city].forEach(function (o, i) {
                assert.strictEqual(o.city, city, 'city #' + i + '=' + o.city);
                assert.ok(Number.isFinite(o.lat), 'lat #' + i + '=' + o.lat);
                assert.ok(Number.isFinite(o.lng), 'lng #' + i + '=' + o.lng);
                Object.keys(o).forEach(function (k) { assert.ok(colSet.has(k), 'extra key ' + k + ' #' + i); });
            });
        });
    });

    // ================= C. byte sizes =================
    CITY_NAMES.forEach(function (city) {
        check('C' + city + ' stat size === manifest bytes && < full', function () {
            const size = fs.statSync(cityFile(city)).size;
            assert.strictEqual(size, manifest.cities[city].bytes);
            assert.ok(size < fullBytes, 'size ' + size + ' not < ' + fullBytes);
            assert.strictEqual(manifest.cities[city].sha256, crypto.createHash('sha256').update(fs.readFileSync(cityFile(city))).digest('hex'));
        });
    });
    check('C4 sum(index bytes) < full file bytes', function () {
        const total = CITY_NAMES.reduce(function (n, c) { return n + manifest.cities[c].bytes; }, 0);
        assert.ok(total < fullBytes, 'index total ' + total + ' not < ' + fullBytes);
    });

    // ================= D. loader prefers index =================
    const requestedD = [];
    const bodiesD = Object.assign({}, CORE, {
        'data/communities/index.json': { body: manifest },
        [cityUrl('上海')]: { body: cityJson['上海'] },
        'data/communities.json': { ok: false, body: null }
    });
    const sbD = newSandbox();
    loadCommunity(sbD);
    loadLoader(sbD);
    sbD.location = { search: '?city=上海' };
    sbD.fetch = recordingFetch(bodiesD, requestedD);
    const dataD = await sbD.HP.RE.loadData();

    check('D1 index.json requested, city file requested', function () {
        assert.ok(requestedD.indexOf('data/communities/index.json') >= 0, 'index not requested');
        assert.ok(requestedD.indexOf(cityUrl('上海')) >= 0, 'city not requested');
    });
    check('D2 full communities.json NOT requested', function () {
        assert.strictEqual(requestedD.indexOf('data/communities.json'), -1, 'full file requested');
    });
    check('D3 properties.length === manifest 上海 rows', function () {
        assert.strictEqual(dataD.properties.length, manifest.cities['上海'].rows);
    });
    check('D4 every property city === 上海', function () {
        dataD.properties.forEach(function (p, i) { assert.strictEqual(p.city, '上海', '#' + i); });
    });
    check('D5 isCityLoaded(上海) === true', function () {
        assert.strictEqual(sbD.HP.RE.isCityLoaded('上海'), true);
    });
    check('D6 cityIndex.total_rows === manifest.total_rows', function () {
        assert.strictEqual(dataD.cityIndex.total_rows, manifest.total_rows);
    });
    check('D7 unique city|id count === length', function () {
        assert.strictEqual(uniqueCount(dataD.properties), dataD.properties.length);
    });

    // ================= E. lazy loadCity =================
    sbD.HP.RE.data = dataD;
    bodiesD[cityUrl('北京')] = { body: cityJson['北京'] };
    const beforeBeijing = countUrl(requestedD, cityUrl('北京'));
    const loadedBeijing = await sbD.HP.RE.loadCity('北京');

    check('E1 loadCity(北京) appends manifest rows', function () {
        assert.strictEqual(dataD.properties.length, manifest.cities['上海'].rows + manifest.cities['北京'].rows);
        assert.strictEqual(loadedBeijing.length, manifest.cities['北京'].rows);
    });
    check('E2 isCityLoaded(北京) === true', function () {
        assert.strictEqual(sbD.HP.RE.isCityLoaded('北京'), true);
    });
    check('E3 unique city|id count === length after lazy load', function () {
        assert.strictEqual(uniqueCount(dataD.properties), dataD.properties.length);
    });
    check('E4 北京 requested exactly once', function () {
        assert.strictEqual(countUrl(requestedD, cityUrl('北京')), beforeBeijing + 1);
    });
    const afterBeijing = countUrl(requestedD, cityUrl('北京'));
    const reloaded = await sbD.HP.RE.loadCity('北京');
    check('E5 repeat loadCity(北京) no extra fetch, same length', function () {
        assert.strictEqual(countUrl(requestedD, cityUrl('北京')), afterBeijing);
        assert.strictEqual(reloaded.length, manifest.cities['北京'].rows);
        assert.strictEqual(dataD.properties.length, manifest.cities['上海'].rows + manifest.cities['北京'].rows);
    });
    const unknown = await sbD.HP.RE.loadCity('不存在市');
    check('E6 loadCity(不存在市) resolves []', function () {
        assert.ok(Array.isArray(unknown));
        assert.strictEqual(unknown.length, 0);
    });

    // ================= G. 全部城市 loadAllCities =================
    sbD.HP.RE.data = dataD;
    bodiesD[cityUrl('广州')] = { body: cityJson['广州'] };
    const allRows = await sbD.HP.RE.loadAllCities();
    const distinct = new Set(dataD.properties.map(function(p){return p.city;}));
    check('G1 全部城市 loadAllCities >=2 cities', function(){ assert.ok(distinct.size>=2,'cities='+Array.from(distinct).join(',')); });
    check('G2 allCitiesLoaded() true after loadAllCities', function(){ assert.strictEqual(sbD.HP.RE.allCitiesLoaded(), true); });
    check('G3 all 3 city rows merged (manifest.total_rows)', function(){ assert.strictEqual(dataD.properties.length, manifest.total_rows); });
    check('G4 loadAllCities resolves merged rows', function(){ assert.ok(Array.isArray(allRows)); assert.strictEqual(allRows.length, manifest.total_rows); });

    // ================= F. fallback parity =================
    const requestedF = [];
    const bodiesF = Object.assign({}, CORE, {
        'data/communities/index.json': { ok: false, body: null },
        'data/communities.json': { body: readJson(FULL_FILE) }
    });
    const sbF = newSandbox();
    loadCommunity(sbF);
    loadLoader(sbF);
    sbF.location = { search: '' };
    sbF.fetch = recordingFetch(bodiesF, requestedF);
    const dataF = await sbF.HP.RE.loadData();

    check('F1 full file requested, cityIndex === null', function () {
        assert.ok(requestedF.indexOf('data/communities.json') >= 0, 'full not requested');
        assert.strictEqual(dataF.cityIndex, null);
    });
    check('F2 fallback properties.length === manifest.total_rows', function () {
        assert.strictEqual(dataF.properties.length, manifest.total_rows);
    });
    check('F3 fallback 上海 rows match city file (projected + normalized)', function () {
        // Legacy full-file fallback has no server-computed evidence; exclude it from parity.
        const cols = cityJson['上海'].cols.filter(function (c) { return c !== 'evidence'; });
        const fromCity = cityObj['上海'].slice().sort(byStringId).map(function (r) { return project(r, cols); });
        const fromFull = dataF.properties.filter(function (p) { return p.city === '上海'; })
            .slice().sort(byStringId).map(function (r) { return project(r, cols); });
        assert.strictEqual(fromFull.length, fromCity.length, 'row count mismatch');
        jEq(fromFull, fromCity, 'fallback parity');
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
