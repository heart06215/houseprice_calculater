(function () {
    const A = HP.analysis, F = HP.format, C = HP.chart;
    const labels = {new_house:'新建商品住宅', second_hand:'二手住宅', rent:'住宅租金', land:'地价'};
    const safe = url => { try {return new URL(url).protocol === 'https:' ? url : null;} catch (_) {return null;} };
    const CityOfficial = {
        props: {rows: {type:Array,default:()=>[]}, label:String},
        setup(props) {
            const el = Vue.ref(null);
            let chart;
            function render() {
                C.dispose(chart);
                const months = A.calendar(props.rows.map(r => r.month)), map = new Map(props.rows.map(r => [r.month,r]));
                chart = HP.analysisChart(el.value,months,[
                    {name:'官方环比',type:'bar',data:months.map(m => ({value:map.get(m)?.mom_pct ?? null,itemStyle:{color:(map.get(m)?.mom_pct || 0) >= 0 ? (C.upColor||'#dc2626') : (C.downColor||'#16a34a')}}))},
                    {name:'官方同比',type:'line',data:months.map(m => map.get(m)?.yoy_pct ?? null),itemStyle:{color:(C.lineColor||'#dc2626')},lineStyle:{color:(C.lineColor||'#dc2626'),width:2.4}}],'%');
            }
            Vue.onMounted(render); Vue.watch(()=>props.rows,render);
            Vue.onBeforeUnmount(()=>C.dispose(chart));
            return {el};
        },
        template:`<section class="audit-band"><h3>{{label}} · 官方价格变动</h3><div ref="el" class="analysis-chart" role="img" :aria-label="label+'环比和同比走势'"></div><p v-if="!rows.length" class="analysis-empty">暂无该城市的官方月度序列</p></section>`
    };
    HP.dashComponents.components.cityAnalysis = {
        components: {CityOfficial, Affordability:HP.dashComponents.components.affordability},
        setup() {
            const ctx = Vue.inject('hpCtx'), pageEl = Vue.ref(null), priceEl = Vue.ref(null);
            const cityName = Vue.computed({get:()=>ctx.mapSelectedCity.value?.city || '北京',
                set:name=>{ctx.mapSelectedCity.value=ctx.cities.value.find(c=>c.city===name);}});
            const city = Vue.computed(()=>ctx.cities.value.find(c=>c.city===cityName.value));
            const months = Vue.computed(()=>[...new Set([
                ...(city.value?.monthly || []).map(r=>r.date),
                ...Object.values(ctx.segments.value).flatMap(s=>(s.cities.find(c=>c.city===cityName.value)?.monthly || []).map(r=>r.month))
            ])].sort().reverse());
            const month = ctx.cityMonth;
            /* 默认观察月依据挂牌价格覆盖率；城市与官方报告的最新时期分别展示。 */
            const userPickedMonth=Vue.ref(false);
            const globalPriceMonthLists=Vue.computed(()=>ctx.cities.value.map(c=>(c.monthly||[]).filter(r=>Number.isFinite(r.price)&&r.price>0).map(r=>r.date)));
            const defaultObsMonth=Vue.computed(()=>A.defaultObservationMonth(globalPriceMonthLists.value));
            Vue.watch([months,defaultObsMonth],([values,d])=>{if(!values.includes(month.value)||!userPickedMonth.value)month.value=(d&&values.includes(d))?d:(values[0]||'');},{immediate:true});
            const snapshot = Vue.computed(()=>month.value?A.citySnapshot(city.value,month.value):{row:{}});
            const info = Vue.computed(()=>A.cityInformation(ctx.information.value,cityName.value,month.value));
            const official = Vue.computed(()=>['new_house','second_hand'].map(kind=>({kind,label:labels[kind],
                rows:ctx.segments.value[kind]?.cities.find(c=>c.city===cityName.value)?.monthly || []})));
            const segmentSnapshots = Vue.computed(()=>official.value.map(s=>({...s,point:s.rows.find(r=>r.month===month.value)||{}})));
            const annualQuote=Vue.computed(()=>(city.value?.annual_quotes||[]).find(r=>r.year===Number(month.value.slice(0,4))));
            const prices = Vue.computed(()=>info.value.prices);
            const historical = Vue.computed(()=>[...(city.value?.monthly || [])].sort((a,b)=>b.date.localeCompare(a.date)));
            const latestCityMonth = Vue.computed(()=>historical.value[0]?.date || '暂无');
            const sign = v => !A.finite(v)?'':v>0?'delta-up':v<0?'delta-down':'';
            const decimal = v=>A.finite(v)?v.toLocaleString('zh-CN',{maximumFractionDigits:2}):'--';
            const evidenceUrl = row=>safe(row.evidence?.source_url || row.evidence?.anchor?.source_url);
            let chart;
            function renderPrice() {
                C.dispose(chart);
                if (priceEl.value && city.value) chart = HP.dashCharts.buildPriceLine(priceEl.value,city.value,{});
            }
            Vue.onMounted(()=>{
                renderPrice();
                window.scrollTo({top:0,behavior:'instant'});
            });
            Vue.watch(city,renderPrice);
            Vue.onBeforeUnmount(()=>{C.dispose(chart);});
            return {ctx,F,A,labels,safe,sign,decimal,evidenceUrl,pageEl,priceEl,cityName,city,months,month,snapshot,info,
                official,segmentSnapshots,prices,historyEvidence,historical,latestCityMonth,annualQuote};
        },
        template:`<main class="audit-page city-analysis" :data-tier="city?.tier" ref="pageEl" aria-labelledby="city-analysis-heading">
          <header class="city-analysis-header"><div><span class="ranking-eyebrow">城市档案 · 70城</span><h2 id="city-analysis-heading">{{cityName}} · 城市分析</h2><p class="audit-note">{{city?.tier_name}} · 均价序列截至 {{latestCityMonth}}</p></div></header>
          <div class="city-analysis-controls"><label>分析城市<select v-model="cityName"><option v-for="c in ctx.cities.value" :key="c.city">{{c.city}}</option></select></label><label>观察月份<select v-model="month"><option v-for="m in months" :key="m">{{m}}</option></select></label><span>{{month}} 月度快照</span></div>
          <div class="city-kpis" aria-live="polite"><div><span>城市均价 · 元/㎡</span><strong class="price-emphasis">{{F.price(snapshot.row.price)}}</strong><small>{{F.priceSource(snapshot.row)}}</small></div><div><span>均价环比</span><strong :class="sign(snapshot.mom_pct)">{{F.pct(snapshot.mom_pct)}}</strong><small>相邻月可比</small></div><div><span>近12个月</span><strong :class="sign(snapshot.yoy_pct)">{{F.pct(snapshot.yoy_pct)}}</strong><small>同月价格比较</small></div><div><span>较截至当月峰值</span><strong :class="sign(snapshot.drop_pct)">{{F.pct(snapshot.drop_pct)}}</strong><small>峰值 {{F.price(snapshot.peak)}} 元/㎡</small></div></div>
          <p v-if="!A.positive(snapshot.row.price)" class="audit-note">该月没有可核对的绝对价格或有效锚点，不参与均价排名；官方价格指数继续展示。</p>
          <p v-if="snapshot.row.model_comparison" class="audit-note">固定旧锚点模型对照：{{snapshot.row.model_comparison.anchor_date}} 锚点推导 {{F.price(snapshot.row.model_comparison.price)}} 元/㎡<template v-if="snapshot.row.model_comparison.deviation_pct!=null">，较同月安居客报价偏离 <span :class="sign(snapshot.row.model_comparison.deviation_pct)">{{F.pct(snapshot.row.model_comparison.deviation_pct)}}</span></template>。仅作对照，不用于排名。</p>
          <nav class="city-section-links" aria-label="城市分析内容"><a href="#city-price">均价</a><a href="#city-official">新房与二手房</a><a href="#city-cih">中指数据</a><a href="#city-news">相关研报</a></nav>
          <p v-if="annualQuote" class="audit-note">安居客 {{annualQuote.year}} 年度榜单参考：<strong>{{F.price(annualQuote.price)}} 元/㎡</strong>。原页未标明月份，不作为所选月份报价或逆推锚点。<a :href="safe(annualQuote.source_url)" target="_blank" rel="noopener">查看原文 ↗</a></p>
          <Affordability :city="cityName" :month="month"/>
          <section class="audit-band" id="city-price"><h3>{{cityName}} · 均价走势</h3><p class="audit-note">逆推结构 / 安居客数据。完整历史序列，逐月来源见悬浮提示，非成交价；上方指标按观察月份计算。</p><div ref="priceEl" class="analysis-chart" role="img" :aria-label="cityName+'均价及环比走势'"></div><p>近3月累计 <b :class="sign(snapshot.change3)">{{F.pct(snapshot.change3)}}</b> · {{snapshot.rising3?'连续3个月上涨超过0.05%':A.finite(snapshot.change3)?'未达到连续3个月上涨条件':'相邻3个月数据不足'}}</p>
            <details class="city-history"><summary>逐月价格与逆推依据（{{historical.length}} 个月）</summary><div class="history-toolbar"><span>默认：月份 / 均价 / 环比 / 来源</span><button type="button" :aria-pressed="historyEvidence?'true':'false'" @click="historyEvidence=!historyEvidence">{{historyEvidence?'收起证据列':'展开证据列'}}</button></div><div class="audit-table-wrap hub-scroll table-cards" tabindex="0"><table class="audit-table city-history-table" :class="{'show-evidence':historyEvidence}"><thead><tr><th class="col-text" scope="col">月份</th><th class="col-primary col-num" scope="col" title="均价（元/㎡）">均价</th><th class="col-secondary col-num" scope="col">环比</th><th class="col-aux col-text" scope="col">来源</th><th class="col-evidence col-aux col-num" scope="col" title="旧锚点模型对照">旧锚点模</th><th class="col-evidence col-aux col-num" scope="col" title="模型偏离报价">模型</th><th class="col-evidence col-aux col-text" scope="col">价格锚点</th><th class="col-evidence col-aux col-text" scope="col">环比链</th><th class="col-evidence col-aux col-text" scope="col">来源证据</th></tr></thead><tbody><tr v-for="r in historical" :key="r.date"><td class="col-text" data-label="月份">{{r.date}}</td><td class="col-primary col-num" data-label="均价">{{F.price(r.price)}}</td><td class="col-secondary col-num" data-label="环比" :class="sign(r.change_pct)">{{F.pct(r.change_pct)}}</td><td class="col-aux col-text" data-label="来源">{{F.priceSource(r)}}</td><td class="col-evidence col-aux col-num" data-label="旧锚点模">{{F.price(r.model_comparison?.price)}}</td><td class="col-evidence col-aux col-num" data-label="模型" :class="sign(r.model_comparison?.deviation_pct)">{{F.pct(r.model_comparison?.deviation_pct)}}</td><td class="col-evidence col-aux col-text" data-label="价格锚点">{{r.anchor_date||'--'}}</td><td class="col-evidence col-aux col-text" data-label="环比链">{{r.chain_months==null?'--':r.chain_months+'个月'}}</td><td class="col-evidence col-aux col-text" data-label="来源证据"><a v-if="evidenceUrl(r)" :href="evidenceUrl(r)" target="_blank" rel="noopener">{{r.method==='listing_observation'?'观测来源':'锚点来源'}}</a><span v-else>暂无链接</span></td></tr></tbody></table></div></details>
          </section>
          <div id="city-official"><CityOfficial v-for="s in official" :key="s.kind" :label="cityName+' · '+s.label" :rows="s.rows"/></div>
          <section class="audit-band"><h3>{{month}} · 分类价格与定基指数</h3><p class="audit-note">挂牌报价来自分类采集，缺失不以中指报价或指数代替；定基指数由官方月环比连乘得到，2021-04 = 100。</p><div class="audit-table-wrap"><table class="audit-table"><thead><tr><th>住宅类别</th><th>挂牌价（元/㎡）</th><th>定基指数</th><th>报价来源</th></tr></thead><tbody><tr v-for="s in segmentSnapshots" :key="s.kind"><td>{{s.label}}</td><td>{{F.price(s.point.price)}}{{s.point.provisional?' · 暂定':''}}</td><td>{{decimal(s.point.index)}}</td><td><a v-if="safe(s.point.price_source)" :href="safe(s.point.price_source)" target="_blank" rel="noopener">分类报价原文</a><span v-else>暂无已采集报价</span></td></tr></tbody></table></div></section>
          <section class="audit-band"><h3>{{month}} · 官方面积分档</h3><p class="audit-note" v-if="info.release">{{info.release.title}} · 发布于 {{info.release.publish_date||'未记录'}} <a :href="safe(info.release.source_url)" target="_blank" rel="noopener">统计局原文</a></p><div class="audit-table-wrap"><table class="audit-table"><thead><tr><th>住宅类别</th><th>面积类别</th><th>环比涨跌</th><th>同比涨跌</th></tr></thead><tbody><tr v-for="r in info.official" :key="r.kind+r.area"><td>{{labels[r.kind]}}</td><td>{{r.area}}</td><td :class="sign(r.mom_pct)">{{F.pct(r.mom_pct)}}</td><td :class="sign(r.yoy_pct)">{{F.pct(r.yoy_pct)}}</td></tr><tr v-if="!info.official.length"><td colspan="4">暂无该城市该月的面积分档发布数据</td></tr></tbody></table></div></section>
          <section class="audit-band" id="city-cih"><h3>{{month}} · 中指样本报价与租金</h3><p class="audit-note">中指样本口径独立展示，不能与城市均价或统计局指数直接拼接。</p><div class="audit-table-wrap"><table class="audit-table"><thead><tr><th>类别</th><th>样本价格</th><th>单位</th><th>环比</th><th>来源</th></tr></thead><tbody><tr v-for="r in prices" :key="r.kind+r.period"><td>{{labels[r.kind]}}</td><td>{{decimal(r.price)}}</td><td>{{r.unit}}</td><td :class="sign(r.mom_pct)">{{F.pct(r.mom_pct)}}</td><td><a :href="safe(r.source_url)" target="_blank" rel="noopener">中指云</a></td></tr><tr v-if="!prices.length"><td colspan="5">暂无该城市该月的中指公开报价</td></tr></tbody></table></div>
            <h3>已采集日度商品住宅成交</h3><div class="audit-table-wrap"><table class="audit-table"><thead><tr><th>日期</th><th>成交套数</th><th>面积（万㎡）</th><th>来源</th></tr></thead><tbody><tr v-for="r in info.daily" :key="r.date"><td>{{r.date}}</td><td>{{F.price(r.units)}}</td><td>{{decimal(r.area_wan_sqm)}}</td><td><a :href="safe(r.source_url)" target="_blank" rel="noopener">中指云</a></td></tr><tr v-if="!info.daily.length"><td colspan="4">暂无该城市的公开日度成交记录</td></tr></tbody></table></div>
            <div v-if="info.metrics.length" class="hub-metrics"><div v-for="m in info.metrics" :key="m.period+m.label"><span>{{m.period}} · {{m.label}}</span><strong>{{decimal(m.value)}} {{m.unit}}</strong><a :href="safe(m.source_url)" target="_blank" rel="noopener">来源</a></div></div>
          </section>
          <section class="audit-band hub-news" id="city-news"><h3>{{cityName}} · 相关研报</h3><p class="audit-note">按已采集研报标题与摘要中提及的城市匹配，保留原始来源。</p><article v-for="r in info.articles" :key="r.url"><a :href="safe(r.url)" target="_blank" rel="noopener">{{r.title}}</a><p>{{r.excerpt}}</p></article><p v-if="!info.articles.length">暂无标题或摘要明确提及该城市的研报</p></section>
        </main>`
    };
            const historyEvidence=Vue.ref(false);
})();
