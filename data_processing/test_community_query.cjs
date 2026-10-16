'use strict';
/* Plain-CommonJS tests for HP.RE.community query API.
   Run: D:/nodejs/node.exe data_processing/test_community_query.cjs (repo root cwd). */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const sandbox={};sandbox.window=sandbox;
vm.runInNewContext(fs.readFileSync(__dirname+'/frontend/realestate/assets/js/community.js','utf8'),sandbox);
const P=sandbox.HP.RE.community;

var checks=0;
function check(name,fn){
  try{ fn(); checks++; }
  catch(e){ console.error('[test_community_query] FAIL: '+name); console.error((e&&e.stack)||String(e)); process.exitCode=1; }
}

/* ---------- helpers ---------- */
/* Empirical meters per 1 degree latitude (avoids guessing earth radius). */
var MPERDEG=P.distance({name:'__probeC',lat:30,lng:120},
  [{name:'__probeS',city:'甲市',lat:31,lng:120,operational_status:'operational'}],{},'operational').meters;
function latAt(meters){ return 30+meters/MPERDEG; }

var nearStation={name:'近站',city:'甲市',lat:30,lng:120,operational_status:'operational'};
function qopts(extra){ return Object.assign({city:'甲市',versions:{},stations:[nearStation]},extra||{}); }
function mkRow(extra){ return Object.assign({name:'r',city:'甲市',district:'A区',lat:30,lng:120},extra||{}); }
function bucketOf(row,options){
  var res=P.query([row],options);
  if(res.supported.indexOf(row)>=0)return 'supported';
  if(res.pending.indexOf(row)>=0)return 'pending';
  if(res.excluded.indexOf(row)>=0)return 'excluded';
  if(res.uncovered.indexOf(row)>=0)return 'uncovered';
  return 'none';
}

/* ---------- A. cacheKey ---------- */
check('cacheKey empty -> "|||"',function(){ assert.strictEqual(P.cacheKey({}),'|||'); });
check('cacheKey partial -> "|S|C|"',function(){ assert.strictEqual(P.cacheKey({station_set:'S',coord_conversion:'C'}),'|S|C|'); });
check('cacheKey order fixed',function(){ assert.strictEqual(P.cacheKey({coord_conversion:'C',station_set:'S'}),'|S|C|'); });
check('cacheKey stable for identical input',function(){
  assert.strictEqual(P.cacheKey({station_set:'S',coord_conversion:'C'}),P.cacheKey({station_set:'S',coord_conversion:'C'}));
});

