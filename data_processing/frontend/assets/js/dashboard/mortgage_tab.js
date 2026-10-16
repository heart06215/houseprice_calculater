(function(){
    HP.dashComponents.components.mortgage={
        setup(){
            const kind=Vue.ref('commercial'),method=Vue.ref('annuity');
            const commercial=Vue.reactive({amount:100,rate:3.1,years:30}),fund=Vue.reactive({amount:50,rate:2.6,years:30});
            const el=Vue.ref(null);let chart,disposed=false,drawToken=0;
            const result=Vue.computed(()=>{
                try {
                    const loans=(kind.value==='combined'?[commercial,fund]:[kind.value==='fund'?fund:commercial]).map(l=>({principal:Number(l.amount)*10000,rate:typeof l.rate==='number'?l.rate:NaN,years:typeof l.years==='number'?l.years:NaN}));
                    if(loans.some(l=>!(l.principal>0)))throw Error('每笔贷款金额须大于 0');
                    return {annuity:HP.mortgage.calculate(loans,'annuity'),principal:HP.mortgage.calculate(loans,'principal')};
                }catch(e){return {error:e.message};}
            });
            const selected=Vue.computed(()=>result.value[method.value]);
            const income=Vue.reactive({salary:'',grossSalary:'',mode:'ratio',employee:0,employer:0,base:'',useCustomBase:false,employeeRate:7,employerRate:7});
            const contributions=Vue.computed(()=>{try{return HP.mortgage.contributions(income);}catch(e){return {error:e.message};}});
            const budget=Vue.computed(()=>{
                if(!selected.value)return null;
                try{return HP.mortgage.budget(selected.value.rows,income);}catch(e){return {error:e.message};}
            });
            const years=Vue.computed(()=>selected.value?selected.value.rows.length/12:0);
            const money=v=>v.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
            async function draw(){
                const token=++drawToken;
                await Vue.nextTick();if(disposed||token!==drawToken)return;HP.chart.dispose(chart);chart=null;
                if(!el.value || !selected.value)return;
                const sums=Array.from({length:years.value},(_,i)=>selected.value.rows.slice(i*12,i*12+12).reduce((s,r)=>({principal:s.principal+r.principal,interest:s.interest+r.interest}),{principal:0,interest:0}));
                chart=HP.analysisChart(el.value,sums.map((_,i)=>'第'+(i+1)+'年'),[
                    {name:'偿还本金',type:'bar',stack:'payment',data:sums.map(s=>+(s.principal/10000).toFixed(2)),itemStyle:{color:'#176878'}},
                    {name:'支付利息',type:'bar',stack:'payment',data:sums.map(s=>+(s.interest/10000).toFixed(2)),itemStyle:{color:'#0aa0c8'}}],'万元');
            }
            Vue.watch(selected,draw);
            Vue.onMounted(draw);Vue.onBeforeUnmount(()=>{disposed=true;HP.chart.dispose(chart);});
            return {kind,method,commercial,fund,result,selected,money,el,income,budget,contributions};
        },
        template:`<main class="audit-page mortgage-page"><header><span class="ranking-eyebrow">住房预算 · 还款规划</span><h2>房贷计算</h2><p class="audit-note">比较月供与总利息，让购房预算更清晰。示例利率可修改；按固定利率、每月还款测算，不含费用、利率调整及提前还款。</p></header>
          <div class="mortgage-layout"><section class="audit-band mortgage-input"><h3>贷款方案</h3><label>贷款类型<select v-model="kind"><option value="commercial">商业贷款</option><option value="fund">公积金贷款</option><option value="combined">组合贷款</option></select></label>
            <fieldset v-for="part in [{key:'commercial',name:'商业贷款',data:commercial},{key:'fund',name:'公积金贷款',data:fund}].filter(p=>kind==='combined'||p.key===kind)" :key="part.key"><legend>{{part.name}}</legend><label>贷款金额（万元）<input type="number" v-model.number="part.data.amount" min="0.01" max="1000000" step="1" :aria-label="part.name+'金额（万元）'"></label><label>年利率（%）<input type="number" v-model.number="part.data.rate" min="0" max="30" step="0.01" :aria-label="part.name+'年利率（%）'"></label><label>贷款期限（年）<input type="number" v-model.number="part.data.years" min="1" max="40" step="1" :aria-label="part.name+'期限（年）'"></label></fieldset>
            <p class="audit-note">期限支持 1–40 年作情景测算，实际可贷年限、额度和利率以当地政策及贷款审批为准。</p>
            <div class="mortgage-fund-input"><h3>公积金抵扣</h3><p class="audit-note">填写后，右侧概览自动减去双方每月缴存额。</p>
              <template v-if="income.mode==='ratio'"><label>税前月工资（元）<input type="number" min="0" step="100" v-model.number="income.grossSalary" placeholder="填写税前月工资" autocomplete="off"></label><div class="fund-rates"><label>个人缴存（%）<input type="number" min="0" max="100" step="0.5" v-model.number="income.employeeRate" autocomplete="off"></label><label>公司缴存（%）<input type="number" min="0" max="100" step="0.5" v-model.number="income.employerRate" autocomplete="off"></label></div></template>
              <template v-else><label>个人月缴存（元）<input type="number" min="0" step="50" v-model.number="income.employee" autocomplete="off"></label><label>公司月缴存（元）<input type="number" min="0" step="50" v-model.number="income.employer" autocomplete="off"></label></template>
              <details class="fund-options"><summary>更多设置 · 缴存基数 / 到手工资</summary><label>缴存计算方式<select v-model="income.mode"><option value="ratio">税前工资 × 比例</option><option value="amount">直接填写月缴存额</option></select></label><template v-if="income.mode==='ratio'"><label class="budget-check"><input type="checkbox" v-model="income.useCustomBase"> 使用实际缴存基数</label><label v-if="income.useCustomBase">实际缴存基数（元/月）<input type="number" min="0" step="100" v-model.number="income.base" autocomplete="off"></label><p class="audit-note">默认按税前工资计算；核定基数或当地上下限不同时可修正。</p></template><label>税后到手工资（元/月，选填）<input type="number" min="0" step="100" v-model.number="income.salary" placeholder="查看还贷后剩余资金" autocomplete="off"></label><p class="audit-note">填写已扣除个税、社保和个人公积金的到账工资。</p></details>
              <small class="mortgage-privacy">工资、公积金比例等私人数据直接运算，不会留存</small>
            </div></section>
          <section class="audit-band mortgage-result"><h3>还款概览</h3><div class="audit-controls"><label>还款方式<select v-model="method"><option value="annuity">等额本息</option><option value="principal">等额本金</option></select></label></div><p v-if="result.error" role="alert">{{result.error}}</p><template v-else><div class="city-kpis"><div><span>首月月供 · 元</span><strong>{{money(selected.first)}}</strong></div><div><span>末月月供 · 元</span><strong>{{money(selected.last)}}</strong></div><div><span>利息总额 · 万元</span><strong>{{money(selected.interest/10000)}}</strong></div><div><span>还款总额 · 万元</span><strong>{{money(selected.total/10000)}}</strong></div></div><p class="audit-note">{{method==='annuity'?'等额本息：同一笔贷款在固定利率期内月供基本不变。':'等额本金：每月归还相同本金，月供逐月递减。'}} 组合贷期限不同时，到期后月供随之减少。</p><div class="audit-table-wrap"><table class="audit-table"><thead><tr><th>方案比较</th><th>首月月供（元）</th><th>总利息（万元）</th></tr></thead><tbody><tr v-for="m in [{key:'annuity',name:'等额本息'},{key:'principal',name:'等额本金'}]" :key="m.key"><td>{{m.name}}</td><td>{{money(result[m.key].first)}}</td><td>{{money(result[m.key].interest/10000)}}</td></tr></tbody></table></div><p>等额本金相较等额本息少付利息 <b>{{money((result.annuity.interest-result.principal.interest)/10000)}}</b> 万元，初期月供更高。</p></template><div class="mortgage-overview-income"><h4>公积金抵扣后月供</h4>
            <p v-if="contributions.error" class="audit-note" role="status">填写税前工资和缴存比例，自动扣除个人与公司公积金。</p>
            <template v-else><p class="fund-total">每月缴存 <strong>{{money(contributions.contribution)}} 元</strong><small>个人 {{money(contributions.employee)}} + 公司 {{money(contributions.employer)}}</small></p>
              <p v-if="budget?.error" class="audit-note" role="alert">{{budget.error}}</p>
              <template v-if="budget && !budget.error"><div class="fund-equation" aria-label="首月月供减公积金抵扣等于现金月供"><div><span>首月原月供</span><strong>{{money(selected.first)}}</strong></div><b aria-hidden="true">−</b><div><span>公积金抵扣</span><strong>{{money(budget.first.fundUsed)}}</strong></div><b aria-hidden="true">=</b><div class="fund-cash"><span>首月现金月供 · 元</span><strong>{{money(budget.first.cashPayment)}}</strong></div></div>
                <p class="audit-note">末月现金月供 {{money(budget.last.cashPayment)}} 元<span v-if="budget.first.fundRetained>0">；首月剩余 {{money(budget.first.fundRetained)}} 元留在公积金账户</span>。</p>
                <p v-if="budget.first.remaining!==null" class="fund-remaining" :class="{'budget-shortfall':budget.first.remaining<0}">{{budget.first.remaining<0?'首月还贷后资金缺口':'首月还贷后剩余'}} <strong>{{money(Math.abs(budget.first.remaining))}} 元</strong><small>按实际到手工资扣除现金月供，不含生活支出。</small></p>
              </template>
            </template><p class="audit-note">按每月缴存额全部可用于还贷测算，最多抵扣当月月供；实际冲还贷以当地规则为准。</p>
</div></section></div>
          <section class="audit-band" v-if="selected"><h3>年度本金与利息</h3><div class="analysis-chart" ref="el" role="img" aria-label="每年还款的本金和利息"></div></section>
          </main>`
    };
})();
