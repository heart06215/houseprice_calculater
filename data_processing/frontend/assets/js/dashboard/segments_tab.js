(function () {
    HP.dashComponents.components.cityRanking={
        setup(){
            const ctx=Vue.inject('hpCtx'),A=HP.analysis,F=HP.format;
            const kind=Vue.ref('average'),metric=Vue.ref('price'),direction=Vue.ref('desc'),query=Vue.ref(''),month=Vue.ref(''),tier=Vue.ref('');
            const segment=Vue.computed(()=>kind.value==='average'?{cities:ctx.cities.value.map(c=>({...c,monthly:c.monthly.map(m=>({...m,month:m.date,mom_pct:m.change_pct}))}))}:ctx.segments.value[kind.value]||{cities:[]});
            const months=Vue.computed(()=>[...new Set(segment.value.cities.flatMap(c=>c.monthly.map(m=>m.month)))].sort().reverse());
            /* 2026-10-01 修复: 默认月按覆盖率回退（新月数据次月中旬才发布齐），用户手动选择后不再自动跟随 */
            const userPickedMonth=Vue.ref(false);
            const priceMonthLists=Vue.computed(()=>segment.value.cities.map(c=>c.monthly.filter(m=>Number.isFinite(m.price)&&m.price>0).map(m=>m.month)));
            const defaultMonth=Vue.computed(()=>A.defaultObservationMonth(priceMonthLists.value));
            const coverage=Vue.computed(()=>A.observationCoverage(priceMonthLists.value));
            const selectedCoverage=Vue.computed(()=>coverage.value[month.value]||0);
            const coverageHint=Vue.computed(()=>{
                const c=selectedCoverage.value, total=segment.value.cities.length;
                return (month.value&&total&&c<total*0.8)?'所选月份仅有 '+c+'/'+total+' 城可排名——该月数据通常次月中旬起才能核算完整；默认已回退到数据较齐的最近月份，可在此手动选择。':'';
            });
            Vue.watch([months,defaultMonth],([ms,d])=>{if(!ms.includes(month.value)||!userPickedMonth.value)month.value=(d&&ms.includes(d))?d:(ms[0]||'');},{immediate:true});
            const labels=Vue.computed(()=>kind.value==='average'?{price:'城市均价',mom_pct:'均价环比',yoy_pct:'近12月涨跌',peak:'截至当月峰值',drop_pct:'较峰值涨跌'}:{cih_price:'中指样本价格',price:'挂牌参考价',mom_pct:'官方环比',yoy_pct:'官方同比'});
            Vue.watch(kind,()=>{metric.value=kind.value==='average'?'price':'cih_price';});
            const tierOf=city=>ctx.cities.value.find(c=>c.city===city);
            function pointAt(c){
                const point={...(c.monthly.find(m=>m.month===month.value)||{})};
                point.annual=(c.annual_quotes||[]).find(r=>r.year===Number(month.value.slice(0,4))); 
                if(kind.value==='average'){
                    const past=c.monthly.filter(m=>m.month<=month.value&&Number.isFinite(m.price)&&m.price>0);
                    point.peak=past.length?Math.max(...past.map(m=>m.price)):null;
                    point.drop_pct=point.price>0&&point.peak>0?(point.price/point.peak-1)*100:null;
                    const previous=c.monthly.find(m=>m.month===(Number(month.value.slice(0,4))-1)+month.value.slice(4));
                    point.yoy_pct=point.price>0&&previous?.price>0?(point.price/previous.price-1)*100:null;
                }
                return point;
            }
            const ranked=Vue.computed(()=>{
                const all=segment.value.cities.map(c=>({city:c.city,cityData:tierOf(c.city),point:pointAt(c)}));
                const val=r=>Number.isFinite(r.point[metric.value])&&(!['price','cih_price','peak'].includes(metric.value)||r.point[metric.value]>0)?r.point[metric.value]:null;
                all.sort((a,b)=>val(a)==null?(val(b)==null?a.city.localeCompare(b.city,'zh-CN'):1):val(b)==null?-1:(val(a)-val(b))*(direction.value==='asc'?1:-1)||a.city.localeCompare(b.city,'zh-CN'));
                let previous=null,rank=0;
                return all.map((r,i)=>{const v=val(r);if(v!==null&&v!==previous)rank=i+1;previous=v;return {...r,value:v,rank:v===null?null:rank};}).filter(r=>r.city.includes(query.value.trim())&&(!tier.value||r.cityData?.tier===tier.value));
            });
            const expanded=Vue.ref(false);
            const validRows=Vue.computed(()=>ranked.value.filter(r=>r.value!==null));
            const summary=Vue.computed(()=>{
                const values=validRows.value.map(r=>r.value).sort((a,b)=>a-b),n=values.length;
                return {count:n,median:n?(values[Math.floor((n-1)/2)]+values[Math.floor(n/2)])/2:null,
                    ...F.priceCoverage(ranked.value,month.value)};
            });
            const barMax=Vue.computed(()=>Math.max(1,...validRows.value.map(r=>Math.abs(r.value))));
            const barWidth=v=>v==null?'0%':Math.min(100,Math.abs(v)/barMax.value*100)+'%';
            function resetFilters(){query.value='';tier.value='';direction.value='desc';}
            const format=v=>v==null?'暂无':metric.value.endsWith('pct')?F.pct(v):F.price(v)+' 元/㎡';
            const source=p=>p.price==null&&p.annual?'安居客年度参考':F.priceSource(p);
            const sourceUrl=p=>{const u=p.evidence?.source_url||p.evidence?.anchor?.source_url||p.annual?.source_url;return /^https:\/\/(?:[a-z0-9-]+\.)*anjuke\.com\//i.test(u||'')?u:null;};
            const sourceDetail=p=>p.price==null&&p.annual?'年度榜单未标月份，不参与月度排名':p.method==='listing_observation'?'当月网页报价 · '+(p.evidence?.fetched_at?.slice(0,10)||'')+' 采集':p.method==='index_estimate'?'锚点 '+p.anchor_date+' · 环比链 '+p.chain_months+' 个月':p.evidence?.note||'无可用价格';
            const money=v=>Number.isFinite(v)&&v>0?F.price(v):'—';
            const percent=v=>Number.isFinite(v)?F.pct(v):'—';
            const color=v=>!Number.isFinite(v)?'':v>0?'var(--trend-up)':v<0?'var(--trend-down)':'';
            return {ctx,kind,metric,direction,query,month,months,ranked,labels,format,tier,source,sourceUrl,sourceDetail,money,percent,color,expanded,summary,barWidth,resetFilters,userPickedMonth,coverageHint};
        },
        template:`<section class="audit-page ranking-panel" :class="{'ranking-expanded':expanded}" aria-labelledby="city-ranking-title">
          <header class="ranking-header"><div><span class="ranking-eyebrow">70城 · 价格观察</span><h2 id="city-ranking-title">城市价格排名</h2><p>从全国名次到城市走势，查看价格水平与周期变化。 <a class="ranking-jump" href="#city-price-table">直达榜单 ↓</a></p></div><div class="ranking-header-right"><div class="ranking-art" aria-hidden="true"><i></i><i></i><i></i><i></i></div><div class="ranking-period"><span>当前观察月份</span><strong>{{month||'暂无'}}</strong></div></div></header>
          <div class="ranking-category" role="group" aria-label="排名类别"><button v-for="item in [{key:'average',name:'房价（均价）',sub:'逆推结构 / 安居客数据'},{key:'new_house',name:'新房',sub:'样本价格 / 官方指数'},{key:'second_hand',name:'二手房',sub:'样本价格 / 官方指数'}]" :key="item.key" :aria-pressed="kind===item.key" @click="kind=item.key"><strong>{{item.name}}</strong><span>{{item.sub}}</span></button></div>
          <div class="ranking-toolbar"><label>排名月份<select v-model="month" @change="userPickedMonth=true"><option v-for="m in months">{{m}}</option></select></label><label>排名指标<select v-model="metric"><option v-for="(label,key) in labels" :value="key">{{label}}</option></select></label><label>排序方向<select v-model="direction"><option value="desc">从高到低</option><option value="asc">从低到高</option></select></label><label>城市能级<select v-model="tier"><option value="">全部能级</option><option v-for="(label,key) in ctx.tierLabels.value" :value="key">{{label}}</option></select></label><label class="ranking-search">搜索城市<input v-model="query" type="search" placeholder="输入城市名称"></label><button class="ranking-action" @click="resetFilters">重置筛选</button></div>
          <div class="ranking-summary" :key="[kind,metric,month,tier].join('|')" aria-live="polite"><div><span>当月可排名 / 城市总数</span><strong>{{summary.count}}<small> / {{ranked.length}} 城</small></strong></div><div><span>{{labels[metric]}} · 中位数</span><strong>{{format(summary.median)}}</strong></div><div><span>{{kind==='average'?'历史逆推覆盖':'当前数据口径'}}</span><strong v-if="kind==='average'">{{summary.historical}}<small> 城 · 当月逆推 {{summary.derived}} 城 / 报价 {{summary.observed}} 城</small></strong><strong v-else class="ranking-summary-label">{{metric.endsWith('pct')?'统计局指数':metric==='cih_price'?'中指样本':'挂牌参考'}}</strong></div></div>
          <p v-if="coverageHint" class="audit-note" style="color:#dc2626">{{coverageHint}}</p>
          <p v-if="kind==='average'" class="audit-note">截至 {{month}}，当前筛选范围内有 {{summary.historical}} 城的历史序列包含逆推价格。当月为网页报价的城市仍可能在缺失月份使用逆推；大数字统计历史覆盖，小字区分所选月份的逆推与网页报价。</p>
          <p v-if="summary.missing.length" class="audit-note" role="status">{{month}} · {{labels[metric]}}缺失：{{summary.missing.join('、')}}（{{summary.missing.length}} 城），保留城市但不参与该指标排名、中位数和全国均价计算。<template v-if="kind==='average' && summary.missing.includes('吉林')">吉林市市场页月度序列已接入；此月超出已采集报价和有效逆推范围，不能用年度参考价替代。</template></p>
          <div class="ranking-table-heading" id="city-price-table"><div><strong>{{labels[metric]}}榜单</strong><span>筛选保留全国名次 · 点击城市查看走势</span></div><button class="ranking-action" :aria-expanded="expanded" @click="expanded=!expanded">{{expanded?'收起榜单窗口':'展开榜单窗口'}} <span aria-hidden="true">{{expanded?'↗':'↙'}}</span></button></div>
          <p class="mobile-table-hint">左右滑动查看峰值、涨跌与数据来源；点击城市查看走势。</p><div class="audit-table-wrap ranking-scroll table-cards" tabindex="0" role="region" aria-label="可滚动城市排名"><table class="audit-table"><thead><tr><th class="col-rank col-text" scope="col">名次</th><th class="col-city col-text" scope="col">城市</th><th class="col-aux col-tier col-text" scope="col">能级</th><th class="col-primary col-num" scope="col">{{labels[metric]}}</th><template v-if="kind==='average'"><th v-if="metric!=='price'" class="col-secondary col-num" scope="col" title="当月均价（元/㎡）">当月均价</th><th class="col-aux col-num" scope="col" title="截至当月峰值（元/㎡）">峰值</th><th class="col-secondary col-num" scope="col" title="较峰值涨跌">较峰值</th><th class="col-secondary col-num" scope="col" title="近12月涨跌">近12月</th></template><th class="col-aux col-text" scope="col">数据口径</th></tr></thead><tbody><tr v-for="r in ranked" :key="r.city" :data-tier="r.cityData?.tier" :style="{'--city-tier':ctx.tierColors[r.cityData?.tier]||'var(--muted)'}"><td class="ranking-position col-rank" data-label="名次"><span class="rank-number" :class="{'rank-leading':r.rank && r.rank<=3}">{{r.rank||'—'}}</span></td><td class="ranking-city col-city" data-label="城市"><button class="rank-city-link" @click="ctx.openOnMap(r.cityData)" :disabled="!r.cityData">{{r.city}}</button><small class="mobile-city-tier">{{r.cityData?.tier_name||'—'}}</small></td><td class="col-aux col-tier" data-label="能级"><span class="ranking-tier">{{r.cityData?.tier_name||'—'}}</span></td><td class="ranking-value col-primary col-num" data-label="当前指标" :class="{'price-emphasis':!metric.endsWith('pct')}" :style="{color:metric.endsWith('pct')?color(r.value):''}"><strong>{{format(r.value)}}</strong><small v-if="kind==='average' && r.value==null && r.point.annual" style="display:block">{{r.point.annual.year}}年度参考 {{money(r.point.annual.price)}} 元/㎡ · 未标月份</small><span class="ranking-bar" aria-hidden="true"><i :style="{width:barWidth(r.value),background:metric.endsWith('pct')?color(r.value):''}"></i></span></td><template v-if="kind==='average'"><td v-if="metric!=='price'" class="col-secondary col-num price-emphasis" data-label="当月均价">{{money(r.point.price)}}</td><td class="col-aux col-num price-emphasis" data-label="峰值">{{money(r.point.peak)}}</td><td class="col-secondary col-num" data-label="较峰值" :style="{color:color(r.point.drop_pct)}">{{percent(r.point.drop_pct)}}</td><td class="col-secondary col-num" data-label="近12月" :style="{color:color(r.point.yoy_pct)}">{{percent(r.point.yoy_pct)}}</td></template><td class="ranking-source col-aux" data-label="数据口径" :title="kind==='average'?(r.point.method==='listing_observation'?'来源：'+r.point.evidence?.source_url:'价格锚点：'+r.point.anchor_date+'；环比链：'+r.point.chain_months+'个月；'+(r.point.evidence?.note||'')):''"><span class="ranking-source-tag" :class="{derived:kind==='average' && r.point.method==='index_estimate'}">{{kind==='average'?source(r.point):metric.endsWith('pct')?'国家统计局指数变动':metric==='cih_price'?'中指样本平均价格':'平台挂牌参考'}}</span><template v-if="kind==='average'"><small style="display:block">{{sourceDetail(r.point)}}</small><a v-if="sourceUrl(r.point)" :href="sourceUrl(r.point)" target="_blank" rel="noopener">{{r.point.annual && r.point.price==null?'年度来源 ↗':r.point.method==='listing_observation'?'报价来源 ↗':'锚点来源 ↗'}}</a></template><span v-if="r.point.provisional"> · 暂定</span></td></tr><tr v-if="!ranked.length"><td colspan="9" class="ranking-empty" data-label="">暂无匹配城市，请调整搜索或能级筛选。</td></tr></tbody></table></div>
          <footer class="ranking-footer"><span>表头固定 · 在榜单内滚动查看更多城市</span><details><summary>价格口径与排名规则</summary><p>房价（均价）按月份区分安居客数据与逆推结构。原始快照核对通过的当月报价优先，缺月才使用近距离观测锚点与完整环比链推导；旧固定锚点估算只在城市分析中作对照，不覆盖已有报价。无可信锚点不发布绝对价格。不是新房与二手房的简单平均，也非核验成交均价。历史峰值只计算至所选月；并列共享名次，缺失不排名。中位数与条形长度按当前筛选范围的有效值计算。</p></details></footer>
        </section>`
    };
})();