/* ---------- B. distance ---------- */
check('distance near station at same point -> known <1m',function(){
  var c={name:'c1',city:'甲市',lat:30,lng:120};
  var st=[{name:'近站',city:'甲市',lat:30,lng:120,operational_status:'operational'}];
  var d=P.distance(c,st,{},'operational');
  assert.strictEqual(d.status,'known');
  assert.ok(d.meters<1,'meters='+d.meters);
});
check('distance memoized -> same object ref',function(){
  var c={name:'c2',city:'甲市',lat:30,lng:120};
  var st=[{name:'近站',city:'甲市',lat:30,lng:120,operational_status:'operational'}];
  var v={station_set:'v1'};
  var d1=P.distance(c,st,v,'operational');
  var d2=P.distance(c,st,v,'operational');
  assert.strictEqual(d1,d2);
});
check('distance recompute on version change -> diff ref, known',function(){
  var c={name:'c3',city:'甲市',lat:30,lng:120};
  var st=[{name:'近站',city:'甲市',lat:30,lng:120,operational_status:'operational'}];
  var v={station_set:'v1'};
  var d1=P.distance(c,st,v,'operational');
  var d3=P.distance(c,st,{station_set:'v2'},'operational');
  assert.notStrictEqual(d1,d3);
  assert.strictEqual(d3.status,'known');
});
check('distance no same-city gate -> picks nearer other-city station',function(){
  var c={name:'c4',city:'甲市',lat:30,lng:120};
  var st=[
    {name:'甲站',city:'甲市',lat:30.1,lng:120,operational_status:'operational'},
    {name:'乙站',city:'乙市',lat:30.001,lng:120,operational_status:'operational'}
  ];
  assert.strictEqual(P.distance(c,st,{},'operational').station,'乙站');
});
check('distance default operational scope skips under_construction',function(){
  var c={name:'c5',city:'甲市',lat:30,lng:120};
  var st=[
    {name:'施工站',city:'甲市',lat:30,lng:120,operational_status:'under_construction'},
    {name:'运营站',city:'甲市',lat:latAt(500),lng:120,operational_status:'operational'}
  ];
  var d=P.distance(c,st,{},'operational');
  assert.strictEqual(d.station,'运营站');
  assert.ok(Math.abs(d.meters-500)<2,'meters='+d.meters);
});
check('distance only planned/under_construction -> unknown',function(){
  var c={name:'c6',city:'甲市',lat:30,lng:120};
  var st=[
    {name:'规划站',city:'甲市',lat:30,lng:120,operational_status:'planned'},
    {name:'施工站',city:'甲市',lat:30,lng:120,operational_status:'under_construction'}
  ];
  assert.strictEqual(P.distance(c,st,{},'operational').status,'unknown');
});
check('metro boundary inclusive (<=): D supported, D-0.5 excluded',function(){
  var c={name:'c7',city:'甲市',lat:30,lng:120};
  var st=[{name:'边界站',city:'甲市',lat:latAt(2000),lng:120,operational_status:'operational'}];
  var v={};
  var D=P.distance(c,st,v,'operational').meters;
  assert.ok(Math.abs(D-2000)<2,'D='+D);
  assert.strictEqual(P.explain(c,{metroMaxM:D,versions:v,stations:st})[2].status,'supported');
  assert.strictEqual(P.explain(c,{metroMaxM:D-0.5,versions:v,stations:st})[2].status,'excluded');
});
check('metro 2000 option: 1999.5m supported, 2000.5m excluded',function(){
  var cIn={name:'c8',city:'甲市',lat:30,lng:120};
  var stIn=[{name:'近边界',city:'甲市',lat:latAt(1999.5),lng:120,operational_status:'operational'}];
  assert.strictEqual(P.explain(cIn,{metroMaxM:2000,versions:{},stations:stIn})[2].status,'supported');
  var cOut={name:'c9',city:'甲市',lat:30,lng:120};
  var stOut=[{name:'远边界',city:'甲市',lat:latAt(2000.5),lng:120,operational_status:'operational'}];
  assert.strictEqual(P.explain(cOut,{metroMaxM:2000,versions:{},stations:stOut})[2].status,'excluded');
});

/* ---------- C. query year cases ---------- */
check('year: 2003-2003 in 2000-2005 -> supported',function(){
  var r=mkRow({completion_year:{status:'recorded',precision:'year',start:2003,end:2003}});
  assert.strictEqual(bucketOf(r,qopts({yearFrom:2000,yearTo:2005,metroMaxM:5000})),'supported');
});
check('year: 2000-2005 in 2000-2005 -> supported',function(){
  var r=mkRow({completion_year:{status:'recorded',precision:'range',start:2000,end:2005}});
  assert.strictEqual(bucketOf(r,qopts({yearFrom:2000,yearTo:2005,metroMaxM:5000})),'supported');
});
check('year: 2010-2012 in 2000-2005 -> excluded',function(){
  var r=mkRow({completion_year:{status:'recorded',precision:'range',start:2010,end:2012}});
  assert.strictEqual(bucketOf(r,qopts({yearFrom:2000,yearTo:2005,metroMaxM:5000})),'excluded');
});
check('year: precision approximate -> pending',function(){
  var r=mkRow({completion_year:{status:'recorded',precision:'approximate',start:2001,end:2001}});
  assert.strictEqual(bucketOf(r,qopts({yearFrom:2000,yearTo:2005,metroMaxM:5000})),'pending');
});
check('year: precision decade -> pending',function(){
  var r=mkRow({completion_year:{status:'recorded',precision:'decade',start:1990,end:1999}});
  assert.strictEqual(bucketOf(r,qopts({yearFrom:2000,yearTo:2005,metroMaxM:5000})),'pending');
});
check('year: status unknown -> pending',function(){
  var r=mkRow({completion_year:{status:'unknown'}});
  assert.strictEqual(bucketOf(r,qopts({yearFrom:2000,yearTo:2005,metroMaxM:5000})),'pending');
});
check('year: {} -> pending',function(){
  var r=mkRow({});
  assert.strictEqual(bucketOf(r,qopts({yearFrom:2000,yearTo:2005,metroMaxM:5000})),'pending');
});
check('year: partial overlap 2000-2005 vs 2003-2010 -> pending',function(){
  var r=mkRow({completion_year:{status:'recorded',precision:'range',start:2000,end:2005}});
  assert.strictEqual(bucketOf(r,qopts({yearFrom:2003,yearTo:2010,metroMaxM:5000})),'pending');
});
check('year: no yearFrom/yearTo -> supported reason 未限定年份',function(){
  var r=mkRow({completion_year:{status:'recorded',precision:'year',start:2003,end:2003}});
  var ex=P.explain(r,qopts({metroMaxM:5000}));
  assert.strictEqual(ex[0].status,'supported');
  assert.strictEqual(ex[0].reason,'未限定年份');
});

