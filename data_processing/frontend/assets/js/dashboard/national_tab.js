/* Official LPR history and national residential price changes. */
(function () {
    const A = HP.analysis, C = HP.chart, F = HP.format;
    HP.analysisChart = function (el, months, series, unit, coverage) {
        const chart = C.create(el);
        const touch = window.matchMedia('(pointer:coarse)').matches;
        chart.setOption({tooltip: {trigger: 'axis', confine: true, formatter: ps => {
            if (!ps.length) return '';
            const i = ps[0].dataIndex;
            return '<b>' + months[i] + '</b>' + ps.map(p => '<br>' + p.marker + p.seriesName + ': ' +
                (unit === '%' ? F.pct(p.value) : F.price(p.value)) + (unit === '%' ? '' : ' ' + unit) +
                (coverage ? ' (' + coverage[p.seriesIndex][i] + '城)' : '')).join('');
        }}, legend: {top: 4}, grid: {left: 20, right: 24, top: 64, bottom: 76, containLabel: true},
            xAxis: C.axis({type: 'category', data: months, splitLine: false}),
            yAxis: C.axis({name: unit, scale: true}),
            dataZoom: [{type: 'inside', disabled:touch, start: 55, zoomOnMouseWheel: 'ctrl', moveOnMouseWheel: false}, {type: 'slider', start: 55, height: touch?44:20, bottom: 8}],
            series: series.map(s => ({type: 'line', showSymbol: false, connectNulls: false, ...s})),
            media: [{query:{maxWidth:500},option:{legend:{textStyle:{fontSize:12}},
                xAxis:{axisLabel:{fontSize:12,hideOverlap:true}},dataZoom:[{disabled:true},{height:44}]}},
                {option:{legend:{textStyle:{fontSize:14}},xAxis:{axisLabel:{fontSize:14,hideOverlap:false}},
                    dataZoom:[{disabled:touch},{height:touch?44:20}]}}]});
        return chart;
    };
    HP.lprChart = function (el, history) {
        const months = history.map(r => r.as_of.slice(0, 7));
        const tokens = getComputedStyle(document.documentElement);
        const chart = HP.analysisChart(el, months, ['one_year', 'five_year'].map((key, i) => ({
            name: i ? '5年期以上 LPR' : '1年期 LPR', data: history.map(r => r[key]),
            step: 'end', smooth: false,
            itemStyle: {color: tokens.getPropertyValue(i ? '--tech-blue' : '--trend-up').trim() || (i ? '#176878' : '#dc2626')},
            lineStyle: {color: tokens.getPropertyValue(i ? '--tech-blue' : '--trend-up').trim() || (i ? '#176878' : '#dc2626'), type: i ? 'dashed' : 'solid', width: 2.4}
        })), '%');
        chart.setOption({tooltip: {formatter: ps => ps.length ? '<b>' + months[ps[0].dataIndex] + '</b>' +
            ps.map(p => '<br>' + p.marker + p.seriesName + ': ' + Number(p.value).toFixed(2) + '%').join('') : ''},
            yAxis: {name: '利率（%）', axisLabel: {formatter: value => Number(value).toFixed(2) + '%'}},
            xAxis: {axisLabel: {showMinLabel: true, showMaxLabel: true}},
            dataZoom: [{start: 0, end: 100}, {start: 0, end: 100}]});
        return chart;
    };
    HP.dashComponents.components.nationalTrends = {
        setup() {
            const ctx = Vue.inject('hpCtx'), lprEl = Vue.ref(null), officialEl = Vue.ref(null), metric = Vue.ref('mom_pct');
            const history = ctx.information.value.macro?.lpr?.history || [], names = ctx.cities.value.map(c => c.city);
            const kinds = ['new_house', 'second_hand'], official = kinds.map(k => A.nationalOfficial(ctx.segments.value[k], names));
            const months = A.calendar(official.flatMap(rows => rows.map(r => r.month)), '2021-04');
            const byMonth = official.map(rows => new Map(rows.map(r => [r.month, r])));
            let lprChart, officialChart;
            function renderOfficial() {
                C.dispose(officialChart);
                officialChart = HP.analysisChart(officialEl.value, months, kinds.map((k, i) => ({name: i ? '二手住宅' : '新建商品住宅',
                    data: months.map(m => byMonth[i].get(m)?.[metric.value] ?? null), itemStyle: {color: i ? '#176878' : (C.lineColor || '#dc2626')}, lineStyle: {color: i ? '#176878' : (C.lineColor || '#dc2626'), width: 2.4}})), '%',
                    byMonth.map(map => months.map(m => map.get(m)?.[metric.value + '_count'] || 0)));
            }
            Vue.onMounted(() => {
                if (history.length) lprChart = HP.lprChart(lprEl.value, history);
                renderOfficial();
            });
            Vue.watch(metric, renderOfficial);
            Vue.onBeforeUnmount(() => { C.dispose(lprChart); C.dispose(officialChart); });
            return {lprEl, officialEl, metric, history};
        },
        template: `<section class="audit-page national-analysis" aria-label="利率与住宅价格走势">
          <h2>利率与住宅价格走势</h2>
          <section class="audit-band" aria-labelledby="lpr-trend"><h3 id="lpr-trend">贷款市场报价利率走势</h3>
            <p class="audit-note">2019年8月新机制实施以来的逐月 LPR，单位为百分比。1年期与5年期以上按各期官方报价绘制；房贷实际执行利率由贷款合同确定。LPR是银行贷款的参考利率；分1年期和5年期以上，是给较短期贷款和房贷等长期贷款分别作参考。</p>
            <div v-if="history.length" ref="lprEl" class="analysis-chart" role="img" aria-label="1年期和5年期以上贷款市场报价利率历史走势"></div>
            <p v-else class="analysis-empty">暂无经过核验的 LPR 历史数据</p>
            <p v-if="history.length" class="audit-note">{{history[0].as_of.slice(0,7)}} — {{history[history.length-1].as_of.slice(0,7)}} · {{history.length}} 期。拖动底部时间滑块可查看局部区间。</p>
            <p class="audit-note">来源：<a href="https://www.chinamoney.com.cn/chinese/bklpr/" target="_blank" rel="noopener">中国货币网 · 全国银行间同业拆借中心</a> · <a href="https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125440/3876551/index.html" target="_blank" rel="noopener">人民银行公告</a></p>
          </section>
          <section class="audit-band"><h3>全国新房与二手房价格变动</h3>
            <div class="analysis-modes" role="group" aria-label="全国价格变动口径"><button :aria-pressed="metric==='mom_pct'" @click="metric='mom_pct'">环比</button><button :aria-pressed="metric==='yoy_pct'" @click="metric='yoy_pct'">同比</button></div>
            <p class="audit-note">统计局70城中各月有效城市涨跌幅的算术平均，悬浮显示覆盖城数。平台汇总，非统计局发布的全国加权指数。</p>
            <div ref="officialEl" class="analysis-chart" role="img" aria-label="全国新房与二手住宅涨跌幅走势"></div>
          </section>
        </section>`
    };
})();
