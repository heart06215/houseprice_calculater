(function(){
    HP.dashComponents.components.updateStatus={
        setup(){
            const status=Vue.ref(window.__UPDATE_STATUS__||null),available=Vue.ref(true);
            const offline=!!window.__HOUSING_DATA__;let timer,disposed=false;
            async function load(){if(offline)return;try{const r=await fetch('update_status.json',{cache:'no-cache'});if(!r.ok)throw Error('missing');const data=await r.json();if(!disposed){status.value=data;available.value=true;}}catch(_){if(!disposed)available.value=false;}}
            Vue.onMounted(()=>{load();if(!offline)timer=setInterval(load,60000);});Vue.onBeforeUnmount(()=>{disposed=true;clearInterval(timer);});
            const names={nbs:'国家统计局',income:'官方收入与最低工资',anjuke:'安居客',cih:'中指云',macro:'官方宏观指标与 LPR'};
            const states={ok:'检查成功',partial:'部分成功',failed:'来源暂不可用',checking:'检查中',cooldown:'来源冷却中',not_due:'等待下次采集'};
            const date=s=>s?new Date(s).toLocaleString('zh-CN',{hour12:false}):'尚未执行';
            const stale=Vue.computed(()=>{const at=Date.parse(status.value?.checked_at||'');return !offline&&Number.isFinite(at)&&Date.now()-at>48*3600*1000;});
            return {status,available,offline,names,states,date,stale};
        },
        template:`<details class="update-status"><summary>数据更新 <span>{{offline?'离线构建快照':status?.state==='checking'?'后台更新中':status?.state==='retained'?'使用上次有效数据':status?.last_publish?'上次发布 '+date(status.last_publish):'等待自动检查'}}</span></summary><p>{{offline?'此单文件不会联网更新，重新运行构建即可取得最新快照。':'数据由 GitHub Actions 定时采集、校验后发布；页面展示最近一次通过校验的数据。'}} {{status?.detail}}</p><p v-if="stale" role="status">最近一次自动检查已超过 48 小时，当前展示历史有效数据。</p><p v-if="!available">尚未读取到更新状态，当前数据仍可正常查看。</p><div class="audit-table-wrap"><table class="audit-table"><thead><tr><th>来源</th><th>状态</th><th>最近检查</th><th>最近成功检查</th><th>数据时期 / 说明</th></tr></thead><tbody><tr v-for="(name,key) in names" :key="key"><td>{{name}}</td><td>{{states[status?.sources?.[key]?.state]||'等待检查'}}</td><td>{{date(status?.sources?.[key]?.checked_at)}}</td><td>{{date(status?.sources?.[key]?.last_success)}}</td><td><span v-if="status?.sources?.[key]?.data_period">{{status.sources[key].data_period}} · </span>{{status?.sources?.[key]?.detail||'尚未执行'}}<small v-if="status?.sources?.[key]?.retry_after"> · 下次可尝试 {{date(status.sources[key].retry_after)}}</small><small v-if="status?.sources?.[key]?.error"> · {{status.sources[key].error}}</small></td></tr></tbody></table></div><p>检查时间与数据所属月份分别记录；检查成功不表示新一期数据已经发布。失败、空页面及验证码不会覆盖历史有效数据，刷新页面后读取最新已发布版本。</p></details>`
    };
})();