/* ---------- C4. B10 cross-source selection layer over raw conflict ---------- */
function policyRow(policy){return mkRow({completion_year:{status:'conflict'},year_policy:policy});}
check('B10 cross-source year conflict: year_policy primary year -> supported',function(){
  var r=policyRow({primary_source:'a.example.com',policy_version:'year-primary-1',selected_source_id:'p',
    year_raw:{start:2003,end:2003,precision:'year',scope:'小区'},
    year_start:2003,year_end:2003,year_scope:'小区',selection_reason:'跨来源差异不阻断',
    cross_source_difference:[{platform:'b.example.com',value:{start:2008,end:2008}}]});
  var opts=qopts({yearFrom:2000,yearTo:2005,metroMaxM:5000});
  assert.strictEqual(P.explain(r,opts)[0].status,'supported');
  assert.strictEqual(bucketOf(r,opts),'supported');
});
check('B10 cross-source selection: primary year outside range -> excluded (range test)',function(){
  var r=policyRow({year_raw:{start:2003,end:2003,precision:'year',scope:'小区'}});
  assert.strictEqual(P.explain(r,qopts({yearFrom:2010,yearTo:2015,metroMaxM:5000}))[0].status,'excluded');
});
check('B10 same-platform year conflict (no year_policy) -> pending',function(){
  var r=mkRow({completion_year:{status:'conflict'}});
  var ex=P.explain(r,qopts({yearFrom:2000,yearTo:2005,metroMaxM:5000}));
  assert.strictEqual(ex[0].status,'pending');
  assert.strictEqual(ex[0].reason,'年份证据冲突');
});

/* ---------- C2. F1 blank year from raw form values (''/null/undefined) ---------- */
function yearRow(name,start,end){return mkRow({name:name,completion_year:{status:'recorded',precision:'year',start:start,end:end==null?start:end}});}
check('F1 blank years: both empty "" -> 西郊庄园 reachable, not excluded',function(){
  var r=yearRow('西郊庄园',2000,2015);
  var opts=qopts({yearFrom:'',yearTo:'',metroMaxM:5000});
  assert.strictEqual(P.explain(r,opts)[0].status,'supported');
  assert.strictEqual(bucketOf(r,opts),'supported');
});
check('F1 blank years: both null -> supported',function(){
  var r=yearRow('西郊庄园',2000,2015);
  assert.strictEqual(P.explain(r,qopts({yearFrom:null,yearTo:null,metroMaxM:5000}))[0].status,'supported');
});
check('F1 blank years: both undefined (omitted) -> supported',function(){
  var r=yearRow('西郊庄园',2000,2015);
  assert.strictEqual(P.explain(r,qopts({yearFrom:undefined,yearTo:undefined,metroMaxM:5000}))[0].status,'supported');
});
check('F1 only start year -> supported (no [2000,0])',function(){
  var r=yearRow('西郊庄园',2000,2015);
  assert.strictEqual(P.explain(r,qopts({yearFrom:'2000',yearTo:'',metroMaxM:5000}))[0].status,'supported');
  assert.strictEqual(P.explain(r,qopts({yearFrom:2000,yearTo:null,metroMaxM:5000}))[0].status,'supported');
});
check('F1 only end year -> supported (no [0,2015] on start)',function(){
  var r=yearRow('西郊庄园',2000,2015);
  assert.strictEqual(P.explain(r,qopts({yearFrom:'',yearTo:'2015',metroMaxM:5000}))[0].status,'supported');
  assert.strictEqual(P.explain(r,qopts({yearFrom:null,yearTo:2015,metroMaxM:5000}))[0].status,'supported');
});
check('F1 reset (null/null) must not exclude',function(){
  var r=yearRow('西郊庄园',2000,2015);
  var res=P.query([r],qopts({yearFrom:null,yearTo:null,metroMaxM:5000}));
  assert.ok(res.excluded.indexOf(r)<0,'blank/reset year must not exclude');
  assert.ok(res.supported.indexOf(r)>=0);
});
check('F1 blank year must not become [0,0]: 2003 row stays supported',function(){
  var r=mkRow({name:'r2003',completion_year:{status:'recorded',precision:'year',start:2003,end:2003}});
  assert.strictEqual(P.explain(r,qopts({yearFrom:'',yearTo:'',metroMaxM:5000}))[0].status,'supported');
});

