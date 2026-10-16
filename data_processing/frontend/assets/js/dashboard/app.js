/* 主看板根应用：数据加载、共享状态 provide、根模板装配。 */
(function () {
    var createApp = Vue.createApp, ref = Vue.ref, computed = Vue.computed,
        provide = Vue.provide, onMounted = Vue.onMounted;

    var CTX = HP.dashComponents.CTX;

    var App = {
        setup: function () {
            var loaded = ref(false);
            var loadError = ref('');
            var tab = ref('information');
            var cities = ref([]);
            var meta = ref({});
            var analytics = ref({});
            var research = ref({groups: []});
            var segments = ref({});
            var information = ref({cih: {}, releases: []});
            var infoView = ref('overview');
            var cityMonth = ref('');
            var priceMode = ref('all');
            var selectedCity = ref(null);
            var mapSelectedCity = ref(null);

            var tierColors = { S: '#dc2626', '1': '#c2410c', '2': '#0aa0c8', '3': '#5b4db8' };
            var tierLabels = computed(function () {
                return (meta.value && meta.value.tier_names) ||
                    { S: '顶级·北上深', '1': '一线', '2': '二线', '3': '三线' };
            });
            var tierCounts = computed(function () {
                var counts = {};
                cities.value.forEach(function (c) { counts[c.tier] = (counts[c.tier] || 0) + 1; });
                return counts;
            });

            function selectCity(city) { selectedCity.value = city; }
            function selectMapCity(city) { mapSelectedCity.value = city; }
            /** Keep the existing callback contract for ranking and legacy detail links. */
            function openOnMap(city) {
                if (!city) return;
                mapSelectedCity.value = city;
                cityMonth.value = '';
                infoView.value = 'city';
                tab.value = 'information';
            }

            provide(CTX, {
                cities: cities, meta: meta, analytics: analytics, research: research, segments: segments, priceMode: priceMode, tab: tab, information: information, infoView: infoView, cityMonth: cityMonth,
                selectedCity: selectedCity, mapSelectedCity: mapSelectedCity,
                tierColors: tierColors, tierLabels: tierLabels, tierCounts: tierCounts,
                selectCity: selectCity, selectMapCity: selectMapCity, openOnMap: openOnMap
            });

            onMounted(function () {
                const fontsReady = document.fonts ? Promise.all([
                    document.fonts.load('400 16px Montserrat'),
                    document.fonts.load('400 16px "Glow Sans SC"', '城市房价'),
                    document.fonts.load('700 16px "Glow Sans SC"', '城市房价'),
                    document.fonts.load('700 24px "Glow Sans SC Wide"', '城市房价')
                ]).catch(function(){return [];}) : Promise.resolve();
                Promise.all([HP.loadData(), fontsReady]).then(function (results) {
                    var data = results[0];
                    cities.value = (data.cities || []).map(function (c) {
                        return Object.assign({}, c, { color: tierColors[c.tier] || '#3d5560' });
                    });
                    meta.value = data.meta || {};
                    analytics.value = data.analytics || {};
                    research.value = data.research || {groups: []};
                    segments.value = data.housing_segments || {};
                    information.value = data.information || {cih: {}, releases: []};
                    loaded.value = true;
                }).catch(function (err) {
                    console.error('数据加载失败:', err);
                    loadError.value = String(err && err.message || err);
                });
            });

            return {
                loaded: loaded, loadError: loadError, tab: tab, priceMode: priceMode,
                meta: meta, information: information, tierColors: tierColors, tierLabels: tierLabels, tierCounts: tierCounts,
                tabComponents: HP.dashComponents.components
            };
        },
        template:
            '<div v-if="!loaded && !loadError" class="loading">' +
            '  <div class="spinner"></div><span style="color:var(--text-secondary)">正在加载房价数据...</span>' +
            '</div>' +
            '<div v-else-if="loadError" class="error-box">' +
            '  <h2>数据加载失败</h2>' +
            '  <p>{{ loadError }}<br>请检查网络连接后刷新页面；若仍无法加载，请稍后重试。</p>' +
            '  <button class="nav-tab" onclick="window.location.reload()">重新加载</button>' +
            '</div>' +
            '<template v-else>' +
            '  <div class="header">' +
            '    <div class="header-left">' +
            '      <h1><span class="brand-mark" aria-hidden="true"></span><span class="title-cut">中国70城房价信息平台</span></h1>' +
            '      <span class="badge">统计局 · 中指云 · 挂牌参考</span>' +
            '      <span class="badge orange" v-if="meta.data_updated">房价最晚月份: {{ meta.data_updated }}</span>' +
            '      <span class="badge" v-if="information.releases?.length">统计局月报: {{ information.releases[0].month }}</span>' +
            '    </div>' +
            '    <div class="tier-legend">' +
            '      <span v-for="(color, tier) in tierColors" :key="tier" class="item">' +
            '        <span :style="{width:\'8px\',height:\'8px\',borderRadius:\'50%\',background:color}"></span>' +
            '        {{ tierLabels[tier] }} <span class="count">({{ tierCounts[tier] || 0 }}城)</span>' +
            '      </span>' +
            '    </div>' +
            '  </div>' +
            '  <div class="nav-tabs">' +
            '    <button class="nav-tab" :class="{active: tab === \'information\'}" @click="tab = \'information\'">信息集成</button>' +
            '  </div>' +
            '  <Transition name="hp-pane" mode="out-in"><component :is="tabComponents[tab]" :key="tab"></component></Transition>' +
            '</template>'
    };

    HP.dashComponents.setChartsLib(HP.dashCharts);

    // 源码版脚本带 defer；单文件内联版脚本无 defer 且位于 head 中——
    // 两种方式都等 DOM 就绪后再挂载。
    function boot() {
        createApp(App).mount('#app');
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
