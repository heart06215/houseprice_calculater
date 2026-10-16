/* Node vm test: HP.RE.loadCity lost-update race, idempotency, failure retry.
 * Run from repo root:  D:/nodejs/node.exe data_processing/test_loader_city.cjs
 * Node built-ins only (fs, vm, assert). No network, no deps.
 */
'use strict';
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const LOADER = __dirname + '/frontend/realestate/assets/js/loader.js';

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
function read(file) { return fs.readFileSync(file, 'utf8'); }
function cityUrl(city) { return 'data/communities/' + encodeURIComponent(city) + '.json'; }
function cityDoc(city, ids) {
    return {
        cols: ['id', 'name', 'city'],
        rows: ids.map(function (id) { return [id, city + '-' + id, city]; })
    };
}
function uniqueCount(rows) {
    const seen = new Set();
    for (let i = 0; i < rows.length; i++) seen.add(rows[i].city + '|' + rows[i].id);
    return seen.size;
}
function countUrl(requested, needle) {
    let n = 0;
    for (let i = 0; i < requested.length; i++) if (requested[i].indexOf(needle) >= 0) n++;
    return n;
}
// Fake fetch keyed by exact URL; records every requested URL.
function makeFetch(bodies, requested) {
    return function (url) {
        requested.push(url);
        const spec = bodies[url];
        if (!spec) return Promise.resolve({ ok: false, status: 404, json: function () { return Promise.resolve(null); } });
        const ok = spec.ok !== false;
        return Promise.resolve({ ok: ok, status: ok ? 200 : 404, json: function () { return Promise.resolve(spec.body); } });
    };
}
function newSandbox() {
    const sandbox = {};
    sandbox.window = sandbox;
    sandbox.console = console;
    return sandbox;
}

(async function main() {
    const rowsA = 3, rowsB = 2, rowsC = 2, rowsD = 0;
    const idsA = ['a1', 'a2', 'a3'];
    const idsB = ['b1', 'b2'];
    const idsC = ['c1', 'c2'];
    const manifest = {
        cities: { A: { rows: rowsA }, B: { rows: rowsB }, C: { rows: rowsC }, D: { rows: rowsD } },
        total_rows: rowsA + rowsB + rowsC + rowsD
    };

    const requested = [];
    const bodies = {};
    bodies[cityUrl('A')] = { body: cityDoc('A', idsA) };
    bodies[cityUrl('B')] = { body: cityDoc('B', idsB) };

    const sb = newSandbox();
    vm.runInNewContext(read(LOADER), sb);
    sb.HP.RE.cityIndex = manifest;
    sb.HP.RE.loadedCities = {};
    sb.HP.RE.data = { properties: [], loadedCities: {} };
    sb.fetch = makeFetch(bodies, requested);

    // ================= A. concurrent different cities survive =================
    const [subsetA, subsetB] = await Promise.all([sb.HP.RE.loadCity('A'), sb.HP.RE.loadCity('B')]);
    const props = sb.HP.RE.data.properties;

    check('A1 concurrent A+B rows summed', function () {
        assert.strictEqual(props.length, rowsA + rowsB);
    });
    check('A2 both cities present', function () {
        const cities = new Set(props.map(function (p) { return p.city; }));
        assert.ok(cities.has('A'), 'missing A');
        assert.ok(cities.has('B'), 'missing B');
    });
    check('A3 unique city|id count === length', function () {
        assert.strictEqual(uniqueCount(props), props.length);
    });
    check('A4 resolved subset sizes match', function () {
        assert.strictEqual(subsetA.length, rowsA);
        assert.strictEqual(subsetB.length, rowsB);
    });
    check('A5 each city fetched once', function () {
        assert.strictEqual(countUrl(requested, cityUrl('A')), 1);
        assert.strictEqual(countUrl(requested, cityUrl('B')), 1);
    });

    // ================= B. idempotent reload =================
    const beforeA = countUrl(requested, cityUrl('A'));
    const beforeLen = props.length;
    const reloaded = await sb.HP.RE.loadCity('A');

    check('B1 reload length unchanged', function () {
        assert.strictEqual(props.length, beforeLen);
    });
    check('B2 no extra fetch for A', function () {
        assert.strictEqual(countUrl(requested, cityUrl('A')), beforeA);
    });
    check('B3 reload no duplicates', function () {
        assert.strictEqual(uniqueCount(props), props.length);
        assert.strictEqual(reloaded.length, rowsA);
    });

    // ================= C. failure does not mark loaded + retry =================
    const beforeC = sb.HP.RE.data.properties.length;
    bodies[cityUrl('C')] = { ok: false, body: null };
    let rejected = false;
    try { await sb.HP.RE.loadCity('C'); } catch (e) { rejected = true; }

    check('C1 failure rejects', function () { assert.strictEqual(rejected, true); });
    check('C2 failure not marked loaded', function () { assert.strictEqual(sb.HP.RE.isCityLoaded('C'), false); });
    check('C3 failure leaves properties unchanged', function () {
        assert.strictEqual(sb.HP.RE.data.properties.length, beforeC);
    });

    bodies[cityUrl('C')] = { body: cityDoc('C', idsC) };
    const addedC = await sb.HP.RE.loadCity('C');

    check('C4 retry resolves and marks loaded', function () {
        assert.strictEqual(sb.HP.RE.isCityLoaded('C'), true);
        assert.strictEqual(addedC.length, rowsC);
    });
    check('C5 retry adds rowsC', function () {
        assert.strictEqual(sb.HP.RE.data.properties.length, beforeC + rowsC);
    });

    // empty payload must reject and stay unloaded
    bodies[cityUrl('D')] = { body: { cols: ['id', 'name', 'city'], rows: [] } };
    let rejectedEmpty = false;
    try { await sb.HP.RE.loadCity('D'); } catch (e) { rejectedEmpty = true; }

    check('C6 empty payload rejects', function () { assert.strictEqual(rejectedEmpty, true); });
    check('C7 empty payload not marked loaded', function () { assert.strictEqual(sb.HP.RE.isCityLoaded('D'), false); });

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