/* ---------- C3. F4 incomplete form list is not negative evidence ---------- */
check('F4 incomplete [楼房]+name hint -> pending, not excluded',function(){
  var r=mkRow({name:'XX别墅',housing_types:['楼房']});
  var opts=qopts({forms:['别墅'],metroMaxM:5000});
  assert.strictEqual(P.explain(r,opts)[1].status,'pending');
  assert.strictEqual(bucketOf(r,opts),'pending');
});
check('F4 incomplete [楼房] no hint -> still pending',function(){
  var r=mkRow({name:'某小区',housing_types:['楼房']});
  assert.strictEqual(P.explain(r,qopts({forms:['别墅'],metroMaxM:5000}))[1].status,'pending');
});
check('F4 explicit negation 不含别墅 -> excluded',function(){
  var r=mkRow({name:'否定别墅',residential_forms:['不含别墅']});
  var ex=P.explain(r,qopts({forms:['别墅'],metroMaxM:5000}))[1];
  assert.strictEqual(ex.status,'excluded');
  assert.ok(ex.reason.indexOf('明确否定')>=0,ex.reason);
});
check('F4 verified complete list without villa -> excluded',function(){
  var r=mkRow({name:'完整清单无别墅',residential_forms:['楼房'],forms_exhaustive:true});
  var ex=P.explain(r,qopts({forms:['别墅'],metroMaxM:5000}))[1];
  assert.strictEqual(ex.status,'excluded');
  assert.ok(ex.reason.indexOf('完整类型清单')>=0,ex.reason);
});
check('F4 name hint never upgraded to positive',function(){
  var r=mkRow({name:'XX别墅',residential_forms:['楼房']});
  var opts=qopts({forms:['别墅'],metroMaxM:5000});
  assert.ok(P.query([r],opts).supported.indexOf(r)<0);
});

check('F4 form states carry spec literals',function(){
  var o=qopts({forms:['别墅'],metroMaxM:5000});
  var hit=P.explain(mkRow({residential_forms:['别墅']}),o)[1];
  assert.strictEqual(hit.state,'发现一种类型');
  assert.ok(hit.reason.indexOf('发现一种类型')>=0,hit.reason);
  var complete=P.explain(mkRow({residential_forms:['楼房'],forms_exhaustive:true}),o)[1];
  assert.strictEqual(complete.state,'完整类型清单');
  assert.ok(complete.reason.indexOf('完整类型清单')>=0,complete.reason);
  var negated=P.explain(mkRow({residential_forms:['不含别墅']}),o)[1];
  assert.strictEqual(negated.state,'明确不存在某类型');
  assert.ok(negated.reason.indexOf('明确不存在某类型')>=0,negated.reason);
});

