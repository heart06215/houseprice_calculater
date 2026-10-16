/* 主看板三个 Tab 组件：图表概览 / 中国地图 / 城市排名。
   通过 inject('hpCtx') 获取根组件的共享响应式状态。
   图表实例在组件 onMounted 创建、onBeforeUnmount 销毁——
   动态组件切换时容器始终可见，天然规避 v-show 隐藏导致
   的零尺寸初始化问题，不再需要手动 resize 补丁。 */
window.HP = window.HP || {};

HP.dashComponents = (function () {
    var F = HP.format;
    var CTX = 'hpCtx';
    var chartsLib = null; // 延迟注入（app.js 启动时赋值）

    function sourceClass(type) {
        var map = { anjuke: 'anjuke', estimated_national: 'national', estimated: 'estimated', derived: 'derived', mixed: 'mixed' };
        return map[type] || 'mixed';
    }
    function sourceLabel(type) {
        return {
            anjuke: '安居客数据', estimated: '统计局估算', estimated_national: '全国趋势估算',
            derived: '逆推推导', mixed: '混合参考',legacy_unverified:'暂无',index_estimate:'逆推结构',reference_estimate:'逆推结构'
        }[type] || type || '混合';
    }

    /* ---------------- 图表概览 ---------------- */
    var TabOverview = {
        name: 'TabOverview',
        setup: function () {
            var _vue = Vue;
            var ctx = _vue.inject(CTX);
            var scatterEl = _vue.ref(null), bubbleEl = _vue.ref(null),
                barEl = _vue.ref(null), detailEl = _vue.ref(null);
            var made = {};

            _vue.onMounted(function () {
                made.scatter = chartsLib.buildScatter(scatterEl.value, ctx.cities.value, ctx.selectCity);
                made.bubble = chartsLib.buildBubble(bubbleEl.value, ctx.cities.value, ctx.selectCity);
                made.bar = chartsLib.buildBar(barEl.value, ctx.cities.value, ctx.selectCity);
            });
            _vue.onBeforeUnmount(function () {
                Object.keys(made).forEach(function (k) { HP.chart.dispose(made[k]); });
                made = {};
            });
            _vue.watch(ctx.selectedCity, function (c) {
                if (!c) return;
                _vue.nextTick(function () {
                    HP.chart.dispose(made.detail);
                    made.detail = chartsLib.buildPriceLine(detailEl.value, c, {});
                });
            });

            return {
                F: F, ctx: ctx, sourceClass: sourceClass, sourceLabel: sourceLabel,
                scatterEl: scatterEl, bubbleEl: bubbleEl, barEl: barEl, detailEl: detailEl,
                sel: ctx.selectedCity
            };
        },
        template:
            '<div class="main">' +
            '  <div class="chart-grid">' +
            '    <div class="chart-card"><div class="chart-card-header"><span class="chart-card-title">当前均价 vs 近12月涨跌幅</span></div>' +
            '      <div class="chart-card-body"><div ref="scatterEl" class="chart-container wide-tall"></div></div></div>' +
            '    <div class="chart-card"><div class="chart-card-header"><span class="chart-card-title">各城市房价峰值年份分布</span></div>' +
            '      <div class="chart-card-body"><div ref="bubbleEl" class="chart-container wide-tall"></div></div></div>' +
            '    <div class="chart-card full-width"><div class="chart-card-header"><span class="chart-card-title">各等级城市当前均价排行</span></div>' +
            '      <div class="chart-card-body"><div ref="barEl" class="chart-container tall"></div></div></div>' +
            '  </div>' +
            '  <div v-if="sel" class="detail-panel">' +
            '    <div class="detail-header">' +
            '      <div style="display:flex;align-items:center;gap:12px;">' +
            '        <span class="detail-city-name">{{ sel.city }}</span>' +
            '        <span :class="[\'source-tag\', sourceClass(sel.source_type)]">{{ sourceLabel(sel.source_type) }}' +
            '          <template v-if="sel.source_type === \'mixed\' && sel.anjuke_count"> ({{ sel.anjuke_count }}+{{ sel.stats_count || 0 }})</template></span>' +
            '      </div>' +
            '      <button class="detail-close" @click="ctx.selectCity(null)">✕</button>' +
            '    </div>' +
            '    <div class="stats-grid">' +
            '      <div class="stat-item"><div class="stat-label">当前均价</div><div class="stat-value neutral">¥{{ F.price(sel.current_price) }}</div><div class="stat-sub">元/m²</div></div>' +
            '      <div class="stat-item"><div class="stat-label">历史峰值</div><div class="stat-value neutral" style="color:var(--accent-orange)">¥{{ F.price(sel.max_price) }}</div><div class="stat-sub">{{ sel.peak_date }}</div></div>' +
            '      <div class="stat-item"><div class="stat-label">较峰值跌幅</div><div :class=\"[\'stat-value\', F.trend(sel.drop_from_peak)]\">{{ F.pct(sel.drop_from_peak) }}</div>' +
            '        <div class="stat-sub">¥{{ F.price((F.num(sel.current_price) || 0) - (F.num(sel.max_price) || 0)) }}</div></div>' +
            '      <div class="stat-item"><div class="stat-label">近12月变化</div><div :class="[\'stat-value\', F.trend(sel.change_pct_12m)]">{{ F.pct(sel.change_pct_12m) }}</div><div class="stat-sub">¥{{ F.price(sel.change_12m) }}</div></div>' +
            '      <div class="stat-item"><div class="stat-label">价格振幅</div><div class="stat-value neutral">¥{{ F.price(sel.price_range) }}</div><div class="stat-sub">¥{{ F.price(sel.min_price) }} ~ ¥{{ F.price(sel.max_price) }}</div></div>' +
            '      <div class="stat-item"><div class="stat-label">数据点数</div><div class="stat-value neutral">{{ sel.data_count || (sel.monthly && sel.monthly.length) || 0 }}</div><div class="stat-sub">个月</div></div>' +
            '    </div>' +
            '    <div class="chart-card-body"><div ref="detailEl" class="chart-container short"></div></div>' +
            '  </div>' +
            '</div>'
    };

    /* ---------------- 中国地图 ---------------- */
    var TabMap = {
        name: 'TabMap',
        setup: function () {
            var _vue = Vue;
            var ctx = _vue.inject(CTX);
            var mapEl = _vue.ref(null), lineEl = _vue.ref(null);
            var citySearch = _vue.ref('');
            var made = { map: null, line: null };

            var filteredCities = _vue.computed(function () {
                var list = ctx.cities.value.slice().sort(function (a, b) { return b.current_price - a.current_price; });
                var q = citySearch.value.trim().toLowerCase();
                if (q) list = list.filter(function (c) { return c.city.toLowerCase().indexOf(q) >= 0; });
                return list;
            });

            _vue.onMounted(function () {
                HP.chart.loadChinaGeo().then(function (geo) {
                    made.map = chartsLib.buildChinaMap(mapEl.value, ctx.cities.value, geo, ctx.selectMapCity);
                }).catch(function (e) { console.error(e); });
            });
            _vue.onBeforeUnmount(function () {
                HP.chart.dispose(made.map); HP.chart.dispose(made.line);
                made = {};
            });
            _vue.watch(ctx.mapSelectedCity, function (c) {
                if (!c) return;
                _vue.nextTick(function () {
                    HP.chart.dispose(made.line);
                    made.line = chartsLib.buildPriceLine(lineEl.value, c, { months: 18, compact: true });
                });
            });

            return {
                F: F, ctx: ctx, citySearch: citySearch, filteredCities: filteredCities,
                mapEl: mapEl, lineEl: lineEl, sel: ctx.mapSelectedCity
            };
        },
        template:
            '<div class="map-layout">' +
            '  <div class="map-main"><div ref="mapEl" class="chart-container map"></div></div>' +
            '  <div class="map-sidebar">' +
            '    <div class="map-legend">' +
            '      <div class="legend-title">城市等级图例</div>' +
            '      <div class="legend-items">' +
            '        <div v-for="(color, tier) in ctx.tierColors.value" :key="tier" class="legend-item">' +
            '          <span class="legend-dot" :style="{background: color}"></span><span>{{ ctx.tierLabels.value[tier] }}</span>' +
            '          <span class="legend-count">{{ ctx.tierCounts.value[tier] || 0 }} 城</span>' +
            '        </div>' +
            '      </div>' +
            '    </div>' +
            '    <div class="map-detail">' +
            '      <div v-if="!sel" class="map-detail-hint">' +
            '        <div style="text-align:center;"><div style="font-size:34px;margin-bottom:8px;">📍</div>' +
            '        <div>点击地图上的城市查看详情</div><div style="font-size:14px;margin-top:4px;">或从下方列表中选择</div></div>' +
            '      </div>' +
            '      <template v-if="sel">' +
            '        <div class="detail-header" style="padding:14px 16px;">' +
            '          <div style="display:flex;align-items:center;gap:8px;">' +
            '            <span :style="{width:\'8px\',height:\'8px\',borderRadius:\'50%\',background: sel.color}"></span>' +
            '            <span style="font-size:18px;font-weight:700;">{{ sel.city }}</span>' +
            '            <span style="font-size:14px;color:var(--text-secondary);">{{ ctx.tierLabels.value[sel.tier] }}</span>' +
            '          </div>' +
            '          <button class="detail-close" style="width:28px;height:28px;font-size:16px;" @click="ctx.selectMapCity(null)">✕</button>' +
            '        </div>' +
            '        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:12px 16px;">' +
            '          <div class="stat-item" style="padding:10px;"><div class="stat-label">均价</div><div style="font-size:18px;font-weight:700;">¥{{ F.price(sel.current_price) }}</div></div>' +
            '          <div class="stat-item" style="padding:10px;"><div class="stat-label">峰值</div><div style="font-size:18px;font-weight:700;color:var(--accent-orange);">¥{{ F.price(sel.max_price) }}</div><div style="font-size:13px;color:var(--text-muted);">{{ sel.peak_date }}</div></div>' +
            '          <div class="stat-item" style="padding:10px;"><div class="stat-label">较峰值</div><div :style="{fontSize:\'16px\',fontWeight:700,color: F.trendColor(sel.drop_from_peak)}">{{ F.pct(sel.drop_from_peak) }}</div></div>' +
            '          <div class="stat-item" style="padding:10px;"><div class="stat-label">近12月</div><div :style="{fontSize:\'16px\',fontWeight:700,color: F.trendColor(sel.change_pct_12m)}">{{ F.pct(sel.change_pct_12m) }}</div></div>' +
            '        </div>' +
            '        <div style="padding:8px 16px;"><div ref="lineEl" class="chart-container" style="height:380px;"></div></div>' +
            '      </template>' +
            '      <div style="padding:8px 12px;border-bottom:1px solid var(--border);">' +
            '        <input v-model="citySearch" class="search-input" placeholder="搜索城市名称...">' +
            '      </div>' +
            '      <div class="city-list" style="max-height:300px;overflow-y:auto;">' +
            '        <div v-for="(city, idx) in filteredCities" :key="city.city" class="city-item" :class="{active: sel && sel.city === city.city}" @click="ctx.selectMapCity(city)">' +
            '          <span class="city-rank">{{ idx + 1 }}</span>' +
            '          <span class="city-dot" :style="{background: city.color}"></span>' +
            '          <div class="city-info"><div class="city-name-text">{{ city.city }}</div><div class="city-price-text">{{ ctx.tierLabels.value[city.tier] }}</div></div>' +
            '          <div style="text-align:right;"><div style="font-size:15px;font-weight:600;">¥{{ F.price(city.current_price) }}</div>' +
            '          <div :class="[\'city-change\', F.trend(city.change_pct_12m)]">{{ F.pct(city.change_pct_12m) }}</div></div>' +
            '        </div>' +
            '      </div>' +
            '    </div>' +
            '  </div>' +
            '</div>'
    };

    /* ---------------- 城市排名 ---------------- */
    var TabRanking = {
        name: 'TabRanking',
        setup: function () {
            var _vue = Vue;
            var ctx = _vue.inject(CTX);
            var rankFilter = _vue.ref('');
            var rankedCities = _vue.computed(function () {
                var list = ctx.cities.value.slice().sort(function (a, b) { return b.current_price - a.current_price; });
                if (rankFilter.value) list = list.filter(function (c) { return c.tier === rankFilter.value; });
                return list;
            });
            function toggleFilter(tier) { rankFilter.value = rankFilter.value === tier ? '' : tier; }
            return {
                F: F, ctx: ctx, rankFilter: rankFilter, rankedCities: rankedCities,
                toggleFilter: toggleFilter, sourceClass: sourceClass, sourceLabel: sourceLabel
            };
        },
        template:
            '<div class="main">' +
            '  <div class="chart-card">' +
            '    <div class="chart-card-header"><span class="chart-card-title">全部城市当前均价排名</span>' +
            '      <div class="rank-filter">' +
            '        <span v-for="(label, tier) in ctx.tierLabels.value" :key="tier" class="rank-chip"' +
            '          :style="{background: rankFilter === tier ? ctx.tierColors.value[tier] + \'22\' : \'transparent\', color: rankFilter === tier ? ctx.tierColors.value[tier] : \'var(--text-secondary)\', borderColor: rankFilter === tier ? ctx.tierColors.value[tier] : \'var(--border)\'}"' +
            '          @click="toggleFilter(tier)">{{ label }}</span>' +
            '      </div>' +
            '    </div>' +
            '    <div style="max-height:calc(100vh - 250px);overflow-y:auto;">' +
            '      <table class="ranking-table">' +
            '        <thead><tr>' +
            '          <th class="center" style="width:50px;">#</th><th>城市</th><th class="center" style="width:70px;">等级</th>' +
            '          <th class="right">当前均价</th><th class="right">历史峰值</th><th class="center">较峰值</th>' +
            '          <th class="center">近12月</th><th class="center" style="width:100px;">数据来源</th>' +
            '        </tr></thead>' +
            '        <tbody>' +
            '          <tr v-for="(city, idx) in rankedCities" :key="city.city" @click="ctx.openOnMap(city)">' +
            '            <td class="center" style="color:var(--text-muted);font-weight:500;">{{ idx + 1 }}</td>' +
            '            <td><div style="display:flex;align-items:center;gap:10px;">' +
            '              <span :style="{width:\'10px\',height:\'10px\',borderRadius:\'50%\',background:city.color,flexShrink:0}"></span>' +
            '              <span style="font-weight:600;">{{ city.city }}</span></div></td>' +
            '            <td class="center"><span :style="{color:city.color,fontWeight:600}">{{ ctx.tierLabels.value[city.tier] }}</span></td>' +
            '            <td class="right" style="font-weight:700;">¥{{ F.price(city.current_price) }}</td>' +
            '            <td class="right" style="color:var(--accent-orange);">¥{{ F.price(city.max_price) }}</td>' +
            '            <td class="center"><span :style="{color: F.trendColor(city.drop_from_peak), fontWeight:700}">{{ F.pct(city.drop_from_peak) }}</span></td>' +
            '            <td class="center"><span :style="{color: F.trendColor(city.change_pct_12m), fontWeight:700}">{{ F.pct(city.change_pct_12m) }}</span></td>' +
            '            <td class="center"><span :class="[\'source-tag\', sourceClass(city.source_type)]">{{ sourceLabel(city.source_type) }}</span></td>' +
            '          </tr>' +
            '        </tbody>' +
            '      </table>' +
            '    </div>' +
            '  </div>' +
            '</div>'
    };

    return {
        setChartsLib: function (lib) { chartsLib = lib; },
        // 键 = 根应用的 tab 值（app.js 模板用 tabComponents[tab] 索引）
        components: { overview: TabOverview, map: TabMap, ranking: TabRanking },
        CTX: CTX
    };
})();
