(function(){
    HP.dashComponents.components.affordability={
        props:{city:{type:String,default:''},month:{type:String,default:''}},
        setup(props){
            const ctx=Vue.inject('hpCtx'),data=Vue.computed(()=>ctx.information.value.income||{});
            const years=Vue.computed(()=>(data.value.income||[]).map(r=>r.year).sort((a,b)=>b-a));
            const year=Vue.ref(years.value[0]);
            const months=Vue.computed(()=>[...new Set([
                ...ctx.cities.value.filter(c=>!props.city||c.city===props.city).flatMap(c=>(c.monthly||[]).map(r=>r.date)),
                ...(props.city&&/^\d{4}-\d{2}$/.test(props.month)?[props.month]:[])
            ])].sort().reverse());
            const selectedMonth=Vue.ref(months.value[0]||'');
            const priceMonth=Vue.computed({get:()=>props.month||selectedMonth.value,set:v=>{if(props.city)ctx.cityMonth.value=v;else selectedMonth.value=v;}});
            Vue.watch(priceMonth,m=>{if(year.value>Number(m.slice(0,4)))year.value=years.value.find(y=>y<=Number(m.slice(0,4)))||year.value;});
            const result=Vue.computed(()=>HP.incomeAnalysis.calculate(ctx.cities.value,data.value,{city:props.city,month:props.month||selectedMonth.value,year:year.value}));
            const wageTablePeriod=Vue.shallowRef('auto');
            const wageTables=Vue.computed(()=>[...(data.value.minimum_wages||[])].sort((a,b)=>b.as_of.localeCompare(a.as_of)));
            const wageTable=Vue.computed(()=>wageTablePeriod.value==='auto'?result.value.wage:
                wageTables.value.find(r=>r.as_of===wageTablePeriod.value));
            const wageTableSources=Vue.computed(()=>{
                const record=wageTable.value;if(!record)return [];
                const sources=[{source_url:record.source_url,label:'最低工资原表'},
                    ...(record.archive_sources||[]).map(s=>({source_url:s.source_url,label:s.part==='hourly'?'小时工资原表':'月工资原表'})),
                    ...(record.corrections||[])];
                return sources.filter((s,i)=>sources.findIndex(r=>r.source_url===s.source_url)===i);
            });
            const wageTierCols=Vue.computed(()=>{
                const rows=(wageTable.value&&wageTable.value.rows)||[];
                const mLen=Math.max(0,...rows.map(r=>(r.monthly||[]).length));
                const hLen=Math.max(0,...rows.map(r=>(r.hourly||[]).length));
                const monthly=[...Array(mLen).keys()].filter(i=>rows.some(r=>Number.isFinite((r.monthly||[])[i])));
                const hourly=[...Array(hLen).keys()].filter(i=>rows.some(r=>Number.isFinite((r.hourly||[])[i])));
                return {monthly:monthly.length?monthly:[0],hourly:hourly.length?hourly:[0]};
            });
            const cityWageTier=Vue.computed(()=>{
                const rows=(result.value&&result.value.wageRows)||[];
                const mLen=Math.max(0,...rows.map(r=>(r.monthly||[]).length));
                const hLen=Math.max(0,...rows.map(r=>(r.hourly||[]).length));
                const monthly=[...Array(mLen).keys()].filter(i=>rows.some(r=>Number.isFinite((r.monthly||[])[i])));
                const hourly=[...Array(hLen).keys()].filter(i=>rows.some(r=>Number.isFinite((r.hourly||[])[i])));
                return {monthly:monthly.length?monthly:[0],hourly:hourly.length?hourly:[0]};
            });
            const rental=Vue.computed(()=>HP.incomeAnalysis.rentalSnapshot(ctx.cities.value,ctx.information.value.cih?.prices||[],{city:props.city,month:result.value.price.month}));
            const num=(v,d=0)=>Number.isFinite(v)?v.toLocaleString('zh-CN',{maximumFractionDigits:d}):'暂无';
            const fixed2=v=>Number.isFinite(v)?v.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}):'暂无';
            const safe=u=>/^https:\/\/www\.(?:stats|mohrss|nx)\.gov\.cn\//.test(u||'')?u:null;
            const highTier=Vue.computed(()=>ctx.cities.value.find(c=>c.city===props.city)?.tier === 'S');
            // Nationwide sparklines use each price month's annual income and applicable wage table.
            const sparkEls=[];const setSpark=(i,el)=>{sparkEls[i]=el;};
            let sparks=[];
            const cssVar=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
            const posNum=v=>Number.isFinite(v)&&v>0;
            const incomeByYear=Vue.computed(()=>{const map={};(data.value.income||[]).forEach(r=>{map[r.year]=r;});return map;});
            // 某月份适用的"当年"收入年度(取 <= 该月年份的最新一期)
            const incomeAt=mm=>{const y=+mm.slice(0,4);let best=null;
                Object.keys(incomeByYear.value).forEach(yy=>{if(+yy<=y&&(!best||+yy>+best))best=yy;});
                return best?incomeByYear.value[best]:null;};
            const wageMinAt=mm=>{const w=HP.incomeAnalysis.wageSnapshot(data.value.minimum_wages,mm);
                if(!w)return null;const vals=w.rows.flatMap(r=>r.monthly||[]).filter(posNum);
                return vals.length?Math.min(...vals):null;};
            // 全国三卡: 逐月倍数, 分母为该月所属年份的收入口径(扩采数据), 价格样本须覆盖 ≥30 城
            const sparkSeries=Vue.computed(()=>{
                if(props.city)return null;
                const asc=[...months.value].sort();
                const rows=asc.map(mm=>{const snap=HP.incomeAnalysis.priceSnapshot(ctx.cities.value,'',mm);
                    return {month:mm,price:snap.value,count:snap.count};})
                    .filter(r=>posNum(r.price)&&r.count>=30);
                if(rows.length<2)return null;
                const ratios=pick=>rows.map(r=>{const a=incomeAt(r.month);const m=a&&a.metrics;
                    return m?HP.incomeAnalysis.ratio(r.price,pick(m)):null;});
                return {months:rows.map(r=>r.month),
                    series:[
                        ratios(m=>m.national?.disposable_mean),
                        ratios(m=>m.national?.disposable_median),
                        ratios(m=>m.urban?.disposable_median)],
                    wage:rows.map(r=>{const wm=wageMinAt(r.month);
                        return posNum(wm)?HP.incomeAnalysis.ratio(r.price,wm*12):null;})};
            });
            const lineSpark=(el,months,row,opt)=>{
                opt=opt||{};
                const color=opt.color||cssVar('--trend-up')||'#dc2626';
                const ink=cssVar('--accent-ink')||'#0aa0c8';
                const digits=opt.digits==null?2:opt.digits,unit=opt.unit||'倍';
                const ch=HP.chart.create(el);
                ch.setOption({
                    backgroundColor:'transparent',
                    tooltip:{trigger:'axis',formatter:ps=>{const v=ps[0].value;
                        return '<b>'+months[ps[0].dataIndex]+'</b><br>'+(v==null?'—':v.toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits}))+' '+unit;}},
                    grid:{left:44,right:28,top:10,bottom:20},
                    xAxis:{type:'category',data:months,axisTick:{show:false},
                        axisLine:{lineStyle:{color:'#8aa8b0',width:1.25}},
                        axisLabel:{fontSize:10,color:'#1a3a42',
                            formatter:months.length>84?(v)=>v.slice(0,4):undefined,
                            interval:(idx,val)=>idx===0||idx===months.length-1||
                                (val.endsWith('-01')&&(months.length<=84||
                                    (+val.slice(0,4))%2===(+months[0].slice(0,4))%2))}},
                    yAxis:{type:'value',scale:true,splitNumber:2,
                        axisLabel:{fontSize:10,color:'#1a3a42'},
                        splitLine:{lineStyle:{color:'#c5d4d8',type:'dashed'}}},
                    series:[{type:'line',data:row,showSymbol:false,
                        lineStyle:{width:1.6,color},itemStyle:{color},
                        areaStyle:opt.area||'rgba(220,38,38,0.08)',
                        markLine:months.includes(priceMonth.value)?{symbol:'none',silent:true,
                            label:{show:false},lineStyle:{color:ink,type:'dashed',width:1},
                            data:[{xAxis:priceMonth.value}]}:undefined}]
                });
                return ch;
            };
            let wageEl=null;const setWageSpark=el=>{wageEl=el;};
            const renderSparks=()=>{
                sparks.forEach(c=>HP.chart.dispose(c));sparks=[];
                const s=sparkSeries.value;if(!s)return;
                s.series.forEach((row,i)=>{
                    const el=sparkEls[i];if(!el)return;
                    sparks.push(lineSpark(el,s.months,row,{}));
                });
                if(wageEl&&s.wage.some(posNum))
                    sparks.push(lineSpark(wageEl,s.months,s.wage,{}));
            };
            Vue.watch(sparkSeries,()=>Vue.nextTick(renderSparks),{immediate:true,flush:'post'});
            // —— 城市视图: 均价/总价/最低工资分档倍数/中位数倍数 逐月折线 ——
            const sumEls=[];const setSumSpark=(i,el)=>{sumEls[i]=el;};
            const bandEls=[];const setBandSpark=(i,el)=>{bandEls[i]=el;};
            let medianEl=null;const setMedianSpark=el=>{medianEl=el;};
            let citySparks=[];
            const cityWageBands=Vue.computed(()=>{
                if(!props.city)return [];
                const city=ctx.cities.value.find(c=>c.city===props.city);
                const months=(city?.monthly||[]).filter(r=>posNum(r.price)).map(r=>r.date);
                const region=['北京','上海','天津','重庆','深圳'].includes(props.city)?props.city:data.value.city_provinces?.[props.city];
                return HP.incomeAnalysis.wageHistoryBands(data.value.minimum_wages,region,months,result.value.wageBands);
            });
            const citySpark=Vue.computed(()=>{
                if(!props.city)return null;
                const c=ctx.cities.value.find(c=>c.city===props.city);if(!c)return null;
                const rows=(c.monthly||[]).filter(r=>posNum(r.price))
                    .map(r=>({month:r.date,price:r.price})).sort((a,b)=>a.month.localeCompare(b.month));
                if(rows.length<2)return null;
                const months=rows.map(r=>r.month),prices=rows.map(r=>r.price);
                const wages=data.value.minimum_wages||[];
                const prov=data.value.city_provinces?.[props.city];
                const direct=['北京','上海','天津','重庆','深圳'].includes(props.city);
                const region=direct?props.city:prov;
                const effAt=mm=>{const w=HP.incomeAnalysis.wageSnapshot(wages,mm);
                    return w?w.rows.find(r=>r.region===region):null;};
                const bands=cityWageBands.value.map(b=>{
                    const tier=(b.tier||1)-1;
                    return rows.map(r=>{const w=effAt(r.month);
                        const v=w&&Array.isArray(w.monthly)?w.monthly[tier]:null;
                        return posNum(v)?HP.incomeAnalysis.ratio(r.price,v*12):null;});
                });
                // 当年口径: 每月用其所属年份的全国中位数(扩采 2014-2025)
                const medianRow=rows.map(r=>{const a=incomeAt(r.month);
                    const med=a&&a.metrics?a.metrics.national?.disposable_median:null;
                    return posNum(med)?HP.incomeAnalysis.ratio(r.price,med):null;});
                return {months,prices,costWan:prices.map(p=>p*90/10000),bands,median:medianRow,
                        hasWage:bands.some(row=>row.some(posNum)),hasMedian:medianRow.some(posNum)};
            });
            const renderCitySparks=()=>{
                citySparks.forEach(c=>HP.chart.dispose(c));citySparks=[];
                const s=citySpark.value;if(!s)return;
                const ink=cssVar('--accent-ink')||'#0aa0c8';
                const inkArea='rgba(13,148,136,0.07)';
                if(sumEls[0]&&posNum(s.prices[0]))
                    citySparks.push(lineSpark(sumEls[0],s.months,s.prices,{color:ink,unit:'元/㎡',digits:0,area:inkArea}));
                if(sumEls[1]&&posNum(s.costWan[0]))
                    citySparks.push(lineSpark(sumEls[1],s.months,s.costWan,{color:ink,unit:'万',digits:1,area:inkArea}));
                s.bands.forEach((row,i)=>{
                    if(bandEls[i]&&s.hasWage)citySparks.push(lineSpark(bandEls[i],s.months,row,{}));
                });
                if(medianEl&&s.hasMedian)citySparks.push(lineSpark(medianEl,s.months,s.median,{}));
            };
            Vue.watch([citySpark,priceMonth],()=>Vue.nextTick(renderCitySparks),{immediate:true,flush:'post'});
            Vue.watch(priceMonth,()=>Vue.nextTick(renderSparks));
            Vue.onBeforeUnmount(()=>{sparks.forEach(c=>HP.chart.dispose(c));sparks=[];
                citySparks.forEach(c=>HP.chart.dispose(c));citySparks=[];});
            return {highTier,data,years,year,months,selectedMonth,priceMonth,result,rental,num,fixed2,safe,
                    wageTierCols,cityWageTier,wageTablePeriod,wageTables,wageTable,wageTableSources,cityWageBands,
                    setSpark,setWageSpark,setSumSpark,setBandSpark,setMedianSpark};
        },
        template:`<section class="audit-page income-panel" aria-label="90平方米房价收入比">
          <header class="income-heading"><div class="income-heading-main"><span class="ranking-eyebrow">居住购买力 · 90㎡</span><h2>{{city?city+' · 房价收入比':'全国样本 · 房价收入比'}}</h2><b class="income-area">按 90 平方米算</b></div>
            <aside class="income-yield" aria-label="中指平均房价租金比">
              <h3>平均房价租金比 <small>年化</small></h3>
              <strong>{{num(rental.annualPct,2)}}<small v-if="rental.count"> %</small></strong>
              <template v-if="rental.count"><p>月租金占房价 {{num(rental.monthlyPct,3)}}%</p><p>中指二手住宅均价 {{num(rental.averagePrice)}} 元/㎡<br>月均租金 {{num(rental.monthlyRent,2)}} 元/㎡·月</p></template>
              <p v-else>所选月份暂无中指二手房价与租金配对数据。<span v-if="rental.latestMonth">已有配对数据的最近月份：{{rental.latestMonth}}。</span></p>
              <p>{{rental.month}} · {{city||'全国样本'}} · 配对 {{rental.count}} / {{rental.total}} 城<br><a href="https://www.cih-index.com/" target="_blank" rel="noopener">数据源自中指云 ↗</a></p>
            </aside>
          </header>
          <div class="income-controls"><label>房价月份<select v-model="priceMonth" aria-label="房价月份"><option v-for="m in months" :key="m">{{m}}</option></select></label><label>收入年份<select v-model.number="year" aria-label="收入年份"><option v-for="y in years" :value="y" :key="y">{{y}}年</option></select></label><span>{{result.price.month}} 房价 / {{year||'暂无'}} 年收入</span></div>
          <p class="income-note">租金比按同城同月中指样本计算：月租金 × 12 ÷ 二手住宅均价 × 100%，表示年化毛租金回报率，未扣除空置、税费和维护成本。{{city?'房价与租金样本并非同一套住房；与左侧城市均价口径不同。':'全国仅取房价与租金均齐全的本平台城市，分别等权平均后相除，不代表全国全部住房。'}}按相同的 90㎡折算，比例不变。</p>
          <p class="income-note">{{city?'最低工资自动匹配所属省级行政区；原表单列深圳时采用深圳标准。多档标准分别测算，不将最低工资当作居民平均收入。本地收入中位数缺失时使用明确标注的全国中位数。':'全国房价参考采用同月有效城市均价等权平均，覆盖 '+result.price.count+' / '+result.price.total+' 城；不是全国全部房产或成交量加权均价。'}}</p>
          <div class="income-summary"><article><span>{{city?'本市均价':'全国房价参考 · 城市等权均价'}}</span><strong :class="{'metric-red':highTier}">{{num(result.price.value)}} <small>元/㎡</small></strong><small>逆推结构 / 安居客数据 · {{result.price.month}}</small><div v-if="city" class="spark" :ref="el=>setSumSpark(0,el)" aria-label="均价逐月走势"></div><small v-if="city" class="spark-cap">按月均价走势(本平台逆推)</small></article><article><span>90㎡参考总价</span><strong :class="{'metric-red':highTier}">{{num(result.cost==null?null:result.cost/10000,2)}} <small>万元</small></strong><small>单价 × 90 平方米</small><div v-if="city" class="spark" :ref="el=>setSumSpark(1,el)" aria-label="总价逐月走势"></div><small v-if="city" class="spark-cap">按月总价走势(90㎡ × 当月均价)</small></article><article><span>{{result.wageLabel}}</span><strong class="metric-red">{{num(result.wageMin)}}<template v-if="city&&result.wageMin!==result.wageMax">–{{num(result.wageMax)}}</template> <small>元/月</small></strong><small>最低工资标准，不是居民实际最低收入</small></article></div>
          <div v-if="!city" class="income-ratios"><article v-for="(r,i) in result.cards" :key="r.label"><span>{{r.label}}</span><p><b :class="{'metric-red':r.label.includes('中位数')}">{{num(r.income)}}</b> 元/人/年</p><strong>{{fixed2(r.ratio)}} <small>倍</small></strong><small>90㎡总价 ÷ 单人人均年收入</small><div class="spark" :ref="el=>setSpark(i,el)" aria-label="逐月倍数走势"></div><small class="spark-cap">{{['按月倍数:当月90㎡总价 ÷ 当年全国居民人均可支配收入','按月倍数:当月90㎡总价 ÷ 当年全国居民人均可支配收入中位数','按月倍数:当月90㎡总价 ÷ 当年城镇居民收入中位数'][i]}}</small></article><article><span>{{city?'最低工资分档对照':'最低工资最低档对照'}}</span><p>按月标准 × 12 个月年化</p><strong><template v-if="city&&result.wageRatioLow!==result.wageRatioHigh">{{num(result.wageRatioLow,1)}}–</template>{{num(result.wageRatioHigh,1)}} <small>倍</small></strong><small>{{city&&!result.wageExact?'分档情景范围，不代表本市单一标准':'一名劳动者全年最低工资基准'}}</small><div v-if="!city" class="spark" :ref="el=>setWageSpark(el)" aria-label="逐月倍数走势"></div><small v-if="!city" class="spark-cap">按月倍数:当月90㎡总价 ÷ 当期全国最低工资最低档年化</small></article></div>
          <div v-else class="income-ratios income-regional"><article v-for="(b,i) in cityWageBands" :key="b.region+b.tier"><span>{{b.region}} · 月最低工资第{{b.tier}}档</span><p v-if="b.monthly!==null"><b class="metric-red">{{num(b.monthly)}}</b> 元/月 · {{num(b.annual)}} 元/年</p><p v-else>所选月份暂无此档标准，仅展示已收录的历史走势。</p><strong>{{fixed2(b.ratio)}} <small>倍</small></strong><small>90㎡总价 ÷ 最低工资年化值{{result.wageExact?'':'；省内分档情景，非本市已确认适用档'}}</small><div class="spark" :ref="el=>setBandSpark(i,el)" aria-label="逐月倍数走势"></div><small class="spark-cap">按月倍数:90㎡总价 ÷ 当期施行的该档最低工资年化</small></article><article><span>收入中位数 · 全国参考</span><p><b class="metric-red">{{num(result.national.disposable_median)}}</b> 元/人/年 · {{year}}年</p><strong>{{fixed2(result.cards[1].ratio)}} <small>倍</small></strong><small>本地中位数未收录，采用全国居民中位数；不代表本市居民收入。</small><div class="spark" :ref="el=>setMedianSpark(el)" aria-label="逐月倍数走势"></div><small class="spark-cap">按月倍数:当月90㎡总价 ÷ 当年全国收入中位数</small></article></div>
          <p class="income-note">公式：房价收入比 = 90㎡参考总价 ÷ 单人年收入。为统一面积的静态比较，不是家庭口径的官方房价收入比，也不是实际还款年限；不计贷款利息、税费。最低工资与可支配收入税费口径不同。</p>
          <p v-if="year&&result.price.month.slice(0,4)!==String(year)" class="income-note">房价与收入年份不同，属于跨年参考对照。</p>
          <p v-if="!result.comparable" class="income-note">缺少对应年度收入，或收入年份晚于价格年份，收入比暂不计算。</p>
          <footer class="income-source"><small v-if="result.annual">{{result.annual.year}}年数据，数据源自国家统计局 · {{result.annual.title}} · <a :href="safe(result.annual.source_url)" target="_blank" rel="noopener">原文 ↗</a></small><small v-for="s in result.annual?.supplements||[]" :key="s.source_url">{{result.annual.year}}年{{s.label}} · <a :href="safe(s.source_url)" target="_blank" rel="noopener">原文 ↗</a></small><small v-if="result.wage">最低工资标准截至 {{result.wage.as_of}}{{result.wage.period_precision==='year'?'（年末快照）':result.wage.period_precision==='month'?'（月末快照）':''}}，源自人力资源社会保障部 · <a :href="safe(result.wage.source_url)" target="_blank" rel="noopener">原表 ↗</a></small><small v-for="s in (result.wage?.archive_sources||[]).filter(s=>s.part==='hourly'&&s.source_url!==result.wage.source_url)" :key="s.source_url">小时工资 · <a :href="safe(s.source_url)" target="_blank" rel="noopener">原表 ↗</a></small><small v-for="s in result.wage?.corrections||[]" :key="s.source_url">{{s.label}} · <a :href="safe(s.source_url)" target="_blank" rel="noopener">校正依据 ↗</a></small><small v-if="!result.wage">暂无能与所选价格月份对齐的最低工资表。</small></footer>
          <details v-if="!city" class="income-more"><summary>展开 {{year}} 年收支指标与最低工资地区分档</summary><div class="income-summary"><article><span>全国人均工资性收入</span><strong>{{num(result.national.wage_income)}} <small>元/年</small></strong></article><article><span>全国人均消费支出</span><strong>{{num(result.national.consumption)}} <small>元/年</small></strong></article><article><span>全国人均收支差额</span><strong>{{num(result.surplus)}} <small>元/年</small></strong><small>可支配收入均值减消费支出，不等同储蓄余额</small></article></div><p>按全国人均收支差额计算：90㎡总价 / 年差额 = <b class="income-red">{{num(result.surplusRatio,1)}} 倍</b>。</p>
          <div class="audit-table-wrap" aria-label="居民收支年度指标"><table class="audit-table"><thead><tr><th>居民范围</th><th>人均可支配收入</th><th>人均收入中位数</th><th>人均消费支出（元/年）</th></tr></thead><tbody><tr v-for="(label,key) in {national:'全国居民',urban:'城镇居民',rural:'农村居民'}"><td>{{label}}</td><td>{{num(result.annual?.metrics?.[key]?.disposable_mean)}}</td><td class="income-red">{{num(result.annual?.metrics?.[key]?.disposable_median)}}</td><td>{{num(result.annual?.metrics?.[key]?.consumption)}}</td></tr></tbody></table></div>
          <div class="income-controls"><label>最低工资表期<select v-model="wageTablePeriod" aria-label="最低工资表期"><option value="auto">随房价月份匹配</option><option v-for="w in wageTables" :key="w.as_of" :value="w.as_of">{{w.as_of}}{{w.period_precision==='year'?'（年末）':w.period_precision==='month'?'（月末）':''}}</option></select></label><span v-if="wageTable">最低工资标准截至 {{wageTable.as_of}}</span></div>
          <p class="income-note">历史查询仅切换下方工资表；房价收入比仍按所选房价月份匹配工资标准。</p>
          <div v-if="wageTable" class="audit-table-wrap income-wages table-cards" tabindex="0" aria-label="各地区最低工资标准"><table class="audit-table"><thead><tr><th class="col-text" scope="col">地区</th><th v-for="i in wageTierCols.monthly" :key="'nm'+i" class="col-num" scope="col" :title="'月最低工资第'+(i+1)+'档（元）'">月{{i+1}}档</th><th v-for="i in wageTierCols.hourly" :key="'nh'+i" class="col-num" scope="col" :title="'小时最低工资第'+(i+1)+'档（元）'">时{{i+1}}档</th></tr></thead><tbody><tr v-for="r in wageTable.rows" :key="r.region"><td class="col-text" data-label="地区">{{r.region}}</td><td v-for="i in wageTierCols.monthly" :key="r.region+'m'+i" class="col-num income-red" :data-label="'月'+(i+1)+'档'">{{r.monthly[i]==null?'—':num(r.monthly[i])}}</td><td v-for="i in wageTierCols.hourly" :key="r.region+'h'+i" class="col-num income-red" :data-label="'时'+(i+1)+'档'">{{r.hourly[i]==null?'—':num(r.hourly[i],1)}}</td></tr></tbody></table></div>
          <p v-else class="income-note">暂无能与所选房价月份对齐的工资表，可切换表期查看已收录的历史标准。</p>
          <footer class="income-source income-wage-table-source"><small v-for="s in wageTableSources" :key="s.source_url">{{s.label}} · <a :href="safe(s.source_url)" target="_blank" rel="noopener">原文 ↗</a></small></footer></details>
          <div v-if="city && result.wage" class="audit-table-wrap income-wages table-cards" tabindex="0" aria-label="所属行政区最低工资标准"><table class="audit-table"><thead><tr><th class="col-text" scope="col">所属行政区</th><th v-for="i in cityWageTier.monthly" :key="'cm'+i" class="col-num" scope="col" :title="'月最低工资第'+(i+1)+'档（元）'">月{{i+1}}档</th><th v-for="i in cityWageTier.hourly" :key="'ch'+i" class="col-num" scope="col" :title="'小时最低工资第'+(i+1)+'档（元）'">时{{i+1}}档</th></tr></thead><tbody><tr v-for="r in result.wageRows" :key="r.region"><td class="col-text" data-label="所属行政区">{{r.region}}</td><td v-for="i in cityWageTier.monthly" :key="r.region+'m'+i" class="col-num income-red" :data-label="'月'+(i+1)+'档'">{{r.monthly[i]==null?'—':num(r.monthly[i])}}</td><td v-for="i in cityWageTier.hourly" :key="r.region+'h'+i" class="col-num income-red" :data-label="'时'+(i+1)+'档'">{{r.hourly[i]==null?'—':num(r.hourly[i],1)}}</td></tr></tbody></table></div>
        </section>`
    };
})();