/* ---------- D. evidence-only villa (all subtypes merged, no subtype filter) ---------- */
check('villa evidence: residential_forms[别墅] -> supported',function(){
  var r=mkRow({residential_forms:['别墅']});
  var opts=qopts({forms:['别墅'],metroMaxM:5000});
  assert.strictEqual(bucketOf(r,opts),'supported');
  assert.ok(P.explain(r,opts)[1].reason.indexOf('别墅')>=0);
});
check('villa evidence: villa_subtypes[叠拼] without forms -> supported (merged)',function(){
  var r=mkRow({villa_subtypes:['叠拼']});
  var opts=qopts({forms:['别墅'],metroMaxM:5000});
  assert.strictEqual(bucketOf(r,opts),'supported');
  assert.ok(P.explain(r,opts)[1].reason.indexOf('别墅')>=0);
});
check('villa evidence: pure_villa true -> supported',function(){
  var r=mkRow({pure_villa:true});
  assert.strictEqual(bucketOf(r,qopts({forms:['别墅'],metroMaxM:5000})),'supported');
});
check('villa name-only hint -> pending 仅名称线索, not supported',function(){
  var r=mkRow({name:'XX别墅',type_hints:['别墅']});
  var opts=qopts({forms:['别墅'],metroMaxM:5000});
  var res=P.query([r],opts);
  assert.ok(res.supported.indexOf(r)<0,'name hint must not be supported');
  assert.ok(res.pending.indexOf(r)>=0,'name hint must be pending');
  assert.strictEqual(P.explain(r,opts)[1].reason,'仅名称线索指向别墅，待核');
});
check('villa hints:false -> pending 缺乏形态证据',function(){
  var r=mkRow({name:'XX别墅',type_hints:['别墅']});
  var opts=qopts({forms:['别墅'],hints:false,metroMaxM:5000});
  assert.strictEqual(bucketOf(r,opts),'pending');
  assert.strictEqual(P.explain(r,opts)[1].reason,'缺乏形态证据');
});
check('villa: incomplete list with only 楼房 -> pending, not excluded',function(){
  var r=mkRow({name:'只有楼房',residential_forms:['楼房']});
  var opts=qopts({forms:['别墅'],metroMaxM:5000});
  assert.strictEqual(bucketOf(r,opts),'pending');
  assert.strictEqual(P.explain(r,opts)[1].status,'pending');
});
check('villa: verified complete list without villa -> excluded, retained and countable',function(){
  var r=mkRow({name:'完整无别墅',residential_forms:['楼房'],forms_exhaustive:true});
  var opts=qopts({forms:['别墅'],metroMaxM:5000});
  var res=P.query([r],opts);
  assert.strictEqual(bucketOf(r,opts),'excluded');
  assert.strictEqual(res.counts.excluded,1);
  assert.ok(res.excluded.indexOf(r)>=0,'excluded row must remain in library bucket');
  assert.ok(P.explain(r,opts)[1].reason.indexOf('完整类型清单')>=0);
});
check('villa+楼房 both evidences -> supported',function(){
  var r=mkRow({residential_forms:['别墅','楼房']});
  assert.strictEqual(bucketOf(r,qopts({forms:['别墅','楼房'],metroMaxM:5000})),'supported');
});
check('高层+别墅 mix matches both 别墅 and 别墅+楼房',function(){
  var r=mkRow({residential_forms:['高层','别墅']});
  assert.strictEqual(bucketOf(r,qopts({forms:['别墅'],metroMaxM:5000})),'supported');
  assert.strictEqual(bucketOf(r,qopts({forms:['别墅','楼房'],metroMaxM:5000})),'supported');
});
check('别墅+楼房 requires both: villa-only -> pending 缺乏楼房证据',function(){
  var r=mkRow({residential_forms:['别墅']});
  var opts=qopts({forms:['别墅','楼房'],metroMaxM:5000});
  assert.strictEqual(bucketOf(r,opts),'pending');
  assert.strictEqual(P.explain(r,opts)[1].reason,'缺乏楼房证据');
});
check('type 形态待核 selects rows lacking form evidence',function(){
  var noform=mkRow({name:'xnf'}),withform=mkRow({name:'wf',residential_forms:['楼房']});
  var res=P.query([noform,withform],qopts({forms:[],requireNoForm:true,metroMaxM:5000}));
  assert.ok(res.supported.indexOf(noform)>=0);
  assert.ok(res.excluded.indexOf(withform)>=0);
});
check('typeOptions always lists all 7 types even with rows=[]',function(){
  var html=P.typeOptions([],{});
  ['楼房','别墅','别墅+楼房','公寓','自建房','乡村住宅','形态待核'].forEach(function(t){assert.ok(html.indexOf('>'+t)>=0,'missing '+t);});
  ['宿舍','纯别墅','独栋/联排住宅'].forEach(function(t){assert.ok(html.indexOf(t)<0,'must not offer '+t);});
});
check('excluded group helper is visible and countable',function(){
  var r=mkRow({name:'只有楼房',residential_forms:['楼房'],forms_exhaustive:true});
  var opts=qopts({forms:['别墅'],metroMaxM:5000});
  var groups=P.query([r],opts);
  var html=P.excludedGroupHtml(groups,opts);
  assert.ok(html.indexOf('已有证据不符 (1)')>=0,'count missing: '+html);
  assert.ok(html.indexOf('只有楼房')>=0,'excluded row hidden');
  assert.ok(html.indexOf('完整类型清单')>=0,'reason missing');
});
check('gap note explains gap when supported===0',function(){
  var r=mkRow({residential_forms:['楼房']});
  var opts=qopts({forms:['别墅'],metroMaxM:5000});
  var html=P.gapNote(P.query([r],opts),opts);
  assert.ok(html.indexOf('没有')>=0&&html.indexOf('资料支持符合')>=0,html);
});

