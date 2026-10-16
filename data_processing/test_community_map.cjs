const fs=require('fs'),vm=require('vm'),assert=require('assert');
const sandbox={};sandbox.window=sandbox;vm.runInNewContext(fs.readFileSync(__dirname+'/frontend/realestate/assets/js/community.js','utf8'),sandbox);
const P=sandbox.HP.RE.community;
const row={city:'上海',name:'康桥 & <花园>'};
const links=P.links(row);assert(links.anjuke.startsWith('https://shanghai.anjuke.com/community/'));
assert.equal(new URL(links.anjuke).searchParams.get('kw'),row.name);
assert(links.beike.startsWith('https://sh.ke.com/xiaoqu/rs'));assert(links.beike.includes(encodeURIComponent(row.name)));
assert(!P.actions(row).includes('<花园>'));assert(P.actions(row).includes('noopener noreferrer'));
const coord=P.gcjToWgs(116.404,39.915);assert(Math.abs(coord[0]-116.397755)<.00001);assert(Math.abs(coord[1]-39.913596)<.00001);
const metro={cities:[{coordinate_system:'GCJ-02',lines:[{stations:[['测试站',116.404,39.915]]}]}]};
const fixed=P.normalizeMetro(metro);assert.equal(metro.cities[0].coordinate_system,'GCJ-02');assert.equal(fixed.cities[0].coordinate_system,'WGS84');
assert.deepEqual(P.normalizeMetro(fixed),fixed,'coordinates must not be shifted twice');
const data=JSON.parse(fs.readFileSync(__dirname+'/frontend/realestate/data/communities.json'));
const manifest=JSON.parse(fs.readFileSync(__dirname+'/frontend/realestate/data/communities/index.json'));
assert.equal(data.communities.length,manifest.total_rows);assert.equal(Object.values(manifest.cities).reduce((n,e)=>n+e.rows,0),manifest.total_rows);assert.equal(new Set(data.communities.map(r=>r.city+'|'+r.id)).size,data.communities.length);
assert(data.communities.every(r=>Number.isFinite(r.lat)&&Number.isFinite(r.lng)&&!('price_avg' in r)&&r.source==='osm'));
console.log('Community map: encoded search links, escaping, metro coordinates, data identity and no listing-price claims passed.');
const places=[{id:'1',city:'上海',name:'中央公寓',district:'浦东新区',address:'测试路１号',type:'公寓',housing_types:['公寓'],_md:.4},{id:'2',city:'上海',name:'中央花园',district:'黄浦区',type:'住宅小区',housing_types:[],_md:null},{id:'3',city:'北京',name:'中央公寓二期',district:'海淀区',type:'公寓',housing_types:['公寓'],_md:1.2}];
assert.equal(P.filter(places,{query:'浦东 公寓',type:'公寓',metro:'.5'})[0].id,'1');
assert.equal(P.filter(places,{query:'测试路1号'})[0].id,'1','Fullwidth search normalization');
assert.equal(P.filter(places,{metro:'2'}).length,2,'Unknown metro distance is not zero');
assert.equal(P.filter(places,{city:'上海',type:'住宅小区',district:'浦东新区'}).length,0);
assert.equal(P.filter(places,{contains:p=>p.id==='2'})[0].id,'2');
assert.equal(P.filter(places,{query:'中央公寓'})[0].id,'1','Exact name precedes suffix names');
assert.equal(places[0].id,'1','Sorting must not mutate source');
console.log('Community filtering: AND keywords, NFKC, type/district/metro/viewport intersection and exact-name ordering passed.');
const profiles=[
    {id:'p1',city:'上海',name:'旧住宅',type:'楼房',housing_types:['楼房'],completion_year:{status:'recorded',start:1997,end:1997,label:'1997年'}},
    {id:'p2',city:'上海',name:'混合住宅',type:'别墅+楼房',housing_types:['别墅','楼房'],completion_year:{status:'recorded',start:2008,end:2012,label:'2008-2012年'}},
    {id:'p3',city:'上海',name:'名称别墅',type:'别墅',housing_types:['别墅'],type_hints:['别墅'],completion_year:{status:'unknown',label:'暂缺'}},
    {id:'p4',city:'上海',name:'资料冲突',type:'公寓',housing_types:['公寓'],completion_year:{status:'conflict',label:'年份待核'}}
];
assert.equal(P.filter(profiles,{type:'公寓'}).length,1,'Ordinary multi-family buildings must not be apartment results');
assert.equal(P.filter(profiles,{type:'别墅+楼房'})[0].id,'p2');
assert(!P.typeOptions().includes('纯别墅'));assert(!P.typeOptions().includes('宿舍'));assert(!P.typeOptions().includes('独栋/联排住宅'));
assert.equal(P.filter([{id:'v1',name:'别墅',city:'上海',housing_types:['别墅'],pure_villa:true}],{type:'别墅'}).length,1,'Villa-only and other villas share one filter');
assert.equal(P.filter([{id:'v2',name:'名称别墅',city:'上海',housing_types:[],type_hints:['别墅']}],{type:'别墅',includeHints:false}).length,0);
assert.equal(P.filter([{id:'v2',name:'名称别墅',city:'上海',housing_types:[],type_hints:['别墅']}],{type:'别墅'}).length,0,'Name-only hints must not count as villa evidence');
assert.equal(P.filter([{id:'v3',name:'叠拼别墅',city:'上海',villa_subtypes:['叠拼']}],{type:'别墅'}).length,1,'Evidence-backed subtype merges into 别墅');
assert.equal(P.filter([{id:'v4',name:'只有楼房',city:'上海',housing_types:['楼房']}],{type:'别墅'}).length,0,'Contradicting evidence stays excluded, not matched');
assert(P.typeOptions(profiles,{year:'1990s'}).includes('楼房 · 1'));
assert(P.activeFilters({type:'别墅',query:'<测试>',includeHints:false}).includes('&lt;测试&gt;'));
assert.equal(P.filter(profiles,{year:'1990s'})[0].id,'p1');
assert.equal(P.filter(profiles,{year:'2010s'})[0].id,'p2','Range intersects decade');
assert.equal(P.filter(profiles,{year:'unknown'})[0].id,'p3');
assert.equal(P.filter(profiles,{year:'conflict'})[0].id,'p4');
assert.equal(P.filter(profiles,{sort:'year'})[0].id,'p2');
assert(P.badges(profiles[2]).includes('名称线索'));
assert(P.profileDetails({...profiles[0],type:'<script>'}).includes('&lt;script&gt;'));
assert(!P.profileDetails({completion_year:{evidence:[{source_url:'javascript:alert(1)',field:'year',value:'2001'}]}}).includes('href="javascript:'));
assert(data.communities.every(r=>(r.completion_year?.evidence||[]).every(e=>e.scope==='小区')));
console.log('Community profiles: morphology, approximate year ranges, missing/conflicting years, source escaping and community-only dates passed.');

const quoted={fang_profile:{source_url:'https://sh.esf.fang.com/loupan/1210022555.htm'},fang_reference:{kind:'directory_reference',value:65432,period_label:'目录展示价（未注明月份）',fetched_at:'2026-09-19',change_pct:0,change_period:'month'}};
assert(P.marketDetails(quoted).includes('65,432'));assert(P.marketDetails(quoted).includes('环比 0.00%'));
assert(!P.marketDetails({...quoted,fang_reference:{...quoted.fang_reference,change_pct:null}}).includes('环比'));
assert.equal(P.marketDetails({...quoted,fang_profile:null}),'');
assert(P.marketDetails({...quoted,fang_reference:{...quoted.fang_reference,stale_after:'2020-01-01'}}).includes('待更新'));
assert(P.actions({...row,...quoted}).includes('房天下小区资料'));
assert(!P.actions({...row,fang_profile:{source_url:'javascript:alert(1)'}}).includes('javascript:'));
console.log('Fang references: identity gate, missing/zero changes, staleness and safe source links passed.');
