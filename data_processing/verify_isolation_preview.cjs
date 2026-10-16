'use strict';
/* Offline/HTTP parity runner for the isolated community-filter preview.
   Plain CommonJS (mirrors test_community_query.cjs).

   Usage:
     node verify_isolation_preview.cjs --root <previewDir> [--base-url http://127.0.0.1:PORT/]

   Loads the ISOLATED frontend modules (community.js/loader.js/app.js) in a vm
   sandbox, builds the dataset twice (HTTP/fetch shim + inlined __RE_DATA__),
   runs the SAME query on both and prints parity JSON. Exits non-zero on any
   failed parity. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function parseArgs(argv) {
    const args = {};
    for (let i = 0; i < argv.length; i++) {
        const token = argv[i];
        if (token.startsWith('--')) {
            const key = token.slice(2);
            const next = argv[i + 1];
            if (next !== undefined && !next.startsWith('--')) { args[key] = next; i++; }
            else { args[key] = true; }
        }
    }
    return args;
}

const args = parseArgs(process.argv.slice(2));
if (!args.root) {
    console.error('usage: node verify_isolation_preview.cjs --root <previewDir> [--base-url URL]');
    process.exit(2);
}
const ROOT = path.resolve(args.root);
const BASE_URL = typeof args['base-url'] === 'string' ? args['base-url'] : null;

const QUERY = {
    city: '上海', forms: ['别墅'], yearFrom: 2000, yearTo: 2015,
    metroMaxM: 2000, stationOperationalScope: 'operational'
};
const CHOSEN_NAME = '万顺水原墅';
const CHOSEN_CITY = '上海';

/* ---------- sandbox ---------- */
function fsFetch() {
    return function (url) {
        const clean = decodeURIComponent(String(url).split('?')[0].split('#')[0]);
        const file = path.join(ROOT, clean);
        try {
            const text = fs.readFileSync(file, 'utf8');
            const data = JSON.parse(text);
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });
        } catch (err) {
            return Promise.resolve({ ok: false, status: 404, json: () => Promise.reject(err) });
        }
    };
}

function httpFetch() {
    return function (url) {
        const full = new URL(String(url), BASE_URL).toString();
        return fetch(full).then(function (res) {
            return { ok: res.ok, status: res.status, json: () => res.json() };
        });
    };
}

function makeSandbox(fetchImpl) {
    const sb = { console, setTimeout, clearTimeout };
    sb.window = sb;
    sb.self = sb;
    sb.location = { search: '' };
    sb.addEventListener = function () {};
    sb.window.addEventListener = function () {};
    sb.fetch = fetchImpl;
    return sb;
}

function runModules(sb) {
    const jsDir = path.join(ROOT, 'app', 'assets', 'js');
    const context = vm.createContext(sb);
    for (const name of ['community.js', 'loader.js', 'app.js']) {
        vm.runInContext(fs.readFileSync(path.join(jsDir, name), 'utf8'), context, { filename: name });
    }
    return sb.HP.RE;
}

/* Locate the injected assignment `window.<name> = { ... };` (loader.js also mentions
   window.__RE_DATA__, so a bare indexOf finds the wrong occurrence). From the '{'
   extract the balanced object with a brace matcher that respects double-quoted
   strings and backslash escapes. */
function extractAssignedObject(html, name) {
    const re = new RegExp('window\\.' + name + '\\s*=\\s*\\{');
    const match = re.exec(html);
    if (!match) {
        const at = html.indexOf('window.' + name);
        const near = at >= 0 ? html.slice(at, at + 120) : '(marker not found)';
        throw new Error('no "window.' + name + ' = {" assignment; near: ' + near);
    }
    const brace = match.index + match[0].length - 1;
    let depth = 0, inStr = false, esc = false;
    for (let i = brace; i < html.length; i++) {
        const ch = html[i];
        if (inStr) {
            if (esc) esc = false;
            else if (ch === '\\') esc = true;
            else if (ch === '"') inStr = false;
            continue;
        }
        if (ch === '"') { inStr = true; continue; }
        if (ch === '{') depth++;
        else if (ch === '}') {
            depth--;
            if (depth === 0) return JSON.parse(html.slice(brace, i + 1));
        }
    }
    throw new Error('unbalanced object literal for window.' + name);
}

function extractReData(html) {
    return extractAssignedObject(html, '__RE_DATA__');
}

async function loadHttpDataset() {
    const sb = makeSandbox(BASE_URL ? httpFetch() : fsFetch());
    const RE = runModules(sb);
    const data = await RE.loadData();
    RE.data = data;
    await RE.loadAllCities();
    return { sb, RE, rows: RE.data.properties, stations: RE.data.stations, versions: RE.data.metroVersions };
}

async function loadOfflineDataset() {
    const sb = makeSandbox(fsFetch());
    const RE = runModules(sb);
    const html = fs.readFileSync(path.join(ROOT, 'index_offline.html'), 'utf8');
    sb.window.__RE_DATA__ = extractReData(html);
    const data = await RE.loadData();
    RE.data = data;
    await RE.loadAllCities();
    return { sb, RE, rows: RE.data.properties, stations: RE.data.stations, versions: RE.data.metroVersions };
}