/* ---------- E. unknown distance -> pending ---------- */
check('unknown metro distance -> pending reason 地铁距离未知',function(){
  var r={name:'无坐标',city:'甲市'};
  var opts={metroMaxM:1000,stations:[],versions:{}};
  assert.strictEqual(bucketOf(r,opts),'pending');
  assert.strictEqual(P.explain(r,opts)[2].reason,'地铁距离未知');
});

/* ---------- F. groups/counts/coverage shape ---------- */
check('query shape: groups, counts, coverage',function(){
  var rows=[
    mkRow({completion_year:{status:'recorded',precision:'year',start:2003,end:2003},residential_forms:['别墅']}),
    mkRow({completion_year:{status:'recorded',precision:'year',start:2010,end:2010},residential_forms:['高层']}),
    mkRow({name:'无坐标',city:'甲市',district:'B区'})
  ];
  var res=P.query(rows,qopts({forms:['别墅'],yearFrom:2000,yearTo:2005,metroMaxM:5000}));
  ['supported','pending','excluded','uncovered'].forEach(function(k){
    assert.ok(Array.isArray(res[k]),k+' not array');
  });
  ['supported','pending','excluded','uncovered','total'].forEach(function(k){
    assert.strictEqual(typeof res.counts[k],'number',k+' not number');
  });
  assert.strictEqual(res.counts.total,rows.length);
  assert.ok(Array.isArray(res.coverage.districts),'coverage.districts not array');
  assert.strictEqual(typeof res.coverage.note,'string','coverage.note not string');
});

/* ---------- G. explain returns 4 conditions ---------- */
check('explain returns 4 conditions in fixed order',function(){
  var ex=P.explain({},{});
  assert.strictEqual(ex.length,4);
  var want=['年份','形态','地铁距离','位置'];
  for(var i=0;i<want.length;i++){ assert.strictEqual(ex[i].condition,want[i],'condition['+i+']'); }
});

/* Plan 15 row 16: 切地图/列表视图、城市、年份后翻页条件一致。
   Plan 15 invariant: 扩大年份范围/距离阈值/子型集合不得缩小 资料支持+待核 集合。 */
function recordedRow(name,start,end){
  return mkRow({name:name,completion_year:{status:'recorded',precision:'year',start:start,end:(end==null?start:end)}});
}
function namesOf(list){ return Array.from(list,function(p){return p.name;}).sort(); }

check('R17 翻页: 同一 options 跨页拼接与整表一致', function(){
  var rows=[];for(var i=0;i<7;i++)rows.push(recordedRow('y'+(2000+i),2000+i,2000+i));
  var opts=qopts({yearFrom:2002,yearTo:2005});
  var full=P.query(rows,opts);
  var ps=3,supported=[],pending=[],total=0;
  for(var s=0;s<rows.length;s+=ps){
    var page=P.query(rows.slice(s,s+ps),opts);
    supported=supported.concat(page.supported);pending=pending.concat(page.pending);total+=page.counts.total;
  }
  assert.strictEqual(total,rows.length);
  assert.deepStrictEqual(namesOf(supported),namesOf(full.supported));
  assert.deepStrictEqual(namesOf(pending),namesOf(full.pending));
});

check('R17 翻页: options 与 rows 不被修改', function(){
  var rows=[recordedRow('a',2003),recordedRow('b',2008)];
  var opts=qopts({yearFrom:2000,yearTo:2005,includePending:true});
  var snapshot=JSON.stringify(opts),rowLen=rows.length;
  P.query(rows,opts);
  assert.strictEqual(JSON.stringify(opts),snapshot);
  assert.strictEqual(rows.length,rowLen);
});

check('R17 视图切换: 地图/列表行序不同结果集一致', function(){
  var rows=[recordedRow('a',2003),recordedRow('b',2008),mkRow({name:'c',completion_year:{status:'unknown'}})];
  var opts=qopts({yearFrom:2000,yearTo:2005});
  var list=P.query(rows,opts),map=P.query(rows.slice().reverse(),opts);
  assert.deepStrictEqual(map.counts,list.counts);
  assert.deepStrictEqual(namesOf(map.supported),namesOf(list.supported));
  assert.deepStrictEqual(namesOf(map.pending),namesOf(list.pending));
  assert.deepStrictEqual(namesOf(map.excluded),namesOf(list.excluded));
});

