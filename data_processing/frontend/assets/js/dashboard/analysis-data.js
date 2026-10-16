/* Shared aggregation rules. Missing values never become zero. */
(function () {
    const finite = Number.isFinite;
    const positive = v => finite(v) && v > 0;
    const mean = values => values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
    const median = values => {
        const sorted = values.slice().sort((a, b) => a - b), n = sorted.length;
        return n ? (sorted[Math.floor((n - 1) / 2)] + sorted[Math.floor(n / 2)]) / 2 : null;
    };
    function shift(month, offset) {
        const [year, number] = month.split('-').map(Number);
        return new Date(Date.UTC(year, number - 1 + offset, 1)).toISOString().slice(0, 7);
    }
    function calendar(months, start) {
        const sorted = months.filter(m => m && (!start || m >= start)).sort();
        if (!sorted.length) return [];
        const result = [];
        for (let m = start || sorted[0]; m <= sorted.at(-1); m = shift(m, 1)) result.push(m);
        return result;
    }
    const cityMap = c => new Map((c.monthly || []).map(r => [r.date, r]));
    function comparable(map, month) {
        const row = map.get(month), previous = map.get(shift(month, -1));
        return positive(row?.price) && positive(previous?.price) && finite(row.change_pct) ? row.change_pct : null;
    }
    function breadth(cities) {
        const maps = cities.map(cityMap);
        return calendar(cities.flatMap(c => (c.monthly || []).map(r => r.date)), '2021-04').map(month => {
            const changes = maps.map(map => comparable(map, month)).filter(finite);
            const rising = changes.filter(v => v > 0.05).length, falling = changes.filter(v => v < -0.05).length;
            return {month, rising, falling, flat: changes.length - rising - falling,
                valid: changes.length, missing: cities.length - changes.length, total: cities.length};
        });
    }
    function nationalPrices(cities) {
        const months = calendar(cities.flatMap(c => (c.monthly || []).map(r => r.date)), '2021-04');
        // Keep a fixed complete cohort so sample changes cannot create false price rises.
        const cohort = cities.map(cityMap).filter(map => months.length && months.every(m => positive(map.get(m)?.price)));
        return {count: cohort.length, total: cities.length, rows: months.map(month => {
            const prices = cohort.map(map => map.get(month).price);
            return {month, mean: mean(prices), median: median(prices)};
        })};
    }
    function nationalOfficial(segment, cityNames) {
        const cities = (segment?.cities || []).filter(c => cityNames.includes(c.city));
        const months = calendar(cities.flatMap(c => (c.monthly || []).map(r => r.month)), '2021-04');
        const maps = cities.map(c => new Map(c.monthly.map(r => [r.month, r])));
        return months.map(month => {
            const row = {month};
            for (const key of ['mom_pct', 'yoy_pct']) {
                const values = maps.map(map => map.get(month)?.[key]).filter(finite);
                row[key] = mean(values); row[key + '_count'] = values.length;
            }
            return row;
        });
    }
    function citySnapshot(city, month) {
        const map = cityMap(city || {}), row = map.get(month) || {};
        const past = [...map].filter(([m, r]) => m <= month && positive(r.price)).map(([, r]) => r.price);
        const peak = past.length ? Math.max(...past) : null, year = map.get(shift(month, -12));
        const last3 = [-2, -1, 0].map(n => comparable(map, shift(month, n)));
        return {row, peak, mom_pct: comparable(map, month),
            yoy_pct: positive(row.price) && positive(year?.price) ? (row.price / year.price - 1) * 100 : null,
            drop_pct: positive(row.price) && peak ? (row.price / peak - 1) * 100 : null,
            rising3: last3.every(v => finite(v) && v > 0.05),
            change3: last3.every(finite) ? (last3.reduce((a, v) => a * (1 + v / 100), 1) - 1) * 100 : null};
    }
    function cityInformation(info, city, month) {
        const cih = info.cih || {}, release = (info.releases || []).find(r => r.month === month);
        return {
            release, official: (release?.rows || []).filter(r => r.city === city),
            prices: (cih.prices || []).filter(r => r.city === city && r.period === month),
            daily: (cih.daily || []).filter(r => r.city === city).slice().sort((a, b) => b.date.localeCompare(a.date)),
            metrics: (cih.metrics || []).filter(r => r.city === city || r.scope === city),
            articles: (cih.articles || []).filter(r => (r.title + ' ' + (r.excerpt || '')).includes(city))
        };
    }
    window.HP = window.HP || {};
    /** 默认观察月取数据覆盖≥ratio*最大覆盖的最近月份，避免少数新报价改变默认范围。
        统计局报告期与挂牌观测期独立；覆盖较少的月份仍可手动选择。
        monthLists: 每城市一个月份字符串数组（同一城市的重复月份自动去重计数）。 */
    function defaultObservationMonth(monthLists, ratio) {
        ratio = ratio || 0.8;
        const counts = {};
        for (const list of (monthLists || [])) {
            const seen = new Set(list || []);
            for (const m of seen) counts[m] = (counts[m] || 0) + 1;
        }
        const entries = Object.entries(counts);
        if (!entries.length) return '';
        const max = Math.max(...entries.map(e => e[1]));
        const threshold = Math.max(1, Math.ceil(max * ratio));
        const ok = entries.filter(e => e[1] >= threshold).map(e => e[0]).sort().reverse();
        return ok[0] || (entries.sort((a, b) => b[1] - a[1])[0] || [''])[0];
    }
    function observationCoverage(monthLists) {
        const counts = {};
        for (const list of (monthLists || [])) {
            const seen = new Set(list || []);
            for (const m of seen) counts[m] = (counts[m] || 0) + 1;
        }
        return counts;
    }
    HP.analysis = {finite, positive, mean, median, shift, calendar, comparable, breadth, nationalPrices, nationalOfficial, citySnapshot, cityInformation, defaultObservationMonth, observationCoverage};
})();
