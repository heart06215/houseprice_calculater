(function () {
    const F = HP.format;
    HP.landBidRatio = function (metrics) {
        const candidates = (Array.isArray(metrics) ? metrics : []).flatMap(row => {
            if (!row || row.scope !== '300城宅地供求' || row.label !== '平均溢价率' || row.unit !== '%' ||
                typeof row.value !== 'number' || !Number.isFinite(row.value) || row.value <= -100) return [];
            const period = /^(\d{4})年(0?[1-9]|1[0-2])(?:-(0?[1-9]|1[0-2]))?月$/.exec(row.period || '');
            if (!period || (period[3] && Number(period[3]) < Number(period[2]))) return [];
            try {
                const url = new URL(row.source_url);
                if (url.protocol !== 'https:' || !['www.cih-index.com', 'cih-index.com'].includes(url.hostname) ||
                    url.pathname !== '/data/land.html') return [];
            } catch (_) { return []; }
            return [{...row, order: Number(period[1]) * 12 + Number(period[3] || period[2])}];
        }).sort((a, b) => b.order - a.order);
        if (!candidates.length) return null;
        const latest = candidates[0];
        if (candidates.some(row => row.order === latest.order &&
            (row.period !== latest.period || row.value !== latest.value))) return null;
        return {scope: latest.scope, period: latest.period, source_url: latest.source_url,
            premium: latest.value, ratio: 1 + latest.value / 100, percent: 100 + latest.value};
    };
    HP.houseLandRatio = function (prices, supply) {
        const official = url => {
            try {
                const parsed = new URL(url);
                return parsed.protocol === 'https:' && ['www.cih-index.com', 'cih-index.com'].includes(parsed.hostname);
            } catch (_) { return false; }
        };
        if (!supply || !official(supply.source_url) || new URL(supply.source_url).pathname !== '/data/land.html') return null;
        const period = /^(\d{4})年(0?[1-9]|1[0-2])(?:-(0?[1-9]|1[0-2]))?月$/.exec(supply.period || '');
        if (!period || Number(period[3] || period[2]) < Number(period[2])) return null;
        const headers = supply.headers || [];
        const column = headers.indexOf('成交楼面均价(元/㎡)');
        if (column < 0 || headers.lastIndexOf('成交楼面均价(元/㎡)') !== column) return null;
        const positive = value => typeof value === 'number' && Number.isFinite(value) && value > 0;
        const land = new Map();
        for (const row of supply.cities || []) {
            const value = row?.values?.[column];
            if (!row?.city || !positive(value)) continue;
            if (land.has(row.city) && land.get(row.city) !== value) return null;
            land.set(row.city, value);
        }
        const candidates = (Array.isArray(prices) ? prices : []).filter(row => {
            if (!row || row.kind !== 'new_house' || row.unit !== '元/㎡' || !official(row.source_url) || !positive(row.price)) return false;
            const month = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(row.period || '');
            return month && Number(month[1]) === Number(period[1]) && Number(month[2]) >= Number(period[2]) &&
                Number(month[2]) <= Number(period[3] || period[2]);
        });
        const housePeriod = [...new Set(candidates.map(row => row.period))].sort().pop();
        if (!housePeriod) return null;
        const house = new Map();
        for (const row of candidates.filter(row => row.period === housePeriod && land.has(row.city))) {
            if (house.has(row.city) && house.get(row.city).price !== row.price) return null;
            house.set(row.city, row);
        }
        const cities = [...house.keys()].sort();
        if (!cities.length) return null;
        // 两边用完全相同的城市、相同的等权汇总；不混用 300 城总价或土地面积单价。
        const housePrice = cities.reduce((sum, city) => sum + house.get(city).price, 0) / cities.length;
        const landPrice = cities.reduce((sum, city) => sum + land.get(city), 0) / cities.length;
        const ratio = housePrice / landPrice;
        if (!Number.isFinite(ratio) || ratio <= 0) return null;
        return {cities, count: cities.length, house_period: housePeriod, land_period: supply.period,
            house_price: housePrice, land_price: landPrice, ratio, land_share: 100 / ratio,
            house_source: house.get(cities[0]).source_url, land_source: supply.source_url};
    };
    const SourcePanel = {
        setup() {
            const ctx=Vue.inject('hpCtx');
            const sampleMode=Vue.ref('rent_land'),query=Vue.ref('');
            const cih=ctx.information.value.cih || {};
            const samplePeriods=Vue.reactive({rent:'',land:'',new_house:'',second_hand:''});
            const sampleKinds=[{key:'rent',label:'租赁住宅',emphasis:true},{key:'land',label:'地价',emphasis:true},{key:'new_house',label:'新房',emphasis:false},{key:'second_hand',label:'二手房',emphasis:false}];
            const periodsByKind=Vue.computed(()=>Object.fromEntries(sampleKinds.map(({key})=>[key,[...new Set((cih.prices||[]).filter(r=>r.kind===key).map(r=>r.period))].sort().reverse()])));
            Vue.watch(periodsByKind,groups=>sampleKinds.forEach(({key})=>{
                if(!groups[key].includes(samplePeriods[key]))samplePeriods[key]=groups[key][0]||'';
            }),{immediate:true});
            const samplePanels=Vue.computed(()=>sampleKinds.filter(({key})=>sampleMode.value==='rent_land'?['rent','land'].includes(key):['new_house','second_hand'].includes(key)).map(item=>({
                ...item,periods:periodsByKind.value[item.key],
                rows:(cih.prices||[]).filter(r=>r.kind===item.key&&r.period===samplePeriods[item.key]&&r.city.includes(query.value.trim())).sort((a,b)=>b.price-a.price)
            })));
            const samplePrice=value=>typeof value==='number'&&Number.isFinite(value)?value.toLocaleString('zh-CN',{maximumFractionDigits:2}):'—';
            const sign=v=>v==null?'neutral':v>0?'delta-up':v<0?'delta-down':'neutral';
            const safe=url=>{try{return new URL(url).protocol==='https:'?url:null;}catch(_){return null;}};
            const rankingKind=Vue.ref('sales'), rankingPeriod=Vue.ref('');
            const rankingPeriods=Vue.computed(()=>(cih.rankings||[]).filter(r=>r.kind===rankingKind.value).map(r=>r.period));
            Vue.watch(rankingPeriods,values=>{if(!values.includes(rankingPeriod.value))rankingPeriod.value=values[0]||'';},{immediate:true});
            const ranking=Vue.computed(()=>(cih.rankings||[]).find(r=>r.kind===rankingKind.value&&r.period===rankingPeriod.value));
            const articlesAll=Vue.computed(()=>cih.articles||[]);
            const articles=Vue.computed(()=>articlesAll.value.slice(0, Math.ceil(articlesAll.value.length/2)));
            const land=Vue.computed(()=>cih.land&&cih.land.sections?cih.land:null);
            const landNum=v=>(typeof v==='number'&&Number.isFinite(v))?v.toLocaleString('zh-CN',{maximumFractionDigits:2}):'—';
            const HEADER_SHORT={'推出土地宗数(宗)':'推出土地宗数','推出规划建面(万㎡)':'推出建面','推出楼面均价(元/㎡)':'推出楼面价','成交土地宗数(宗)':'成交土地宗数','成交规划建面(万㎡)':'成交建面','成交楼面均价(元/㎡)':'成交楼面价','土地出让金(万元)':'出让金','平均溢价率(%)':'溢价率','排名':'排名','地块名称':'地块','出让金(万元)':'出让金','成交楼面价(元/㎡)':'楼面价'};
            const shortHeader=h=>{const s=String(h||'');return HEADER_SHORT[s]||s.replace(/[\uFF08(][^\uFF09)]*[\uFF09)]/g,'').trim()||s;};
            const landWeekly=Vue.computed(()=>land.value?land.value.sections.weekly_300:null);
            const landSupply=Vue.computed(()=>land.value?land.value.sections.supply_22:null);
            const landSupplyMetrics=Vue.computed(()=>{const s=landSupply.value;if(!s||!s.aggregate)return[];return (s.aggregate.headers||s.headers||[]).map((h,i)=>({label:h,value:(s.aggregate.values||[])[i]})).filter(m=>m.value!=null);});
            const landTop=Vue.computed(()=>land.value?land.value.sections.top_deals:null);
            const latestPeriod=Vue.computed(()=>{const p=[...new Set((cih.prices||[]).filter(r=>r.kind==='second_hand').map(r=>r.period))].sort().reverse();return p[0]||'';});
            const rentPeriod=Vue.computed(()=>[...new Set((cih.prices||[]).filter(r=>r.kind==='rent').map(r=>r.period))].sort().reverse()[0]||'');
            const shRows=Vue.computed(()=>(cih.prices||[]).filter(r=>r.kind==='second_hand'&&r.period===latestPeriod.value&&Number.isFinite(r.mom_pct)).sort((a,b)=>b.mom_pct-a.mom_pct));
            const rentRows=Vue.computed(()=>(cih.prices||[]).filter(r=>r.kind==='rent'&&r.period===rentPeriod.value&&Number.isFinite(r.price)).sort((a,b)=>b.price-a.price));
            const nhRows=Vue.computed(()=>(cih.prices||[]).filter(r=>r.kind==='new_house'&&r.period===latestPeriod.value));
            const shUp=Vue.computed(()=>shRows.value.filter(r=>r.mom_pct>0).length);
            const shDown=Vue.computed(()=>shRows.value.filter(r=>r.mom_pct<0).length);
            const shFlat=Vue.computed(()=>shRows.value.filter(r=>r.mom_pct===0).length);
            const bothPrice=Vue.computed(()=>{const nh={};nhRows.value.forEach(r=>{if(Number.isFinite(r.price))nh[r.city]=r.price;});return (cih.prices||[]).filter(r=>r.kind==='second_hand'&&r.period===latestPeriod.value&&Number.isFinite(r.price)&&nh[r.city]>0).map(r=>({city:r.city,new_price:nh[r.city],sh_price:r.price,ratio:r.price/nh[r.city]})).sort((a,b)=>b.ratio-a.ratio);});
            const landAgg=Vue.computed(()=>land.value?land.value.sections.supply_22:null);
            const landHeaders=Vue.computed(()=>landAgg.value?(landAgg.value.aggregate?.headers||landAgg.value.headers||[]):[]);
            const landValues=Vue.computed(()=>landAgg.value?(landAgg.value.aggregate||{}).values||[]:[]);
            const landAreaItems=Vue.computed(()=>landHeaders.value.map((label,i)=>({label,value:landValues.value[i]})).filter(x=>x.label.includes('万㎡')&&Number.isFinite(x.value)));
            const landPremium=Vue.computed(()=>{const h=landHeaders.value;const i=h.findIndex(x=>x.includes('溢价率'));return i>=0?landValues.value[i]:null;});
            const landBid=Vue.computed(()=>HP.landBidRatio(cih.metrics));
            const houseLand=Vue.computed(()=>HP.houseLandRatio(cih.prices,landSupply.value));
            const dailyExpanded=Vue.ref(false);
            const chartEls=Vue.reactive({shDist:null,rentRank:null,priceRatio:null,landBar:null,lpr:null,macro:null});
            const made=[];let disposed=false;
            const chartColors=Object.fromEntries(['--text-primary','--text-secondary','--accent-ink','--trend-up','--trend-down'].map(key=>[key,getComputedStyle(document.documentElement).getPropertyValue(key).trim()]));
            function createChart(el){const chart=HP.chart.create(el);made.push(chart);return chart;}
            Vue.onMounted(()=>{Vue.nextTick(()=>{if(!disposed)renderCharts();});});
            Vue.onBeforeUnmount(()=>{disposed=true;made.forEach(HP.chart.dispose);});
            function renderCharts(){
              const E=window.echarts; if(!E) return;
              chartEls.shDist=document.querySelector('[data-chart=shDist]');
              if(chartEls.shDist&&shRows.value.length){
                const c=createChart(chartEls.shDist);
                const data=shRows.value.slice(0,30).map(r=>({name:r.city,value:+(r.mom_pct||0).toFixed(2)}));
                c.setOption({title:{text:'百城二手住宅环比涨跌 TOP30 ('+latestPeriod.value+')',left:'center',textStyle:{fontSize:15,color:chartColors['--text-primary']}},
                  grid:{left:70,right:30,top:40,bottom:30},xAxis:{type:'value',axisLabel:{formatter:'{value}%'}},
                  yAxis:{type:'category',data:data.map(d=>d.name).reverse(),axisLabel:{fontSize:12,color:chartColors['--text-primary']}},
                  series:[{type:'bar',data:data.map(d=>({name:d.name,value:d.value,itemStyle:{color:d.value>=0?chartColors['--trend-up']:chartColors['--trend-down']}})).reverse(),
                    label:{show:true,position:'right',formatter:'{c}%',fontSize:11,color:chartColors['--text-secondary']}}],tooltip:{trigger:'axis',confine:true},
                  media:[{query:{maxWidth:500},option:{title:{text:'二手住宅环比涨跌 TOP30'},grid:{left:60,right:50},xAxis:{splitNumber:3}}},
                    {option:{title:{text:'百城二手住宅环比涨跌 TOP30 ('+latestPeriod.value+')'},grid:{left:70,right:30},xAxis:{splitNumber:5}}}]});
              }
              chartEls.rentRank=document.querySelector('[data-chart=rentRank]');
              if(chartEls.rentRank&&rentRows.value.length){
                const c=createChart(chartEls.rentRank);
                const data=rentRows.value.slice(0,25).map(r=>({name:r.city,value:r.price}));
                c.setOption({title:{text:'50城住宅租金 TOP25 ('+rentPeriod.value+')',left:'center',textStyle:{fontSize:15,color:chartColors['--text-primary']}},
                  grid:{left:70,right:50,top:40,bottom:30},xAxis:{type:'value'},
                  yAxis:{type:'category',data:data.map(d=>d.name).reverse(),axisLabel:{fontSize:12,color:chartColors['--text-primary']}},
                  series:[{type:'bar',data:data.map(d=>d.value).reverse(),itemStyle:{color:chartColors['--accent-ink']},
                    label:{show:true,position:'right',formatter:'{c} 元/㎡·月',fontSize:11,color:chartColors['--text-secondary']}}],tooltip:{trigger:'axis',confine:true},
                  media:[{query:{maxWidth:500},option:{title:{text:'住宅租金 TOP25'},xAxis:{splitNumber:3},series:[{label:{formatter:'{c}'}}]}},
                    {option:{title:{text:'50城住宅租金 TOP25 ('+rentPeriod.value+')'},xAxis:{splitNumber:5},series:[{label:{formatter:'{c} 元/㎡·月'}}]}}]});
              }
              chartEls.priceRatio=document.querySelector('[data-chart=priceRatio]');
              if(chartEls.priceRatio&&bothPrice.value.length){
                const c=createChart(chartEls.priceRatio);
                const data=bothPrice.value.slice(0,20);
                c.setOption({title:{text:'二手住宅 / 新房样本价格比',left:'center',textStyle:{fontSize:15,color:chartColors['--text-primary']}},
                  grid:{left:70,right:30,top:40,bottom:60},xAxis:{type:'category',data:data.map(d=>d.city),axisLabel:{rotate:45,fontSize:11}},
                  yAxis:{type:'value',name:'比值',nameLocation:'middle',nameGap:45},
                  series:[{type:'bar',data:data.map(d=>+(d.ratio.toFixed(3))),itemStyle:{color:d=>d.value>=1?chartColors['--trend-up']:chartColors['--trend-down']},
                    label:{show:true,position:'top',fontSize:10,color:chartColors['--text-secondary']}}],tooltip:{trigger:'axis',confine:true},
                  media:[{query:{maxWidth:500},option:{series:[{label:{show:false}}]}},
                    {option:{series:[{label:{show:true}}]}}]});
              }
              chartEls.landBar=document.querySelector('[data-chart=landBar]');
              if(chartEls.landBar&&landAreaItems.value.length){
                const c=createChart(chartEls.landBar);
                const items=landAreaItems.value;
                c.setOption({title:{text:'22城宅地规划建面 · '+(landAgg.value.period||'时期未取得'),left:'center',textStyle:{fontSize:15,color:chartColors['--text-primary']}},
                  grid:{left:120,right:60,top:50,bottom:40},
                  xAxis:{type:'value',name:'万㎡'},
                  yAxis:{type:'category',data:items.map(x=>x.label).reverse(),axisLabel:{fontSize:11,color:chartColors['--text-primary'],width:110,overflow:'truncate'}},
                  series:[{type:'bar',data:items.map(x=>x.value).reverse(),itemStyle:{color:chartColors['--accent-ink']},
                    label:{show:true,position:'right',fontSize:11,color:chartColors['--text-secondary'],formatter:p=>p.value.toLocaleString()}}],tooltip:{trigger:'axis',confine:true},
                  media:[{query:{maxWidth:500},option:{title:{text:'22城宅地规划建面'},grid:{left:106,right:24},xAxis:{name:'',splitNumber:2},series:[{label:{show:false}}]}},
                    {option:{title:{text:'22城宅地规划建面 · '+(landAgg.value.period||'时期未取得')},grid:{left:120,right:60},xAxis:{name:'万㎡',splitNumber:5},series:[{label:{show:true}}]}}]});
              }
            }
            const macro=Vue.computed(()=>ctx.information.value.macro?.realestate||null);
            return {ctx,cih,sampleMode,query,samplePeriods,samplePanels,samplePrice,F,sign,safe,rankingKind,rankingPeriod,rankingPeriods,ranking,land,landNum,shortHeader,landWeekly,landSupply,landSupplyMetrics,landTop,articles,
              latestPeriod,rentPeriod,shRows,rentRows,nhRows,shUp,shDown,shFlat,bothPrice,landAgg,landHeaders,landValues,landAreaItems,landPremium,landBid,houseLand,chartEls,macro,dailyExpanded};
        },
        template:`<main class="audit-page">
          <h2>中指市场情报</h2><p class="audit-note">中指百城样本价格、50城租赁及重点城市交易数据。样本范围与统计局70城不同；保留独立来源，不与挂牌序列拼接。</p>
          <section class="audit-band cih-land-ratio" aria-labelledby="cih-land-ratio-title"><h3 id="cih-land-ratio-title">土地成本占新房售价</h3>
            <div v-if="houseLand" class="hub-metrics"><div>
              <span>{{houseLand.count}}个共同城市 · 城市等权平均</span>
              <strong data-house-land-ratio>{{landNum(houseLand.land_share)}}<small>%</small></strong>
              <span>新房样本售价 {{landNum(houseLand.house_price)}} 元/㎡（{{houseLand.house_period}}）</span>
              <span>宅地成交楼面价 {{landNum(houseLand.land_price)}} 元/㎡（{{houseLand.land_period}}累计）</span>
              <span>每100元售价，对应土地成本约 {{landNum(houseLand.land_share)}} 元。</span>
              <span><a data-house-price-source :href="safe(houseLand.house_source)" target="_blank" rel="noopener">新房来源</a> · <a data-land-price-source :href="safe(houseLand.land_source)" target="_blank" rel="noopener">地价来源</a></span>
            </div></div>
            <p v-else class="analysis-empty">暂无城市、时期与面积口径可核对的新房和宅地价格，暂不显示比值。</p>
            <p class="audit-note">土地成本占比 = 拿地楼面均价 ÷ 新房样本均价 × 100%。两边使用同一批城市的等权均价；新房样本报价近似售价，地价按规划建筑面积折算。月度房价与累计地价反映市场参考水平，未对应同一项目的买地和售房。实际利润还需扣除建安、税费、融资等成本。</p>
            <details v-if="houseLand" class="audit-note"><summary>参与计算的城市（{{houseLand.count}}个）</summary><p>{{houseLand.cities.join('、')}}</p></details>
            <p v-if="landBid" class="audit-note cih-bid-note">拿地成交价 / 政府起拍价：<b data-land-bid-ratio>{{landNum(landBid.percent)}}%</b>（{{landBid.scope}} · {{landBid.period}}累计）。按平均溢价率 {{landNum(landBid.premium)}}% 换算，即 1 + 溢价率；<a data-land-bid-source :href="safe(landBid.source_url)" target="_blank" rel="noopener">来源</a>。</p>
            <p v-else class="audit-note cih-bid-note">拿地成交价 / 政府起拍价：暂无可用于换算的300城平均溢价率。</p>
          </section>
          <section class="audit-band"><h3>住宅供需与土地市场</h3><div class="hub-metrics"><div v-for="m in cih.metrics" :key="m.scope+m.label+m.source_url"><span>{{m.scope}} · {{m.period}}</span><strong>{{m.value.toLocaleString('zh-CN',{maximumFractionDigits:2})}} <small>{{m.unit}}</small></strong><span>{{m.label}} <a :href="safe(m.source_url)" target="_blank" rel="noopener">来源</a></span></div></div><p v-if="!(cih.metrics||[]).length">暂无可用公开指标</p></section>
          <section class="audit-band cih-samples"><h3>城市样本价格与租金</h3>
          <div class="audit-controls"><div class="cih-sample-modes" role="group" aria-label="样本类别组合"><button type="button" :aria-pressed="sampleMode==='rent_land'" @click="sampleMode='rent_land'">租赁住宅 / 地价</button><button type="button" :aria-pressed="sampleMode==='housing'" @click="sampleMode='housing'">新房 / 二手房</button></div><label>城市 <input type="search" v-model="query" placeholder="同时筛选两栏城市"></label></div>
          <p class="audit-note">两栏分别选择来源时期；租金与地价的发布周期、城市样本和单位不同。</p>
          <div class="cih-sample-grid"><article v-for="panel in samplePanels" :key="panel.key" class="cih-sample-panel" :data-kind="panel.key" :aria-labelledby="'cih-sample-'+panel.key">
          <h4 :id="'cih-sample-'+panel.key">{{panel.label}}</h4><div class="audit-controls"><label>时期 <select v-model="samplePeriods[panel.key]" :aria-label="panel.label+'时期'" :disabled="!panel.periods.length"><option v-for="p in panel.periods" :key="p" :value="p">{{p}}</option><option v-if="!panel.periods.length" value="">暂无时期</option></select></label><span>{{panel.rows.length}} 城</span></div>
          <p class="mobile-table-hint">左右滑动查看单位与来源。</p><div class="audit-table-wrap hub-scroll table-cards" tabindex="0" :aria-label="panel.label+'样本数据，可滚动'"><table class="audit-table sticky-city"><thead><tr><th class="col-city col-text" scope="col">城市</th><th class="col-primary col-num" scope="col" title="样本平均值">样本均价</th><th class="col-secondary col-num" scope="col" title="环比变化">环比</th><th class="col-aux col-text" scope="col">单位</th><th class="col-aux col-text" scope="col">来源</th></tr></thead><tbody><tr v-for="r in panel.rows" :key="r.city"><td class="col-city col-text" data-label="城市">{{r.city}}</td><td class="cih-sample-value col-primary col-num" data-label="样本均价" class="cih-sample-value" :class="{'cih-sample-emphasis':panel.emphasis}">{{samplePrice(r.price)}}</td><td class="col-secondary col-num" data-label="环比" :class="sign(r.mom_pct)">{{F.pct(r.mom_pct)}}</td><td class="col-aux" data-label="单位">{{r.unit}}</td><td class="col-aux" data-label="来源"><a :href="safe(r.source_url)" target="_blank" rel="noopener">中指云</a></td></tr><tr v-if="!panel.rows.length"><td colspan="5" data-label="">暂无符合该时期与城市条件的数据</td></tr></tbody></table></div>
          </article></div></section>
          <section class="audit-band"><h3>房地产企业排名</h3><div class="audit-controls"><label>榜单 <select v-model="rankingKind"><option value="sales">销售金额 TOP100</option><option value="land">权益拿地金额 TOP100</option></select></label><label>累计时期 <select v-model="rankingPeriod"><option v-for="p in rankingPeriods">{{p}}</option></select></label></div>
          <p class="audit-note">当前公开首页未返回实时排名。以下为中指公开脚本留存的历史榜单，金额按来源展示；累计时期不能直接作为单月数据比较。</p>
          <template v-if="ranking"><p>{{ranking.period}} · {{ranking.status}} · <a :href="safe(ranking.source_url)" target="_blank" rel="noopener">原始来源</a></p><div class="audit-table-wrap hub-scroll"><table class="audit-table table-cards"><thead><tr><th class="col-rank col-text" scope="col">排名</th><th class="col-text" scope="col">企业</th><th class="col-primary col-num" scope="col" :title="ranking.label+'（'+ranking.unit+'）'">{{shortHeader(ranking.label)}}</th></tr></thead><tbody><tr v-for="r in ranking.rows.slice(0,100)" :key="r.rank"><td class="col-rank" data-label="排名">{{r.rank}}</td><td class="col-text" data-label="企业">{{r.enterprise}}</td><td class="col-primary col-num" :data-label="shortHeader(ranking.label)">{{r.amount.toLocaleString('zh-CN')}}</td></tr></tbody></table></div></template><p v-else>暂无已核对的公开榜单明细</p>
          <ul><li v-for="r in cih.ranking_reports" :key="r.url"><a :href="safe(r.url)" target="_blank" rel="noopener">{{r.title}}</a></li></ul></section>
          <section class="audit-band"><h3>日度商品住宅成交 <button class="tog" @click="dailyExpanded=!dailyExpanded" aria-label="展开或收起">{{dailyExpanded?'收起 ↑':'展开 ↓'}} {{(cih.daily||[]).length}}条</button></h3><div class="audit-table-wrap" v-show="dailyExpanded" style="max-height:360px;overflow-y:auto"><table class="audit-table sticky-city table-cards"><thead><tr><th class="col-city col-text" scope="col">城市</th><th class="col-text" scope="col">日期</th><th class="col-primary col-num" scope="col">成交套数</th><th class="col-secondary col-num" scope="col" title="成交面积（万㎡）">成交面积</th></tr></thead><tbody><tr v-for="r in cih.daily" :key="r.city+r.date"><td class="col-city" data-label="城市">{{r.city}}</td><td data-label="日期">{{r.date}}</td><td class="col-primary col-num" data-label="成交套数">{{F.price(r.units)}}</td><td class="col-secondary col-num" data-label="成交面积">{{r.area_wan_sqm}}</td></tr></tbody></table></div></section>
          <section class="audit-band"><h3>市场动态与报告</h3><ul class="hub-news-compact"><li v-for="a in articles" :key="a.url"><a :href="safe(a.url)" target="_blank" rel="noopener">{{a.title}}</a><span class="hub-news-date">{{a.fetched_at.slice(0,10)}}</span></li></ul><div class="hub-more"><a class="hub-more-btn" href="https://www.cih-index.com/" target="_blank" rel="noopener">查看更多 → 中指云</a></div></section>
          <section class="audit-band" aria-labelledby="cih-land-title"><h3 id="cih-land-title">宅地市场（中指土地数据）</h3>
          <p class="audit-note">来源：中指云土地页（读取站点自行渲染的表格）。300城成交为周度，22城供求为月度累计口径按来源展示；来源周期内无更新时不重复爬取，仅在有更新后重新采集。</p>
          <template v-if="land">
            <div class="hub-metrics" v-if="landWeekly && landWeekly.period">
              <div><span>300城 · {{landWeekly.period}}</span><strong>{{landNum(landWeekly.stats['成交宗数_宗'])}} <small>宗</small></strong><span>成交宗数</span></div>
              <div><span>300城 · {{landWeekly.period}}</span><strong>{{landNum(landWeekly.stats['规划建筑面积_万㎡'])}} <small>万㎡</small></strong><span>规划建筑面积</span></div>
              <div><span>300城 · {{landWeekly.period}}</span><strong>{{landNum(landWeekly.stats['成交楼面价_元㎡'])}} <small>元/㎡</small></strong><span>成交楼面价</span></div>
            </div>
            <p v-if="landWeekly && !landWeekly.period" class="audit-note">300城周度成交：本页未取得周期标注，未展示</p>
            <div class="hub-metrics" v-if="landSupplyMetrics.length">
              <div v-for="m in landSupplyMetrics" :key="m.label"><span>22城宅地供求 · {{landSupply.period || '时期未取得'}}</span><strong>{{landNum(m.value)}}</strong><span>{{m.label}}</span></div>
            </div>
            <div class="audit-table-wrap hub-scroll" tabindex="0" v-if="landSupply && (landSupply.cities||[]).length" :aria-label="'22城宅地供求明细，可滚动'">
              <table class="audit-table land-supply-table sticky-city"><thead><tr><th class="col-city col-text" scope="col">城市</th><th class="col-num" scope="col" v-for="h in landSupply.headers" :key="h" :title="h">{{shortHeader(h)}}</th></tr></thead><tbody><tr v-for="c in landSupply.cities" :key="c.city"><td class="col-city col-text" data-label="城市">{{c.city}}</td><td class="col-num" v-for="(v,i) in c.values" :key="c.city+i" :data-label="shortHeader(landSupply.headers[i])">{{landNum(v)}}</td></tr></tbody></table>
            </div>
            <p class="audit-note" v-if="land.fetched_at">宅地数据采集时间：{{land.fetched_at.slice(0,10)}}<template v-if="land.changed_sections && land.changed_sections.length"> · 本次更新区块：{{land.changed_sections.join('、')}}</template><template v-else> · 周期内无更新沿用</template></p>
            <template v-if="landTop">
              <template v-if="(landTop.tables||[]).length">
                <div v-for="(t,ti) in landTop.tables" :key="'top'+ti" class="audit-table-wrap hub-scroll table-cards"><table class="audit-table sticky-city"><thead><tr><th scope="col" :class="hi===0?'col-city col-text':'col-num'" v-for="(h,hi) in Object.keys(t.rows[0])" :key="hi" :title="h">{{shortHeader(h)}}</th></tr></thead><tbody><tr v-for="(r,ri) in t.rows" :key="ri"><td v-for="(h,hi) in Object.keys(t.rows[0])" :key="hi" :class="hi===0?'col-city col-text':'col-num'" :data-label="shortHeader(h)">{{typeof r[h]==='number'?landNum(r[h]):r[h]}}</td></tr></tbody></table></div>
              </template>
              <p v-else-if="landTop.note" class="audit-note">全国地块成交TOP10：{{landTop.note}}（表头与时期选项已记录：{{(landTop.period_options||[]).slice(0,1).join('')}}）</p>
            </template>
          </template>
          <p v-else class="audit-note">暂未取得可核对的宅地数据，自动更新将在后续批次重试。</p>
          </section>
          <section class="audit-band" aria-labelledby="cih-sh-dist"><h3 id="cih-sh-dist">百城二手住宅环比涨跌</h3>
            <div style="width:100%;height:400px" id="shDist" data-chart="shDist"></div>
            <p class="audit-note">{{shUp}} 城上涨 · {{shDown}} 城下跌 · {{shFlat}} 城持平（{{latestPeriod}}，共 {{shRows.length}} 城）</p>
          </section>
          <section class="audit-band" aria-labelledby="cih-rent-rank"><h3 id="cih-rent-rank">50城住宅租金排名</h3>
            <div style="width:100%;height:400px" data-chart="rentRank"></div>
            <p class="audit-note">{{rentPeriod}} · 租金单位：元/㎡·月。仅显示 TOP25，全部数据见上方城市样本表格。</p>
          </section>
          <section class="audit-band" aria-labelledby="cih-price-ratio"><h3 id="cih-price-ratio">新房 vs 二手价格对比</h3>
            <div v-if="bothPrice.length" style="width:100%;height:400px" id="priceRatio" data-chart="priceRatio"></div><p v-else>暂无同城市、同月份的新房与二手房样本对照。</p>
            <p class="audit-note">比值>1 表示二手房价高于同城市新房样本价——可能反映区域供给差异或样本结构不同。</p>
          </section>
          <section class="audit-band" aria-labelledby="cih-land-bar"><h3 id="cih-land-bar">22城宅地供求汇总</h3>
            <div v-if="landAreaItems.length" style="width:100%;height:300px" data-chart="landBar"></div><p v-else>暂无带单位的供求面积数据。</p>
            <p class="audit-note" v-if="landPremium!=null">平均溢价率：<b>{{landPremium}}%</b></p>
          </section>
          <section v-if="macro" class="audit-band" aria-labelledby="cih-macro"><h3 id="cih-macro">全国房地产开发宏观指标</h3>
            <p class="audit-note">{{macro.period}} · {{macro.source}} · {{macro.basis}} · <a :href="safe(macro.source_url)" target="_blank" rel="noopener">统计局原文</a></p>
            <div class="hub-metrics"><div v-for="m in macro.items" :key="m.label"><span>{{m.label}}</span><strong>{{landNum(m.value)}} <small>{{m.unit}}</small></strong><span :class="sign(m.yoy)">同比 {{F.pct(m.yoy)}}</span></div></div>
          </section>

          <details class="audit-band"><summary>公开来源与采集状态</summary><ul class="hub-sources"><li v-for="p in cih.pages" :key="p.url"><a :href="safe(p.url)" target="_blank" rel="noopener">{{p.title}}</a> · {{p.status}} · {{p.fetched_at.slice(0,10)}}</li></ul></details>
        </main>`
    };
    const ReleasesPanel={
        setup(){
            const ctx=Vue.inject('hpCtx');
            const releases=ctx.information.value.releases||[];
            const month=Vue.ref(releases[0]?.month||''),kind=Vue.ref('second_hand'),area=Vue.ref('合计'),query=Vue.ref('');
            const release=Vue.computed(()=>releases.find(r=>r.month===month.value));
            const areas=Vue.computed(()=>[...new Set((release.value?.rows||[]).filter(r=>r.kind===kind.value).map(r=>r.area))]);
            const rows=Vue.computed(()=>(release.value?.rows||[]).filter(r=>r.kind===kind.value&&r.area===area.value&&r.city.includes(query.value.trim())).sort((a,b)=>b.mom_pct-a.mom_pct));
            const summary=Vue.computed(()=>({up:rows.value.filter(r=>r.mom_pct>0).length,down:rows.value.filter(r=>r.mom_pct<0).length,flat:rows.value.filter(r=>r.mom_pct===0).length}));
            const sign=v=>v==null?'neutral':v>0?'delta-up':v<0?'delta-down':'neutral';
            const label=v=>v==null?'暂无':v>0?'上涨 '+F.pct(v):v<0?'下跌 '+F.pct(v):'持平 0%';
            return {ctx,releases,month,kind,area,query,release,areas,rows,summary,sign,label};
        },
        template:`<main class="audit-page"><h2>统计局月度发布</h2>
          <div class="audit-controls"><label>报告月份 <select v-model="month"><option v-for="r in releases">{{r.month}}</option></select></label><label>住宅类别 <select v-model="kind"><option value="new_house">新建商品住宅</option><option value="second_hand">二手住宅</option></select></label><label>面积类别 <select v-model="area"><option v-for="a in areas">{{a}}</option></select></label><label>城市 <input v-model="query" type="search" placeholder="城市名称"></label></div>
          <p v-if="release" class="audit-note">{{release.title}} · 发布日期 {{release.publish_date||'未记录'}} · <a :href="release.source_url" target="_blank" rel="noopener">统计局原文</a></p>
          <div class="hub-metrics"><div><span>环比上涨</span><strong class="delta-up">{{summary.up}} 城</strong></div><div><span>环比下跌</span><strong class="delta-down">{{summary.down}} 城</strong></div><div><span>环比持平</span><strong>{{summary.flat}} 城</strong></div></div>
          <div class="release-grid"><article v-for="r in rows" :key="r.city"><h3>{{r.city}}</h3><div><span>较上月</span><strong :class="sign(r.mom_pct)">{{label(r.mom_pct)}}</strong></div><div><span>较去年同月</span><strong :class="sign(r.yoy_pct)">{{label(r.yoy_pct)}}</strong></div></article></div><p v-if="!rows.length">没有符合条件的数据</p>
        </main>`
    };
    HP.dashComponents.components.information={
        components:{SourcePanel,ReleasesPanel},
        setup(){
            const ctx=Vue.inject('hpCtx');
            function scrollView(){
                if(window.matchMedia('(max-width:700px), (max-height:500px) and (max-width:950px)').matches){
                    const hub=document.querySelector('.information-hub');
                    if(hub)window.scrollTo({top:hub.getBoundingClientRect().top+window.scrollY,behavior:'auto'});
                }
            }
            return {ctx,views:HP.dashComponents.components,scrollView};
        },
        template:`<div class="information-hub"><nav class="hub-tabs" aria-label="信息分类"><button v-for="item in [{key:'overview',name:'市场概览'},{key:'city',name:'城市分析'},{key:'cih',name:'中指数据'},{key:'releases',name:'统计局月报'},{key:'mortgage',name:'房贷计算'}]" :key="item.key" :aria-pressed="ctx.infoView.value===item.key" @click="ctx.infoView.value=item.key">{{item.name}}</button></nav>
          <component :is="views.updateStatus"/>
          <Transition name="hp-pane" mode="out-in" @after-enter="scrollView"><SourcePanel v-if="ctx.infoView.value==='cih'"/><ReleasesPanel v-else-if="ctx.infoView.value==='releases'"/>
          <component :is="views.mortgage" v-else-if="ctx.infoView.value==='mortgage'"/>
          <component :is="views.cityAnalysis" v-else-if="ctx.infoView.value==='city'"/>
          <section v-else aria-label="市场概览">
            <component :is="views.cityRanking"/>
            <component :is="views.affordability"/>
            <component :is="views.overview"/>
            <component :is="views.realestateTrends"/>
            <component :is="views.nationalTrends"/>
            <p class="report-sources">数据来源:安居客(挂牌价格锚点)· 中指网(独立住宅与租赁样本)· 国家统计局(70城房价指数、居民收支)· 人社部(最低工资)。可核对的安居客挂牌观测优先,缺月以统计局环比推导并标注估算;中指样本价格独立展示,官方指数不作为元/㎡单价。</p>
          </section></Transition>
        </div>`
    };
})();
