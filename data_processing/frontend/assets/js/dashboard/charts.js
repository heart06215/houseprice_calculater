/* 主看板图表模块：散点 / 峰值气泡 / 排行条形 / 中国地图 / 价格趋势线。
   每个函数签名 (el, 数据, 回调) 返回 echarts 实例，由调用方负责 dispose。 */
window.HP = window.HP || {};

HP.dashCharts = (function () {
    var F = HP.format, C = HP.chart;
    var tierNames = { S: '顶级（北上深）', '1': '一线城市', '2': '二线城市', '3': '三线城市' };

    /** 均价 vs 近12月涨跌幅（对数横轴），按等级分系列 */
    function buildScatter(el, cities, onPick) {
        var chart = C.create(el);
        var series = {};
        cities.forEach(function (c) {
            (series[c.tier] = series[c.tier] || []).push({
                value: [c.current_price, c.change_pct_12m, c.drop_from_peak, c.city],
                name: c.city,
                itemStyle: { color: c.color }
            });
        });
        chart.setOption({
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'item',
                formatter: function (p) {
                    var d = p.value;
                    return '<b>' + d[3] + '</b><br/>均价: ¥' + F.price(d[0]) + '/m²<br/>'
                        + '近12月: ' + F.pct(d[1]) + '<br/>较峰值: ' + F.pct(d[2]);
                }
            },
            grid: C.grid(),
            xAxis: C.axis({
                type: 'log', log: true, name: '当前均价（元/m²）',
                formatter: function (v) { return v >= 10000 ? (v / 10000) + '万' : v; }
            }),
            yAxis: C.axis({ name: '近12月变化（%）' }),
            legend: { top: 0, right: 0, textStyle: { color: C.cText, fontSize: 14 } },
            series: Object.entries(series).map(function (kv) {
                return {
                    name: tierNames[kv[0]] || kv[0],
                    type: 'scatter',
                    data: kv[1],
                    symbolSize: 10,
                    emphasis: { scale: 2, itemStyle: { shadowBlur: 10, borderColor: '#fff', borderWidth: 1 } }
                };
            })
        });
        chart.on('click', function (p) {
            var city = cities.find(function (c) { return c.city === p.value[3]; });
            if (city && onPick) onPick(city);
        });
        return chart;
    }

    /** 峰值年份分布气泡图: x=峰值年份(真实时间轴, 保留年份间隔), y=城市数,
        气泡大小=该年峰值城市的平均峰值价, 颜色=占比最高的城市等级 */
    function buildBubble(el, cities, onPick) {
        var chart = C.create(el);
        var groups = {};
        var unknown = 0;
        cities.forEach(function (c) {
            var y = parseInt(c.peak_year);
            if (!y) { unknown++; return; }
            (groups[y] = groups[y] || []).push(c);
        });
        var data = Object.keys(groups).map(function (k) {
            var list = groups[k];
            var avgMax = Math.round(list.reduce(function (s, c) { return s + c.max_price; }, 0) / list.length);
            var tiers = {};
            list.forEach(function (c) { tiers[c.tier] = (tiers[c.tier] || 0) + 1; });
            var dominant = Object.entries(tiers).sort(function (a, b) { return b[1] - a[1]; })[0][0];
            var names = list.slice().sort(function (a, b) { return b.current_price - a.current_price; })
                .map(function (c) { return c.city; });
            return { year: +k, avgMax: avgMax, count: list.length, names: names,
                     color: tierColorOf(cities, dominant) };
        }).sort(function (a, b) { return a.year - b.year; });

        var yMin = Math.min.apply(null, data.map(function (d) { return d.year; }));
        var yMax = Math.max.apply(null, data.map(function (d) { return d.year; }));
        var maxCount = Math.max.apply(null, data.map(function (d) { return d.count; }));

        chart.setOption({
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'item',
                formatter: function (p) {
                    var d = p.data;
                    var names = d.names.slice(0, 8).join('、') + (d.names.length > 8 ? '...' : '');
                    return '<b>' + d.value[0] + '年达到峰值: ' + d.value[1] + ' 城</b>'
                        + '<br/>平均峰值: ¥' + F.price(d.value[2]) + '/m²'
                        + '<br/>' + names;
                }
            },
            grid: C.grid(),
            xAxis: C.axis({
                type: 'value', name: '峰值年份',
                min: yMin - 0.5, max: yMax + 0.5, interval: 1,
                axisLabel: { formatter: function (v) { return v + '年'; } }
            }),
            yAxis: C.axis({ name: '城市数', minInterval: 1 }),
            series: [{
                type: 'scatter',
                data: data.map(function (d) {
                    return { value: [d.year, d.count, d.avgMax], name: d.year + '年',
                             itemStyle: { color: d.color, opacity: 0.72 } };
                }),
                // 气泡大小随平均峰值价变化(城市数已在 y 轴显式表达)
                symbolSize: function (d) { return Math.max(12, Math.min(54, Math.sqrt(d[2]) / 3.2)); },
                label: {
                    show: true, position: 'top', color: '#111111', fontSize: 14, fontWeight: 600,
                    formatter: function (p) { return p.value[1] + '城'; }
                },
                emphasis: { scale: 1.25, itemStyle: { shadowBlur: 15, borderColor: '#fff', borderWidth: 2 } }
            }],
            graphic: unknown ? [{
                type: 'text', right: 12, top: 8,
                style: { text: unknown + ' 城峰值未知未计入', fill: '#3d5560', fontSize: 13 }
            }] : []
        });
        chart.on('click', function (p) {
            var year = parseInt(p.value[0]);
            var inYear = cities.filter(function (c) { return c.peak_year === year; })
                .sort(function (a, b) { return b.current_price - a.current_price; });
            if (inYear.length && onPick) onPick(inYear[0]);
        });
        return chart;
    }

    function tierColorOf(cities, tier) {
        var hit = cities.find(function (c) { return c.tier === tier; });
        return hit ? hit.color : '#0aa0c8';
    }

    /** 各等级城市均价排行（横向条形 + dataZoom） */
    function buildBar(el, cities, onPick) {
        var chart = C.create(el);
        var tierOrder = ['S', '1', '2', '3'];
        var allData = [];
        tierOrder.forEach(function (tier) {
            cities.filter(function (c) { return c.tier === tier; })
                .sort(function (a, b) { return a.current_price - b.current_price; })
                .forEach(function (c) { allData.push(c); });
        });
        chart.setOption({
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'axis',
                axisPointer: { type: 'shadow' },
                formatter: function (params) {
                    var c = allData[params[0].dataIndex];
                    return '<b>' + c.city + '</b>（' + (tierNames[c.tier] || c.tier) + '）<br/>'
                        + '当前均价: ¥' + F.price(c.current_price) + '/m²<br/>较峰值: ' + F.pct(c.drop_from_peak);
                }
            },
            grid: { left: 90, right: 150, top: 20, bottom: 40 },
            xAxis: C.axis({ formatter: function (v) { return (v / 10000).toFixed(1) + '万'; } }),
            yAxis: {
                type: 'category',
                data: allData.map(function (c) { return c.city; }),
                axisLabel: { color: C.cText, fontSize: 14 },
                axisLine: { lineStyle: { color: C.cLine } },
                axisTick: { show: false }
            },
            series: [{
                type: 'bar',
                barMaxWidth: 28,
                barMinWidth: 8,
                data: allData.map(function (c) {
                    return { value: c.current_price, itemStyle: { color: c.color, opacity: 0.85, borderRadius: [0, 4, 4, 0] } };
                }),
                label: {
                    show: true, position: 'right', fontSize: 14, color: C.cText,
                    formatter: function (p) {
                        return '¥' + (p.value / 10000).toFixed(1) + '万 (' + allData[p.dataIndex].drop_from_peak + '%)';
                    }
                }
            }],
            dataZoom: [{
                type: 'slider', start: 0, end: 100, height: 20, bottom: 5,
                borderColor: C.cLine, backgroundColor: 'transparent',
                fillerColor: 'rgba(10,160,200,0.22)', handleStyle: { color: '#0aa0c8' }
            }]
        });
        chart.on('click', function (p) {
            var c = allData[p.dataIndex];
            if (c && onPick) onPick(c);
        });
        return chart;
    }

    /** 中国地图：geo 散点，等级决定符号大小，labelLayout 防重叠 */
    function buildChinaMap(el, cities, geojson, onPick) {
        echarts.registerMap('china', geojson);
        var chart = C.create(el);
        var mapData = cities.map(function (c) {
            return {
                name: c.map_name || c.city,
                value: [c.lon, c.lat, c.current_price, c.change_pct_12m, c.drop_from_peak],
                cityRealName: c.city,
                tier: c.tier,
                itemStyle: { color: c.color }
            };
        });
        chart.setOption({
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'item',
                formatter: function (p) {
                    if (!p.data || !p.data.cityRealName) return p.name;
                    var d = p.data.value;
                    return '<b>' + p.data.cityRealName + '</b><br/>均价: ¥' + F.price(d[2]) + '/m²<br/>'
                        + '近12月: ' + F.pct(d[3]) + '<br/>较峰值: ' + F.pct(d[4]);
                }
            },
            geo: {
                map: 'china',
                roam: true,
                zoom: 1.15,
                center: [104.5, 36],
                label: { show: false },
                itemStyle: { areaColor: '#dce8ec', borderColor: '#8aa8b0' },
                emphasis: { itemStyle: { areaColor: '#c5dce3' }, label: { show: false } }
            },
            series: [{
                type: 'scatter',
                coordinateSystem: 'geo',
                data: mapData,
                symbolSize: function (v, p) {
                    var price = v[2], tier = p.data.tier;
                    if (tier === 'S') return Math.max(18, price / 2000);
                    if (tier === '1') return Math.max(13, price / 3000);
                    if (tier === '2') return Math.max(10, price / 4000);
                    return Math.max(8, price / 5000);
                },
                itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,0.15)' },
                emphasis: { scale: 2, itemStyle: { shadowBlur: 20, borderColor: '#fff', borderWidth: 2 } },
                label: {
                    show: true, position: 'bottom', fontSize: 13, color: C.cText,
                    formatter: function (p) { return p.data.cityRealName; }
                },
                labelLayout: { hideOverlap: true }
            }]
        });
        chart.on('click', function (p) {
            if (!p.data || !p.data.cityRealName) return;
            var city = cities.find(function (c) { return c.city === p.data.cityRealName; });
            if (city && onPick) onPick(city);
        });
        return chart;
    }

    /** 价格趋势双轴图（opts.months 限制最近 N 个月，opts.compact 紧凑模式） */
    function buildPriceLine(el, city, opts) {
        opts = opts || {};
        var chart = C.create(el);
        var raw = (city && city.monthly) || [];
        var byMonth = new Map(raw.map(m => [m.date, m]));
        var monthly = HP.analysis ? HP.analysis.calendar(raw.map(m => m.date)).map(date => byMonth.get(date) || {date:date,price:null,change_pct:null}) : raw;
        var recent = opts.months ? monthly.slice(-opts.months) : monthly;
        var months = recent.map(function (m) { return m.date; });
        var prices = recent.map(function (m) { return m.price; });
        var changes = recent.map(function (m) { return Number.isFinite(m.change_pct) ? m.change_pct : null; });
        var color = (C.lineColor) || '#dc2626';
        var labelSize = opts.compact ? 10 : 11;
        var touch = window.matchMedia('(pointer:coarse)').matches;

        chart.setOption({
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'axis',
                confine: true,
                formatter: function (params) {
                    if (!params.length) return '';
                    var html = params[0].axisValue + '<br/>';
                    params.forEach(function (p) {
                        var val = p.seriesName === '均价' || p.seriesName === '价格'
                            ? '¥' + F.price(p.value) : F.pct(p.value);
                        html += p.marker + ' ' + p.seriesName + ': ' + val + '<br/>';
                    });
                    html += F.priceSource(recent[params[0].dataIndex]);
                    return html;
                }
            },
            grid: opts.compact
                ? { left: 55, right: 45, top: 20, bottom: 40 }
                : {left:18,right:18,top:65,bottom:80,containLabel:true},
            xAxis: C.axis({ type: 'category', data: months, rotate: 45, splitLine: false }),
            yAxis: [
                C.axis({
                    name: opts.compact ? '' : '价格（元/m²）',
                    formatter: function (v) { return (v / 10000).toFixed(1) + '万'; }
                }),
                C.axis({ name: opts.compact ? '' : '环比（%）', splitLine: false })
            ],
            series: [
                {
                    name: '均价', type: 'line', data: prices, smooth: false, showSymbol: false, connectNulls: false,
                    lineStyle: { color: color, width: 2.5 },
                    itemStyle: { color: color },
                    areaStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: color + '33' },
                            { offset: 1, color: color + '05' }
                        ])
                    }
                },
                {
                    name: '环比变化', type: 'bar', yAxisIndex: 1,
                    data: changes.map(function (v) {
                        return { value: v, itemStyle: { color: v >= 0 ? ((C.upColor || '#dc2626') + 'cc') : ((C.downColor || '#16a34a') + 'cc') } };
                    })
                }
            ],
            legend: opts.compact ? undefined : { top: 0, left: 'center', textStyle: { color: C.cText, fontSize: 14 } },
            dataZoom: opts.compact ? [] : [{type:'inside',disabled:touch,start:65,zoomOnMouseWheel:'ctrl',moveOnMouseWheel:false},{type:'slider',start:65,height:touch?44:20,bottom:8}],
            media: opts.compact ? [] : [
                {query:{maxWidth:500},option:{legend:{textStyle:{fontSize:12}},
                    xAxis:{axisLabel:{rotate:0,fontSize:12,hideOverlap:true}},
                    yAxis:[{axisLabel:{fontSize:12}},{axisLabel:{fontSize:12}}],
                    dataZoom:[{disabled:true},{height:44}]}},
                {option:{legend:{textStyle:{fontSize:14}},xAxis:{axisLabel:{rotate:45,fontSize:14,hideOverlap:false}},
                    yAxis:[{axisLabel:{fontSize:14}},{axisLabel:{fontSize:14}}],dataZoom:[{disabled:touch},{height:touch?44:20}]}}
            ]
        });
        return chart;
    }

    return {
        buildScatter: buildScatter,
        buildBubble: buildBubble,
        buildBar: buildBar,
        buildChinaMap: buildChinaMap,
        buildPriceLine: buildPriceLine
    };
})();