check('R17 切换城市: 非目标城市转 uncovered 且跨页一致', function(){
  var rows=[mkRow({name:'a'}),mkRow({name:'b',city:'乙市'}),recordedRow('c',2003),mkRow({name:'d',city:'乙市'})];
  var opts=qopts({yearFrom:2000,yearTo:2005});
  var full=P.query(rows,opts);
  assert.strictEqual(full.counts.uncovered,2);
  var unc=[];
  for(var s=0;s<rows.length;s+=1)unc=unc.concat(P.query([rows[s]],opts).uncovered);
  assert.deepStrictEqual(namesOf(unc),namesOf(full.uncovered));
  var other=P.query(rows,Object.assign({},opts,{city:'乙市'}));
  assert.strictEqual(other.counts.uncovered,2);
  assert.strictEqual(other.counts.pending,2);
});

check('R17 切换年份: 条件重算且跨页一致', function(){
  var rows=[recordedRow('a',1999),recordedRow('b',2003),recordedRow('c',2008)];
  assert.strictEqual(P.query(rows,qopts({yearFrom:2000,yearTo:2005})).counts.supported,1);
  assert.strictEqual(P.query(rows,qopts({yearFrom:1998,yearTo:2010})).counts.supported,3);
  var acc=[];
  rows.forEach(function(r){ acc=acc.concat(P.query([r],qopts({yearFrom:2000,yearTo:2005})).supported); });
  assert.deepStrictEqual(namesOf(acc),namesOf(P.query(rows,qopts({yearFrom:2000,yearTo:2005})).supported));
});

check('R17 状态分组: includePending=false 仅隐藏待核', function(){
  var rows=[recordedRow('a',2003),mkRow({name:'b',completion_year:{status:'unknown'}}),recordedRow('z',2008)];
  var full=P.query(rows,qopts({yearFrom:2000,yearTo:2005}));
  var hidden=P.query(rows,qopts({yearFrom:2000,yearTo:2005,includePending:false}));
  assert.strictEqual(hidden.pending.length,0);
  assert.strictEqual(hidden.counts.pending,0);
  assert.deepStrictEqual(namesOf(hidden.supported),namesOf(full.supported));
  assert.deepStrictEqual(namesOf(hidden.excluded),namesOf(full.excluded));
});

check('R17 分组互斥: 每行恰好进入一个桶', function(){
  var rows=[recordedRow('a',2003),mkRow({name:'b',completion_year:{status:'unknown'}}),mkRow({name:'c',city:'乙市'}),recordedRow('d',2008)];
  var res=P.query(rows,qopts({yearFrom:2000,yearTo:2005}));
  var all={};
  res.supported.concat(res.pending,res.excluded,res.uncovered).forEach(function(p){all[p.name]=(all[p.name]||0)+1;});
  assert.strictEqual(Object.keys(all).length,rows.length);
  Object.keys(all).forEach(function(k){assert.strictEqual(all[k],1);});
  assert.strictEqual(res.counts.total,rows.length);
});

check('单调性: 扩大年份范围不缩小 资料支持+待核', function(){
  var rows=[];for(var y=1996;y<=2010;y++)rows.push(recordedRow('y'+y,y,y));
  rows.push(mkRow({name:'conflict',completion_year:{status:'conflict'}}));
  rows.push(mkRow({name:'unknown',completion_year:{status:'unknown'}}));
  var n=namesOf(P.query(rows,qopts({yearFrom:2000,yearTo:2005})).supported.concat(P.query(rows,qopts({yearFrom:2000,yearTo:2005})).pending));
  var w=namesOf(P.query(rows,qopts({yearFrom:1990,yearTo:2015})).supported.concat(P.query(rows,qopts({yearFrom:1990,yearTo:2015})).pending));
  n.forEach(function(k){assert.ok(w.indexOf(k)>=0,'widening year range dropped '+k);});
});

check('单调性: 扩大距离阈值不缩小 资料支持+待核', function(){
  var rows=[mkRow({name:'near',lat:30,lng:120}),mkRow({name:'far',lat:latAt(5000),lng:120})];
  var n=namesOf(P.query(rows,qopts({metroMaxM:1000})).supported.concat(P.query(rows,qopts({metroMaxM:1000})).pending));
  var w=namesOf(P.query(rows,qopts({metroMaxM:10000})).supported.concat(P.query(rows,qopts({metroMaxM:10000})).pending));
  n.forEach(function(k){assert.ok(w.indexOf(k)>=0,'widening metro threshold dropped '+k);});
});

