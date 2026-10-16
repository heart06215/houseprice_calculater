/* The national overview and city analysis share aggregation and missing-data rules. */
(function () {
    const F = HP.format, A = HP.analysis, R = HP.dashChartsReport;
    const historicalPolicies = [
        {date:'2022-11',name:'金融16条',desc:'央行、银保监会通过信贷、债券、股权等渠道支持房企融资。'},
        {date:'2023-08',name:'认房不认贷',desc:'住建部等三部门推动落实认房不认贷，多地调整房贷利率与购房首付门槛。'},
        {date:'2024-05',name:'517新政',desc:'取消全国层面房贷利率下限，首套房首付比例降至15%；设立3000亿元保障性住房再贷款。'},
        {date:'2024-09',name:'一揽子增量政策',desc:'存量房贷利率平均下调约0.5个百分点，二套房首付统一降至15%，一线城市集中放宽限购。'}
    ];
    const Overview = {
        setup() {
            const ctx = Vue.inject('hpCtx'), momentumEl = Vue.ref(null), tierEl = Vue.ref(null), ddEl = Vue.ref(null);
            const policies = historicalPolicies.concat(ctx.information.value.policies || []), selectedPolicy = Vue.ref(null);
            const cities = ctx.cities.value, breadth = A.breadth(cities);
            const latest = breadth.at(-1) || {month:'',rising:0,falling:0,flat:0,valid:0,missing:cities.length};
            const groups = ['S','1','2','3'].map(tier => {
                const list = cities.filter(c => c.tier === tier), snapshots = list.map(c => A.citySnapshot(c, latest.month));
                const valid = snapshots.filter(s => A.finite(s.change3));
                return {tier, total:list.length, valid:valid.length, rising:valid.filter(s => s.rising3).length,
                    change3:A.median(valid.map(s => s.change3)), drop:A.median(snapshots.map(s => s.drop_pct).filter(A.finite))};
            });
            const medianDrop = A.median(cities.map(c => A.citySnapshot(c,latest.month).drop_pct).filter(A.finite));
            const rising3 = groups.reduce((n,g) => n + g.rising, 0);
            function togglePolicy(value) {
                const policy = typeof value === 'string' ? policies.find(p => p.name === value) : value;
                selectedPolicy.value = selectedPolicy.value === policy ? null : policy;
            }
            let charts = [];
            Vue.onMounted(() => { charts = [R.buildMomentumStack(momentumEl.value,cities),
                R.buildTierIndex(tierEl.value,cities,togglePolicy,policies),R.buildDrawdownHist(ddEl.value,cities)]; });
            Vue.onBeforeUnmount(() => charts.forEach(HP.chart.dispose));
            return {ctx,F,latest,groups,medianDrop,rising3,momentumEl,tierEl,ddEl,policies,selectedPolicy,togglePolicy};
        },
        template: `<main class="main report national-report">
          <header class="report-title-row"><h2>全国市场概览</h2><span class="report-meta">{{latest.month}} · 70城样本 · 逆推结构 / 安居客数据</span></header>
          <p class="report-summary">本月 {{latest.valid}} 城可比，上涨 {{latest.rising}} 城、下跌 {{latest.falling}} 城、持平 {{latest.flat}} 城，另有 {{latest.missing}} 城不可比。样本期回撤中位数 <span :class="F.trendClass(medianDrop)">{{F.pct(medianDrop)}}</span>。挂牌参考及指数推导不代表核验成交价。</p>
          <div class="report-stats"><div class="stat-item"><div class="stat-label">本月上涨</div><div class="stat-value up-t">{{latest.rising}}</div><div class="stat-sub">城</div></div><div class="stat-item"><div class="stat-label">本月下跌</div><div class="stat-value down-t">{{latest.falling}}</div><div class="stat-sub">城</div></div><div class="stat-item"><div class="stat-label">样本回撤中位数</div><div class="stat-value" :class="F.trendClass(medianDrop)">{{F.pct(medianDrop)}}</div></div><div class="stat-item"><div class="stat-label">连续3个月上涨</div><div class="stat-value">{{rising3}}</div><div class="stat-sub">城</div></div></div>
          <section class="audit-band"><h3>一、月度涨跌广度</h3><p class="audit-note">当月与上月有有效价格及环比值才可比；环比大于 0.05% 为上涨，小于 -0.05% 为下跌，其余持平。灰色区分持平与不可比，缺失不计为持平。</p><div ref="momentumEl" class="analysis-chart" role="img" aria-label="每月上涨持平下跌及不可比城市数量"></div></section>
          <section class="audit-band"><h3>二、各能级房价走势（2021年4月 = 100）</h3><p class="audit-note">各能级使用整个展示期都有有效价格的固定城市样本；悬浮提示显示可比城市数，避免新增报价改变样本均价。</p><div ref="tierEl" class="analysis-chart" role="img" aria-label="各能级价格走势和政策节点"></div>
            <div class="policy-row"><button v-for="p in policies" :key="p.date+p.name" class="policy-chip" :class="{sel:selectedPolicy===p}" :aria-pressed="selectedPolicy===p" @click="togglePolicy(p)">{{p.date}} {{p.name}}</button></div>
            <div class="policy-desc" v-if="selectedPolicy"><b>{{selectedPolicy.published_at||selectedPolicy.date}} {{selectedPolicy.name}}：</b>{{selectedPolicy.desc}}<p v-if="selectedPolicy.effective_at">实施日期：{{selectedPolicy.effective_at}} · {{selectedPolicy.scope}}</p><a v-if="selectedPolicy.source_url" :href="selectedPolicy.source_url" target="_blank" rel="noopener">正式文件</a> <a v-if="selectedPolicy.mirror_url" :href="selectedPolicy.mirror_url" target="_blank" rel="noopener">商务部政策库</a></div>
          </section>
          <section class="audit-band"><h3>三、全国城市回撤分布</h3><div ref="ddEl" class="analysis-chart analysis-chart-short" role="img" aria-label="城市价格较各自峰值的回撤分布"></div></section>
          <section class="audit-band"><h3>四、各能级企稳情况</h3><p class="audit-note">以相邻3个月的可比环比复合计算累计涨跌；每月上涨超过 0.05% 计入连涨。各城市明细见具体城市分析。</p><div class="audit-table-wrap table-cards"><table class="audit-table"><thead><tr><th class="col-text" scope="col">能级</th><th class="col-num" scope="col" title="可比 / 总城市数">可比/总数</th><th class="col-primary col-num" scope="col" title="连续3个月上涨">连涨3月</th><th class="col-secondary col-num" scope="col" title="近3月累计中位数">近3月中位</th><th class="col-secondary col-num" scope="col" title="回撤中位数">回撤中位</th></tr></thead><tbody><tr v-for="g in groups" :key="g.tier"><td class="col-text" data-label="能级">{{ctx.tierLabels.value[g.tier]}}</td><td class="col-num" data-label="可比/总数">{{g.valid}} / {{g.total}}</td><td class="col-primary col-num" data-label="连涨3月">{{g.rising}} 城</td><td class="col-secondary col-num" data-label="近3月中位" :class="F.trendClass(g.change3)">{{F.pct(g.change3)}}</td><td class="col-secondary col-num" data-label="回撤中位" :class="F.trendClass(g.drop)">{{F.pct(g.drop)}}</td></tr></tbody></table></div></section>
          <section class="audit-band"><h3>五、全国新房与二手房分化 · {{ctx.research.value.month}}</h3><p class="audit-note">{{ctx.research.value.basis}}。扩散净值 =（上涨城数 − 下跌城数）/ 可比城数；分化为两类环比中位数之差。</p>
            <div class="audit-table-wrap table-cards"><table class="audit-table"><thead><tr><th class="col-text" scope="col">能级</th><th class="col-num" scope="col" title="新房涨 / 平 / 跌">新房涨平跌</th><th class="col-num" scope="col" title="二手房涨 / 平 / 跌">二手涨平跌</th><th class="col-secondary col-num" scope="col">新房同比</th><th class="col-secondary col-num" scope="col">二手同比</th><th class="col-primary col-num" scope="col" title="二手扩散净值">二手扩散</th><th class="col-aux col-num" scope="col" title="环比分化">环比分化</th></tr></thead><tbody><tr v-for="g in ctx.research.value.groups" :key="g.tier"><td class="col-text" data-label="能级">{{g.tier==='all'?'全部城市':ctx.tierLabels.value[g.tier]}}</td><td class="col-num" data-label="新房涨平跌">{{g.new_house.up}} / {{g.new_house.flat}} / {{g.new_house.down}}</td><td class="col-num" data-label="二手涨平跌">{{g.second_hand.up}} / {{g.second_hand.flat}} / {{g.second_hand.down}}</td><td class="col-secondary col-num" data-label="新房同比" :class="F.trendClass(g.new_house.mean_yoy)">{{F.pct(g.new_house.mean_yoy)}}</td><td class="col-secondary col-num" data-label="二手同比" :class="F.trendClass(g.second_hand.mean_yoy)">{{F.pct(g.second_hand.mean_yoy)}}</td><td class="col-primary col-num" data-label="二手扩散" :class="F.trendClass(g.second_hand.breadth)">{{F.pct(g.second_hand.breadth)}}</td><td class="col-aux col-num" data-label="环比分化">{{g.gap_pp}} 个百分点</td></tr></tbody></table></div>
          </section>
        </main>`
    };
    HP.dashComponents.components.overview = Overview;
    HP.dashComponents.components.report = Overview;
})();
