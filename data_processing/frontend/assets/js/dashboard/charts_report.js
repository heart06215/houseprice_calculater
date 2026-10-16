/* 市场研报图表模块: 风向标堆积柱 / 能级分化指数 / 回撤分布直方图。
   框架参考主流券商 70 城房价点评与机构研究(国家统计局解读、CF40、券商研报):
   ① 涨跌城市数风向标 ② 能级分化 ③ 政策节点 ④ 企稳信号。 */
window.HP = window.HP || {};

HP.dashChartsReport = (function () {
    var F = HP.format, C = HP.chart;
    var tierNames = { S: '一线城市', '1': '新一线城市', '2': '二线城市', '3': '三线城市' };

    function tierColorOf(cities, tier) {
        var hit = cities.find(function (c) { return c.tier === tier; });
        return hit ? hit.color : '#0aa0c8';
    }

    /** 图一: 70城价格风向标 — 每月环比上涨/持平/下跌城市数堆积柱 */
    function buildMomentumStack(el, cities) {
        var chart = C.create(el);
        var rows = HP.analysis.breadth(cities);
        var dates = rows.map(r => r.month), up = rows.map(r => r.rising),
            flat = rows.map(r => r.flat), down = rows.map(r => r.falling);
        chart.setOption({
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'axis',
                confine: true,
                formatter: function (ps) {
                    if (!ps.length) return '';
                    var i = ps[0].dataIndex;
                    var total = up[i] + flat[i] + down[i];
                    var html = '<b>' + dates[i] + '</b>（可比 ' + total + '/' + rows[i].total + ' 城）';
                    ps.forEach(function (p) {
                        html += '<br/>' + p.marker + p.seriesName + ': ' + p.value + ' 城';
                        if (p.seriesName !== '不可比' && total) html += '（' + Math.round(p.value / total * 100) + '%）';
                    });
                    return html;
                }
            },
            legend: { top: 0, textStyle: { color: C.cText, fontSize: 14 } },
            grid: {left: 18, right: 20, top: 55, bottom: 85, containLabel: true},
            dataZoom: [{type:'inside',start:55,zoomOnMouseWheel:'ctrl',moveOnMouseWheel:false},{type:'slider',start:55,height:20,bottom:8}],
            xAxis: C.axis({
                type: 'category', data: dates,
                axisLabel: {
                    rotate: dates.length > 12 ? 45 : 0,
                    interval: Math.max(0, Math.ceil(dates.length / 18) - 1)
                }
            }),
            yAxis: C.axis({ name: '城市数', minInterval: 1, max: cities.length }),
            series: [
                { name: '上涨', type: 'bar', stack: 'm', data: up, barMaxWidth: 26,
                  itemStyle: { color: '#dc2626' }, emphasis: { focus: 'series' } },
                { name: '持平', type: 'bar', stack: 'm', data: flat,
                  itemStyle: { color: '#94a3a8' }, emphasis: { focus: 'series' } },
                { name: '下跌', type: 'bar', stack: 'm', data: down,
                  itemStyle: { color: '#16a34a' }, emphasis: { focus: 'series' } },
                { name: '不可比', type: 'bar', stack: 'm', data: rows.map(r => r.missing),
                  itemStyle: { color: '#e8ecee', borderColor: '#6b7c82', borderWidth: 1 } }
            ]
        });
        return chart;
    }

    /** 图二: 能级分化 — 各能级定基指数(2021-04=100) + 政策节点标注(可点击) */
    function buildTierIndex(el, cities, onPolicyPick, policies) {
        var chart = C.create(el);
        var POLICIES = policies || [];
        var series = [];
        ['S', '1', '2', '3'].forEach(function (tier) {
            var list = cities.filter(function (c) { return c.tier === tier; });
            if (!list.length) return;
            const cohort = HP.analysis.nationalPrices(list);
            if (!cohort.count) return;
            const baseAvg = cohort.rows.find(row => row.month === '2021-04')?.mean;
            if (!Number.isFinite(baseAvg) || baseAvg <= 0) return;
            series.push({
                name: tierNames[tier] || tier,
                color: ({ S: '#dc2626', '1': '#b91c1c', '2': '#ea580c', '3': '#9f1239' })[tier] || '#dc2626',
                count: cohort.count, total: list.length,
                points: cohort.rows.map(row => [row.month, Math.round(row.mean / baseAvg * 1000) / 10])
            });
        });
        chart.setOption({
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'axis',
                formatter: function (ps) {
                    var html = '<b>' + ps[0].value[0] + '</b>';
                    ps.forEach(function (p) {
                        const sample = series.find(row => row.name === p.seriesName);
                        html += '<br/>' + p.marker + p.seriesName + ': ' + p.value[1] +
                            '（固定可比 ' + sample.count + '/' + sample.total + ' 城）';
                    });
                    return html;
                }
            },
            legend: { top: 0, textStyle: { color: C.cText, fontSize: 14 } },
            grid: C.grid({right:35,top:65}),
            xAxis: C.axis({ type: 'time', max:extent=>Math.max(extent.max,Date.parse(POLICIES.reduce((latest,p)=>p.date>latest?p.date:latest,'2021-04')+'-28')) }),
            yAxis: C.axis({ name: '定基指数（2021-04=100）' }),
            series: series.map(function (s,index) {
                return {
                    name: s.name, type: 'line', color: s.color,
                    data: s.points, showSymbol: false,
                    emphasis: { focus: 'series' },
                    markLine: {
                        symbol: 'none', animation: false,
                        data: (index===0?POLICIES:[]).map(function (p) {
                            return {
                                name: p.name,
                                xAxis: p.published_at || p.date,
                                lineStyle: { color: '#c2410c', type: 'dashed', width: 1.6 },
                                label: { formatter: p.name + ' ▸', fontSize: 12, color: '#c2410c', fontWeight: 600, position: 'insideEndTop' }
                            };
                        })
                    }
                };
            })
        });
        // 点击政策节点虚线 → 展开简略描述
        chart.on('click', function (p) {
            if (p.componentType === 'markLine' && onPolicyPick) {
                var name = (p.data && p.data.name) || '';
                onPolicyPick(name);
            }
        });
        return chart;
    }

    /** 图三: 较峰值回撤分布直方图 */
    function bucketOf(d) {
        if (d > -10) return 0;
        if (d > -20) return 1;
        if (d > -30) return 2;
        if (d > -40) return 3;
        if (d > -50) return 4;
        return 5;
    }
    var DD_BUCKETS = [
        { l: '0~-10%', c: '#7dd3e8' },
        { l: '-10~-20%', c: '#0aa0c8' },
        { l: '-20~-30%', c: '#176878' },
        { l: '-30~-40%', c: '#c2410c' },
        { l: '-40~-50%', c: '#dc2626' },
        { l: '超-50%', c: '#7f1d1d' }
    ];

    function buildDrawdownHist(el, cities) {
        var chart = C.create(el);
        var counts = [0, 0, 0, 0, 0, 0];
        var members = [[], [], [], [], [], []];
        cities.forEach(function (c) {
            if (!Number.isFinite(c.drop_from_peak)) return;
            var b = bucketOf(c.drop_from_peak);
            counts[b]++;
            members[b].push(c.city);
        });
        chart.setOption({
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'item',
                formatter: function (p) {
                    var list = members[p.dataIndex].slice(0, 10).join('、');
                    return '<b>较峰值跌幅 ' + DD_BUCKETS[p.dataIndex].l + '</b><br/>'
                        + p.value + ' 城<br/>' + list + (members[p.dataIndex].length > 10 ? '...' : '');
                }
            },
            grid: C.grid(),
            xAxis: Object.assign(C.axis({ type: 'category', data: DD_BUCKETS.map(function (b) { return b.l; }) }), {name: '较峰值跌幅区间', nameLocation: 'middle', nameGap: 32, axisLabel: {color: C.cText, fontSize: 14, hideOverlap: true}}),
            yAxis: C.axis({ name: '城市数', minInterval: 1 }),
            series: [{
                type: 'bar', barMaxWidth: 44,
                data: DD_BUCKETS.map(function (b, i) {
                    return { value: counts[i], itemStyle: { color: b.c, opacity: 0.85, borderRadius: [4, 4, 0, 0] } };
                }),
                label: {
                    show: true, position: 'top', color: C.cText, fontSize: 14,
                    formatter: function (p) { return p.value + '城'; }
                }
            }]
        });
        return chart;
    }

    return {
        buildMomentumStack: buildMomentumStack,
        buildTierIndex: buildTierIndex,
        buildDrawdownHist: buildDrawdownHist,
        DD_BUCKETS: DD_BUCKETS,
        bucketOf: bucketOf
    };
})();