check('形态语义: 未限定匹配全部；单形态按证据；别墅+楼房需两类证据', function(){
  var rows=[
    mkRow({name:'gaoceng',residential_forms:['高层']}),
    mkRow({name:'bie',residential_forms:['别墅']}),
    mkRow({name:'villaSub',villa_subtypes:['联排']}),
    mkRow({name:'mixed',residential_forms:['高层','别墅']}),
    mkRow({name:'noform'})
  ];
  assert.strictEqual(P.query(rows,qopts({forms:[]})).supported.length,rows.length,'forms:[] 应不限定形态');
  assert.deepStrictEqual(namesOf(P.query(rows,qopts({forms:['别墅']})).supported),['bie','mixed','villaSub']);
  assert.deepStrictEqual(namesOf(P.query(rows,qopts({forms:['高层']})).supported),['gaoceng','mixed']);
  assert.deepStrictEqual(namesOf(P.query(rows,qopts({forms:['别墅','楼房']})).supported),['mixed'],'别墅+楼房需别墅与楼房两类证据');
  assert.strictEqual(bucketOf(rows[4],qopts({forms:['别墅']})),'pending','无形态证据进待核');
});
check('单调性: 纯建筑形态集合扩大不缩小 supported（不含别墅+楼房组合）', function(){
  var rows=[
    mkRow({name:'gaoceng',residential_forms:['高层']}),
    mkRow({name:'yangfang',residential_forms:['洋房']}),
    mkRow({name:'xiaogaoceng',residential_forms:['小高层']}),
    mkRow({name:'noform'})
  ];
  function snap(sel){var r=P.query(rows,qopts({forms:sel}));return {sup:namesOf(r.supported),plus:namesOf(r.supported.concat(r.pending))};}
  snap(['高层']).sup.forEach(function(k){assert.ok(snap(['高层','洋房']).sup.indexOf(k)>=0,'形态单调性违例 '+k);});
  snap(['洋房']).sup.forEach(function(k){assert.ok(snap(['洋房','小高层']).sup.indexOf(k)>=0,'形态单调性违例 '+k);});
  assert.strictEqual(snap([]).sup.length,rows.length);
});

check('合并子型: villa_subtypes 不再作为筛选维度，任何子型都归入别墅', function(){
  var rows=[
    mkRow({name:'a',villa_subtypes:['叠拼']}),
    mkRow({name:'b',villa_subtypes:['联排']}),
    mkRow({name:'c',villa_subtypes:['独栋']}),
    mkRow({name:'d',residential_forms:['别墅']}),
    mkRow({name:'e',residential_forms:['楼房'],forms_exhaustive:true})
  ];
  var res=P.query(rows,qopts({forms:['别墅']}));
  assert.deepStrictEqual(namesOf(res.supported),['a','b','c','d']);
  assert.deepStrictEqual(namesOf(res.excluded),['e']);
  assert.strictEqual(res.counts.excluded,1);
});

/* ---------- F8 dataset coverage + pending queue ---------- */
check('F8 coverageNote renders dataset-level status, not per-community counts',function(){
  var cov={note:'x',sources:{fang:{status:'截断'}},cities:{'北京':{verification_state:'unverified_legacy',directory_truncated:true,last_success_time:'2026-09-21T08:14:53Z'}}};
  var html=P.coverageNote(cov);
  assert.ok(html.indexOf('单小区计数不能代替数据集覆盖')>=0,html);
  assert.ok(html.indexOf('截断')>=0,html);
  assert.ok(html.indexOf('未采完')>=0,html);
  assert.ok(html.indexOf('站点未核验')>=0,html);
  assert.ok(html.indexOf('目录截断')>=0,html);
});
check('F8 coverageNote empty -> ""',function(){ assert.strictEqual(P.coverageNote(null),''); });
check('F8 pendingQueueHtml renders 待核 unlocated queue',function(){
  var html=P.pendingQueueHtml({records:[{id:'x',name:'某小区',city:'北京',reason:'无坐标'}]});
  assert.ok(html.indexOf('待核')>=0,html);
  assert.ok(html.indexOf('某小区')>=0,html);
  assert.ok(html.indexOf('来源记录未定位')>=0,html);
});

console.log('[test_community_query] '+checks+' checks passed');