function idsOf(list) { return list.map(function (row) { return String(row.id); }).sort(); }
function sameSet(a, b) { return a.length === b.length && a.every(function (id, i) { return id === b[i]; }); }

function queryOf(dataset) {
    const options = Object.assign({}, QUERY, {
        stations: dataset.stations, versions: dataset.versions
    });
    const res = dataset.RE.community.query(dataset.rows, options);
    const combined = res.supported.concat(res.pending);
    return { options, res, combined, listIds: idsOf(combined) };
}

function markerIdsOf(combined) {
    return idsOf(combined.filter(function (row) {
        return Number.isFinite(row.lat) && Number.isFinite(row.lng);
    }));
}

function chosenOf(dataset, result) {
    const chosen = dataset.rows.find(function (row) {
        return row.name === CHOSEN_NAME && row.city === CHOSEN_CITY;
    });
    if (!chosen) return { found: false };
    const finite = Number.isFinite(chosen.lat) && Number.isFinite(chosen.lng);
    const metro = dataset.RE.community.distance(chosen, dataset.stations, dataset.versions);
    const explain = dataset.RE.community.explain(chosen, result.options);
    return {
        found: true,
        id: chosen.id,
        in_supported: result.res.supported.indexOf(chosen) >= 0,
        finite_coords: finite,
        lat: chosen.lat, lng: chosen.lng,
        nearest_station: metro.station || null,
        nearest_metres: Number.isFinite(metro.meters) ? Math.round(metro.meters * 10) / 10 : null,
        metro_status: metro.status,
        conditions: explain.map(function (c) { return { condition: c.condition, status: c.status, reason: c.reason }; })
    };
}

function pendingCheck(dataset, queryResult) {
    const pendingPath = path.join(ROOT, 'data', 'pending_location.json');
    let records = [];
    try { records = (JSON.parse(fs.readFileSync(pendingPath, 'utf8')).records) || []; }
    catch (err) { records = []; }
    const pendingIds = new Set(records.map(function (r) { return String(r.id); }));
    const markers = new Set(markerIdsOf(queryResult.combined));
    let withCoords = 0, inMarkers = 0;
    dataset.rows.forEach(function (row) {
        if (!pendingIds.has(String(row.id))) return;
        if (Number.isFinite(row.lat) && Number.isFinite(row.lng)) withCoords++;
        if (markers.has(String(row.id))) inMarkers++;
    });
    return {
        pending_records: records.length,
        pending_ids_in_dataset: dataset.rows.filter(function (r) { return pendingIds.has(String(r.id)); }).length,
        pending_rows_with_coords: withCoords,
        pending_ids_in_marker_set: inMarkers,
        assert_no_pending_with_coords: withCoords === 0,
        assert_no_pending_in_markers: inMarkers === 0
    };
}

async function main() {
    const http = await loadHttpDataset();
    const offline = await loadOfflineDataset();

    const httpResult = queryOf(http);
    const offlineResult = queryOf(offline);

    // Real frontend: map `state.filtered` and the list both use supported.concat(pending);
    // markers are that same set with finite coordinates (community-map.js clusterLayer).
    const mapFilteredIds = idsOf(offlineResult.res.supported.concat(offlineResult.res.pending));
    const markerIds = markerIdsOf(offlineResult.combined);
    const mapListParity = sameSet(mapFilteredIds, offlineResult.listIds)
        && markerIds.every(function (id) { return mapFilteredIds.indexOf(id) >= 0; });

    const chosen = chosenOf(offline, offlineResult);
    const clickJumpCheck = !!(chosen.found && chosen.in_supported && chosen.finite_coords
        && markerIds.indexOf(String(chosen.id)) >= 0
        && typeof offline.RE.openCityView === 'function');

    const pending = pendingCheck(offline, offlineResult);

    const out = {
        mode: BASE_URL ? 'http' : 'fs',
        base_url: BASE_URL,
        http_result_ids: httpResult.listIds,
        offline_result_ids: offlineResult.listIds,
        same_result_set: sameSet(httpResult.listIds, offlineResult.listIds),
        map_list_parity: mapListParity,
        map_filtered_ids: mapFilteredIds,
        marker_ids: markerIds,
        http_marker_ids: markerIdsOf(httpResult.combined),
        chosen: chosen,
        click_jump_check: clickJumpCheck,
        open_city_view_type: typeof offline.RE.openCityView,
        pending_check: pending
    };

    const failed = !out.same_result_set || !out.map_list_parity || !out.click_jump_check
        || !pending.assert_no_pending_with_coords || !pending.assert_no_pending_in_markers;
    console.log(JSON.stringify(out));
    process.exit(failed ? 1 : 0);
}

main().catch(function (err) {
    console.error('[verify_isolation_preview] FAIL: ' + (err && err.stack || err));
    process.exit(1);
});
