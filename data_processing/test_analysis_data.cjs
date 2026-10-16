const fs = require('fs'), vm = require('vm'), assert = require('assert');
const sandbox = {window: {}, Map, Date, Number};
sandbox.window = sandbox;
vm.runInNewContext(fs.readFileSync(__dirname+'/frontend/assets/js/dashboard/analysis-data.js','utf8'),sandbox);
const A = sandbox.HP.analysis;
const city = (name, monthly) => ({city:name,monthly});
const row = (date, price, change_pct) => ({date,price,change_pct});
const cities = [
    city('A',[row('2021-03',100,null),row('2021-04',110,10),row('2021-05',110,0)]),
    city('B',[row('2021-03',200,null),row('2021-04',190,-5),row('2021-05',190,0.05)]),
    city('C',[row('2021-04',9999,99)]),
    city('D',[row('2021-03',100,null),row('2021-04',100,null)]),
];
const breadth = A.breadth(cities);
assert.equal(breadth[0].rising,1); assert.equal(breadth[0].falling,1);
assert.equal(breadth[0].flat,0); assert.equal(breadth[0].missing,2);
assert.equal(breadth[1].flat,2); assert.equal(breadth[1].missing,2);
assert(breadth.every(r=>r.rising+r.falling+r.flat+r.missing===cities.length));
const national = A.nationalPrices(cities);
assert.equal(national.count,2,'intermittent expensive city must not change national cohort');
assert.equal(national.rows[0].mean,150); assert.equal(national.rows[1].mean,150);
assert.equal(A.nationalPrices([city('gap',[row('2021-04',100),row('2021-06',100)])]).count,0,'calendar gaps must disqualify incomplete cohort');
const official = A.nationalOfficial({cities:[{city:'A',monthly:[{month:'2021-04',mom_pct:0,yoy_pct:null}]},{city:'B',monthly:[{month:'2021-04',mom_pct:-2,yoy_pct:10}]},{city:'outside',monthly:[{month:'2021-04',mom_pct:99}]}]},['A','B']);
assert.equal(official[0].mom_pct,-1); assert.equal(official[0].mom_pct_count,2);
assert.equal(official[0].yoy_pct,10); assert.equal(official[0].yoy_pct_count,1);
const snapshot = A.citySnapshot(city('A',[row('2025-08',100),row('2026-05',110,1),row('2026-07',120,1),row('2026-08',130,1),row('2026-09',999,1)]),'2026-08');
assert.equal(snapshot.peak,130); assert.equal(Math.round(snapshot.yoy_pct),30);
assert.equal(snapshot.rising3,false); assert.equal(snapshot.change3,null,'missing month is not a continuous rise');
const info = A.cityInformation({cih:{prices:[{city:'A',period:'2026-08'},{city:'B',period:'2026-08'},{city:'A',period:'2026-07'}],daily:[{city:'B',date:'2026-08-01'}],metrics:[{scope:'300城',value:100}],articles:[{title:'A住宅报告'},{title:'B住宅报告'}]},releases:[{month:'2026-08',rows:[{city:'A'},{city:'B'}]}]},'A','2026-08');
assert.equal(info.prices.length,1); assert.equal(info.official.length,1); assert.equal(info.daily.length,0);
assert.equal(info.metrics.length,0,'national land indicators cannot be attributed to selected city');
assert.equal(info.articles.length,1);
const actual = require('./frontend/intersection_housing_data.json');
const all = A.breadth(actual.cities);
assert(all.every(r=>r.valid+r.missing===70));
const nationalActual=A.nationalPrices(actual.cities);
assert(nationalActual.count>0 && nationalActual.count<=70);
assert.equal(nationalActual.total,70,'unpriced cities remain in the universe without entering the average');
console.log('Analysis data checks passed: gaps, nulls, fixed cohorts, official coverage, historical cutoff and city isolation.');
