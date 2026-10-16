const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const options = [];
const scope = {HP:{format:{},chart:{create:()=>({setOption:option=>options.push(option),on:()=>{}}),grid:()=>({}),axis:value=>value}},Date};
scope.window = scope;
vm.createContext(scope);
for (const file of ['analysis-data.js','charts_report.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname,'frontend/assets/js/dashboard',file),'utf8'),scope);
}
const city = (name, prices) => ({city:name,tier:'S',color:'#123',monthly:prices.map((price,index)=>({date:['2021-04','2021-05','2021-06'][index],price}))});
const sample = [city('甲',[100,90,81]),city('乙',[1000,900])];
scope.HP.dashChartsReport.buildTierIndex({},sample);
assert.deepEqual(JSON.parse(JSON.stringify(options[0].series[0].data)),[['2021-04',100],['2021-05',90],['2021-06',81]],'partial new month must not replace the city cohort');
assert(options[0].tooltip.formatter([{value:['2021-06',81],seriesName:options[0].series[0].name,marker:''}]).includes('1/2'));
options.length=0;
scope.HP.dashChartsReport.buildTierIndex({},[city('甲',[100,null,81]),city('乙',[1000,900])]);
assert.equal(options[0].series.length,0,'no complete cohort must not fabricate or shift the base month');
console.log('Tier chart passed: fixed cities, partial release, gaps, explicit sample coverage and stable base.');
