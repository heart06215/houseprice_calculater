'use strict';
/* Plain-CommonJS tests for F5 judgement evidence wiring in HP.RE.community.
   Run: D:/nodejs/node.exe data_processing/test_judgement.cjs (repo root cwd).
   Five counterexamples must never land in the `supported` bucket. */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const sandbox={};sandbox.window=sandbox;
vm.runInNewContext(fs.readFileSync(__dirname+'/frontend/realestate/assets/js/community.js','utf8'),sandbox);
const P=sandbox.HP.RE.community;

var checks=0;
function check(name,fn){
  try{ fn(); checks++; }
  catch(e){ console.error('[test_judgement] FAIL: '+name); console.error((e&&e.stack)||String(e)); process.exitCode=1; }
}

/* ---------- fixtures ---------- */
var station={name:'近站',city:'甲市',lat:30,lng:120,operational_status:'operational'};
function row(extra){ return Object.assign({name:'r',city:'甲市',district:'A区',lat:30.01,lng:120},extra||{}); }
function opts(extra){ return Object.assign({city:'甲市',yearFrom:2000,yearTo:2010,metroMaxM:2000,stations:[station],versions:{}},extra||{}); }
function bucketOf(r,o){
  var res=P.query([r],o);
  if(res.supported.indexOf(r)>=0)return 'supported';
  if(res.pending.indexOf(r)>=0)return 'pending';
  if(res.excluded.indexOf(r)>=0)return 'excluded';
  if(res.uncovered.indexOf(r)>=0)return 'uncovered';
  return 'none';
}
function neverSupported(name,r,o){
  check(name,function(){
    assert.ok(P.query([r],o).supported.indexOf(r)<0,'must not be supported');
    assert.notStrictEqual(bucketOf(r,o),'supported');
  });
}

/* ---------- F5.1 single-building date is not a community completion year ---------- */
var r1=row({completion_year:{status:'recorded',start:2003,end:2003,precision:'year',scope:'building'}});
check('F5.1 单栋年份 -> 年份 pending',function(){
  var ex=P.explain(r1,opts());
  assert.strictEqual(ex[0].status,'pending');
  assert.ok(ex[0].reason.indexOf('单栋')>=0,ex[0].reason);
});
neverSupported('F5.1 single-building year not supported',r1,opts());

/* ---------- F5.2 year phase contradicts villa phase ---------- */
var r2=row({
  completion_year:{status:'recorded',start:2003,end:2003,precision:'year',scope:'phase',phase:'一期'},
  type_evidence:[{type:'别墅',scope:'phase',phase:'二期',confidence:'tag'}]
});
check('F5.2 年份分期与别墅分期不一致 -> 年份 pending',function(){
  var ex=P.explain(r2,opts());
  assert.strictEqual(ex[0].status,'pending');
  assert.ok(ex[0].reason.indexOf('不一致')>=0,ex[0].reason);
});
neverSupported('F5.2 phase mismatch not supported',r2,opts());

/* ---------- F5.3 opening event cannot stand in for completion ---------- */
var r3=row({completion_year:{status:'recorded',start:2003,end:2003,precision:'year',meaning:'opening'},year_meaning:'opening'});
check('F5.3 开盘语义 -> 年份 pending',function(){
  var ex=P.explain(r3,opts());
  assert.strictEqual(ex[0].status,'pending');
  assert.ok(ex[0].reason.indexOf('事件语义')>=0,ex[0].reason);
});
neverSupported('F5.3 opening event not supported',r3,opts());

/* ---------- F5.4 street-level center is not a usable location ---------- */
var r4=row({precision:'street'});
check('F5.4 街道中心 -> 位置/地铁距离 pending 待核',function(){
  var ex=P.explain(r4,opts());
  assert.strictEqual(ex[3].status,'pending');
  assert.ok(ex[3].reason.indexOf('待核定位')>=0,ex[3].reason);
  assert.strictEqual(ex[2].status,'pending');
});
neverSupported('F5.4 street center not supported',r4,opts());

/* ---------- F5.5 accuracy wider than threshold -> metro pending ---------- */
var r5=row({coord_accuracy_m:1500,accuracy_m:1500});
check('F5.5 定位误差跨越阈值 -> 地铁距离 pending',function(){
  var ex=P.explain(r5,opts());
  assert.strictEqual(ex[2].status,'pending');
  assert.ok(ex[2].reason.indexOf('跨越')>=0,ex[2].reason);
});
neverSupported('F5.5 uncertainty boundary not supported',r5,opts());

/* ---------- F5.6 form states use spec literals ---------- */
check('F5.6 别墅 -> 发现一种类型',function(){
  var ex=P.explain(row({residential_forms:['别墅']}),opts({forms:['别墅']}));
  assert.strictEqual(ex[1].state,'发现一种类型');
  assert.ok(ex[1].reason.indexOf('发现一种类型')>=0,ex[1].reason);
});
check('F5.6 完整类型清单不含别墅 -> 完整类型清单',function(){
  var ex=P.explain(row({residential_forms:['楼房'],forms_exhaustive:true}),opts({forms:['别墅']}));
  assert.strictEqual(ex[1].state,'完整类型清单');
  assert.ok(ex[1].reason.indexOf('完整类型清单')>=0,ex[1].reason);
});
check('F5.6 不含别墅 -> 明确不存在某类型',function(){
  var ex=P.explain(row({residential_forms:['不含别墅']}),opts({forms:['别墅']}));
  assert.strictEqual(ex[1].state,'明确不存在某类型');
  assert.ok(ex[1].reason.indexOf('明确不存在某类型')>=0,ex[1].reason);
});

console.log('[test_judgement] '+checks+' checks passed');
