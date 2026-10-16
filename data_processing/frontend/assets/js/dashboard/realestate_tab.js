/* Official cumulative YoY is the trend; cumulative totals are a separate view. */
(function () {
    const C = HP.chart;
    const metrics = [
        {label: '全国房地产开发投资额', title: '房地产开发投资', unit: '亿元', token: '--trend-up'},
        {label: '房屋新开工面积', title: '房屋新开工面积', unit: '万㎡', token: '--tech-blue'}
    ];
    const number = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 2});
    /** @typedef {{label:string,value:number,unit:string,yoy:number}} MacroMetric */
    /** @typedef {{month:string,period:string,published_at:string,source_url:string,items:MacroMetric[]}} MacroReport */
    /** @param {MacroReport|undefined} report @param {{label:string,unit:string}} metric @returns {MacroMetric|undefined} */
    function item(report, metric) {
        const matches = (report?.items || []).filter(row => row.label === metric.label && row.unit === metric.unit);
        const row = matches.length === 1 ? matches[0] : undefined;
        return row && Number.isFinite(row.value) && row.value > 0 && Number.isFinite(row.yoy) ? row : undefined;
    }
    /** @param {MacroReport[]} history @param {{label:string,unit:string}} metric @param {string} mode @returns {{months:string[],values:(number|null)[],rows:(MacroReport|undefined)[]}} */
    HP.realestateSeries = function (history, metric, mode) {
        const months = HP.analysis.calendar(history.map(row => row.month))
            .filter(month => mode !== 'yoy' || !month.endsWith('-01'));
        const grouped = new Map(months.map(month => [month, history.filter(row => row.month === month)]));
        const rows = months.map(month => grouped.get(month).length === 1 ? grouped.get(month)[0] : undefined);
        return {months, rows, values: rows.map(row => {
            const value = item(row, metric);
            return value ? value[mode === 'yoy' ? 'yoy' : 'value'] : null;
        })};
    };
    /** @param {{months:string[],values:(number|null)[]}} data @returns {{from:number,to:number,months:string[]}[]} */
    HP.realestateGaps = function (data) {
        const gaps = [];
        let previous = -1;
        data.values.forEach((value, index) => {
            if (value == null) return;
            if (previous >= 0 && index > previous + 1) {
                const missing = data.months.slice(previous + 1, index).filter(month => !month.endsWith('-01'));
                if (missing.length) gaps.push({from: previous, to: index, months: missing});
            }
            previous = index;
        });
        return gaps;
    };
    /** @param {HTMLElement} el @param {MacroReport[]} history @param {{label:string,title:string,unit:string,token:string}} metric @param {string} mode @returns {object} */
    HP.realestateChart = function (el, history, metric, mode) {
        const data = HP.realestateSeries(history, metric, mode);
        const unit = mode === 'yoy' ? '%' : metric.unit;
        const tokens = getComputedStyle(document.documentElement);
        const color = tokens.getPropertyValue(metric.token).trim();
        const gapColor = tokens.getPropertyValue('--trend-up').trim() || '#dc2626';
        const gaps = HP.realestateGaps(data);
        const chart = HP.analysisChart(el, data.months, [{name: metric.title, data: data.values,
            smooth: false, itemStyle: {color}, lineStyle: {type: 'solid', color, width: 2},
            markLine: {symbol: ['none', 'none'], animation: false, label: {show: false},
                lineStyle: {color: gapColor, width: 2, type: 'solid'},
                tooltip: {trigger: 'item', formatter: point => '缺失报告：' + gaps[point.dataIndex].months.join('、') +
                    '<br>红线连接前后有效点，缺期数值留空。'},
                data: gaps.map(gap => [
                    {name: '缺失报告：' + gap.months.join('、'), coord: [data.months[gap.from], data.values[gap.from]]},
                    {coord: [data.months[gap.to], data.values[gap.to]]}
                ])}}], unit);
        chart.setOption({legend: {show: false}, grid: {top: 40},
            xAxis: {inverse: false, axisLabel: {showMinLabel: true, showMaxLabel: true}},
            yAxis: {name: mode === 'yoy' ? '累计同比（%）' : '累计（' + unit + '）',
                inverse: false, min: mode === 'yoy' ? null : 0,
                axisLabel: {formatter: value => number.format(value) + (mode === 'yoy' ? '%' : '')}},
            tooltip: {formatter: points => {
                const row = data.rows[points[0]?.dataIndex];
                const metricItem = item(row, metric);
                if (!metricItem) return data.months[points[0]?.dataIndex]?.endsWith('-01') ?
                    '1—2月合并发布，无单独1月数据' : '该统计期报告缺失，红线仅连接前后有效点';
                return row.period + '<br>' + metric.title + '：' + number.format(metricItem.value) + ' ' + metric.unit +
                    '<br>累计同比：' + number.format(metricItem.yoy) + '%<br>发布：' + row.published_at;
            }}, dataZoom: [{start: 0, end: 100}, {start: 0, end: 100}]});
        return chart;
    };
    HP.dashComponents.components.realestateTrends = {
        setup() {
            const ctx = Vue.inject('hpCtx');
            const source = Vue.computed(() => ctx.information.value.macro?.realestate);
            const history = Vue.computed(() => source.value?.history || []);
            const mode = Vue.shallowRef('yoy'), year = Vue.shallowRef('all');
            const investmentEl = Vue.ref(null), startsEl = Vue.ref(null);
            const years = Vue.computed(() => [...new Set(history.value.map(row => row.month.slice(0, 4)))].reverse());
            const visible = Vue.computed(() => history.value.filter(row => year.value === 'all' || row.month.startsWith(year.value)));
            const latest = Vue.computed(() => history.value.at(-1));
            const displayed = Vue.computed(() => visible.value.at(-1));
            const displayedItems = Vue.computed(() => metrics.map(metric => item(displayed.value, metric)));
            const gapMonths = Vue.computed(() => [...new Set(metrics.flatMap(metric =>
                HP.realestateGaps(HP.realestateSeries(visible.value, metric, mode.value)).flatMap(gap => gap.months)))]);
            let charts = [];
            /** @returns {void} */
            function render() {
                charts.forEach(C.dispose);
                charts = visible.value.length ? [investmentEl.value, startsEl.value].map((el, index) =>
                    HP.realestateChart(el, visible.value, metrics[index], mode.value)) : [];
            }
            Vue.onMounted(render);
            Vue.watch([visible, mode], render, {flush: 'post'});
            Vue.onBeforeUnmount(() => charts.forEach(C.dispose));
            /** @param {string} value @returns {string} */
            function safe(value) {
                return /^https:\/\/www\.stats\.gov\.cn\//.test(value || '') ? value : 'https://www.stats.gov.cn/sj/zxfb/';
            }
            const sign = v => !Number.isFinite(v) ? '' : v > 0 ? 'delta-up' : v < 0 ? 'delta-down' : '';
            return {history, latest, displayed, displayedItems, gapMonths, visible, mode, year, years, investmentEl, startsEl,
                metrics, item, safe, sign, num: value => value == null ? '暂无' : number.format(value)};
        },
        template: `<section class="audit-page realestate-analysis" aria-labelledby="realestate-title">
          <h2 id="realestate-title">全国房地产开发与新开工</h2>
          <p class="audit-note">默认显示统计局公布的累计同比增速，与上年同期累计值比较；包含住宅、办公楼和商业等房屋。1—2月合并发布，同比线从12月连接到次年1—2月，不生成单独1月数据。</p>
          <p v-if="gapMonths.length" class="audit-note realestate-gap-note"><span class="realestate-gap-key" aria-hidden="true"></span>青色直线表示缺失报告区间，连接前后有效点；缺期数值留空。缺失月份：{{gapMonths.join('、')}}。</p>
          <p v-if="latest" class="audit-note">最新统计期：{{latest.period}} · 发布日期：{{latest.published_at}} · <a :href="safe(latest.source_url)" target="_blank" rel="noopener">统计局原文</a></p>
          <div v-if="history.length" class="audit-controls">
            <label>开发指标口径 <select v-model="mode"><option value="yoy">官方累计同比（%）</option><option value="value">年初累计总量</option></select></label>
            <label>开发指标年份 <select v-model="year"><option value="all">全部年份</option><option v-for="y in years" :key="y" :value="y">{{y}}年</option></select></label>
          </div>
          <p v-if="mode==='value'" class="audit-note">年初累计总量随月份累加，每年重新起算；不能作为单月增减走势。正常跨年重置不连线，实际缺期按红线标注；金额与面积的单位分别标注。</p>
          <div class="realestate-chart-grid">
            <article class="audit-band"><h3>房地产开发投资 · {{mode==='yoy'?'累计同比':'年初累计'}}</h3>
              <p v-if="displayed" class="realestate-latest"><span :class="mode==='yoy'?sign(displayedItems[0]?.yoy):''">{{num(displayedItems[0]?.[mode==='yoy'?'yoy':'value'])}}{{mode==='yoy'?'%':' 亿元'}}</span> <small>{{displayed.period}} · <template v-if="mode==='yoy'">累计投资 {{num(displayedItems[0]?.value)}} 亿元</template><template v-else>官方累计同比 <span :class="sign(displayedItems[0]?.yoy)">{{num(displayedItems[0]?.yoy)}}%</span></template></small></p>
              <div v-if="visible.length" ref="investmentEl" class="analysis-chart" role="img" aria-label="全国房地产开发投资累计走势"></div>
              <p v-else class="analysis-empty">暂无经过核验的开发投资历史数据，采集成功后自动显示。</p>
            </article>
            <article class="audit-band"><h3>房屋新开工面积 · {{mode==='yoy'?'累计同比':'年初累计'}}</h3>
              <p v-if="displayed" class="realestate-latest"><span :class="mode==='yoy'?sign(displayedItems[1]?.yoy):''">{{num(displayedItems[1]?.[mode==='yoy'?'yoy':'value'])}}{{mode==='yoy'?'%':' 万㎡'}}</span> <small>{{displayed.period}} · <template v-if="mode==='yoy'">累计新开工 {{num(displayedItems[1]?.value)}} 万㎡</template><template v-else>官方累计同比 <span :class="sign(displayedItems[1]?.yoy)">{{num(displayedItems[1]?.yoy)}}%</span></template></small></p>
              <div v-if="visible.length" ref="startsEl" class="analysis-chart" role="img" aria-label="全国房屋新开工面积累计走势"></div>
              <p v-else class="analysis-empty">暂无经过核验的新开工面积历史数据，采集成功后自动显示。</p>
            </article>
          </div>
          <p class="audit-note">来源：<a href="https://www.stats.gov.cn/sj/zxfb/" target="_blank" rel="noopener">国家统计局</a> · 已核验 {{history.length}} 期。检查时间不代表统计期；尚未发布或采集失败时保留已核验数据。</p>
          <details v-if="visible.length" class="realestate-history"><summary>逐期数值与原文（{{visible.length}}期）</summary>
            <div class="audit-table-wrap hub-scroll table-cards"><table class="audit-table"><thead><tr><th class="col-text" scope="col">累计统计期</th><th class="col-primary col-num" scope="col" title="开发投资（亿元）">开发投资</th><th class="col-secondary col-num" scope="col">投资同比</th><th class="col-primary col-num" scope="col" title="新开工面积（万㎡）">新开工</th><th class="col-secondary col-num" scope="col">面积同比</th><th class="col-aux col-text" scope="col" title="发布日期 / 原文">发布/原文</th></tr></thead><tbody>
              <tr v-for="r in visible" :key="r.month"><td class="col-text" data-label="累计统计期">{{r.period}}</td><td class="col-primary col-num" data-label="开发投资">{{num(item(r,metrics[0])?.value)}}</td><td class="col-secondary col-num" data-label="投资同比" :class="sign(item(r,metrics[0])?.yoy)">{{num(item(r,metrics[0])?.yoy)}}%</td><td class="col-primary col-num" data-label="新开工">{{num(item(r,metrics[1])?.value)}}</td><td class="col-secondary col-num" data-label="面积同比" :class="sign(item(r,metrics[1])?.yoy)">{{num(item(r,metrics[1])?.yoy)}}%</td><td class="col-aux" data-label="发布/原文"><a :href="safe(r.source_url)" target="_blank" rel="noopener">{{r.published_at}}</a></td></tr>
            </tbody></table></div>
          </details>
        </section>`
    };
})();
