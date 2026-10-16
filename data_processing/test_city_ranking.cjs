const fs=require('fs'),vm=require('vm'),assert=require('assert');
const sandbox={console,setTimeout,clearTimeout};sandbox.window=sandbox;
vm.runInNewContext(fs.readFileSync(__dirname+'/frontend/assets/vendor/vue.global.prod.js','utf8'),sandbox);
const V=sandbox.Vue;
const ctx={cities:V.ref([
 {city:'甲',tier:'1',monthly:[{date:'2025-08',price:100,method:'index_estimate'},{date:'2026-08',price:150,change_pct:2,method:'index_estimate'},{date:'2026-09',price:999}]},
 {city:'乙',tier:'2',monthly:[{date:'2025-08',price:200},{date:'2026-08',price:150,change_pct:-1,method:'listing_observation'}]},
 {city:'丙',tier:'2',monthly:[{date:'2026-08',price:null}]}
]),segments:V.ref({new_house:{cities:[{city:'甲',monthly:[{month:'2026-08',cih_price:300,mom_pct:1}]}]}})};
sandbox.Vue={...V,inject:()=>ctx};sandbox.HP={dashComponents:{components:{}}};
vm.runInNewContext(fs.readFileSync(__dirname+'/frontend/assets/js/shared/format.js','utf8'),sandbox);
vm.runInNewContext(fs.readFileSync(__dirname+'/frontend/assets/js/dashboard/analysis-data.js','utf8'),sandbox);
vm.runInNewContext(fs.readFileSync(__dirname+'/frontend/assets/js/dashboard/segments_tab.js','utf8'),sandbox);
(async()=>{
 const s=sandbox.HP.dashComponents.components.cityRanking.setup();
 assert.equal(s.kind.value,'average');assert.equal(s.metric.value,'price');
 /* 2026-10-02 事故回归: 仅1城有2026-09报价时，默认月必须回退到覆盖最齐的2026-08 */
 assert.equal(s.month.value,'2026-08','default month must fall back to the best-covered month');
 s.month.value='2026-08';await V.nextTick();
 let rows=s.ranked.value;assert.equal(rows[0].rank,1);assert.equal(rows[1].rank,1);assert.equal(rows[2].rank,null);
 const a=rows.find(r=>r.city==='甲');assert.equal(a.point.peak,150,'future prices cannot leak into historical peak');assert.equal(a.point.yoy_pct,50);assert.equal(s.source(a.point),'逆推结构');
 s.direction.value='asc';await V.nextTick();assert.equal(s.ranked.value.at(-1).value,null,'missing price must stay last');
 s.query.value='乙';assert.equal(s.ranked.value[0].rank,1,'search preserves overall rank');
 s.query.value='';s.tier.value='1';assert.equal(s.ranked.value.length,1);
 s.tier.value='';s.kind.value='new_house';await V.nextTick();assert.equal(s.metric.value,'cih_price');assert.equal(s.ranked.value[0].value,300);
 s.kind.value='average';await V.nextTick();assert.equal(s.metric.value,'price');
 console.log('City ranking regression tests passed: engine prices, ties, missing, historical peak, annual change, tier, source and category switching');
})().catch(e=>{console.error(e);process.exitCode=1});
